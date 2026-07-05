"""gdrive-sync-daemon entry point.

A non-GUI Gio.Application: owning the application id doubles as owning the
D-Bus name expected by the systemd user unit (Type=dbus) and enables
Gio.Notification without a display. Manages one sync Engine per account.
"""

from __future__ import annotations

import logging
import signal
import sys

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

from .. import const, rclone
from ..config import Config
from . import notify
from .dbus_service import DBusService
from .engine import Engine
from .tray import TrayIcon
from .watcher import LocalWatcher

log = logging.getLogger("gdrive-sync-daemon")


class AccountManager:
    """Keeps one Engine (+ watcher) per configured account."""

    def __init__(self, config: Config, version: rclone.RcloneVersion, app) -> None:
        self.config = config
        self.version = version
        self.app = app
        self.engines: dict[str, Engine] = {}
        self.watchers: dict[str, LocalWatcher] = {}
        self.service: DBusService | None = None
        self.tray: TrayIcon | None = None
        config.connect_accounts_changed(self.reconcile)

    def reconcile(self) -> None:
        """Create/destroy engines to match the configured account list."""
        wanted = set(self.config.account_ids)
        current = set(self.engines)

        for account_id in current - wanted:
            log.info("removing account %s", account_id)
            self.watchers.pop(account_id).stop()
            self.engines.pop(account_id).shutdown()
        for account_id in sorted(wanted - current):
            log.info("adding account %s", account_id)
            self._add_engine(account_id)

        if self.service and (wanted != current):
            self.service.emit_accounts_changed()
        if self.tray:
            self.tray.refresh()

    def _add_engine(self, account_id: str) -> None:
        account = self.config.account(account_id)
        engine = Engine(account, self.version)
        watcher = LocalWatcher(
            account.local_dir, lambda aid=account_id: self._on_fs_change(aid))
        engine.watcher = watcher

        engine.on_state_changed = (
            lambda state, aid=account_id: self._on_state_changed(aid, state))
        engine.on_sync_completed = (
            lambda ok, n, aid=account_id: self._on_sync_completed(aid, ok, n))
        engine.on_notify = (
            lambda kind, title, body, aid=account_id:
            notify.send(self.app, kind, title, body, aid))
        account.connect_changed(
            lambda key, aid=account_id: self._on_account_setting_changed(aid, key))

        self.engines[account_id] = engine
        self.watchers[account_id] = watcher
        engine.start()
        watcher.start()

    def _on_fs_change(self, account_id: str) -> None:
        engine = self.engines.get(account_id)
        if engine:
            engine.request_sync("fs-change")

    def _on_state_changed(self, account_id: str, state) -> None:
        if self.service:
            self.service.emit_state_changed(account_id, state.value)
        if self.tray:
            self.tray.refresh()

    def _on_sync_completed(self, account_id: str, ok: bool, conflicts: int) -> None:
        if self.service:
            self.service.emit_sync_completed(account_id, ok, conflicts)

    def _on_account_setting_changed(self, account_id: str, key: str) -> None:
        engine = self.engines.get(account_id)
        watcher = self.watchers.get(account_id)
        if engine is None:
            return
        if key == "local-dir" and watcher is not None:
            watcher.restart(engine.account.local_dir)
        engine.reload_config()

    def reload(self) -> None:
        self.reconcile()
        for engine in self.engines.values():
            engine.reload_config()

    def shutdown(self) -> None:
        for watcher in self.watchers.values():
            watcher.stop()
        for engine in self.engines.values():
            engine.shutdown()


class DaemonApp(Gio.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=const.DBUS_NAME,
            flags=Gio.ApplicationFlags.IS_SERVICE,
        )
        self.manager: AccountManager | None = None
        self.service: DBusService | None = None
        self.tray: TrayIcon | None = None

        action = Gio.SimpleAction.new("open-gui", None)
        action.connect("activate", lambda *_: notify.launch_gui())
        self.add_action(action)

    def do_dbus_register(self, connection: Gio.DBusConnection, object_path: str) -> bool:
        if not Gio.Application.do_dbus_register(self, connection, object_path):
            return False
        config = Config()
        if not config.schema_ok and any(const.CONFIG_DIR.glob("filters-*.txt")):
            # Schemas can be briefly unavailable while a package upgrade
            # recompiles them; a memory-backed config would report zero
            # accounts. Fail startup and let systemd retry.
            log.error("GSettings schemas unavailable but accounts exist on "
                      "disk; refusing to start with an empty configuration")
            return False
        config.migrate()
        try:
            version = rclone.detect_version()
        except rclone.RcloneNotFoundError:
            log.error("rclone not found; daemon cannot start")
            return False
        self.manager = AccountManager(config, version, self)
        self.service = DBusService(self.manager)
        self.manager.service = self.service
        self.service.register(connection)
        self.tray = TrayIcon(self.manager, connection)
        self.manager.tray = self.tray
        if config.tray_icon:
            self.tray.enable()
        config.connect_tray_icon_changed(
            lambda: self.tray.enable() if config.tray_icon else self.tray.disable())
        return True

    def do_dbus_unregister(self, connection, object_path) -> None:
        if self.service:
            self.service.unregister()
        Gio.Application.do_dbus_unregister(self, connection, object_path)

    def do_startup(self) -> None:
        Gio.Application.do_startup(self)
        self.hold()  # stay alive: we are a daemon, not request-driven
        const.ensure_dirs()
        self.manager.reconcile()
        if not self.manager.engines:
            log.info("no accounts configured; idling")

        for signum in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, signum, self._on_quit_signal)

    def _on_quit_signal(self) -> bool:
        log.info("shutting down")
        if self.manager:
            self.manager.shutdown()
        self.release()
        return GLib.SOURCE_REMOVE


def main() -> int:
    const.ensure_dirs()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.FileHandler(const.DAEMON_LOG_FILE),
        ],
    )
    app = DaemonApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
