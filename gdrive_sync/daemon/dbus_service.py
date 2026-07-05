"""D-Bus interface exported by the daemon — the GUI↔daemon contract (v2, multi-account)."""

from __future__ import annotations

import logging
from pathlib import Path

from gi.repository import Gio, GLib

from .. import const

log = logging.getLogger(__name__)

INTERFACE_XML = f"""
<node>
  <interface name="{const.DBUS_INTERFACE}">
    <method name="ListAccounts">
      <arg name="ids" type="as" direction="out"/>
    </method>
    <method name="GetAccountInfo">
      <arg name="id" type="s" direction="in"/>
      <arg name="info" type="a{{sv}}" direction="out"/>
    </method>
    <method name="SyncNow">
      <arg name="id" type="s" direction="in"/>
    </method>
    <method name="Pause">
      <arg name="id" type="s" direction="in"/>
    </method>
    <method name="Resume">
      <arg name="id" type="s" direction="in"/>
    </method>
    <method name="Resync">
      <arg name="id" type="s" direction="in"/>
    </method>
    <method name="CancelSync">
      <arg name="id" type="s" direction="in"/>
    </method>
    <method name="GetRecentLog">
      <arg name="id" type="s" direction="in"/>
      <arg name="lines" type="u" direction="in"/>
      <arg name="text" type="as" direction="out"/>
    </method>
    <method name="ReloadConfig"/>
    <signal name="StateChanged">
      <arg name="account" type="s"/>
      <arg name="status" type="s"/>
    </signal>
    <signal name="SyncCompleted">
      <arg name="account" type="s"/>
      <arg name="ok" type="b"/>
      <arg name="conflicts" type="u"/>
    </signal>
    <signal name="AccountsChanged"/>
  </interface>
</node>
"""


class DBusService:
    """Bridges D-Bus calls to the daemon's account manager.

    The manager must expose: engines: dict[str, Engine], reload().
    """

    def __init__(self, manager) -> None:
        self.manager = manager
        self._connection: Gio.DBusConnection | None = None
        self._registration_id = 0

    def register(self, connection: Gio.DBusConnection) -> None:
        node = Gio.DBusNodeInfo.new_for_xml(INTERFACE_XML)
        self._registration_id = connection.register_object(
            const.DBUS_PATH, node.interfaces[0], self._on_method_call, None, None)
        self._connection = connection

    def unregister(self) -> None:
        if self._connection and self._registration_id:
            self._connection.unregister_object(self._registration_id)
            self._registration_id = 0

    def _engine(self, account_id: str):
        engine = self.manager.engines.get(account_id)
        if engine is None:
            raise KeyError(f"account sconosciuto: {account_id}")
        return engine

    # ------------------------------------------------------------- inbound

    def _on_method_call(self, _conn, _sender, _path, _iface, method, params, invocation) -> None:
        try:
            if method == "ListAccounts":
                ids = list(self.manager.engines.keys())
                invocation.return_value(GLib.Variant("(as)", (ids,)))
            elif method == "GetAccountInfo":
                (account_id,) = params.unpack()
                invocation.return_value(GLib.Variant(
                    "(a{sv})", (self._account_info(account_id),)))
            elif method in ("SyncNow", "Pause", "Resume", "Resync", "CancelSync"):
                (account_id,) = params.unpack()
                engine = self._engine(account_id)
                {
                    "SyncNow": lambda: engine.request_sync("dbus"),
                    "Pause": engine.pause,
                    "Resume": engine.resume,
                    "Resync": engine.request_resync,
                    "CancelSync": engine.cancel_sync,
                }[method]()
                invocation.return_value(None)
            elif method == "GetRecentLog":
                account_id, lines = params.unpack()
                engine = self._engine(account_id)
                invocation.return_value(GLib.Variant(
                    "(as)", (self._read_log(engine.account.sync_log_file, lines),)))
            elif method == "ReloadConfig":
                self.manager.reload()
                invocation.return_value(None)
            else:
                invocation.return_error_literal(
                    Gio.dbus_error_quark(), Gio.DBusError.UNKNOWN_METHOD, method)
        except Exception as e:  # surface errors to the caller
            log.exception("D-Bus method %s failed", method)
            invocation.return_error_literal(
                Gio.dbus_error_quark(), Gio.DBusError.FAILED, str(e))

    def _account_info(self, account_id: str) -> dict:
        engine = self._engine(account_id)
        return {
            "status": GLib.Variant("s", engine.state.value),
            "last-sync-time": GLib.Variant("x", engine.last_sync_time),
            "last-error": GLib.Variant("s", engine.last_error),
            "conflict-count": GLib.Variant("u", engine.conflict_count),
            "paused": GLib.Variant("b", engine.paused),
            "display-name": GLib.Variant("s", engine.account.display_name),
            "local-dir": GLib.Variant("s", str(engine.account.local_dir)),
            "remote": GLib.Variant("s", engine.account.remote),
        }

    @staticmethod
    def _read_log(sync_log: Path, lines: int) -> list[str]:
        lines = min(int(lines) or 200, 2000)
        try:
            return Path(sync_log).read_text(errors="replace").splitlines()[-lines:]
        except OSError:
            return []

    # ------------------------------------------------------------- outbound

    def _emit(self, signal: str, variant: GLib.Variant | None) -> None:
        if self._connection is None:
            return
        self._connection.emit_signal(
            None, const.DBUS_PATH, const.DBUS_INTERFACE, signal, variant)

    def emit_state_changed(self, account_id: str, status: str) -> None:
        self._emit("StateChanged", GLib.Variant("(ss)", (account_id, status)))

    def emit_sync_completed(self, account_id: str, ok: bool, conflicts: int) -> None:
        self._emit("SyncCompleted", GLib.Variant("(sbu)", (account_id, ok, conflicts)))

    def emit_accounts_changed(self) -> None:
        self._emit("AccountsChanged", None)
