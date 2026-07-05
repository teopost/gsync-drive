"""Main window: one row per account with status and per-account actions."""

from __future__ import annotations

import logging
import shutil
import threading
import time

from gi.repository import Adw, Gio, GLib, Gtk

from .. import const, rclone
from ..config import Config
from ..i18n import _, ngettext
from .conflicts_page import ConflictsDialog
from .daemon_proxy import DaemonProxy
from .preferences import PreferencesDialog
from . import bookmarks

log = logging.getLogger(__name__)

_STATE_UI = {
    # status: (icon, short label)
    "idle": ("emblem-ok-symbolic", _("Up to date")),
    "syncing": ("emblem-synchronizing-symbolic", _("Synchronizing…")),
    "resyncing": ("emblem-synchronizing-symbolic", _("First synchronization…")),
    "paused": ("media-playback-pause-symbolic", _("Paused")),
    "offline": ("network-offline-symbolic", _("Offline")),
    "error": ("dialog-warning-symbolic", _("Error")),
    "needs_resync": ("dialog-error-symbolic", _("Needs repair")),
    "unconfigured": ("preferences-system-symbolic", _("Not configured")),
}


def _ago(ts: int) -> str:
    if not ts:
        return _("never synchronized")
    delta = int(time.time()) - ts
    if delta < 90:
        return _("just now")
    if delta < 3600:
        return _("{minutes} min ago").format(minutes=delta // 60)
    if delta < 86400:
        return _("{hours} h ago").format(hours=delta // 3600)
    return _("{days} days ago").format(days=delta // 86400)


class AccountRow(Adw.ActionRow):
    def __init__(self, window: "MainWindow", account_id: str) -> None:
        super().__init__(title=account_id, activatable=True)
        self.account_id = account_id
        self.info: dict = {}

        self._icon = Gtk.Image(icon_name="content-loading-symbolic")
        self.add_prefix(self._icon)

        self._conflict_badge = Gtk.Label()
        self._conflict_badge.add_css_class("warning")
        self.add_suffix(self._conflict_badge)

        self._sync_btn = Gtk.Button(icon_name="emblem-synchronizing-symbolic",
                                    valign=Gtk.Align.CENTER,
                                    tooltip_text=_("Synchronize now"))
        self._sync_btn.add_css_class("flat")
        self._sync_btn.connect("clicked",
                               lambda *_: window.proxy.sync_now(account_id))
        self.add_suffix(self._sync_btn)

        menu = Gio.Menu()
        menu.append(_("Pause / Resume"), f"win.toggle-pause('{account_id}')")
        menu.append(_("Conflicts…"), f"win.conflicts('{account_id}')")
        menu.append(_("Log…"), f"win.show-log('{account_id}')")
        menu.append(_("Preferences…"), f"win.prefs('{account_id}')")
        menu.append(_("Repair…"), f"win.repair('{account_id}')")
        menu.append(_("Remove account…"), f"win.remove('{account_id}')")
        menu_btn = Gtk.MenuButton(icon_name="view-more-symbolic",
                                  valign=Gtk.Align.CENTER, menu_model=menu)
        menu_btn.add_css_class("flat")
        self.add_suffix(menu_btn)

        self.connect("activated", lambda *_: window.open_folder(self))

    def update(self, info: dict) -> None:
        self.info = info
        status = info.get("status", "unconfigured")
        icon, label = _STATE_UI.get(status, ("dialog-question-symbolic", status))
        self._icon.set_from_icon_name(icon)
        self.set_title(info.get("display-name", self.account_id))
        parts = [label]
        if status == "idle":
            parts.append(_ago(info.get("last-sync-time", 0)))
        parts.append(info.get("local-dir", ""))
        self.set_subtitle(" · ".join(p for p in parts if p))
        n = info.get("conflict-count", 0)
        self._conflict_badge.set_label(
            ngettext("{n} conflict", "{n} conflicts", n).format(n=n) if n else "")
        self._sync_btn.set_sensitive(status in ("idle", "error"))


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application, config: Config, proxy: DaemonProxy) -> None:
        super().__init__(application=application, title="GDrive Sync",
                         default_width=560, default_height=520)
        self.config = config
        self.proxy = proxy
        self._rows: dict[str, AccountRow] = {}
        self._preview_run: rclone.BisyncRun | None = None

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()

        add_btn = Gtk.Button(icon_name="list-add-symbolic",
                             tooltip_text=_("Add Google account"))
        add_btn.connect("clicked", lambda *_: self.get_application().start_wizard())
        header.pack_start(add_btn)

        menu = Gio.Menu()
        menu.append(_("About"), "win.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))
        toolbar.add_top_bar(header)

        self.banner = Adw.Banner(button_label=_("Repair…"))
        self.banner.connect("button-clicked", self._on_banner_repair)
        toolbar.add_top_bar(self.banner)
        self._banner_account = ""

        self.accounts_group = Adw.PreferencesGroup(
            title=_("Accounts"), margin_start=24, margin_end=24, margin_top=12)
        accounts_page = Gtk.ScrolledWindow(
            child=self.accounts_group, vexpand=True)

        self.empty_page = Adw.StatusPage(
            icon_name=const.APP_ID,
            title=_("No account configured"),
            description=_("Connect a Google account to start synchronizing."),
            vexpand=True)
        empty_btn = Gtk.Button(label=_("Add Google account"), halign=Gtk.Align.CENTER)
        empty_btn.add_css_class("suggested-action")
        empty_btn.add_css_class("pill")
        empty_btn.connect("clicked", lambda *_: self.get_application().start_wizard())
        self.empty_page.set_child(empty_btn)

        self.unavailable_page = Adw.StatusPage(
            icon_name="dialog-question-symbolic",
            title=_("Service not running"),
            description=_("The synchronization service is not responding.\n"
                          "Start it with: {command}").format(
                command=f"systemctl --user start {const.SYSTEMD_UNIT}"),
            vexpand=True)

        self.stack = Gtk.Stack()
        self.stack.add_named(accounts_page, "accounts")
        self.stack.add_named(self.empty_page, "empty")
        self.stack.add_named(self.unavailable_page, "unavailable")
        toolbar.set_content(self.stack)
        self.set_content(toolbar)

        # per-account actions (parameter: account id)
        for name, cb in (("toggle-pause", self._act_toggle_pause),
                         ("conflicts", self._act_conflicts),
                         ("show-log", self._act_show_log),
                         ("prefs", self._act_prefs),
                         ("repair", self._act_repair),
                         ("remove", self._act_remove)):
            action = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            action.connect("activate", lambda _a, p, cb=cb: cb(p.unpack()))
            self.add_action(action)
        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", lambda *_: self._show_about())
        self.add_action(about)

        proxy.connect("state-changed", lambda _p, aid, _s: self._refresh_account(aid))
        proxy.connect("sync-completed", lambda _p, aid, _ok, _n: self._refresh_account(aid))
        proxy.connect("accounts-changed", lambda _p: self.refresh())
        proxy.connect("availability-changed", lambda _p, _a: self.refresh())
        GLib.timeout_add_seconds(30, self._periodic_refresh)
        self.refresh()

    # ------------------------------------------------------------ rendering

    def refresh(self) -> None:
        if not self.proxy.available:
            self.stack.set_visible_child_name("unavailable")
            return

        def got_ids(ids: list[str]) -> None:
            if not ids:
                self.stack.set_visible_child_name("empty")
                return
            self.stack.set_visible_child_name("accounts")
            for stale in set(self._rows) - set(ids):
                self.accounts_group.remove(self._rows.pop(stale))
            for account_id in ids:
                if account_id not in self._rows:
                    row = AccountRow(self, account_id)
                    self._rows[account_id] = row
                    self.accounts_group.add(row)
                self._refresh_account(account_id)

        self.proxy.list_accounts(got_ids, on_error=lambda _m: None)

    def _refresh_account(self, account_id: str) -> None:
        row = self._rows.get(account_id)
        if row is None:
            self.refresh()
            return

        def got(info: dict) -> None:
            row.update(info)
            self._update_banner()

        self.proxy.get_account_info(account_id, got, on_error=lambda _m: None)

    def _update_banner(self) -> None:
        broken = [(aid, r) for aid, r in self._rows.items()
                  if r.info.get("status") == "needs_resync"]
        if broken:
            aid, row = broken[0]
            self._banner_account = aid
            self.banner.set_title(
                _("{account}: synchronization interrupted, needs attention").format(
                    account=row.info.get("display-name", aid)))
            self.banner.set_revealed(True)
        else:
            self.banner.set_revealed(False)

    def _periodic_refresh(self) -> bool:
        self.refresh()
        return GLib.SOURCE_CONTINUE

    # -------------------------------------------------------------- actions

    def open_folder(self, row: AccountRow) -> None:
        path = row.info.get("local-dir", "")
        if path:
            Gio.AppInfo.launch_default_for_uri(
                GLib.filename_to_uri(path, None), None)

    def _act_toggle_pause(self, account_id: str) -> None:
        row = self._rows.get(account_id)
        if row and row.info.get("status") == "paused":
            self.proxy.resume(account_id)
        else:
            self.proxy.pause(account_id)

    def _act_conflicts(self, account_id: str) -> None:
        ConflictsDialog(self.config.account(account_id), self.proxy).present(self)

    def _act_prefs(self, account_id: str) -> None:
        PreferencesDialog(self.config.account(account_id), self.proxy,
                          self.config).present(self)

    def _act_show_log(self, account_id: str) -> None:
        dialog = Adw.Dialog(title=_("Synchronization log"),
                            content_width=700, content_height=500)
        view = Gtk.TextView(editable=False, monospace=True)
        scroll = Gtk.ScrolledWindow(child=view, vexpand=True)
        tb = Adw.ToolbarView(content=scroll)
        tb.add_top_bar(Adw.HeaderBar())
        dialog.set_child(tb)
        self.proxy.get_recent_log(
            account_id, 400,
            on_done=lambda lines: view.get_buffer().set_text("\n".join(lines)),
            on_error=lambda msg: view.get_buffer().set_text(
                _("Log not available: {message}").format(message=msg)),
        )
        dialog.present(self)

    def _on_banner_repair(self, *_args) -> None:
        if self._banner_account:
            self._act_repair(self._banner_account)

    def _act_remove(self, account_id: str) -> None:
        account = self.config.account(account_id)
        dialog = Adw.AlertDialog(
            heading=_("Remove the account?"),
            body=_("Synchronization of “{account}” will stop.\n"
                   "The local files in {folder} will NOT be deleted.").format(
                account=account.display_name, folder=account.local_dir),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("remove", _("Remove"))
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")

        def on_response(_d, response: str) -> None:
            if response != "remove":
                return
            bookmarks.remove_bookmark(account.local_dir)
            remote_name = account.remote_name if account.remote.endswith(":") else ""
            self.config.remove_account(account_id)
            if remote_name:
                threading.Thread(target=rclone.delete_remote, args=(remote_name,),
                                 daemon=True).start()
            self.proxy.reload_config()
            GLib.timeout_add(500, lambda: (self.refresh(), GLib.SOURCE_REMOVE)[1])

        dialog.connect("response", on_response)
        dialog.present(self)

    def _show_about(self) -> None:
        from .. import __version__
        Adw.AboutDialog(
            application_name="GDrive Sync",
            application_icon=const.APP_ID,
            version=__version__,
            developer_name="Stefano Teodorani",
            license_type=Gtk.License.GPL_3_0,
            comments=_("Bidirectional Google Drive synchronization for GNOME, based on rclone."),
        ).present(self)

    # ------------------------------------------------------------- recovery

    def _act_repair(self, account_id: str) -> None:
        account = self.config.account(account_id)
        # Only one preview at a time; a previous one may still be running.
        if self._preview_run is not None:
            self._preview_run.cancel()

        dialog = Adw.AlertDialog(
            heading=_("Repair of “{account}”").format(account=account.display_name),
            body=_("Computing what the repair would involve…"),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("resync", _("Repair"))
        dialog.set_response_appearance("resync", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_enabled("resync", False)
        dialog.set_default_response("cancel")

        def on_response(_d, response: str) -> None:
            # The dry-run must never race the real resync (or the next
            # periodic sync): kill it as soon as the dialog is answered.
            if self._preview_run is not None:
                self._preview_run.cancel()
            if response == "resync":
                self.proxy.resync(account_id)

        dialog.connect("response", on_response)
        dialog.present(self)

        # The preview runs in its own scratch workdir: sharing the daemon's
        # workdir would leave a bisync lock that makes the real repair (and
        # any concurrent sync) fail instantly.
        preview_workdir = const.STATE_DIR / f"resync-preview-{account_id}.workdir"

        def worker() -> None:
            shutil.rmtree(preview_workdir, ignore_errors=True)
            preview_workdir.mkdir(parents=True, exist_ok=True)
            version = rclone.detect_version()
            cmd = rclone.build_bisync_cmd(
                account.local_dir, account.remote,
                version=version, resync=True, dry_run=True,
                filters_file=account.filters_file,
                workdir=preview_workdir,
                log_file=const.STATE_DIR / f"resync-preview-{account_id}.log")
            run = rclone.BisyncRun(cmd)
            self._preview_run = run
            result = run.wait()
            self._preview_run = None
            shutil.rmtree(preview_workdir, ignore_errors=True)
            if result.outcome is rclone.Outcome.CANCELLED:
                return
            changes = result.log_tail.count("Would copy") + result.log_tail.count("NOTICE:")
            GLib.idle_add(self._recovery_preview_ready, dialog, result, changes)

        threading.Thread(target=worker, daemon=True).start()

    def _recovery_preview_ready(self, dialog: Adw.AlertDialog,
                                result: rclone.SyncResult, changes: int) -> bool:
        dialog.set_body(
            _("The repair realigns both sides and resumes synchronization.\n\n"
              "⚠ For files that differ on both sides, the LOCAL version wins.\n"
              "⚠ Files deleted on one side only after the last successful "
              "synchronization will be RESTORED.\n\n"
              "Expected operations: about {changes}.").format(changes=changes)
            if result.ok or result.outcome is rclone.Outcome.NEEDS_RESYNC else
            _("Preview not available ({error}). You can proceed anyway.").format(
                error=result.stderr[-200:]))
        dialog.set_response_enabled("resync", True)
        return GLib.SOURCE_REMOVE
