#!/usr/bin/env python3
"""Minimal .po → .mo compiler (used at build time when msgfmt is missing).

Usage: compile-mo.py input.po output.mo
Supports plain entries and plural entries; no msgctxt, no fuzzy handling.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path


def _unescape(s: str) -> str:
    return (s.replace("\\\\", "\x00")
             .replace('\\"', '"')
             .replace("\\n", "\n")
             .replace("\\t", "\t")
             .replace("\x00", "\\"))


def parse_po(text: str) -> dict[bytes, bytes]:
    entries: dict[bytes, bytes] = {}
    msgid: list[str] = []
    msgid_plural: list[str] = []
    msgstrs: dict[int, list[str]] = {}
    current: list[str] | None = None

    def flush() -> None:
        nonlocal msgid, msgid_plural, msgstrs, current
        if msgid or msgstrs:
            key = "".join(msgid)
            if msgid_plural:
                key = key + "\x00" + "".join(msgid_plural)
                val = "\x00".join("".join(msgstrs[i]) for i in sorted(msgstrs))
            else:
                val = "".join(msgstrs.get(0, []))
            if val:  # untranslated entries are omitted (gettext falls back)
                entries[key.encode()] = val.encode()
        msgid, msgid_plural, msgstrs, current = [], [], {}, None

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("msgid_plural "):
            current = msgid_plural
            line = line[len("msgid_plural "):]
        elif line.startswith("msgid "):
            flush()
            current = msgid
            line = line[len("msgid "):]
        elif line.startswith("msgstr["):
            idx = int(line[7:line.index("]")])
            msgstrs[idx] = []
            current = msgstrs[idx]
            line = line[line.index("]") + 2:]
        elif line.startswith("msgstr "):
            msgstrs[0] = []
            current = msgstrs[0]
            line = line[len("msgstr "):]
        if current is None or not line.startswith('"'):
            continue
        current.append(_unescape(line[1:-1]))
    flush()
    return entries


def write_mo(entries: dict[bytes, bytes], path: Path) -> None:
    keys = sorted(entries)
    offsets = []
    ids = b""
    strs = b""
    for k in keys:
        v = entries[k]
        offsets.append((len(ids), len(k), len(strs), len(v)))
        ids += k + b"\x00"
        strs += v + b"\x00"
    n = len(keys)
    keystart = 7 * 4 + 16 * n
    valuestart = keystart + len(ids)
    koffsets = []
    voffsets = []
    for o1, l1, o2, l2 in offsets:
        koffsets += [l1, o1 + keystart]
        voffsets += [l2, o2 + valuestart]
    output = struct.pack("Iiiiiii", 0x950412DE, 0, n, 7 * 4, 7 * 4 + n * 8, 0, 0)
    output += struct.pack(f"{len(koffsets)}i", *koffsets)
    output += struct.pack(f"{len(voffsets)}i", *voffsets)
    output += ids + strs
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(output)


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    write_mo(parse_po(src.read_text()), dst)
    return 0


if __name__ == "__main__":
    sys.exit(main())
