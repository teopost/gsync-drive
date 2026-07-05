"""Per-account preferences: sync options, exclusion patterns, integration toggles."""

from __future__ import annotations

import logging
import re
import shutil
import threading
from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk

from ..config import AccountConfig, read_user_filters, write_filters
from ..i18n import _
from . import bookmarks
from .daemon_proxy import DaemonProxy
from .drive_tree import DriveFolderTree

log = logging.getLogger(__name__)

_BWLIMIT_RE = re.compile(r"^$|^\d+(\.\d+)?[KMG]?(:(\d+(\.\d+)?[KMG]?|off))?$|^off$", re.I)


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, account: AccountConfig, proxy: DaemonProxy) -> None:
        super().__init__(title=_("Preferences — {account}").format(account=account.display_name))
        self.account = account
        self.proxy = proxy

        page = Adw.PreferencesPage(title=_("General"), icon_name="emblem-system-symbolic")
        self.add(page)

        # ---------------------------------------------------------- account
        account_group = Adw.PreferencesGroup(title=_("Account"))
        page.add(account_group)

        self.name_row = Adw.EntryRow(title=_("Account name"))
        self.name_row.set_text(account.display_name)
        self.name_row.connect(
            "changed",
            lambda row: setattr(account, "display_name", row.get_text().strip()))
        account_group.add(self.name_row)

        self.local_row = Adw.ActionRow(title=_("Local folder"),
                                       subtitle=str(account.local_dir))
        move_btn = Gtk.Button(label=_("Move…"), valign=Gtk.Align.CENTER)
        move_btn.connect("clicked", self._on_move_folder)
        self.local_row.add_suffix(move_btn)
        account_group.add(self.local_row)

        # ------------------------------------------------------------- sync
        sync_group = Adw.PreferencesGroup(title=_("Synchronization"))
        page.add(sync_group)

        self.interval_row = Adw.SpinRow(
            title=_("Synchronization interval"),
            subtitle=_("Seconds between periodic synchronizations"),
            adjustment=Gtk.Adjustment(lower=60, upper=86400, step_increment=60,
                                      page_increment=300),
        )
        sync_group.add(self.interval_row)

        self.bwlimit_row = Adw.EntryRow(title=_("Bandwidth limit (e.g. 1M, 500K, empty = none)"))
        sync_group.add(self.bwlimit_row)

        self.max_delete_row = Adw.SpinRow(
            title=_("Deletion safety threshold"),
            subtitle=_("Aborts a sync that would delete more than this % of files"),
            adjustment=Gtk.Adjustment(lower=1, upper=100, step_increment=5,
                                      page_increment=10),
        )
        sync_group.add(self.max_delete_row)

        if account.settings is not None:
            account.settings.bind("sync-interval", self.interval_row, "value",
                                  Gio.SettingsBindFlags.DEFAULT)
            account.settings.bind("max-delete", self.max_delete_row, "value",
                                  Gio.SettingsBindFlags.DEFAULT)
        self.bwlimit_row.set_text(account.bandwidth_limit)
        self.bwlimit_row.connect("changed", self._on_bwlimit_changed)

        # ----------------------------------------------------- drive folders
        folders_group = Adw.PreferencesGroup(title=_("Drive folders"))
        page.add(folders_group)

        self.folders_row = Adw.ActionRow(title=_("Synchronized folders"))
        edit_btn = Gtk.Button(label=_("Edit…"), valign=Gtk.Align.CENTER)
        edit_btn.connect("clicked", self._on_edit_folders)
        self.folders_row.add_suffix(edit_btn)
        folders_group.add(self.folders_row)
        self._update_folders_subtitle()

        # ------------------------------------------------------- exclusions
        self.excl_group = Adw.PreferencesGroup(
            title=_("Exclusions"),
            description=_("rclone patterns of files not to synchronize (e.g. *.iso, cache/**)"),
        )
        page.add(self.excl_group)

        self.new_pattern_row = Adw.EntryRow(title=_("New pattern"))
        add_btn = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER,
                             tooltip_text=_("Add"))
        add_btn.add_css_class("flat")
        add_btn.connect("clicked", self._on_add_pattern)
        self.new_pattern_row.add_suffix(add_btn)
        self.new_pattern_row.connect("entry-activated", self._on_add_pattern)
        self.excl_group.add(self.new_pattern_row)

        self._pattern_rows: list[Adw.ActionRow] = []
        self._reload_patterns()

        # ------------------------------------------------------ integration
        integ_group = Adw.PreferencesGroup(title=_("Integration"))
        page.add(integ_group)

        self.sidebar_row = Adw.SwitchRow(
            title=_("Folder in the Files sidebar"))
        self.sidebar_row.set_active(account.sidebar_bookmark)
        self.sidebar_row.connect("notify::active", self._on_sidebar_toggled)
        integ_group.add(self.sidebar_row)

        self.connect("closed", lambda *_: self.proxy.reload_config())

    # ----------------------------------------------------------------- sync

    def _on_bwlimit_changed(self, row: Adw.EntryRow) -> None:
        text = row.get_text().strip()
        if _BWLIMIT_RE.match(text):
            row.remove_css_class("error")
            self.account._set("bandwidth-limit", text)
        else:
            row.add_css_class("error")

    # ---------------------------------------------------------- local folder

    def _on_move_folder(self, *_args) -> None:
        dialog = Gtk.FileDialog(title=_("Choose the new synchronization folder"))

        def picked(d, result):
            try:
                f = d.select_folder_finish(result)
            except GLib.Error:
                return
            self._confirm_move(Path(f.get_path()))

        dialog.select_folder(self.get_root(), None, picked)

    def _confirm_move(self, target: Path) -> None:
        current = self.account.local_dir
        if target == current:
            return
        if str(target).startswith(str(current) + "/"):
            self.add_toast(Adw.Toast(
                title=_("The destination cannot be inside the current folder")))
            return
        if target.exists() and any(target.iterdir()):
            self.add_toast(Adw.Toast(title=_("The destination folder must be empty")))
            return

        confirm = Adw.AlertDialog(
            heading=_("Move the synchronized folder?"),
            body=_("The files will be moved from {current} to {target} and "
                   "the synchronization realigned. This does not change "
                   "anything on Google Drive.").format(current=current, target=target),
        )
        confirm.add_response("cancel", _("Cancel"))
        confirm.add_response("move", _("Move"))
        confirm.set_response_appearance("move", Adw.ResponseAppearance.SUGGESTED)
        confirm.set_default_response("move")
        confirm.connect(
            "response",
            lambda _d, r: r == "move" and self._apply_move(current, target))
        confirm.present(self)

    def _apply_move(self, current: Path, target: Path) -> None:
        # Stop any run first: bisync must not be walking the tree mid-move.
        self.proxy.cancel_sync(self.account.id)

        def worker() -> None:
            try:
                if current.is_dir():
                    if target.exists():
                        target.rmdir()  # verified empty in _confirm_move
                    shutil.move(str(current), str(target))
                else:
                    # Folder already renamed by hand: only adopt the new path.
                    target.mkdir(parents=True, exist_ok=True)
                GLib.idle_add(self._on_move_done, current, target, None)
            except OSError as e:
                GLib.idle_add(self._on_move_done, current, target, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _on_move_done(self, current: Path, target: Path, error: str | None) -> bool:
        if error:
            self.add_toast(Adw.Toast(
                title=_("Cannot move the folder: {message}").format(message=error)))
            return GLib.SOURCE_REMOVE
        self.account.local_dir = target
        bookmarks.remove_bookmark(current)
        if self.account.sidebar_bookmark:
            bookmarks.add_bookmark(target)
            bookmarks.set_folder_icon(target)
        self.local_row.set_subtitle(str(target))
        self.add_toast(Adw.Toast(title=_("Folder moved; realigning…")))
        # The bisync listings are keyed to the old path: realign right away.
        # The daemon restarts the file watcher on the local-dir change itself.
        self.proxy.resync(
            self.account.id,
            on_error=lambda m: self.add_toast(Adw.Toast(
                title=_("Realign not started: {message}").format(message=m))))
        return GLib.SOURCE_REMOVE

    # --------------------------------------------------------- drive folders

    def _update_folders_subtitle(self) -> None:
        sel = self.account.sync_folders
        if not sel:
            self.folders_row.set_subtitle(_("Whole Drive"))
        else:
            shown = ", ".join(sel[:3]) + ("…" if len(sel) > 3 else "")
            self.folders_row.set_subtitle(
                _("{count} selected: {folders}").format(count=len(sel), folders=shown))

    def _on_edit_folders(self, *_args) -> None:
        dialog = Adw.Dialog(title=_("Folders to synchronize"),
                            content_width=520, content_height=560)
        tree = DriveFolderTree(remote=self.account.remote,
                               initial_selection=self.account.sync_folders)

        header = Adw.HeaderBar()
        apply_btn = Gtk.Button(label=_("Apply"))
        apply_btn.add_css_class("suggested-action")
        apply_btn.connect("clicked", lambda *_a: self._on_folders_chosen(dialog, tree))
        header.pack_end(apply_btn)

        hint = Gtk.Label(
            label=_("With no selection, the whole Drive is synchronized."),
            wrap=True)
        hint.add_css_class("dim-label")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      margin_start=12, margin_end=12, margin_bottom=12)
        box.append(tree)
        box.append(hint)
        tb = Adw.ToolbarView(content=box)
        tb.add_top_bar(header)
        dialog.set_child(tb)

        tree.load()
        dialog.present(self)

    def _on_folders_chosen(self, dialog: Adw.Dialog, tree: DriveFolderTree) -> None:
        new = tree.selected_paths()
        if new == self.account.sync_folders:
            dialog.close()
            return

        confirm = Adw.AlertDialog(
            heading=_("Apply the new folder selection?"),
            body=_("The synchronization will be realigned now (resync). "
                   "Folders removed from the selection stay on this computer "
                   "but are no longer synchronized."),
        )
        confirm.add_response("cancel", _("Cancel"))
        confirm.add_response("apply", _("Apply"))
        confirm.set_response_appearance("apply", Adw.ResponseAppearance.SUGGESTED)
        confirm.set_default_response("apply")

        def on_response(_d, response: str) -> None:
            if response != "apply":
                return
            self.account.sync_folders = new
            # Rewriting the filters preserves the user exclusion patterns;
            # rclone then requires --resync, which we request right away.
            write_filters(read_user_filters(self.account.filters_file),
                          self.account.filters_file,
                          include_dirs=self.account.sync_folders)
            self._update_folders_subtitle()
            dialog.close()
            self.add_toast(Adw.Toast(title=_("Folder selection updated; realigning…")))
            self.proxy.resync(
                self.account.id,
                on_error=lambda m: self.add_toast(Adw.Toast(
                    title=_("Realign not started: {message}").format(message=m))))

        confirm.connect("response", on_response)
        confirm.present(dialog)

    # ----------------------------------------------------------- exclusions

    def _reload_patterns(self) -> None:
        for row in self._pattern_rows:
            self.excl_group.remove(row)
        self._pattern_rows.clear()
        for pattern in read_user_filters(self.account.filters_file):
            row = Adw.ActionRow(title=pattern)
            rm = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text=_("Remove"))
            rm.add_css_class("flat")
            rm.connect("clicked", self._on_remove_pattern, pattern)
            row.add_suffix(rm)
            self.excl_group.add(row)
            self._pattern_rows.append(row)

    def _on_add_pattern(self, *_args) -> None:
        pattern = self.new_pattern_row.get_text().strip()
        if not pattern:
            return
        patterns = read_user_filters(self.account.filters_file)
        if pattern not in patterns:
            patterns.append(pattern)
            write_filters(patterns, self.account.filters_file,
                          include_dirs=self.account.sync_folders)
        self.new_pattern_row.set_text("")
        self._reload_patterns()

    def _on_remove_pattern(self, _btn, pattern: str) -> None:
        patterns = [p for p in read_user_filters(self.account.filters_file) if p != pattern]
        write_filters(patterns, self.account.filters_file,
                      include_dirs=self.account.sync_folders)
        self._reload_patterns()

    # ---------------------------------------------------------- integration

    def _on_sidebar_toggled(self, row: Adw.SwitchRow, _pspec) -> None:
        self.account._set("sidebar-bookmark", row.get_active())
        if row.get_active():
            bookmarks.add_bookmark(self.account.local_dir)
        else:
            bookmarks.remove_bookmark(self.account.local_dir)

