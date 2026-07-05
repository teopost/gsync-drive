"""Application-wide constants: identifiers, paths, defaults."""

import os
from pathlib import Path

APP_ID = "io.github.teopost.GDriveSync"
DAEMON_APP_ID = "io.github.teopost.GDriveSync.Daemon"

DBUS_NAME = DAEMON_APP_ID
DBUS_PATH = "/io/github/teopost/GDriveSync/Daemon"
DBUS_INTERFACE = DAEMON_APP_ID

GSCHEMA_ID = APP_ID
ACCOUNT_GSCHEMA_ID = f"{APP_ID}.Account"
ACCOUNT_GSCHEMA_PATH = "/io/github/teopost/GDriveSync/accounts/"

# Legacy single-account remote (v0.1/0.2); migrated accounts keep using it.
REMOTE_NAME = "gdrive-sync"
REMOTE = f"{REMOTE_NAME}:"


def default_remote(account_id: str) -> str:
    return f"gdrive-sync-{account_id}:"

_XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
_XDG_STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))

CONFIG_DIR = _XDG_CONFIG / "gdrive-sync"
STATE_DIR = _XDG_STATE / "gdrive-sync"
BISYNC_WORKDIR = STATE_DIR / "bisync"
SYNC_LOG_FILE = STATE_DIR / "last-sync.log"
DAEMON_LOG_FILE = STATE_DIR / "daemon.log"
FILTERS_FILE = CONFIG_DIR / "filters.txt"
STATUS_FILE = STATE_DIR / "status.json"


def workdir_for(account_id: str) -> Path:
    return BISYNC_WORKDIR / account_id


def sync_log_for(account_id: str) -> Path:
    return STATE_DIR / f"last-sync-{account_id}.log"


def filters_for(account_id: str) -> Path:
    return CONFIG_DIR / f"filters-{account_id}.txt"


def status_file_for(account_id: str) -> Path:
    return STATE_DIR / f"status-{account_id}.json"

DEFAULT_LOCAL_DIR = Path.home() / "GoogleDrive"

DEFAULT_SYNC_INTERVAL = 300  # seconds
DEBOUNCE_SECONDS = 10
AUTHORIZE_TIMEOUT = 300  # seconds to complete the browser OAuth flow
SYNC_TIMEOUT = 6 * 3600  # hard cap for a single bisync run

# Retry backoff for transient errors, in seconds; last value repeats.
RETRY_BACKOFF = [60, 300, 900, 3600]

# Patterns the sync must always ignore, regardless of user filters.
# rclone filter syntax (leading "- " added when writing filters.txt).
INTERNAL_EXCLUDES = [
    "*..path1",
    "*..path2",
    "*.conflict[0-9]*",
    "*.partial",
    "*.tmp",
    "*~",
    ".~lock.*#",
    ".#*",
    "**/.Trash-*/**",
    "RCLONE_TEST",
]

SYSTEMD_UNIT = "gdrive-sync-daemon.service"

GTK_BOOKMARKS_FILE = _XDG_CONFIG / "gtk-3.0" / "bookmarks"


def ensure_dirs() -> None:
    """Create the config/state directories this app writes to."""
    for d in (CONFIG_DIR, STATE_DIR, BISYNC_WORKDIR):
        d.mkdir(parents=True, exist_ok=True)
