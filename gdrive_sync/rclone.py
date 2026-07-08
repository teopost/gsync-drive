"""rclone wrapper: version detection, OAuth authorization, remote setup and
bisync execution/parsing.

This module must stay importable without GTK/GLib so it can be unit-tested
and reused by both the daemon and the GUI.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from . import const

log = logging.getLogger(__name__)

RCLONE_BIN = "rclone"


class RcloneNotFoundError(Exception):
    pass


class AuthorizationError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Version detection and feature gating
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class RcloneVersion:
    major: int
    minor: int
    raw: str = ""

    def at_least(self, major: int, minor: int) -> bool:
        return (self.major, self.minor) >= (major, minor)

    @property
    def has_bisync(self) -> bool:
        return self.at_least(1, 58)

    @property
    def has_resilient_flags(self) -> bool:
        """--resilient, --create-empty-src-dirs, --ignore-listing-checksum"""
        return self.at_least(1, 64)

    @property
    def has_recover_flags(self) -> bool:
        """--recover, --max-lock, --conflict-resolve, --conflict-suffix"""
        return self.at_least(1, 66)

    @property
    def is_recommended(self) -> bool:
        return self.at_least(1, 66)


def parse_version(output: str) -> RcloneVersion:
    """Parse the first line of `rclone version`, e.g. 'rclone v1.60.1-DEV'."""
    m = re.search(r"rclone v(\d+)\.(\d+)", output)
    if not m:
        raise ValueError(f"unrecognized rclone version output: {output[:200]!r}")
    return RcloneVersion(int(m.group(1)), int(m.group(2)), output.splitlines()[0].strip())


def detect_version() -> RcloneVersion:
    if shutil.which(RCLONE_BIN) is None:
        raise RcloneNotFoundError("rclone is not installed or not in PATH")
    out = subprocess.run(
        [RCLONE_BIN, "version"], capture_output=True, text=True, timeout=30
    )
    return parse_version(out.stdout)


# --------------------------------------------------------------------------- #
# Authorization and remote configuration
# --------------------------------------------------------------------------- #

def parse_authorize_output(stdout: str) -> str:
    """Extract the OAuth token JSON from `rclone authorize drive` output.

    The token appears between '--->' and '<---' markers; parse defensively by
    accepting the first line that decodes to a dict containing access_token.
    """
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "access_token" in data:
            return json.dumps(data, separators=(",", ":"))
    raise AuthorizationError("no OAuth token found in rclone authorize output")


def start_authorize() -> subprocess.Popen:
    """Start `rclone authorize drive` (opens the browser). Caller reads/kills it."""
    return subprocess.Popen(
        [RCLONE_BIN, "authorize", "drive"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def authorize(timeout: int = const.AUTHORIZE_TIMEOUT) -> str:
    """Run the OAuth flow to completion and return the token JSON (blocking)."""
    proc = start_authorize()
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise AuthorizationError("authorization timed out")
    if proc.returncode != 0:
        raise AuthorizationError(f"rclone authorize failed: {stderr.strip()[:500]}")
    return parse_authorize_output(stdout)


def create_remote(token_json: str, root_folder_id: str = "",
                  name: str = const.REMOTE_NAME) -> None:
    """Create (or overwrite) a dedicated Drive remote with the given token."""
    cmd = [
        RCLONE_BIN, "config", "create", name, "drive",
        "scope=drive",
        f"token={token_json}",
        "--non-interactive",
    ]
    if root_folder_id:
        cmd.insert(-1, f"root_folder_id={root_folder_id}")
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise RuntimeError(f"rclone config create failed: {res.stderr.strip()[:500]}")


def update_remote_root(root_folder_id: str, name: str = const.REMOTE_NAME) -> None:
    """Set (or clear) the Drive folder the remote is rooted at."""
    res = subprocess.run(
        [RCLONE_BIN, "config", "update", name,
         f"root_folder_id={root_folder_id}", "--non-interactive"],
        capture_output=True, text=True, timeout=60,
    )
    if res.returncode != 0:
        raise RuntimeError(f"rclone config update failed: {res.stderr.strip()[:500]}")


@dataclass(frozen=True)
class DriveFolder:
    folder_id: str
    name: str


def parse_lsf_folders(output: str) -> list[DriveFolder]:
    """Parse `rclone lsf --dirs-only --format "ip" --separator ";"` output."""
    folders = []
    for line in output.splitlines():
        if ";" not in line:
            continue
        folder_id, name = line.split(";", 1)
        name = name.rstrip("/")
        if folder_id and name:
            folders.append(DriveFolder(folder_id, name))
    return sorted(folders, key=lambda f: f.name.lower())


def list_folders(path: str = "", remote: str = const.REMOTE) -> list[DriveFolder]:
    """List the sub-folders of a Drive path (blocking; call from a thread)."""
    res = subprocess.run(
        [RCLONE_BIN, "lsf", f"{remote}{path}", "--dirs-only",
         "--format", "ip", "--separator", ";"],
        capture_output=True, text=True, timeout=120,
    )
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip()[:300] or "rclone lsf failed")
    return parse_lsf_folders(res.stdout)


@dataclass(frozen=True)
class FolderSize:
    bytes: int
    count: int


def parse_size_output(output: str) -> FolderSize:
    """Parse `rclone size --json` output: {"count":123,"bytes":4567,...}."""
    data = json.loads(output)
    return FolderSize(int(data.get("bytes", 0)), int(data.get("count", 0)))


def folder_size(path: str = "", remote: str = const.REMOTE) -> FolderSize:
    """Total size of a Drive path (blocking; call from a thread)."""
    res = subprocess.run(
        [RCLONE_BIN, "size", f"{remote}{path}", "--json"],
        capture_output=True, text=True, timeout=300,
    )
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip()[:300] or "rclone size failed")
    return parse_size_output(res.stdout)


def remote_exists(name: str = const.REMOTE_NAME) -> bool:
    res = subprocess.run(
        [RCLONE_BIN, "listremotes"], capture_output=True, text=True, timeout=30
    )
    return f"{name}:" in res.stdout.split()


def delete_remote(name: str = const.REMOTE_NAME) -> None:
    subprocess.run(
        [RCLONE_BIN, "config", "delete", name],
        capture_output=True, text=True, timeout=30,
    )


def check_remote(remote: str = const.REMOTE) -> bool:
    """True if the remote is reachable (cheap listing)."""
    res = subprocess.run(
        [RCLONE_BIN, "lsd", remote, "--max-depth", "1"],
        capture_output=True, text=True, timeout=120,
    )
    return res.returncode == 0


def get_about(remote: str = const.REMOTE) -> dict:
    """Return quota info: {'total': bytes, 'used': bytes, 'free': bytes} (may be partial)."""
    res = subprocess.run(
        [RCLONE_BIN, "about", remote, "--json"],
        capture_output=True, text=True, timeout=120,
    )
    if res.returncode != 0:
        return {}
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return {}


# --------------------------------------------------------------------------- #
# Bisync
# --------------------------------------------------------------------------- #

class Outcome(Enum):
    SUCCESS = "success"
    TRANSIENT = "transient"        # retry with backoff
    NEEDS_RESYNC = "needs_resync"  # bisync aborted; requires user-approved --resync
    LOCKED = "locked"              # prior lock file found
    FATAL = "fatal"                # non-recoverable / usage error
    CANCELLED = "cancelled"        # killed on user request


@dataclass
class SyncResult:
    outcome: Outcome
    exit_code: int
    stderr: str = ""
    log_tail: str = ""
    files_transferred: int = 0
    conflicts_hinted: bool = field(default=False)

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.SUCCESS


# Markers that genuinely require --resync. NB: "Bisync aborted" alone is NOT
# one — rclone prints it on every critical error, including plain network
# failures ("Error is retryable without --resync due to --resilient mode").
_RESYNC_RE = re.compile(r"must run --resync|cannot find prior|empty prior", re.I)
_NETWORK_RE = re.compile(
    r"dial tcp|no such host|server misbehaving|connection re(?:fused|set)|"
    r"i/o timeout|TLS handshake|network is unreachable|temporary failure|"
    r"couldn't fetch token|context deadline exceeded", re.I)
_LOCK_RE = re.compile(r"prior lock file found", re.I)
_CONFLICT_RE = re.compile(r"\.\.path[12]\b|\.conflict\d|NOTICE:.*conflict", re.I)
_TRANSFERRED_RE = re.compile(r"Transferred:\s+(\d+)\s*/\s*\d+", re.M)
_PROGRESS_RE = re.compile(r"Transferred:.*?,\s*(\d+)%", re.M)


def parse_progress(log_text: str) -> float | None:
    """Latest transfer progress (0.0–1.0) from periodic --stats log lines.

    None while no stats block has been written yet (listing phase).
    """
    matches = _PROGRESS_RE.findall(log_text)
    if not matches:
        return None
    return min(int(matches[-1]), 100) / 100.0


def build_bisync_cmd(
    local_dir: str | Path,
    remote: str = const.REMOTE,
    *,
    version: RcloneVersion,
    resync: bool = False,
    dry_run: bool = False,
    bwlimit: str = "",
    max_delete: int = 25,
    filters_file: str | Path = const.FILTERS_FILE,
    workdir: str | Path = const.BISYNC_WORKDIR,
    log_file: str | Path | None = const.SYNC_LOG_FILE,
    stats: str = "",
) -> list[str]:
    """Build the bisync command line, gating flags on the rclone version."""
    cmd = [
        RCLONE_BIN, "bisync", str(local_dir), remote,
        "--filters-file", str(filters_file),
        "--workdir", str(workdir),
        "--log-level", "INFO",
    ]
    if stats:
        cmd += ["--stats", stats, "--stats-log-level", "INFO"]
    if log_file:
        cmd += ["--log-file", str(log_file)]
    if resync:
        cmd.append("--resync")
    else:
        cmd += ["--max-delete", str(max_delete)]
    if dry_run:
        cmd.append("--dry-run")
    if bwlimit:
        cmd += ["--bwlimit", bwlimit]
    if version.has_resilient_flags:
        cmd += ["--resilient", "--create-empty-src-dirs"]
    if version.has_recover_flags:
        cmd += ["--recover", "--max-lock", "5m"]
    return cmd


def classify_result(exit_code: int, stderr: str, log_tail: str) -> Outcome:
    """Map an rclone bisync exit code + output to an Outcome.

    rclone exit codes: 0 ok; 1 usage; 2 uncategorized; 3/4 not found;
    5 temporary; 6 less-serious; 7 fatal; 8 transfer limit; 9 ok-no-transfer.
    In practice bisync critical aborts surface as exit 1 or 7 depending on
    the rclone version, so both get the output pattern checks.
    """
    text = f"{stderr}\n{log_tail}"
    if exit_code in (0, 9):
        return Outcome.SUCCESS
    if _LOCK_RE.search(text):
        # A lock abort is a lock abort whatever the exit code; the engine
        # clears stale locks and retries.
        return Outcome.LOCKED
    if exit_code in (1, 7):
        if _RESYNC_RE.search(text):
            return Outcome.NEEDS_RESYNC
        if _NETWORK_RE.search(text):
            # bisync aborts even on plain connectivity failures;
            # those must be retried, not surfaced as needing a resync.
            return Outcome.TRANSIENT
        return Outcome.FATAL
    # 2..6, 8 and anything unknown: worth retrying
    if _RESYNC_RE.search(text):
        return Outcome.NEEDS_RESYNC
    return Outcome.TRANSIENT


def _read_log_tail(log_file: str | Path | None, max_bytes: int = 65536) -> str:
    if not log_file:
        return ""
    try:
        p = Path(log_file)
        size = p.stat().st_size
        with open(p, "r", errors="replace") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            return f.read()
    except OSError:
        return ""


class BisyncRun:
    """A running bisync subprocess that can be cancelled from another thread."""

    def __init__(self, cmd: list[str]) -> None:
        self.cmd = cmd
        self.cancelled = False
        self._log_file = None
        if "--log-file" in cmd:
            self._log_file = cmd[cmd.index("--log-file") + 1]
            # Truncate so the tail we read belongs to this run only.
            try:
                Path(self._log_file).parent.mkdir(parents=True, exist_ok=True)
                Path(self._log_file).write_text("")
            except OSError:
                pass
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def cancel(self) -> None:
        self.cancelled = True
        try:
            self._proc.kill()
        except OSError:
            pass

    def wait(self, timeout: int = const.SYNC_TIMEOUT) -> SyncResult:
        """Block until the run ends and classify the result (call in a thread)."""
        try:
            _, stderr = self._proc.communicate(timeout=timeout)
            exit_code = self._proc.returncode
        except subprocess.TimeoutExpired:
            self.cancel()
            self._proc.communicate()
            return SyncResult(Outcome.TRANSIENT, -1, stderr="bisync timed out")

        if self.cancelled:
            return SyncResult(Outcome.CANCELLED, exit_code, stderr="cancelled by user")

        log_tail = _read_log_tail(self._log_file)
        outcome = classify_result(exit_code, stderr, log_tail)
        m = _TRANSFERRED_RE.search(log_tail)
        return SyncResult(
            outcome=outcome,
            exit_code=exit_code,
            stderr=stderr[-4000:],
            log_tail=log_tail[-8000:],
            files_transferred=int(m.group(1)) if m else 0,
            conflicts_hinted=bool(_CONFLICT_RE.search(log_tail)),
        )


def run_bisync(cmd: list[str], timeout: int = const.SYNC_TIMEOUT) -> SyncResult:
    """Run a prebuilt bisync command and classify the result (blocking)."""
    return BisyncRun(cmd).wait(timeout)


def clear_stale_lock(workdir: str | Path = const.BISYNC_WORKDIR) -> bool:
    """Remove bisync .lck files if no rclone bisync process is running.

    Returns True if a lock was removed.
    """
    locks = list(Path(workdir).glob("*.lck"))
    if not locks:
        return False
    check = subprocess.run(["pgrep", "-f", "rclone bisync"], capture_output=True)
    if check.returncode == 0:
        log.warning("bisync lock present and an rclone bisync process is alive; not removing")
        return False
    for lck in locks:
        log.info("removing stale bisync lock %s", lck)
        lck.unlink(missing_ok=True)
    return True
