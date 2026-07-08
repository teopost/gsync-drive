# GDrive Sync

> 🇮🇹 Versione italiana: [README-IT.md](README-IT.md)

Bidirectional Google Drive synchronization for GNOME (Ubuntu 26.04+).

GNOME 49 removed the Google Drive integration from GNOME Online Accounts.
GDrive Sync brings it back in a more robust form: your files live in a local
folder (`~/GoogleDrive`) usable by any application, while a background
service propagates changes in both directions using
[rclone bisync](https://rclone.org/bisync/).

## Features

- **Local files**: you always work on disk, even offline; changes are
  synchronized when the network comes back.
- **Automatic two-way sync**: local change detection (inotify, 10 s
  debounce) plus a configurable periodic synchronization.
- **Multiple Google accounts**: each account has its own remote, local
  folder, interval, exclusions and conflict handling.
- **Native GNOME app** (GTK4/libadwaita): setup wizard with Google login in
  the browser, account list, preferences, logs. The first synchronization
  runs in the service: closing the window lets it continue in the background.
- **Folder selection**: the wizard shows your Drive folders as a checkbox
  tree (with the size of each folder); you can pick several folders to
  synchronize, or none to synchronize the whole Drive.
- **Multi-language**: English, French and Italian, chosen automatically from
  the system language.
- **Conflict handling**: files modified on both sides are kept in two copies
  and resolved from the app (keep local / Drive / both).
- **Data safety**: no automatic resyncs, a threshold against mass deletions
  (`--max-delete`), and a guided repair with a dry-run preview.
- **Desktop integration**: notifications, sync folder in the Files sidebar,
  autostart via a systemd user service, D-Bus activation.
- **Tray icon** (optional): a status icon in the system tray with a menu to
  sync now, pause/resume and open the app. Native on KDE Plasma; on GNOME
  it requires the [AppIndicator extension](https://extensions.gnome.org/extension/615/appindicator-support/).

## Installation

Download the `.deb` from the
[latest release](https://github.com/teopost/gsync-drive/releases/latest), or
build it yourself:

```bash
./build-deb.sh
sudo apt install ./gdrive-sync_0.8.3_all.deb
```

Then launch **GDrive Sync** from the activities overview and follow the
setup wizard.

### RPM (openSUSE)

An RPM for openSUSE (Leap 15.6+/Tumbleweed) can be built with:

```bash
./build-rpm.sh      # requires rpmbuild (on Ubuntu/Debian: sudo apt install rpm)
sudo zypper install ./gdrive-sync-0.8.3-1.noarch.rpm
```

The app also runs on KDE Plasma: the GTK4/libadwaita runtime is pulled in as
a dependency. Note that the "Files sidebar" toggle only affects GTK file
managers (Dolphin does not read GTK bookmarks); notifications, autostart and
D-Bus activation work normally.

The package depends on `rclone` from the Ubuntu repositories (1.60): it
works, but for a more robust sync rclone ≥ 1.66 from
[rclone.org](https://rclone.org/install/) is recommended — the app detects
the version and enables the extra protections by itself (`--resilient`,
`--recover`, `--max-lock`).

## Architecture

| Component | Description |
|---|---|
| `gdrive-sync` | GTK4/libadwaita app (wizard, status, preferences, conflicts) |
| `gdrive-sync-daemon` | systemd user service (`Type=dbus`) running the sync state machine |
| D-Bus | `io.github.teopost.GDriveSync.Daemon` — ListAccounts, GetAccountInfo, SyncNow, Pause/Resume, Resync, CancelSync, logs (all per-account) |
| rclone | Sync engine; one dedicated remote per account (`gdrive-sync-accountN:`) with OAuth handled by rclone |

Daemon states: `unconfigured, idle, syncing, paused, offline, error,
needs_resync, resyncing`. Transient errors are retried with backoff
(1 m → 5 m → 15 m → 1 h); states that need user attention raise a
notification and are resolved from the app.

Files and directories used (for every account `<id>`: `account1`,
`account2`, …):

- `~/.config/gdrive-sync/filters-<id>.txt` — exclusions and folder selection
  (rclone filter syntax: the chosen folders become `+ /Folder/**` rules)
- `~/.local/state/gdrive-sync/` — per-account logs and bisync state
- GSettings `io.github.teopost.GDriveSync` (plus the relocatable `.Account`
  schema at `/io/github/teopost/GDriveSync/accounts/<id>/`) — preferences
- `~/.config/rclone/rclone.conf` — credentials of the `gdrive-sync-<id>` remotes

## Development

```bash
# dependencies: python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1, rclone; for the tests:
python3 -m venv --system-site-packages .venv && .venv/bin/pip install pytest watchdog -e .
.venv/bin/pytest

# run from the checkout (GSettings schema compiled into a local dir):
mkdir -p /tmp/gds-schemas && cp data/*.gschema.xml /tmp/gds-schemas && glib-compile-schemas /tmp/gds-schemas
GSETTINGS_SCHEMA_DIR=/tmp/gds-schemas python3 -m gdrive_sync.daemon.main   # daemon
GSETTINGS_SCHEMA_DIR=/tmp/gds-schemas python3 -m gdrive_sync.gui.main      # GUI
```

To test without touching your real Drive, point the hidden `remote` key to a
local directory (`gsettings set io.github.teopost.GDriveSync remote
/tmp/fake-drive`).

Translatable strings live in `po/` (`it.po`, `fr.po`; template
`gdrive-sync.pot`). The `.mo` catalogs are compiled at build time (`msgfmt`
when available, otherwise `po/compile-mo.py`); to try them from the
checkout: `python3 po/compile-mo.py po/it.po locale/it/LC_MESSAGES/gdrive-sync.mo`.

The official Debian build uses `dpkg-buildpackage -us -uc -b` (requires
`debhelper`, `dh-python`, `python3-all`, `pybuild-plugin-pyproject`);
`build-deb.sh` is the toolchain-free equivalent for quick use.

## Authorship

All the code and documentation in this project were written entirely by
**Fable** (Claude Fable 5, Anthropic's AI model) through
[Claude Code](https://claude.com/claude-code), working from the directions
and specifications of Stefano Teodorani, who guided the project and
validated the results.

## License

GPL-3.0-or-later — © 2026 Stefano Teodorani
