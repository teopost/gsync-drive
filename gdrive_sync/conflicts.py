"""Detect and resolve bisync conflict files in the local sync folder.

rclone bisync renames conflicting files:
  - 1.58–1.65:  name.ext..path1 (local side), name.ext..path2 (remote side)
  - 1.66+:      name.ext.conflict1, name.ext.conflict2, ... (with default suffix)
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

_OLD_STYLE = re.compile(r"^(?P<base>.+)\.\.path(?P<n>[12])$")
_NEW_STYLE = re.compile(r"^(?P<base>.+)\.conflict(?P<n>\d+)$")


@dataclass
class Conflict:
    base_path: Path            # the original path (may not exist anymore)
    local_variant: Path | None   # ..path1 / .conflict1
    remote_variant: Path | None  # ..path2 / .conflict2+

    @property
    def name(self) -> str:
        return self.base_path.name


def _match(path: Path) -> tuple[Path, int] | None:
    for rx in (_OLD_STYLE, _NEW_STYLE):
        m = rx.match(path.name)
        if m:
            return path.with_name(m.group("base")), int(m.group("n"))
    return None


def scan(local_dir: str | Path) -> list[Conflict]:
    """Scan the sync folder for conflict-suffixed files, grouped by base path."""
    groups: dict[Path, Conflict] = {}
    root = Path(local_dir)
    if not root.is_dir():
        return []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        matched = _match(path)
        if not matched:
            continue
        base, n = matched
        c = groups.setdefault(base, Conflict(base, None, None))
        if n == 1:
            c.local_variant = path
        else:
            c.remote_variant = path
    return sorted(groups.values(), key=lambda c: str(c.base_path))


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(str(path))


def resolve_keep(conflict: Conflict, keep: str) -> None:
    """Resolve a conflict with plain local file operations.

    keep: 'local', 'remote' or 'both'. The next bisync run propagates the result.
    """
    local, remote = conflict.local_variant, conflict.remote_variant
    base = conflict.base_path
    if keep == "local":
        if local:
            base.unlink(missing_ok=True)
            shutil.move(local, base)
        if remote:
            remote.unlink(missing_ok=True)
    elif keep == "remote":
        if remote:
            base.unlink(missing_ok=True)
            shutil.move(remote, base)
        if local:
            local.unlink(missing_ok=True)
    elif keep == "both":
        if local:
            shutil.move(local, _unique_path(
                base.with_name(f"{base.stem} (local){base.suffix}")))
        if remote:
            shutil.move(remote, _unique_path(
                base.with_name(f"{base.stem} (remote){base.suffix}")))
    else:
        raise ValueError(f"keep must be local/remote/both, got {keep!r}")
