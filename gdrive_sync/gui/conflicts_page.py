"""Conflict review dialog: keep local / keep remote / keep both."""

from __future__ import annotations

import logging

from gi.repository import Adw, Gtk

from .. import conflicts
from ..config import AccountConfig
from ..i18n import _
from .daemon_proxy import DaemonProxy

log = logging.getLogger(__name__)


class ConflictsDialog(Adw.Dialog):
    def __init__(self, account: AccountConfig, proxy: DaemonProxy) -> None:
        super().__init__(title=_("Conflicts — {account}").format(account=account.display_name),
                         content_width=640, content_height=480)
        self.account = account
        self.proxy = proxy

        self.status = Adw.StatusPage(
            icon_name="object-select-symbolic",
            title=_("No conflicts"),
            description=_("All files are aligned."),
            vexpand=True,
        )
        self.group = Adw.PreferencesGroup(
            margin_start=16, margin_end=16, margin_top=16, margin_bottom=16,
            description=_("These files were modified both locally and on Drive. "
                          "Choose which version to keep; the choice will be applied "
                          "at the next synchronization."))
        scroll = Gtk.ScrolledWindow(child=self.group, vexpand=True)

        self.stack = Gtk.Stack()
        self.stack.add_named(self.status, "empty")
        self.stack.add_named(scroll, "list")

        tb = Adw.ToolbarView(content=self.stack)
        tb.add_top_bar(Adw.HeaderBar())
        self.set_child(tb)

        self._rows: list[Adw.ActionRow] = []
        self._reload()

    def _reload(self) -> None:
        for row in self._rows:
            self.group.remove(row)
        self._rows.clear()

        found = conflicts.scan(self.account.local_dir)
        self.stack.set_visible_child_name("list" if found else "empty")

        for c in found:
            row = Adw.ActionRow(
                title=c.name,
                subtitle=str(c.base_path.parent.relative_to(self.account.local_dir))
                if c.base_path.is_relative_to(self.account.local_dir) else "",
            )
            for label, keep in ((_("Local"), "local"), (_("Drive"), "remote"), (_("Both"), "both")):
                btn = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
                btn.add_css_class("flat")
                btn.connect("clicked", self._on_resolve, c, keep)
                row.add_suffix(btn)
            self.group.add(row)
            self._rows.append(row)

    def _on_resolve(self, _btn, conflict: conflicts.Conflict, keep: str) -> None:
        try:
            conflicts.resolve_keep(conflict, keep)
        except OSError as e:
            log.warning("cannot resolve %s: %s", conflict.name, e)
        self._reload()
        self.proxy.sync_now(self.account.id)
