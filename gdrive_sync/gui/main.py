"""gdrive-sync GUI entry point."""

from __future__ import annotations

import logging
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio

from .. import const
from ..config import Config
from .daemon_proxy import DaemonProxy
from .window import MainWindow
from .wizard import SetupWizard

log = logging.getLogger("gdrive-sync")


class GDriveSyncApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=const.APP_ID,
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.config: Config | None = None
        self.proxy: DaemonProxy | None = None
        self.window: MainWindow | None = None

        action = Gio.SimpleAction.new("show-conflicts", None)
        action.connect("activate", lambda *_: self.do_activate())
        self.add_action(action)

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        const.ensure_dirs()
        self.config = Config()
        self.config.migrate()
        self.proxy = DaemonProxy()

    def do_activate(self) -> None:
        if not self.config.account_ids:
            self.start_wizard()
            return
        self._present_main_window()

    def _present_main_window(self) -> None:
        if self.window is None:
            self.window = MainWindow(self, self.config, self.proxy)
        self.window.present()
        self.window.refresh()

    def start_wizard(self) -> None:
        wizard = SetupWizard(self, self.config, self.proxy,
                             on_finished=self._on_setup_finished)
        wizard.present()

    def _on_setup_finished(self) -> None:
        self.proxy.reload_config()
        self._present_main_window()


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    return GDriveSyncApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
