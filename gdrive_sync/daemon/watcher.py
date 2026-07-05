"""Local filesystem change detection with debounce.

watchdog (inotify) callbacks arrive on a worker thread; events are forwarded
to the GLib main loop where a debounce timer coalesces bursts into a single
sync trigger. Degrades to no-op (interval-only sync) if watchdog is missing.
"""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path

from gi.repository import GLib

from .. import const

log = logging.getLogger(__name__)

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
    HAVE_WATCHDOG = True
except ImportError:  # pragma: no cover
    HAVE_WATCHDOG = False
    FileSystemEventHandler = object

_IGNORE_PATTERNS = const.INTERNAL_EXCLUDES + [".goutputstream-*"]


def _ignored(path: str) -> bool:
    name = Path(path).name
    return any(fnmatch.fnmatch(name, pat) for pat in _IGNORE_PATTERNS)


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: "LocalWatcher") -> None:
        self._watcher = watcher

    def on_any_event(self, event) -> None:
        if event.is_directory and event.event_type not in ("moved", "deleted", "created"):
            return
        for p in (getattr(event, "src_path", ""), getattr(event, "dest_path", "")):
            if p and not _ignored(p):
                GLib.idle_add(self._watcher._on_event_main_thread)
                return


class LocalWatcher:
    """Watch local_dir recursively; call on_change() after DEBOUNCE_SECONDS of quiet."""

    def __init__(self, local_dir: str | Path, on_change) -> None:
        self.local_dir = Path(local_dir)
        self.on_change = on_change
        self._observer = None
        self._paused = False
        self._pending_after_pause = False
        self._debounce_id = 0

    @property
    def active(self) -> bool:
        return self._observer is not None

    def start(self) -> None:
        if not HAVE_WATCHDOG:
            log.warning("python3-watchdog not available; interval-only sync")
            return
        if self._observer is not None or not self.local_dir.is_dir():
            return
        try:
            self._observer = Observer()
            self._observer.schedule(_Handler(self), str(self.local_dir), recursive=True)
            self._observer.start()
            log.info("watching %s", self.local_dir)
        except OSError as e:  # e.g. inotify watch limit reached
            log.warning("file watching unavailable (%s); interval-only sync", e)
            self._observer = None

    def stop(self) -> None:
        self._cancel_debounce()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

    def restart(self, new_dir: str | Path) -> None:
        self.stop()
        self.local_dir = Path(new_dir)
        self.start()

    def pause(self) -> None:
        """Suppress triggers (used while rclone itself writes to the folder)."""
        self._paused = True
        self._cancel_debounce()
        self._pending_after_pause = False

    def resume(self) -> None:
        self._paused = False
        if self._pending_after_pause:
            self._pending_after_pause = False
            self._arm_debounce()

    # -- main-loop side ----------------------------------------------------- #

    def _on_event_main_thread(self) -> bool:
        if self._paused:
            self._pending_after_pause = True
        else:
            self._arm_debounce()
        return GLib.SOURCE_REMOVE

    def _arm_debounce(self) -> None:
        self._cancel_debounce()
        self._debounce_id = GLib.timeout_add_seconds(
            const.DEBOUNCE_SECONDS, self._fire)

    def _cancel_debounce(self) -> None:
        if self._debounce_id:
            GLib.source_remove(self._debounce_id)
            self._debounce_id = 0

    def _fire(self) -> bool:
        self._debounce_id = 0
        self.on_change()
        return GLib.SOURCE_REMOVE
