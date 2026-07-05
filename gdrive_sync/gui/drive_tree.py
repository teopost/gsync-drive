"""Checkbox tree of Google Drive folders, with per-folder sizes.

Used by the setup wizard to pick which folders to synchronize. Children are
listed lazily when a row is expanded; sizes are computed in background
threads (a few at a time) via `rclone size`.
"""

from __future__ import annotations

import logging
import threading

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from .. import rclone
from ..config import minimal_paths
from ..i18n import _

log = logging.getLogger(__name__)

_SIZE_WORKERS = 3  # concurrent `rclone size` queries


def _status_page(icon: str, title: str, description: str = "") -> Adw.StatusPage:
    return Adw.StatusPage(icon_name=icon, title=title,
                          description=description, vexpand=True)


def format_size(n: int) -> str:
    """Human-readable size, e.g. 1.4 GB (SI units, one decimal)."""
    value = float(n)
    for unit in ("B", "kB", "MB", "GB", "TB", "PB"):
        if value < 1000 or unit == "PB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1000
    return f"{int(n)} B"


class FolderItem(GObject.Object):
    """One Drive folder in the tree."""

    __gtype_name__ = "GdsFolderItem"

    name = GObject.Property(type=str, default="")
    size_label = GObject.Property(type=str, default="")
    selected = GObject.Property(type=bool, default=False)
    # True when an ancestor is selected: shown checked and not editable.
    inherited = GObject.Property(type=bool, default=False)

    def __init__(self, name: str, path: str, parent: "FolderItem | None") -> None:
        super().__init__()
        self.name = name
        self.path = path  # Drive path relative to the root, no leading slash
        self.parent = parent
        self.children_store: Gio.ListStore | None = None
        self.size_pending = False


class DriveFolderTree(Gtk.Box):
    """Expandable folder tree with a checkbox and a size per row.

    on_selection_changed() is invoked after every check/uncheck.
    """

    def __init__(self, remote: str, on_selection_changed=None,
                 initial_selection: list[str] | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.remote = remote
        self.on_selection_changed = on_selection_changed
        self._size_sem = threading.Semaphore(_SIZE_WORKERS)
        # Paths selected before the tree is (fully) loaded. Consumed when the
        # matching item materializes; the rest still counts as selected, so a
        # nested saved selection survives even if never expanded.
        self._pending = set(minimal_paths(initial_selection or []))

        self._root_store = Gio.ListStore(item_type=FolderItem)
        self._tree = Gtk.TreeListModel.new(
            self._root_store, False, False, self._create_children)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._row_setup)
        factory.connect("bind", self._row_bind)
        factory.connect("unbind", self._row_unbind)
        self._list = Gtk.ListView(
            model=Gtk.NoSelection(model=self._tree), factory=factory)
        self._list.add_css_class("card")
        scroll = Gtk.ScrolledWindow(child=self._list, vexpand=True,
                                    min_content_height=260)

        self._spinner = Gtk.Spinner(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                                    width_request=32, height_request=32, vexpand=True)
        self._empty = _status_page(icon="folder-symbolic", title=_("No folders"),
                                 description=_("Your Drive has no folders; the whole "
                                               "Drive will be synchronized."))
        self._error = _status_page(icon="dialog-warning-symbolic",
                                 title=_("Cannot list Drive folders"))
        retry = Gtk.Button(label=_("Retry"), halign=Gtk.Align.CENTER)
        retry.add_css_class("pill")
        retry.connect("clicked", lambda *_a: self.load())
        self._error.set_child(retry)

        self._stack = Gtk.Stack(vexpand=True)
        self._stack.add_named(self._spinner, "loading")
        self._stack.add_named(scroll, "list")
        self._stack.add_named(self._empty, "empty")
        self._stack.add_named(self._error, "error")
        self.append(self._stack)

    # ------------------------------------------------------------ loading

    def load(self) -> None:
        """(Re)load the top-level Drive folders."""
        self._stack.set_visible_child_name("loading")
        self._spinner.start()

        def worker() -> None:
            try:
                folders = rclone.list_folders("", self.remote)
                GLib.idle_add(self._show_root, folders, None)
            except Exception as e:
                GLib.idle_add(self._show_root, [], str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _show_root(self, folders: list[rclone.DriveFolder], error: str | None) -> bool:
        self._spinner.stop()
        if error is not None:
            self._error.set_description(error[:300])
            self._stack.set_visible_child_name("error")
            return GLib.SOURCE_REMOVE
        self._root_store.remove_all()
        for f in folders:
            item = FolderItem(f.name, f.name, None)
            self._adopt_pending(item)
            self._root_store.append(item)
        self._stack.set_visible_child_name("list" if folders else "empty")
        return GLib.SOURCE_REMOVE

    def _adopt_pending(self, item: FolderItem) -> None:
        if item.path in self._pending:
            self._pending.discard(item.path)
            item.selected = True

    def _create_children(self, item: FolderItem) -> Gio.ListStore:
        """TreeListModel child factory: fill the store asynchronously."""
        store = Gio.ListStore(item_type=FolderItem)
        item.children_store = store

        def worker() -> None:
            try:
                folders = rclone.list_folders(item.path, self.remote)
            except Exception as e:
                log.warning("cannot list %r: %s", item.path, e)
                folders = []
            GLib.idle_add(fill, folders)

        def fill(folders: list[rclone.DriveFolder]) -> bool:
            for f in folders:
                child = FolderItem(f.name, f"{item.path}/{f.name}", item)
                child.inherited = item.inherited or item.selected
                self._adopt_pending(child)
                store.append(child)
            return GLib.SOURCE_REMOVE

        threading.Thread(target=worker, daemon=True).start()
        return store

    # -------------------------------------------------------------- sizes

    def _request_size(self, item: FolderItem) -> None:
        if item.size_pending or item.size_label:
            return
        item.size_pending = True

        def worker() -> None:
            with self._size_sem:
                try:
                    size = rclone.folder_size(item.path, self.remote)
                    label = format_size(size.bytes)
                except Exception as e:
                    log.debug("size of %r failed: %s", item.path, e)
                    label = "—"
            GLib.idle_add(item.set_property, "size-label", label)

        threading.Thread(target=worker, daemon=True).start()

    # ---------------------------------------------------------------- rows

    def _row_setup(self, _factory, list_item: Gtk.ListItem) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                      margin_top=4, margin_bottom=4, margin_start=4, margin_end=8)
        expander = Gtk.TreeExpander()
        check = Gtk.CheckButton()
        icon = Gtk.Image(icon_name="folder-symbolic")
        name = Gtk.Label(xalign=0, hexpand=True, ellipsize=3)  # ELLIPSIZE_END
        size = Gtk.Label(xalign=1)
        size.add_css_class("dim-label")
        size.add_css_class("numeric")
        for w in (expander, check, icon, name, size):
            box.append(w)
        list_item.set_child(box)
        # stash references for bind/unbind
        list_item._expander, list_item._check = expander, check
        list_item._name, list_item._size = name, size
        list_item._toggled_id = check.connect("toggled", self._on_toggled, list_item)
        list_item._notify_ids = []

    def _row_bind(self, _factory, list_item: Gtk.ListItem) -> None:
        row: Gtk.TreeListRow = list_item.get_item()
        item: FolderItem = row.get_item()
        list_item._item = item
        list_item._expander.set_list_row(row)
        list_item._name.set_label(item.name)
        self._sync_check(list_item)
        self._sync_size(list_item)
        list_item._notify_ids = [
            item.connect("notify::selected", lambda *_a: self._sync_check(list_item)),
            item.connect("notify::inherited", lambda *_a: self._sync_check(list_item)),
            item.connect("notify::size-label", lambda *_a: self._sync_size(list_item)),
        ]
        self._request_size(item)

    def _row_unbind(self, _factory, list_item: Gtk.ListItem) -> None:
        item = getattr(list_item, "_item", None)
        if item is not None:
            for hid in list_item._notify_ids:
                item.disconnect(hid)
            list_item._notify_ids = []
            list_item._item = None

    def _sync_check(self, list_item) -> None:
        item: FolderItem = list_item._item
        check: Gtk.CheckButton = list_item._check
        check.handler_block(list_item._toggled_id)
        check.set_active(item.selected or item.inherited)
        check.set_sensitive(not item.inherited)
        check.handler_unblock(list_item._toggled_id)

    def _sync_size(self, list_item) -> None:
        item: FolderItem = list_item._item
        list_item._size.set_label(item.size_label or "…")

    def _on_toggled(self, check: Gtk.CheckButton, list_item) -> None:
        item: FolderItem = getattr(list_item, "_item", None)
        if item is None or item.inherited:
            return
        item.selected = check.get_active()
        self._propagate_inherited(item)
        if self.on_selection_changed is not None:
            self.on_selection_changed()

    def _propagate_inherited(self, item: FolderItem) -> None:
        covered = item.selected or item.inherited
        if item.children_store is None:
            return
        for child in item.children_store:
            if child.inherited != covered:
                child.inherited = covered
            self._propagate_inherited(child)

    # ------------------------------------------------------------ selection

    def selected_paths(self) -> list[str]:
        """Checked folder paths, minus those already covered by an ancestor."""
        paths: list[str] = []

        def walk(store: Gio.ListStore) -> None:
            for item in store:
                if item.selected:
                    paths.append(item.path)
                if item.children_store is not None:
                    walk(item.children_store)

        walk(self._root_store)
        return minimal_paths(paths + list(self._pending))
