"""Client-side wrapper around the daemon's multi-account D-Bus interface."""

from __future__ import annotations

import logging

from gi.repository import Gio, GLib, GObject

from .. import const

log = logging.getLogger(__name__)


class DaemonProxy(GObject.Object):
    """Watches the daemon and re-exposes its state as GObject signals.

    Signals:
      state-changed(account: str, status: str)
      sync-completed(account: str, ok: bool, conflicts: int)
      accounts-changed()
      availability-changed(available: bool)
    """

    __gsignals__ = {
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        "sync-completed": (GObject.SignalFlags.RUN_FIRST, None, (str, bool, int)),
        "accounts-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "availability-changed": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
    }

    def __init__(self) -> None:
        super().__init__()
        self._proxy: Gio.DBusProxy | None = None
        # Auto-starts the daemon via D-Bus activation when installed.
        Gio.DBusProxy.new_for_bus(
            Gio.BusType.SESSION,
            Gio.DBusProxyFlags.NONE,
            None,
            const.DBUS_NAME,
            const.DBUS_PATH,
            const.DBUS_INTERFACE,
            None,
            self._on_proxy_ready,
        )

    def _on_proxy_ready(self, _source, result) -> None:
        try:
            self._proxy = Gio.DBusProxy.new_for_bus_finish(result)
        except GLib.Error as e:
            log.warning("cannot create daemon proxy: %s", e.message)
            self.emit("availability-changed", False)
            return
        self._proxy.connect("g-signal", self._on_signal)
        self._proxy.connect("notify::g-name-owner",
                            lambda *_: self.emit("availability-changed", self.available))
        self.emit("availability-changed", self.available)

    def _on_signal(self, _proxy, _sender, signal, params) -> None:
        if signal == "StateChanged":
            account, status = params.unpack()
            self.emit("state-changed", account, status)
        elif signal == "SyncCompleted":
            account, ok, conflicts = params.unpack()
            self.emit("sync-completed", account, ok, conflicts)
        elif signal == "AccountsChanged":
            self.emit("accounts-changed")

    @property
    def available(self) -> bool:
        return self._proxy is not None and self._proxy.get_name_owner() is not None

    # --------------------------------------------------------------- methods

    def _call(self, method: str, params: GLib.Variant | None = None,
              on_done=None, on_error=None) -> None:
        if self._proxy is None:
            if on_error:
                on_error("Servizio non disponibile")
            return

        def done(proxy, result):
            try:
                value = proxy.call_finish(result)
                if on_done:
                    on_done(value)
            except GLib.Error as e:
                log.warning("%s failed: %s", method, e.message)
                if on_error:
                    on_error(e.message)

        self._proxy.call(method, params, Gio.DBusCallFlags.NONE, 10_000, None, done)

    def list_accounts(self, on_done, on_error=None) -> None:
        self._call("ListAccounts",
                   on_done=lambda v: on_done(v.unpack()[0]), on_error=on_error)

    def get_account_info(self, account_id: str, on_done, on_error=None) -> None:
        self._call("GetAccountInfo", GLib.Variant("(s)", (account_id,)),
                   on_done=lambda v: on_done(v.unpack()[0]), on_error=on_error)

    def sync_now(self, account_id: str, **kw) -> None:
        self._call("SyncNow", GLib.Variant("(s)", (account_id,)), **kw)

    def pause(self, account_id: str, **kw) -> None:
        self._call("Pause", GLib.Variant("(s)", (account_id,)), **kw)

    def resume(self, account_id: str, **kw) -> None:
        self._call("Resume", GLib.Variant("(s)", (account_id,)), **kw)

    def resync(self, account_id: str, **kw) -> None:
        self._call("Resync", GLib.Variant("(s)", (account_id,)), **kw)

    def cancel_sync(self, account_id: str, **kw) -> None:
        self._call("CancelSync", GLib.Variant("(s)", (account_id,)), **kw)

    def reload_config(self, **kw) -> None:
        self._call("ReloadConfig", **kw)

    def get_recent_log(self, account_id: str, lines: int, on_done, on_error=None) -> None:
        self._call("GetRecentLog", GLib.Variant("(su)", (account_id, lines)),
                   on_done=lambda v: on_done(v.unpack()[0]), on_error=on_error)
