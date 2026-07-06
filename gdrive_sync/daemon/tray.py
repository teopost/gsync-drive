"""System tray icon (StatusNotifierItem) exported by the daemon.

Implements org.kde.StatusNotifierItem and com.canonical.dbusmenu over plain
Gio D-Bus — no GTK involved. Works natively on KDE Plasma; on GNOME it
requires the "AppIndicator and KStatusNotifierItem Support" extension.

The item owns its own bus name (org.kde.StatusNotifierItem-<pid>-1, as the
SNI spec suggests), so releasing the name is enough to make every host drop
the icon when the user disables it.
"""

from __future__ import annotations

import logging
import os

from gi.repository import Gio, GLib

from .. import const
from ..i18n import _
from . import notify

log = logging.getLogger(__name__)

WATCHER_NAME = "org.kde.StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/StatusNotifierMenu"

_SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="Activate">
      <arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/>
    </method>
    <method name="ContextMenu">
      <arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/>
    </method>
    <method name="Scroll">
      <arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/>
    </method>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
    <signal name="NewToolTip"/>
  </interface>
</node>
"""

_MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg name="parentId" type="i" direction="in"/>
      <arg name="recursionDepth" type="i" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="revision" type="u" direction="out"/>
      <arg name="layout" type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="properties" type="a(ia{sv})" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg name="id" type="i" direction="in"/>
      <arg name="name" type="s" direction="in"/>
      <arg name="value" type="v" direction="out"/>
    </method>
    <method name="Event">
      <arg name="id" type="i" direction="in"/>
      <arg name="eventId" type="s" direction="in"/>
      <arg name="data" type="v" direction="in"/>
      <arg name="timestamp" type="u" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg name="events" type="a(isvu)" direction="in"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg name="id" type="i" direction="in"/>
      <arg name="needUpdate" type="b" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="updatesNeeded" type="ai" direction="out"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg name="updatedProps" type="a(ia{sv})"/>
      <arg name="removedProps" type="a(ias)"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg name="revision" type="u"/><arg name="parent" type="i"/>
    </signal>
    <signal name="ItemActivationRequested">
      <arg name="id" type="i"/><arg name="timestamp" type="u"/>
    </signal>
  </interface>
</node>
"""

# Fixed menu item ids
_ID_SYNC_NOW = 1
_ID_PAUSE = 2
_ID_OPEN = 3
_ID_SEP_A = 4
_ID_SEP_B = 5
_ID_ACCOUNT_BASE = 100  # + index: informative per-account rows


# Aggregated tray state -> themed icon: the icon itself shows what the
# daemon is doing (sync spinner, pause, offline, warning).
_STATE_ICONS = {
    "error": "dialog-warning-symbolic",
    "syncing": f"{const.APP_ID}-syncing-0-symbolic",
    "paused": "media-playback-pause-symbolic",
    "offline": "network-offline-symbolic",
    "idle": f"{const.APP_ID}-symbolic",
}

# While syncing, the arrows spin: SNI has no animation support, so we cycle
# through pre-rotated icon frames and emit NewIcon (the Syncthing approach).
_SYNC_FRAMES = 18   # 10° steps; 180° covers a full cycle by symmetry
_FRAME_INTERVAL_MS = 100


def aggregate_state(states: list[str]) -> str:
    """Collapse per-account engine states into one tray state, worst first."""
    states = set(states)
    if states & {"error", "needs_resync"}:
        return "error"
    if states & {"syncing", "resyncing"}:
        return "syncing"
    if states and states <= {"paused"}:
        return "paused"
    if states and states <= {"offline", "paused"}:
        return "offline"
    return "idle"


def _status_label(status: str) -> str:
    return {
        "idle": _("Up to date"),
        "syncing": _("Synchronizing…"),
        "resyncing": _("Realigning…"),
        "paused": _("Paused"),
        "offline": _("Offline"),
        "error": _("Error"),
        "needs_resync": _("Needs repair"),
        "unconfigured": _("Not configured"),
    }.get(status, status)


class TrayIcon:
    """One SNI item summarizing all accounts, with a small action menu."""

    def __init__(self, manager, connection: Gio.DBusConnection) -> None:
        self.manager = manager
        self.connection = connection
        self._revision = 1
        self._registrations: list[int] = []
        self._bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self._own_id = 0
        self._watch_id = 0
        self._enabled = False
        self._frame = 0
        self._anim_timer = 0

    # ------------------------------------------------------------- lifecycle

    def enable(self) -> None:
        if self._enabled:
            return
        self._enabled = True
        sni = Gio.DBusNodeInfo.new_for_xml(_SNI_XML).interfaces[0]
        menu = Gio.DBusNodeInfo.new_for_xml(_MENU_XML).interfaces[0]
        self._registrations = [
            self.connection.register_object(
                ITEM_PATH, sni, self._on_sni_call, self._get_sni_property, None),
            self.connection.register_object(
                MENU_PATH, menu, self._on_menu_call, self._get_menu_property, None),
        ]
        self._own_id = Gio.bus_own_name_on_connection(
            self.connection, self._bus_name, Gio.BusNameOwnerFlags.NONE,
            None, None)
        # (Re-)register whenever a watcher is available: covers shell restarts
        # and the GNOME extension being toggled on later.
        self._watch_id = Gio.bus_watch_name_on_connection(
            self.connection, WATCHER_NAME, Gio.BusNameWatcherFlags.NONE,
            lambda *_a: self._register_with_watcher(), None)
        log.info("tray icon enabled (%s)", self._bus_name)

    def disable(self) -> None:
        if not self._enabled:
            return
        self._enabled = False
        if self._watch_id:
            Gio.bus_unwatch_name(self._watch_id)
            self._watch_id = 0
        if self._own_id:
            Gio.bus_unown_name(self._own_id)  # hosts drop the item with the name
            self._own_id = 0
        for reg in self._registrations:
            self.connection.unregister_object(reg)
        self._registrations = []
        self._stop_animation()
        log.info("tray icon disabled")

    def _register_with_watcher(self) -> None:
        self.connection.call(
            WATCHER_NAME, "/StatusNotifierWatcher", WATCHER_NAME,
            "RegisterStatusNotifierItem",
            GLib.Variant("(s)", (self._bus_name,)),
            None, Gio.DBusCallFlags.NONE, 5000, None,
            lambda conn, res: self._on_registered(conn, res))

    def _on_registered(self, conn, res) -> None:
        try:
            conn.call_finish(res)
            log.info("registered with StatusNotifierWatcher")
        except GLib.Error as e:
            log.warning("tray registration failed: %s", e.message)

    # --------------------------------------------------------------- updates

    def refresh(self) -> None:
        """Recompute status/tooltip/menu after any account state change."""
        if not self._enabled:
            return
        if self._aggregate() == "syncing":
            self._start_animation()
        else:
            self._stop_animation()
        self._revision += 1
        self._emit(ITEM_PATH, "org.kde.StatusNotifierItem", "NewIcon", None)
        self._emit(ITEM_PATH, "org.kde.StatusNotifierItem", "NewStatus",
                   GLib.Variant("(s)", (self._overall_status(),)))
        self._emit(ITEM_PATH, "org.kde.StatusNotifierItem", "NewToolTip", None)
        self._emit(MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated",
                   GLib.Variant("(ui)", (self._revision, 0)))

    # ------------------------------------------------------------ animation

    def _start_animation(self) -> None:
        if not self._anim_timer:
            self._anim_timer = GLib.timeout_add(_FRAME_INTERVAL_MS, self._on_frame)

    def _stop_animation(self) -> None:
        if self._anim_timer:
            GLib.source_remove(self._anim_timer)
            self._anim_timer = 0
            self._frame = 0

    def _on_frame(self) -> bool:
        self._frame = (self._frame + 1) % _SYNC_FRAMES
        self._emit(ITEM_PATH, "org.kde.StatusNotifierItem", "NewIcon", None)
        return GLib.SOURCE_CONTINUE

    def _emit(self, path: str, iface: str, signal: str, params) -> None:
        try:
            self.connection.emit_signal(None, path, iface, signal, params)
        except GLib.Error as e:
            log.debug("tray signal %s failed: %s", signal, e.message)

    # ----------------------------------------------------------- SNI object

    def _aggregate(self) -> str:
        return aggregate_state([e.state.value for e in self.manager.engines.values()])

    def _overall_status(self) -> str:
        return "NeedsAttention" if self._aggregate() == "error" else "Active"

    def _tooltip_text(self) -> str:
        lines = [f"{e.account.display_name}: {_status_label(e.state.value)}"
                 for e in self.manager.engines.values()]
        return "\n".join(lines) or _("No account configured")

    def _get_sni_property(self, _conn, _sender, _path, _iface, name):
        if name == "Category":
            return GLib.Variant("s", "ApplicationStatus")
        if name == "Id":
            return GLib.Variant("s", const.APP_ID)
        if name == "Title":
            return GLib.Variant("s", "GDrive Sync")
        if name == "Status":
            return GLib.Variant("s", self._overall_status())
        if name == "IconName":
            state = self._aggregate()
            if state == "syncing":
                return GLib.Variant(
                    "s", f"{const.APP_ID}-syncing-{self._frame}-symbolic")
            return GLib.Variant("s", _STATE_ICONS[state])
        if name == "AttentionIconName":
            return GLib.Variant("s", _STATE_ICONS["error"])
        if name == "ToolTip":
            return GLib.Variant("(sa(iiay)ss)",
                                ("", [], "GDrive Sync", self._tooltip_text()))
        if name == "ItemIsMenu":
            return GLib.Variant("b", True)
        if name == "Menu":
            return GLib.Variant("o", MENU_PATH)
        return None

    def _on_sni_call(self, _conn, _sender, _path, _iface, method, _params, invocation):
        # Every click variant shows the menu on SNI hosts (ItemIsMenu);
        # Activate is only reached on hosts that bypass it.
        if method in ("Activate", "SecondaryActivate"):
            notify.launch_gui()
        invocation.return_value(None)

    # ---------------------------------------------------------- menu object

    def _any_paused(self) -> bool:
        return any(e.paused for e in self.manager.engines.values())

    def _menu_items(self) -> list[tuple[int, dict]]:
        """Flat list of (id, dbusmenu properties)."""
        items: list[tuple[int, dict]] = []
        for i, engine in enumerate(self.manager.engines.values()):
            label = f"{engine.account.display_name} — {_status_label(engine.state.value)}"
            items.append((_ID_ACCOUNT_BASE + i,
                          {"label": GLib.Variant("s", label),
                           "enabled": GLib.Variant("b", False)}))
        if items:
            items.append((_ID_SEP_A, {"type": GLib.Variant("s", "separator")}))
        items.append((_ID_SYNC_NOW,
                      {"label": GLib.Variant("s", _("Synchronize now")),
                       "enabled": GLib.Variant("b", bool(self.manager.engines))}))
        items.append((_ID_PAUSE,
                      {"label": GLib.Variant(
                          "s", _("Resume synchronization") if self._any_paused()
                          else _("Pause synchronization")),
                       "enabled": GLib.Variant("b", bool(self.manager.engines))}))
        items.append((_ID_SEP_B, {"type": GLib.Variant("s", "separator")}))
        items.append((_ID_OPEN,
                      {"label": GLib.Variant("s", _("Open GDrive Sync"))}))
        return items

    def _layout_variant(self):
        children = [
            GLib.Variant("v", GLib.Variant("(ia{sv}av)", (item_id, props, [])))
            for item_id, props in self._menu_items()
        ]
        root_props = {"children-display": GLib.Variant("s", "submenu")}
        return GLib.Variant("(u(ia{sv}av))",
                            (self._revision, (0, root_props, children)))

    def _get_menu_property(self, _conn, _sender, _path, _iface, name):
        if name == "Version":
            return GLib.Variant("u", 3)
        if name == "Status":
            return GLib.Variant("s", "normal")
        if name == "TextDirection":
            return GLib.Variant("s", "ltr")
        if name == "IconThemePath":
            return GLib.Variant("as", [])
        return None

    def _on_menu_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        if method == "GetLayout":
            invocation.return_value(self._layout_variant())
        elif method == "GetGroupProperties":
            ids = set(params.unpack()[0])
            props = [(i, p) for i, p in self._menu_items() if not ids or i in ids]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (props,)))
        elif method == "GetProperty":
            item_id, name = params.unpack()
            value = dict(self._menu_items()).get(item_id, {}).get(
                name, GLib.Variant("s", ""))
            invocation.return_value(GLib.Variant("(v)", (value,)))
        elif method == "Event":
            item_id, event_id, _data, _ts = params.unpack()
            if event_id == "clicked":
                self._on_item_clicked(item_id)
            invocation.return_value(None)
        elif method == "EventGroup":
            for item_id, event_id, _data, _ts in params.unpack()[0]:
                if event_id == "clicked":
                    self._on_item_clicked(item_id)
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (True,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_error_literal(
                Gio.dbus_error_quark(), Gio.DBusError.UNKNOWN_METHOD, method)

    def _on_item_clicked(self, item_id: int) -> None:
        if item_id == _ID_OPEN:
            notify.launch_gui()
        elif item_id == _ID_SYNC_NOW:
            for engine in self.manager.engines.values():
                engine.request_sync("tray")
        elif item_id == _ID_PAUSE:
            resume = self._any_paused()
            for engine in self.manager.engines.values():
                engine.resume() if resume else engine.pause()
            self.refresh()
