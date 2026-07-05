"""Desktop notifications sent by the daemon via Gio.Notification."""

from __future__ import annotations

import logging

from gi.repository import Gio

from .. import const
from ..i18n import _

log = logging.getLogger(__name__)

_URGENT = {"needs-resync", "error"}


def send(app: Gio.Application, kind: str, title: str, body: str,
         account_id: str = "") -> None:
    n = Gio.Notification.new(title)
    n.set_body(body)
    n.set_icon(Gio.ThemedIcon.new(const.APP_ID))
    if kind in _URGENT:
        n.set_priority(Gio.NotificationPriority.URGENT)
    if kind == "conflicts":
        n.add_button(_("Review"), "app.open-gui")
    n.set_default_action("app.open-gui")
    app.send_notification(f"gdrive-sync-{account_id}-{kind}", n)


def launch_gui() -> None:
    """Open the GTK app (used by notification actions)."""
    try:
        info = Gio.DesktopAppInfo.new(f"{const.APP_ID}.desktop")
        if info is not None:
            info.launch([], None)
            return
    except Exception:
        pass
    # Development fallback when the desktop file is not installed
    try:
        Gio.Subprocess.new(["gdrive-sync"], Gio.SubprocessFlags.NONE)
    except Exception:
        log.warning("could not launch the GUI")
