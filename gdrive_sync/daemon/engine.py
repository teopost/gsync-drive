"""Per-account sync engine: state machine, scheduling, retry/backoff, recovery.

State changes happen on the GLib main loop; rclone runs in a worker thread.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from enum import Enum

from gi.repository import Gio, GLib

from .. import conflicts, const, rclone
from ..config import AccountConfig, ensure_filters_file
from ..i18n import _, ngettext

log = logging.getLogger(__name__)

# A prior listing can only be empty if the last known state tracked zero
# files; a resync then cannot delete anything, so automating it is safe.
_EMPTY_LISTING_RE = re.compile(r"empty prior Path[12] listing", re.I)


class State(Enum):
    UNCONFIGURED = "unconfigured"
    IDLE = "idle"
    SYNCING = "syncing"
    PAUSED = "paused"
    OFFLINE = "offline"
    ERROR = "error"
    NEEDS_RESYNC = "needs_resync"
    RESYNCING = "resyncing"


class Engine:
    """Drives bisync runs for one account. Public methods run on the main loop.

    Callbacks (set by the daemon):
      on_state_changed(state: State)
      on_sync_completed(ok: bool, conflict_count: int)
      on_notify(kind: str, title: str, body: str)
        kind: conflicts | needs-resync | error | first-sync
    """

    def __init__(self, account: AccountConfig, version: rclone.RcloneVersion) -> None:
        self.account = account
        self.version = version
        self.state = State.UNCONFIGURED
        self.last_sync_time = self._load_persisted_sync_time()
        self.last_error = ""
        self.conflict_count = 0
        self._notified_conflicts: set[str] = set()
        self._dirty = False           # changes seen while a sync was running
        self._retry_index = 0
        self._lock_retried = False
        self._empty_listing_retried = False
        self._timer_id = 0
        self._state_before_pause: State | None = None
        self._current_run: rclone.BisyncRun | None = None
        self.watcher = None           # set by the daemon; exposes pause()/resume()

        self.on_state_changed = lambda state: None
        self.on_sync_completed = lambda ok, n_conflicts: None
        self.on_notify = lambda kind, title, body: None

        self._netmon = Gio.NetworkMonitor.get_default()
        self._net_handler = self._netmon.connect(
            "notify::network-available", self._on_network_changed)

    # ----------------------------------------------------------------- state

    def _set_state(self, state: State, error: str = "") -> None:
        if state is self.state and error == self.last_error:
            return
        log.info("[%s] state: %s -> %s %s",
                 self.account.id, self.state.value, state.value, error or "")
        self.state = state
        self.last_error = error
        self._write_status_file()
        self.on_state_changed(state)

    def _load_persisted_sync_time(self) -> int:
        try:
            return int(json.loads(self.account.status_file.read_text())["last_sync_time"])
        except (OSError, ValueError, KeyError):
            return 0

    def _write_status_file(self) -> None:
        """Tiny state dump: persistence for last_sync_time + Nautilus consumer."""
        try:
            self.account.status_file.parent.mkdir(parents=True, exist_ok=True)
            self.account.status_file.write_text(json.dumps({
                "state": self.state.value,
                "last_sync_time": self.last_sync_time,
                "conflict_count": self.conflict_count,
                "local_dir": str(self.account.local_dir),
            }))
        except OSError:
            pass

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        ensure_filters_file(self.account.filters_file, self.account.sync_folders)
        self.account.workdir.mkdir(parents=True, exist_ok=True)
        needs_remote = self.account.remote.endswith(":")
        if needs_remote and not rclone.remote_exists(self.account.remote_name):
            self._set_state(State.UNCONFIGURED,
                            _("rclone remote {remote} is missing").format(remote=self.account.remote))
            return
        if not self._netmon.get_network_available():
            self._set_state(State.OFFLINE)
            return
        self._set_state(State.IDLE)
        self._rescan_conflicts()
        self.request_sync("startup")

    def shutdown(self) -> None:
        self._cancel_timer()
        self._netmon.disconnect(self._net_handler)
        if self._current_run is not None:
            self._current_run.cancel()

    # ------------------------------------------------------------ scheduling

    def _arm_timer(self, seconds: int | None = None) -> None:
        self._cancel_timer()
        interval = seconds if seconds is not None else self.account.sync_interval
        self._timer_id = GLib.timeout_add_seconds(interval, self._on_timer)

    def _cancel_timer(self) -> None:
        if self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = 0

    def _on_timer(self) -> bool:
        self._timer_id = 0
        self.request_sync("timer")
        return GLib.SOURCE_REMOVE

    def _on_network_changed(self, *_args) -> None:
        available = self._netmon.get_network_available()
        if not available and self.state in (State.IDLE, State.ERROR):
            self._cancel_timer()
            self._set_state(State.OFFLINE)
        elif available and self.state is State.OFFLINE:
            self._set_state(State.IDLE)
            self.request_sync("back-online")

    def reload_config(self) -> None:
        if self.state is State.IDLE:
            self._arm_timer()
        elif self.state is State.UNCONFIGURED:
            self.start()

    # ------------------------------------------------------------ public ops

    def request_sync(self, reason: str = "manual") -> None:
        if self.state in (State.SYNCING, State.RESYNCING):
            self._dirty = True
            return
        if self.state in (State.UNCONFIGURED, State.PAUSED, State.NEEDS_RESYNC, State.OFFLINE):
            log.debug("[%s] sync request (%s) ignored in state %s",
                      self.account.id, reason, self.state.value)
            return
        log.info("[%s] sync requested (%s)", self.account.id, reason)
        # Very first sync of this account: bisync needs --resync to build
        # its listings. Safe: it only merges the two sides.
        self._start_run(resync=self.last_sync_time == 0)

    def request_resync(self) -> None:
        """User-approved resync: recovery, or realign after the folder
        selection / filters changed. Never triggered automatically."""
        if self.state not in (State.NEEDS_RESYNC, State.IDLE, State.ERROR):
            raise RuntimeError(f"Resync not allowed in state {self.state.value}")
        self._start_run(resync=True)

    def cancel_sync(self) -> None:
        """Kill the run in progress (no error state, no retry)."""
        if self._current_run is not None:
            log.info("[%s] sync cancelled by user", self.account.id)
            self._current_run.cancel()

    def pause(self) -> None:
        if self.state is State.PAUSED:
            return
        self._state_before_pause = self.state
        self._cancel_timer()
        self._set_state(State.PAUSED)

    def resume(self) -> None:
        if self.state is not State.PAUSED:
            return
        previous = self._state_before_pause or State.IDLE
        self._state_before_pause = None
        if previous is State.NEEDS_RESYNC:
            self._set_state(State.NEEDS_RESYNC, self.last_error)
        else:
            self._set_state(State.IDLE)
            self.request_sync("resumed")

    @property
    def paused(self) -> bool:
        return self.state is State.PAUSED

    # ------------------------------------------------------------- sync runs

    def _start_run(self, resync: bool) -> None:
        # Self-heal: a missing filters file would abort every run (and, if
        # regenerated without the folder selection, widen the sync scope).
        ensure_filters_file(self.account.filters_file, self.account.sync_folders)
        first_sync = self.last_sync_time == 0
        self._set_state(State.RESYNCING if resync else State.SYNCING)
        self._cancel_timer()
        self._dirty = False
        if self.watcher:
            self.watcher.pause()
        cmd = rclone.build_bisync_cmd(
            self.account.local_dir,
            self.account.remote,
            version=self.version,
            resync=resync,
            dry_run=self.account.dry_run,
            bwlimit=self.account.bandwidth_limit,
            max_delete=self.account.max_delete,
            filters_file=self.account.filters_file,
            workdir=self.account.workdir,
            log_file=self.account.sync_log_file,
            stats="2s" if first_sync else "",
        )

        def worker() -> None:
            try:
                run = rclone.BisyncRun(cmd)
            except OSError as e:
                GLib.idle_add(self._on_run_done,
                              rclone.SyncResult(rclone.Outcome.FATAL, -1, stderr=str(e)),
                              resync, first_sync)
                return
            self._current_run = run
            result = run.wait()
            self._current_run = None
            GLib.idle_add(self._on_run_done, result, resync, first_sync)

        threading.Thread(target=worker, name=f"bisync-{self.account.id}", daemon=True).start()

    def _on_run_done(self, result: rclone.SyncResult, was_resync: bool, first_sync: bool) -> bool:
        if self.watcher:
            self.watcher.resume()

        if result.outcome is rclone.Outcome.CANCELLED:
            self._set_state(State.IDLE)
            self._arm_timer()
            return GLib.SOURCE_REMOVE

        if result.ok:
            self._retry_index = 0
            self._lock_retried = False
            self._empty_listing_retried = False
            self.last_sync_time = int(time.time())
            new_conflicts = self._rescan_conflicts()
            self._set_state(State.IDLE)
            self.on_sync_completed(True, self.conflict_count)
            if first_sync:
                self.on_notify(
                    "first-sync",
                    _("{account}: first synchronization completed").format(
                        account=self.account.display_name),
                    _("The folder {folder} is now synchronized with Google Drive.").format(
                        folder=self.account.local_dir),
                )
            if new_conflicts:
                self.on_notify(
                    "conflicts",
                    _("{account}: synchronization conflicts").format(
                        account=self.account.display_name),
                    ngettext("{n} file modified both locally and on Drive needs a decision.",
                             "{n} files modified both locally and on Drive need a decision.",
                             self.conflict_count).format(n=self.conflict_count),
                )
            if self._dirty:
                self.request_sync("dirty-after-run")
            else:
                self._arm_timer()
            return GLib.SOURCE_REMOVE

        log.warning("[%s] bisync failed: %s exit=%d err=%s", self.account.id,
                    result.outcome.value, result.exit_code, result.stderr[-300:])
        self.on_sync_completed(False, self.conflict_count)

        if result.outcome is rclone.Outcome.LOCKED:
            if not self._lock_retried and rclone.clear_stale_lock(self.account.workdir):
                self._lock_retried = True
                self._set_state(State.IDLE)
                self.request_sync("stale-lock-cleared")
            else:
                self._enter_needs_resync(
                    _("A bisync lock persists: another synchronization may be "
                      "running, or a previous one was interrupted abruptly."))
        elif result.outcome is rclone.Outcome.NEEDS_RESYNC:
            text = f"{result.stderr}\n{result.log_tail}"
            if _EMPTY_LISTING_RE.search(text) and not self._empty_listing_retried:
                # The account started out empty (fresh folder / empty Drive
                # selection): bisync aborts every run until a resync. Nothing
                # is tracked, so nothing can be lost — resync automatically.
                self._empty_listing_retried = True
                log.info("[%s] empty prior listing; auto-resync (nothing tracked)",
                         self.account.id)
                self._set_state(State.IDLE)
                self._start_run(resync=True)
            else:
                self._enter_needs_resync(result.stderr or result.log_tail[-500:])
        elif result.outcome is rclone.Outcome.TRANSIENT:
            delay = const.RETRY_BACKOFF[min(self._retry_index, len(const.RETRY_BACKOFF) - 1)]
            self._retry_index += 1
            self._set_state(State.ERROR, result.stderr[-500:] or _("temporary error"))
            self._arm_timer(delay)
            if self._retry_index == len(const.RETRY_BACKOFF):
                self.on_notify("error",
                               _("{account}: synchronization failing").format(
                                   account=self.account.display_name),
                               _("Google Drive has been unreachable for several attempts."))
        else:  # FATAL
            self._set_state(State.ERROR, result.stderr[-500:] or _("fatal error"))
            self.on_notify("error",
                           _("{account}: synchronization error").format(
                               account=self.account.display_name),
                           _("Synchronization stopped due to an error. Open GDrive Sync for details."))
        return GLib.SOURCE_REMOVE

    def _enter_needs_resync(self, detail: str) -> None:
        self._set_state(State.NEEDS_RESYNC, detail)
        self.on_notify(
            "needs-resync",
            _("{account}: synchronization interrupted").format(
                account=self.account.display_name),
            _("A manual check is needed before resuming. Open GDrive Sync for the guided repair."),
        )

    def _rescan_conflicts(self) -> int:
        """Update conflict_count; return how many conflicts are new (not yet notified)."""
        found = conflicts.scan(self.account.local_dir)
        self.conflict_count = len(found)
        keys = {str(c.base_path) for c in found}
        new = keys - self._notified_conflicts
        self._notified_conflicts = keys
        self._write_status_file()
        return len(new)
