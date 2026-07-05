"""Add-account wizard: Google account → Drive folders → local folder → first sync.

Page 1 only authenticates the Google user; page 2 shows a checkbox tree of the
Drive folders (with sizes) to choose what gets synchronized.

The first sync is performed by the daemon (the wizard only monitors it), so
closing the window can safely leave it running in the background.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from pathlib import Path

from gi.repository import Adw, GLib, Gtk

from .. import const, rclone
from ..config import Config, write_filters
from ..i18n import _
from . import bookmarks
from .daemon_proxy import DaemonProxy
from .drive_tree import DriveFolderTree

log = logging.getLogger(__name__)


def _default_local_dir(config: Config) -> Path:
    used = {str(config.account(i).local_dir) for i in config.account_ids}
    base = const.DEFAULT_LOCAL_DIR
    if str(base) not in used:
        return base
    n = 2
    while str(base.parent / f"{base.name}-{n}") in used:
        n += 1
    return base.parent / f"{base.name}-{n}"


class SetupWizard(Adw.Window):
    def __init__(self, application: Adw.Application | None, config: Config,
                 proxy: DaemonProxy | None, on_finished) -> None:
        super().__init__(
            application=application,
            title=_("Add Google Drive account"),
            default_width=560,
            default_height=600,
        )
        self.config = config
        self.proxy = proxy
        self.on_finished = on_finished

        self.account_id = config.reserve_account_id()
        self.account = config.account(self.account_id)
        self.remote_name = f"gdrive-sync-{self.account_id}"
        self.chosen_dir: Path = _default_local_dir(config)
        self.selected_folders: list[str] = []

        self._auth_proc: subprocess.Popen | None = None
        self._authenticated = False
        self._published = False
        self._sync_running = False
        self._daemon_mode = False
        self._local_run: rclone.BisyncRun | None = None
        self._allow_close = False
        self._log_timer = 0
        self._state_handler = 0

        self.nav = Adw.NavigationView()
        toolbar = Adw.ToolbarView(content=self.nav)
        toolbar.add_top_bar(Adw.HeaderBar())
        self.set_content(toolbar)

        self.nav.add(self._page_account())
        self.connect("close-request", self._on_close_request)

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _page(tag: str, title: str, status: Adw.StatusPage) -> Adw.NavigationPage:
        clamp = Adw.Clamp(child=status, maximum_size=480)
        return Adw.NavigationPage(title=title, tag=tag, child=clamp)

    @staticmethod
    def _suggested(label: str) -> Gtk.Button:
        b = Gtk.Button(label=label, halign=Gtk.Align.CENTER)
        b.add_css_class("suggested-action")
        b.add_css_class("pill")
        return b

    # ----------------------------------------------------- 1. Google account

    def _page_account(self) -> Adw.NavigationPage:
        status = Adw.StatusPage(
            icon_name=const.APP_ID,
            title=_("Connect your Google account"),
            description=_("Your browser will open to authorize access to "
                          "Google Drive.\nCredentials stay on this computer "
                          "only."),
        )
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

        try:
            version = rclone.detect_version()
            if not version.is_recommended:
                box.append(Adw.Banner(
                    title=_("rclone {major}.{minor} detected: sync works but is "
                            "more robust with rclone ≥ 1.66 (rclone.org)").format(
                        major=version.major, minor=version.minor),
                    revealed=True))
        except rclone.RcloneNotFoundError:
            status.set_title(_("rclone not found"))
            status.set_description(
                _("GDrive Sync uses rclone as its synchronization engine.\n"
                  "Install the “rclone” package and reopen this assistant."))
            status.set_icon_name("dialog-error-symbolic")
            status.set_child(box)
            return self._page("account", _("Account"), status)

        self._auth_button = self._suggested(_("Connect Google account"))
        self._auth_button.connect("clicked", self._on_authorize_clicked)
        self._auth_spinner = Gtk.Spinner(halign=Gtk.Align.CENTER)
        self._auth_label = Gtk.Label(label="", wrap=True)
        self._auth_label.add_css_class("dim-label")

        self._account_next = self._suggested(_("Next"))
        self._account_next.set_sensitive(False)
        self._account_next.connect(
            "clicked", lambda *_a: self.nav.push(self._page_drive_folders()))

        for w in (self._auth_button, self._auth_spinner, self._auth_label,
                  self._account_next):
            box.append(w)
        status.set_child(box)
        page = self._page("account", _("Account"), status)
        page.connect("hidden", lambda *_a: self._cancel_auth())
        return page

    def _on_authorize_clicked(self, *_args) -> None:
        self._auth_button.set_sensitive(False)
        self._auth_spinner.start()
        self._auth_label.set_label(_("Waiting for the authorization in the browser…"))

        def worker() -> None:
            try:
                self._auth_proc = rclone.start_authorize()
                stdout, stderr = self._auth_proc.communicate(timeout=const.AUTHORIZE_TIMEOUT)
                if self._auth_proc.returncode != 0:
                    raise rclone.AuthorizationError(
                        stderr.strip()[-300:] or _("authorization cancelled"))
                token = rclone.parse_authorize_output(stdout)
                rclone.create_remote(token, name=self.remote_name)
                if not rclone.check_remote(f"{self.remote_name}:"):
                    raise rclone.AuthorizationError(
                        _("the remote does not respond after configuration"))
                GLib.idle_add(self._on_auth_done, None)
            except subprocess.TimeoutExpired:
                if self._auth_proc:
                    self._auth_proc.kill()
                GLib.idle_add(self._on_auth_done,
                              _("Timed out: authorization not completed."))
            except Exception as e:
                GLib.idle_add(self._on_auth_done, str(e))
            finally:
                self._auth_proc = None

        threading.Thread(target=worker, daemon=True).start()

    def _cancel_auth(self) -> None:
        if self._auth_proc is not None:
            self._auth_proc.kill()

    def _on_auth_done(self, error: str | None) -> bool:
        self._auth_spinner.stop()
        if error:
            self._auth_button.set_sensitive(True)
            self._auth_label.set_label(_("Error: {message}").format(message=error))
        else:
            self._authenticated = True
            self._auth_label.set_label(_("Account connected ✓"))
            self._account_next.set_sensitive(True)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------ 2. Drive folders

    def _page_drive_folders(self) -> Adw.NavigationPage:
        status = Adw.StatusPage(
            icon_name="folder-remote-symbolic",
            title=_("Folders to synchronize"),
            description=_("Check the Drive folders you want to keep on this "
                          "computer. With no selection, the whole Drive is "
                          "synchronized."),
        )
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

        self._folder_tree = DriveFolderTree(
            remote=f"{self.remote_name}:",
            on_selection_changed=self._on_drive_selection_changed)
        box.append(self._folder_tree)

        self._selection_label = Gtk.Label(label=_("Whole Drive"), wrap=True)
        self._selection_label.add_css_class("dim-label")
        box.append(self._selection_label)

        folders_next = self._suggested(_("Next"))
        folders_next.connect("clicked", self._on_drive_folders_next)
        box.append(folders_next)

        status.set_child(box)
        self._folder_tree.load()
        return self._page("drive-folders", _("Drive folders"), status)

    def _on_drive_selection_changed(self) -> None:
        paths = self._folder_tree.selected_paths()
        if not paths:
            self._selection_label.set_label(_("Whole Drive"))
        else:
            shown = ", ".join(paths[:3]) + ("…" if len(paths) > 3 else "")
            self._selection_label.set_label(
                _("{count} selected: {folders}").format(
                    count=len(paths), folders=shown))

    def _on_drive_folders_next(self, *_args) -> None:
        self.selected_folders = self._folder_tree.selected_paths()
        self.nav.push(self._page_folder())

    # ------------------------------------------------------- 3. local folder

    def _page_folder(self) -> Adw.NavigationPage:
        status = Adw.StatusPage(
            icon_name="folder-symbolic",
            title=_("Local folder"),
            description=_("Choose where to keep the files synchronized with "
                          "Google Drive."),
        )
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

        group = Adw.PreferencesGroup()
        self._name_row = Adw.EntryRow(title=_("Account name (e.g. Personal, Work)"))
        n = len(self.config.account_ids)
        self._name_row.set_text("Google Drive" if n == 0 else f"Google Drive {n + 1}")
        group.add(self._name_row)

        self._folder_row = Adw.ActionRow(
            title=_("Folder"), subtitle=str(self.chosen_dir))
        pick = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER)
        pick.connect("clicked", self._on_pick_folder)
        self._folder_row.add_suffix(pick)
        group.add(self._folder_row)

        self._sidebar_switch = Adw.SwitchRow(
            title=_("Show in the Files sidebar"), active=True)
        group.add(self._sidebar_switch)
        self._autostart_switch = Adw.SwitchRow(
            title=_("Start synchronization at login"), active=True)
        group.add(self._autostart_switch)
        box.append(group)

        next_btn = self._suggested(_("Start first synchronization"))
        next_btn.connect("clicked", self._on_folder_confirmed)
        box.append(next_btn)

        status.set_child(box)
        return self._page("folder", _("Folder"), status)

    def _on_pick_folder(self, *_args) -> None:
        dialog = Gtk.FileDialog(title=_("Choose the synchronization folder"))

        def done(d, result):
            try:
                f = d.select_folder_finish(result)
            except GLib.Error:
                return
            self.chosen_dir = Path(f.get_path())
            self._folder_row.set_subtitle(str(self.chosen_dir))

        dialog.select_folder(self, None, done)

    def _on_folder_confirmed(self, *_args) -> None:
        const.ensure_dirs()
        self.chosen_dir.mkdir(parents=True, exist_ok=True)
        # Stale state from a removed account (or an older installation) that
        # reused this id would make bisync skip the initial --resync.
        self.account.purge_state(include_filters=True)
        self.account.local_dir = self.chosen_dir
        self.account.display_name = self._name_row.get_text().strip() or "Google Drive"
        self.account.remote = f"{self.remote_name}:"
        self.account.sync_folders = self.selected_folders
        write_filters([], self.account.filters_file,
                      include_dirs=self.selected_folders)

        if self._sidebar_switch.get_active():
            bookmarks.add_bookmark(self.chosen_dir)
            bookmarks.set_folder_icon(self.chosen_dir)
        else:
            self.account._set("sidebar-bookmark", False)
        if self._autostart_switch.get_active():
            subprocess.run(
                ["systemctl", "--user", "enable", "--now", const.SYSTEMD_UNIT],
                capture_output=True)

        # Publishing the account makes the daemon create its engine and start
        # the first sync (a brand-new account always begins with --resync).
        self.config.publish_account(self.account_id)
        self._published = True
        self.nav.push(self._page_initial_sync())

    # ------------------------------------------------------- 4. initial sync

    def _page_initial_sync(self) -> Adw.NavigationPage:
        status = Adw.StatusPage(
            icon_name="emblem-synchronizing-symbolic",
            title=_("First synchronization…"),
            description=_("You can close this window: the synchronization can "
                          "continue in the background."),
        )
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._sync_finished = False
        self._seen_running = False

        self._progress = Gtk.ProgressBar(show_text=True, text=_("Analyzing files…"))
        self._progress.set_pulse_step(0.05)
        box.append(self._progress)

        self._log_expander = Gtk.Expander(label=_("Show details"))
        self._log_view = Gtk.TextView(editable=False, monospace=True)
        scroll = Gtk.ScrolledWindow(child=self._log_view, min_content_height=180)
        scroll.add_css_class("card")
        self._log_expander.set_child(scroll)
        self._log_expander.connect(
            "notify::expanded", lambda *_a: self._refresh_progress(force_log=True))
        box.append(self._log_expander)

        self._finish_button = self._suggested(_("Done"))
        self._finish_button.set_visible(False)
        self._finish_button.connect("clicked", lambda *_a: self._finish())
        box.append(self._finish_button)

        self._retry_button = self._suggested(_("Retry"))
        self._retry_button.set_visible(False)
        self._retry_button.connect("clicked", self._on_retry)
        box.append(self._retry_button)

        status.set_child(box)
        self._sync_status = status
        GLib.idle_add(self._start_initial_sync)
        return self._page("sync", _("Synchronization"), status)

    def _start_initial_sync(self) -> bool:
        self._sync_running = True
        self._daemon_mode = self.proxy is not None and self.proxy.available
        self._log_timer = GLib.timeout_add(500, self._refresh_progress)

        if self._daemon_mode:
            self._state_handler = self.proxy.connect(
                "state-changed", self._on_daemon_state_changed)
            # Nudge the daemon in case the accounts-changed signal raced.
            self.proxy.reload_config()
        else:
            log.warning("daemon unavailable; running the first sync in-process")
            self._start_local_sync()
        return GLib.SOURCE_REMOVE

    # -- daemon-driven ------------------------------------------------------ #

    def _on_daemon_state_changed(self, _proxy, account_id: str, status: str) -> None:
        if account_id != self.account_id or self._sync_finished:
            return
        if status in ("resyncing", "syncing"):
            self._seen_running = True
        elif status == "idle" and self._seen_running:
            self._on_sync_outcome(True, "")
        elif status in ("error", "needs_resync"):
            def got(info: dict) -> None:
                self._on_sync_outcome(False, info.get("last-error", ""))
            self.proxy.get_account_info(self.account_id, got,
                                        on_error=lambda _m: self._on_sync_outcome(False, ""))

    # -- local fallback (development / daemon not installed) ---------------- #

    def _start_local_sync(self) -> None:
        version = rclone.detect_version()
        cmd = rclone.build_bisync_cmd(
            self.account.local_dir, self.account.remote, version=version,
            resync=True, filters_file=self.account.filters_file,
            workdir=self.account.workdir, log_file=self.account.sync_log_file,
            stats="2s")

        def worker() -> None:
            self._local_run = rclone.BisyncRun(cmd)
            result = self._local_run.wait()
            self._local_run = None
            GLib.idle_add(self._on_local_sync_done, result)

        threading.Thread(target=worker, daemon=True).start()

    def _on_local_sync_done(self, result: rclone.SyncResult) -> bool:
        if result.outcome is rclone.Outcome.CANCELLED:
            return GLib.SOURCE_REMOVE
        if result.ok:
            import json
            import time
            self.account.status_file.write_text(json.dumps(
                {"state": "idle", "last_sync_time": int(time.time()),
                 "conflict_count": 0, "local_dir": str(self.account.local_dir)}))
        self._on_sync_outcome(result.ok, result.stderr or result.log_tail[-400:])
        return GLib.SOURCE_REMOVE

    # -- shared outcome handling -------------------------------------------- #

    def _on_sync_outcome(self, ok: bool, detail: str) -> None:
        if self._sync_finished:
            return
        self._sync_finished = True
        self._sync_running = False
        if self._log_timer:
            GLib.source_remove(self._log_timer)
            self._log_timer = 0
        self._refresh_progress(force_log=True)
        if ok:
            self._progress.set_fraction(1.0)
            self._progress.set_text(_("Completed"))
            self._sync_status.set_title(_("All set"))
            self._sync_status.set_icon_name("emblem-ok-symbolic")
            self._sync_status.set_description(
                _("The folder {folder} is synchronized with Google Drive.").format(
                    folder=self.chosen_dir))
            self._finish_button.set_visible(True)
        else:
            self._sync_status.set_title(_("Synchronization failed"))
            self._sync_status.set_icon_name("dialog-error-symbolic")
            self._sync_status.set_description(detail[-400:] or _("Unknown error"))
            self._retry_button.set_visible(True)

    def _on_retry(self, *_args) -> None:
        self._retry_button.set_visible(False)
        self._sync_finished = False
        self._seen_running = False
        self._sync_running = True
        self._progress.set_fraction(0)
        self._progress.set_text(_("Analyzing files…"))
        self._sync_status.set_title(_("First synchronization…"))
        self._sync_status.set_icon_name("emblem-synchronizing-symbolic")
        self._log_timer = GLib.timeout_add(500, self._refresh_progress)
        if self._daemon_mode:
            self.proxy.sync_now(self.account_id)
        else:
            self._start_local_sync()

    def _refresh_progress(self, force_log: bool = False) -> bool:
        try:
            text = self.account.sync_log_file.read_text(errors="replace")
        except OSError:
            text = ""

        if not self._sync_finished:
            fraction = rclone.parse_progress(text)
            if fraction is None:
                self._progress.pulse()
                self._progress.set_text(_("Analyzing files…"))
            else:
                self._progress.set_fraction(fraction)
                self._progress.set_text(
                    _("Transferring… {percent}%").format(percent=int(fraction * 100)))

        if force_log or self._log_expander.get_expanded():
            buf = self._log_view.get_buffer()
            buf.set_text(text[-8000:])
            buf.place_cursor(buf.get_end_iter())
            self._log_view.scroll_to_iter(buf.get_end_iter(), 0, False, 0, 1)
        return GLib.SOURCE_CONTINUE

    # ------------------------------------------------------------ closing

    def _on_close_request(self, *_args) -> bool:
        if self._allow_close:
            return False  # let it close

        if self._sync_running:
            self._ask_close_during_sync()
            return True  # keep open until the user decides

        # Abandoned before completing setup: clean up the remote we created.
        if self._authenticated and not self._published:
            threading.Thread(target=rclone.delete_remote,
                             args=(self.remote_name,), daemon=True).start()
        return False

    def _ask_close_during_sync(self) -> None:
        dialog = Adw.AlertDialog(
            heading=_("First synchronization in progress"),
            body=_("Do you want the synchronization to continue in the "
                   "background, or would you rather stop it and cancel this "
                   "account setup?"),
        )
        dialog.add_response("cancel", _("Back to setup"))
        if self._daemon_mode:
            dialog.add_response("background", _("Continue in background"))
            dialog.set_response_appearance("background", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("background")
        dialog.add_response("stop", _("Stop and cancel"))
        dialog.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._on_close_dialog_response)
        dialog.present(self)

    def _on_close_dialog_response(self, _dialog, response: str) -> None:
        if response == "background":
            # The daemon owns the sync; a notification will arrive when done.
            self._allow_close = True
            self.on_finished()
            self.close()
        elif response == "stop":
            if self._daemon_mode:
                self.proxy.cancel_sync(self.account_id)
            elif self._local_run is not None:
                self._local_run.cancel()
            bookmarks.remove_bookmark(self.account.local_dir)
            if self._published:
                self.config.remove_account(self.account_id)
                if self.proxy is not None:
                    self.proxy.reload_config()
            threading.Thread(target=rclone.delete_remote,
                             args=(self.remote_name,), daemon=True).start()
            self._allow_close = True
            self.close()
        # "cancel": do nothing, stay open

    # ---------------------------------------------------------------- finish

    def _finish(self) -> None:
        self._allow_close = True
        self.on_finished()
        self.close()
