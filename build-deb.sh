#!/bin/bash
# Build gdrive-sync .deb with plain dpkg-deb (no debhelper required).
# For official builds use: dpkg-buildpackage -us -uc -b  (needs debhelper,
# dh-python, python3-all, pybuild-plugin-pyproject).
set -euo pipefail

cd "$(dirname "$0")"
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

PKGDIR="$STAGE/gdrive-sync_${VERSION}_all"

# --- Python package -> dist-packages (arch-independent, pure python) --------
DEST="$PKGDIR/usr/lib/python3/dist-packages"
mkdir -p "$DEST"
cp -r gdrive_sync "$DEST/"
find "$DEST" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

# --- executables -------------------------------------------------------------
mkdir -p "$PKGDIR/usr/bin"
cat > "$PKGDIR/usr/bin/gdrive-sync" <<'EOF'
#!/usr/bin/python3
import sys
from gdrive_sync.gui.main import main
sys.exit(main())
EOF
cat > "$PKGDIR/usr/bin/gdrive-sync-daemon" <<'EOF'
#!/usr/bin/python3
import sys
from gdrive_sync.daemon.main import main
sys.exit(main())
EOF
chmod 755 "$PKGDIR/usr/bin/gdrive-sync" "$PKGDIR/usr/bin/gdrive-sync-daemon"

# --- data files ---------------------------------------------------------------
install -Dm644 data/io.github.teopost.GDriveSync.desktop \
    "$PKGDIR/usr/share/applications/io.github.teopost.GDriveSync.desktop"
install -Dm644 data/io.github.teopost.GDriveSync.Daemon.desktop \
    "$PKGDIR/usr/share/applications/io.github.teopost.GDriveSync.Daemon.desktop"
install -Dm644 data/io.github.teopost.GDriveSync.gschema.xml \
    "$PKGDIR/usr/share/glib-2.0/schemas/io.github.teopost.GDriveSync.gschema.xml"
install -Dm644 data/io.github.teopost.GDriveSync.Daemon.service \
    "$PKGDIR/usr/share/dbus-1/services/io.github.teopost.GDriveSync.Daemon.service"
install -Dm644 data/gdrive-sync-daemon.service \
    "$PKGDIR/usr/lib/systemd/user/gdrive-sync-daemon.service"
install -Dm644 data/io.github.teopost.GDriveSync.metainfo.xml \
    "$PKGDIR/usr/share/metainfo/io.github.teopost.GDriveSync.metainfo.xml"
install -Dm644 data/icons/hicolor/scalable/apps/io.github.teopost.GDriveSync.svg \
    "$PKGDIR/usr/share/icons/hicolor/scalable/apps/io.github.teopost.GDriveSync.svg"
install -Dm644 data/icons/hicolor/symbolic/apps/io.github.teopost.GDriveSync-symbolic.svg \
    "$PKGDIR/usr/share/icons/hicolor/symbolic/apps/io.github.teopost.GDriveSync-symbolic.svg"

# --- translations --------------------------------------------------------------
for po in po/*.po; do
    lang=$(basename "$po" .po)
    mo="$PKGDIR/usr/share/locale/$lang/LC_MESSAGES/gdrive-sync.mo"
    mkdir -p "$(dirname "$mo")"
    if command -v msgfmt >/dev/null; then
        msgfmt -o "$mo" "$po"
    else
        python3 po/compile-mo.py "$po" "$mo"
    fi
done

# --- copyright/changelog (policy) ---------------------------------------------
install -Dm644 debian/copyright "$PKGDIR/usr/share/doc/gdrive-sync/copyright"
gzip -9nc debian/changelog > "$STAGE/changelog.gz"
install -Dm644 "$STAGE/changelog.gz" "$PKGDIR/usr/share/doc/gdrive-sync/changelog.gz"

# --- DEBIAN/control ------------------------------------------------------------
mkdir -p "$PKGDIR/DEBIAN"
SIZE=$(du -sk "$PKGDIR" --exclude=DEBIAN | cut -f1)
cat > "$PKGDIR/DEBIAN/control" <<EOF
Package: gdrive-sync
Version: $VERSION
Section: net
Priority: optional
Architecture: all
Installed-Size: $SIZE
Depends: python3, python3-gi, python3-gi-cairo, gir1.2-gtk-4.0, gir1.2-adw-1, python3-watchdog, rclone (>= 1.58)
Maintainer: Stefano Teodorani <stefano.teodorani@horsa.it>
Homepage: https://github.com/teopost/gsync-drive
Description: Bidirectional Google Drive synchronization for GNOME
 GDrive Sync keeps a local folder synchronized with Google Drive.
 Files live on disk, usable by any application; a background systemd
 user service propagates changes in both directions using rclone
 bisync, with desktop notifications, conflict review and a GTK4 /
 libadwaita configuration app.
 .
 It restores the Google Drive integration removed from GNOME Online
 Accounts in GNOME 49.
EOF

dpkg-deb --build --root-owner-group "$PKGDIR" .
echo "Pacchetto creato: $(ls -1 gdrive-sync_${VERSION}_all.deb)"
