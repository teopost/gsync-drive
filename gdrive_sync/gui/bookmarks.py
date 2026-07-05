"""GNOME Files (Nautilus) integration: sidebar bookmark and folder icon."""

from __future__ import annotations

import logging
from pathlib import Path

from gi.repository import Gio, GLib

from .. import const

log = logging.getLogger(__name__)

_LABEL = "Google Drive"


def _bookmark_line(folder: Path) -> str:
    uri = GLib.filename_to_uri(str(folder), None)
    return f"{uri} {_LABEL}"


def add_bookmark(folder: Path) -> None:
    """Idempotently add the sync folder to the GTK bookmarks (Files sidebar)."""
    bm = const.GTK_BOOKMARKS_FILE
    bm.parent.mkdir(parents=True, exist_ok=True)
    uri = GLib.filename_to_uri(str(folder), None)
    lines = bm.read_text().splitlines() if bm.exists() else []
    if any(line.split(" ")[0] == uri for line in lines):
        return
    lines.append(_bookmark_line(folder))
    bm.write_text("\n".join(lines) + "\n")


def remove_bookmark(folder: Path) -> None:
    bm = const.GTK_BOOKMARKS_FILE
    if not bm.exists():
        return
    uri = GLib.filename_to_uri(str(folder), None)
    lines = [l for l in bm.read_text().splitlines() if l.split(" ")[0] != uri]
    bm.write_text("\n".join(lines) + ("\n" if lines else ""))


def set_folder_icon(folder: Path) -> None:
    """Give the sync folder the app icon in Files (gvfs metadata)."""
    try:
        f = Gio.File.new_for_path(str(folder))
        info = Gio.FileInfo.new()
        info.set_attribute_string("metadata::custom-icon-name", const.APP_ID)
        f.set_attributes_from_info(info, Gio.FileQueryInfoFlags.NONE, None)
    except GLib.Error as e:
        log.debug("cannot set folder icon: %s", e.message)
