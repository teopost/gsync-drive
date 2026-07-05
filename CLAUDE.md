# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

GDrive Sync: bidirectional Google Drive synchronization for GNOME, based on
rclone bisync. GTK4/libadwaita app + systemd user daemon, pure Python
(PyGObject), packaged as .deb (Ubuntu) and noarch .rpm (openSUSE).
GitHub repo: https://github.com/teopost/gsync-drive (repo name differs from
the package name `gdrive-sync`).

The maintainer communicates in Italian: reply in Italian. Code, comments,
commit messages and the primary README stay in English; README-IT.md is the
Italian copy — **keep both READMEs in sync** when documenting changes.

## Commands

```bash
.venv/bin/pytest                 # run tests (venv already set up)
./build-deb.sh                   # build gdrive-sync_<version>_all.deb
./build-rpm.sh                   # build noarch RPM (needs rpmbuild)

# run from the checkout (schemas must be compiled first):
mkdir -p /tmp/gds-schemas && cp data/*.gschema.xml /tmp/gds-schemas && glib-compile-schemas /tmp/gds-schemas
GSETTINGS_SCHEMA_DIR=/tmp/gds-schemas python3 -m gdrive_sync.gui.main
GSETTINGS_SCHEMA_DIR=/tmp/gds-schemas python3 -m gdrive_sync.daemon.main
```

## Architecture

- `gdrive_sync/rclone.py` — rclone wrapper (version gating, OAuth, bisync,
  folder listing/size). Must stay importable **without GTK** (unit-tested,
  shared by GUI and daemon).
- `gdrive_sync/config.py` — GSettings-backed multi-account config with an
  in-memory fallback when schemas are missing (tests); rclone filter files.
- `gdrive_sync/daemon/` — systemd user service: per-account sync engines
  (state machine, retry/backoff), D-Bus API, inotify watcher, notifications.
- `gdrive_sync/gui/` — libadwaita app: main window, setup wizard
  (auth → Drive folder tree → local folder → first sync), preferences,
  conflicts dialog. `drive_tree.py` is the checkbox tree with folder sizes.
- `gdrive_sync/i18n.py` — gettext setup (domain `gdrive-sync`).

## Invariants (violating these has caused real bugs)

- **Filters**: the Drive folder selection is stored in the `sync-folders`
  GSettings key AND encoded as `+ /Folder/**` … `- **` rules in the
  per-account filters file. Any code rewriting a filters file must pass
  `include_dirs=account.sync_folders` to `write_filters()`, or the
  selection is silently lost. Changing the filters file content makes
  bisync demand `--resync`.
- **Account ids are reused** (`account1`, `account2`, …): stale on-disk
  state (bisync listings, status file) from a removed account or an old
  install makes the daemon skip the initial `--resync` and the first sync
  fails. The wizard calls `account.purge_state()` before publishing;
  `Config.remove_account()` purges too. Keep it that way.
- The daemon decides `--resync` from `last_sync_time == 0` (status file).
- **Never run a second bisync in the daemon's workdir** (not even
  `--dry-run`): its lock makes the real sync/resync fail instantly. Any
  GUI-side preview must use its own scratch workdir and be cancelled when
  its dialog closes.

## i18n workflow

UI strings are **English msgids** wrapped in `_()` / `ngettext()` from
`gdrive_sync.i18n`. After adding/changing strings: update `po/it.po` and
`po/fr.po` (and `po/gdrive-sync.pot`). No `msgfmt`/`xgettext` on this
machine — `.mo` files are compiled by the pure-Python `po/compile-mo.py`
(both build scripts call it automatically). Dev catalogs go to the
gitignored `locale/` dir, which `i18n.py` prefers over /usr/share/locale.

## Release flow

1. Bump version in `pyproject.toml` AND `gdrive_sync/__init__.py`.
2. Add entries to `debian/changelog` and `data/*.metainfo.xml` releases.
3. `./build-deb.sh` and `./build-rpm.sh`; run the tests.
4. Commit, tag `vX.Y.Z`, push, create the GitHub release and attach both
   packages. Built artifacts (*.deb, *.rpm) are gitignored — release-only.
5. No stored git credentials: the maintainer provides a short-lived PAT.

## Attribution

Per the maintainer's explicit request, the READMEs, commits and release
notes state that all code and documentation were written entirely by Fable
(Claude) via Claude Code, under the maintainer's direction. Preserve this
attribution in new commits and releases.
