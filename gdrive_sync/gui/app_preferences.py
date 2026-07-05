"""Application-wide preferences: options that are not tied to one account."""

from __future__ import annotations

import logging
import subprocess

from gi.repository import Adw, Gtk

from .. import const
from ..config import Config
from ..i18n import _

log = logging.getLogger(__name__)


class AppPreferencesDialog(Adw.PreferencesDialog):
    """Global settings (tray icon, autostart) and the service log."""

    def __init__(self, config: Config) -> None:
        super().__init__(title=_("Preferences"))
        self.config = config

        page = Adw.PreferencesPage(title=_("General"), icon_name="emblem-system-symbolic")
        self.add(page)

        integ_group = Adw.PreferencesGroup(title=_("Integration"))
        page.add(integ_group)

        self.tray_row = Adw.SwitchRow(
            title=_("Tray icon"),
            subtitle=_("On GNOME it requires the AppIndicator extension"))
        self.tray_row.set_active(config.tray_icon)
        self.tray_row.connect(
            "notify::active",
            lambda row, _p: setattr(config, "tray_icon", row.get_active()))
        integ_group.add(self.tray_row)

        self.autostart_row = Adw.SwitchRow(
            title=_("Start synchronization at login"))
        self.autostart_row.set_active(self._autostart_enabled())
        self.autostart_row.connect("notify::active", self._on_autostart_toggled)
        integ_group.add(self.autostart_row)

        diag_group = Adw.PreferencesGroup(title=_("Diagnostics"))
        page.add(diag_group)

        log_row = Adw.ActionRow(
            title=_("Service log"),
            subtitle=_("Messages from the synchronization service"),
            activatable=True)
        log_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        log_row.connect("activated", self._on_show_log)
        diag_group.add(log_row)

    # -------------------------------------------------------------- autostart

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

    # ------------------------------------------------------------ service log

    def _on_show_log(self, *_args) -> None:
        dialog = Adw.Dialog(title=_("Service log"),
                            content_width=700, content_height=500)
        view = Gtk.TextView(editable=False, monospace=True)
        scroll = Gtk.ScrolledWindow(child=view, vexpand=True)
        tb = Adw.ToolbarView(content=scroll)
        tb.add_top_bar(Adw.HeaderBar())
        dialog.set_child(tb)
        try:
            text = const.DAEMON_LOG_FILE.read_text(errors="replace")
            lines = text.splitlines()[-500:]
            view.get_buffer().set_text("\n".join(lines))
        except OSError as e:
            view.get_buffer().set_text(
                _("Log not available: {message}").format(message=e))
        dialog.present(self)
