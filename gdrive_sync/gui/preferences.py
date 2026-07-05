"""Per-account preferences: sync options, exclusion patterns, integration toggles."""

from __future__ import annotations

import logging
import re
import subprocess

from gi.repository import Adw, Gio, Gtk

from .. import const
from ..config import AccountConfig, read_user_filters, write_filters
from ..i18n import _
from . import bookmarks
from .daemon_proxy import DaemonProxy

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

        self.autostart_row = Adw.SwitchRow(
            title=_("Start synchronization at login"),
            subtitle=_("Applies to all accounts"))
        self.autostart_row.set_active(self._autostart_enabled())
        self.autostart_row.connect("notify::active", self._on_autostart_toggled)
        integ_group.add(self.autostart_row)

        self.connect("closed", lambda *_: self.proxy.reload_config())

    # ----------------------------------------------------------------- sync

    def _on_bwlimit_changed(self, row: Adw.EntryRow) -> None:
        text = row.get_text().strip()
        if _BWLIMIT_RE.match(text):
            row.remove_css_class("error")
            self.account._set("bandwidth-limit", text)
        else:
            row.add_css_class("error")

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

    @staticmethod
    def _autostart_enabled() -> bool:
        res = subprocess.run(
            ["systemctl", "--user", "is-enabled", const.SYSTEMD_UNIT],
            capture_output=True, text=True)
        return res.stdout.strip() == "enabled"

    def _on_autostart_toggled(self, row: Adw.SwitchRow, _pspec) -> None:
        verb = "enable" if row.get_active() else "disable"
        subprocess.run(["systemctl", "--user", verb, const.SYSTEMD_UNIT],
                       capture_output=True)
