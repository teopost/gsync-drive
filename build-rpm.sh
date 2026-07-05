#!/bin/bash
# Build a noarch RPM of gdrive-sync (target: openSUSE Leap 15.6+/Tumbleweed).
# Works on any distro that has rpmbuild (Ubuntu: sudo apt install rpm).
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v rpmbuild >/dev/null; then
    echo "rpmbuild non trovato. Su Ubuntu/Debian: sudo apt install rpm" >&2
    exit 1
fi

VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)
TOP=$(mktemp -d)
trap 'rm -rf "$TOP"' EXIT
mkdir -p "$TOP"/{SPECS,SOURCES,BUILD,RPMS,SRPMS}

# Source tarball with exactly what the spec needs.
SRC="$TOP/gdrive-sync-$VERSION"
mkdir -p "$SRC"
cp -r gdrive_sync data po LICENSE README.md README-IT.md "$SRC/"
find "$SRC" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
tar -C "$TOP" -czf "$TOP/SOURCES/gdrive-sync-$VERSION.tar.gz" "gdrive-sync-$VERSION"

# v4 format + gzip payload: installable also by older rpm (openSUSE Leap).
rpmbuild -bb packaging/gdrive-sync.spec \
    --define "_topdir $TOP" \
    --define "app_version $VERSION" \
    --define "_rpmformat 4" \
    --define "_binary_payload w9.gzdio" \
    --quiet

cp "$TOP"/RPMS/noarch/gdrive-sync-"$VERSION"-*.noarch.rpm .
echo "Pacchetto creato: $(ls -1 gdrive-sync-${VERSION}-*.noarch.rpm)"
