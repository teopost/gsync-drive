"""Multi-account settings (GSettings relocatable schema) and rclone filter files.

Falls back to an in-memory store when the schemas are not installed
(development checkouts, unit tests).
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

from gi.repository import Gio, GLib

from . import const

log = logging.getLogger(__name__)

_ACCOUNT_DEFAULTS = {
    "display-name": "",
    "local-dir": "",
    "sync-interval": const.DEFAULT_SYNC_INTERVAL,
    "bandwidth-limit": "",
    "max-delete": 25,
    "dry-run": False,
    "root-folder-id": "",
    "sidebar-bookmark": True,
    "remote": "",
    "sync-folders": [],
}

_VARIANT_TYPES = {
    "display-name": "s", "local-dir": "s", "sync-interval": "i",
    "bandwidth-limit": "s", "max-delete": "i", "dry-run": "b",
    "root-folder-id": "s", "sidebar-bookmark": "b", "remote": "s",
    "sync-folders": "as",
}


def _lookup_schema(schema_id: str) -> Gio.SettingsSchema | None:
    source = Gio.SettingsSchemaSource.get_default()
    return source.lookup(schema_id, True) if source else None


class AccountConfig:
    """Settings and per-account file paths for one Google account."""

    def __init__(self, account_id: str, settings: Gio.Settings | None) -> None:
        self.id = account_id
        self.settings = settings
        if settings is None:
            self._mem = dict(_ACCOUNT_DEFAULTS)

    def _get(self, key: str):
        if self.settings is not None:
            return self.settings.get_value(key).unpack()
        return self._mem[key]

    def _set(self, key: str, value) -> None:
        if self.settings is not None:
            self.settings.set_value(key, GLib.Variant(_VARIANT_TYPES[key], value))
        else:
            self._mem[key] = value

    def connect_changed(self, callback) -> None:
        """callback(key: str) on any change of this account's settings."""
        if self.settings is not None:
            self.settings.connect("changed", lambda _s, key: callback(key))

    # ------------------------------------------------------------ properties

    @property
    def display_name(self) -> str:
        return str(self._get("display-name")) or self.id

    @display_name.setter
    def display_name(self, v: str) -> None:
        self._set("display-name", v)

    @property
    def local_dir(self) -> Path:
        raw = str(self._get("local-dir"))
        return Path(raw).expanduser() if raw else const.DEFAULT_LOCAL_DIR

    @local_dir.setter
    def local_dir(self, v: str | Path) -> None:
        self._set("local-dir", str(v))

    @property
    def sync_interval(self) -> int:
        return max(60, int(self._get("sync-interval")))

    @property
    def bandwidth_limit(self) -> str:
        return str(self._get("bandwidth-limit"))

    @property
    def max_delete(self) -> int:
        return int(self._get("max-delete"))

    @property
    def dry_run(self) -> bool:
        return bool(self._get("dry-run"))

    @property
    def root_folder_id(self) -> str:
        return str(self._get("root-folder-id"))

    @root_folder_id.setter
    def root_folder_id(self, v: str) -> None:
        self._set("root-folder-id", v)

    @property
    def sidebar_bookmark(self) -> bool:
        return bool(self._get("sidebar-bookmark"))

    @property
    def remote(self) -> str:
        """Sync target ('name:' rclone remote, or a path when testing)."""
        return str(self._get("remote")) or const.default_remote(self.id)

    @remote.setter
    def remote(self, v: str) -> None:
        self._set("remote", v)

    @property
    def remote_name(self) -> str:
        return self.remote.rstrip(":")

    @property
    def sync_folders(self) -> list[str]:
        """Drive folders (paths relative to the Drive root) to synchronize.

        Empty list = the whole Drive.
        """
        return [str(p) for p in self._get("sync-folders")]

    @sync_folders.setter
    def sync_folders(self, paths: list[str]) -> None:
        self._set("sync-folders", minimal_paths(paths))

    # ----------------------------------------------------------------- paths

    @property
    def filters_file(self) -> Path:
        return const.filters_for(self.id)

    @property
    def workdir(self) -> Path:
        return const.workdir_for(self.id)

    @property
    def sync_log_file(self) -> Path:
        return const.sync_log_for(self.id)

    @property
    def status_file(self) -> Path:
        return const.status_file_for(self.id)

    def reset(self) -> None:
        """Clear all keys (used when an account is removed)."""
        if self.settings is not None:
            for key in _ACCOUNT_DEFAULTS:
                self.settings.reset(key)
        else:
            self._mem = dict(_ACCOUNT_DEFAULTS)

    def purge_state(self, include_filters: bool = False) -> None:
        """Delete this account's on-disk state (bisync listings, status, log).

        Account ids are reused (account1, account2, …), so files left behind
        by a removed account — or by a previous installation — would make
        bisync believe it already synchronized and skip --resync.
        """
        shutil.rmtree(self.workdir, ignore_errors=True)
        for p in (self.status_file, self.sync_log_file):
            try:
                p.unlink()
            except OSError:
                pass
        if include_filters:
            try:
                self.filters_file.unlink()
            except OSError:
                pass


class Config:
    """Top-level settings: the account list plus legacy (v0.2) keys."""

    def __init__(self) -> None:
        self._main_schema = _lookup_schema(const.GSCHEMA_ID)
        self._account_schema = _lookup_schema(const.ACCOUNT_GSCHEMA_ID)
        if self._main_schema is not None:
            self.settings: Gio.Settings | None = Gio.Settings.new(const.GSCHEMA_ID)
        else:
            log.warning("GSettings schema %s not installed; using defaults", const.GSCHEMA_ID)
            self.settings = None
            self._mem_accounts: list[str] = []
        self._account_cache: dict[str, AccountConfig] = {}

    # --------------------------------------------------------------- account

    @property
    def account_ids(self) -> list[str]:
        if self.settings is not None:
            return list(self.settings.get_strv("accounts"))
        return list(self._mem_accounts)

    def _set_account_ids(self, ids: list[str]) -> None:
        if self.settings is not None:
            self.settings.set_strv("accounts", ids)
        else:
            self._mem_accounts = ids

    def account(self, account_id: str) -> AccountConfig:
        """AccountConfig for an id (existing or reserved-but-unpublished)."""
        if account_id not in self._account_cache:
            settings = None
            if self._account_schema is not None:
                settings = Gio.Settings.new_with_path(
                    const.ACCOUNT_GSCHEMA_ID,
                    f"{const.ACCOUNT_GSCHEMA_PATH}{account_id}/")
            self._account_cache[account_id] = AccountConfig(account_id, settings)
        return self._account_cache[account_id]

    def accounts(self) -> list[AccountConfig]:
        return [self.account(i) for i in self.account_ids]

    def reserve_account_id(self) -> str:
        """Next free id; not listed until publish_account() is called."""
        existing = set(self.account_ids)
        n = 1
        while f"account{n}" in existing:
            n += 1
        return f"account{n}"

    def publish_account(self, account_id: str) -> None:
        ids = self.account_ids
        if account_id not in ids:
            self._set_account_ids(ids + [account_id])

    def remove_account(self, account_id: str) -> None:
        self._set_account_ids([i for i in self.account_ids if i != account_id])
        account = self.account(account_id)
        account.purge_state(include_filters=True)
        account.reset()
        self._account_cache.pop(account_id, None)

    def connect_accounts_changed(self, callback) -> None:
        """callback() whenever the account list changes (no-op without schema)."""
        if self.settings is not None:
            self.settings.connect("changed::accounts", lambda *_: callback())

    # ------------------------------------------------------------- migration

    def migrate(self) -> None:
        """Import the v0.2 single-account configuration as the first account."""
        if self.settings is None or self.account_ids:
            return
        if not self.settings.get_boolean("setup-done"):
            return
        log.info("migrating v0.2 single-account configuration")
        acc = self.account("account1")
        acc.display_name = "Google Drive"
        acc.local_dir = self.settings.get_string("local-dir") or str(const.DEFAULT_LOCAL_DIR)
        acc.root_folder_id = self.settings.get_string("root-folder-id")
        acc.remote = self.settings.get_string("remote") or const.REMOTE  # keep legacy remote
        acc._set("sync-interval", self.settings.get_int("sync-interval"))
        acc._set("bandwidth-limit", self.settings.get_string("bandwidth-limit"))
        acc._set("max-delete", self.settings.get_int("max-delete"))
        acc._set("sidebar-bookmark", self.settings.get_boolean("sidebar-bookmark"))
        # Reuse the old shared filters/listings so no resync is needed.
        if const.FILTERS_FILE.exists() and not acc.filters_file.exists():
            acc.filters_file.parent.mkdir(parents=True, exist_ok=True)
            acc.filters_file.write_text(const.FILTERS_FILE.read_text())
        if const.BISYNC_WORKDIR.is_dir() and not acc.workdir.exists():
            listings = [p for p in const.BISYNC_WORKDIR.iterdir() if p.is_file()]
            if listings:
                acc.workdir.mkdir(parents=True, exist_ok=True)
                for p in listings:
                    (acc.workdir / p.name).write_bytes(p.read_bytes())
        if const.STATUS_FILE.exists() and not acc.status_file.exists():
            acc.status_file.write_bytes(const.STATUS_FILE.read_bytes())
        self.publish_account("account1")


# --------------------------------------------------------------------------- #
# Filters files (rclone --filters-file syntax)
# --------------------------------------------------------------------------- #

_FILTERS_HEADER = "# Managed by gdrive-sync. User patterns below; internal excludes are appended.\n"

# rclone glob metacharacters that must be escaped when a literal folder path
# is turned into a filter pattern.
_GLOB_SPECIALS = "\\*?[]{}"


def minimal_paths(paths: list[str]) -> list[str]:
    """Normalize a folder selection: strip slashes, drop duplicates and any
    path whose ancestor is also selected (the ancestor already covers it)."""
    cleaned = sorted({p.strip().strip("/") for p in paths if p.strip().strip("/")})
    result: list[str] = []
    for p in cleaned:
        if not any(p.startswith(kept + "/") for kept in result):
            result.append(p)
    return result


def _escape_glob(path: str) -> str:
    return "".join(f"\\{c}" if c in _GLOB_SPECIALS else c for c in path)


def read_user_filters(path: Path) -> list[str]:
    """Return the user-defined exclusion patterns (without the '- ' prefix)."""
    patterns: list[str] = []
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("+ "):
                continue
            if line.startswith("- "):
                line = line[2:]
            if line in const.INTERNAL_EXCLUDES or line == "**":
                continue
            patterns.append(line)
    except FileNotFoundError:
        pass
    return patterns


def write_filters(user_patterns: list[str], path: Path,
                  include_dirs: list[str] | None = None) -> None:
    """Write a filters file: user excludes, the permanent internal ones, then
    (when a folder selection is active) include rules limiting the sync to
    the chosen Drive folders."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [_FILTERS_HEADER]
    lines += [f"- {p}\n" for p in user_patterns if p.strip()]
    lines.append("# Internal (do not edit)\n")
    lines += [f"- {p}\n" for p in const.INTERNAL_EXCLUDES]
    dirs = minimal_paths(include_dirs or [])
    if dirs:
        lines += [f"+ /{_escape_glob(d)}/**\n" for d in dirs]
        lines.append("- **\n")
    path.write_text("".join(lines))


def ensure_filters_file(path: Path) -> None:
    if not path.exists():
        write_filters([], path)
