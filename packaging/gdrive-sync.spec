# RPM spec for gdrive-sync, targeting openSUSE (Leap 15.6+ / Tumbleweed).
# Built cross-distro by build-rpm.sh: the Python package is installed into a
# private directory (/usr/lib/gdrive-sync) instead of site-packages, so the
# same noarch RPM works regardless of the build host's Python layout.

%global appid io.github.teopost.GDriveSync

Name:           gdrive-sync
Version:        %{app_version}
Release:        1
Summary:        Bidirectional Google Drive synchronization for GNOME
License:        GPL-3.0-or-later
URL:            https://github.com/teopost/gsync-drive
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch

# openSUSE package names; on Fedora the equivalents are python3-gobject,
# gtk4, libadwaita and python3-watchdog.
Requires:       python3 >= 3.10
Requires:       python3-gobject
Requires:       python3-gobject-Gdk
Requires:       typelib-1_0-Gtk-4_0
Requires:       typelib-1_0-Adw-1
Requires:       python3-watchdog
Requires:       rclone >= 1.58

%description
GDrive Sync keeps a local folder synchronized with Google Drive. Files live
on disk, usable by any application; a background systemd user service
propagates changes in both directions using rclone bisync, with desktop
notifications, conflict review and a GTK4/libadwaita configuration app.

It restores the Google Drive integration removed from GNOME Online Accounts
in GNOME 49. It also runs on other desktops (e.g. KDE Plasma) provided the
GTK4/libadwaita runtime is installed.

%prep
%setup -q

%build
# Compile translation catalogs (pure-Python fallback when msgfmt is absent).
for po in po/*.po; do
    lang=$(basename "$po" .po)
    mkdir -p "locale/$lang/LC_MESSAGES"
    if command -v msgfmt >/dev/null 2>&1; then
        msgfmt -o "locale/$lang/LC_MESSAGES/gdrive-sync.mo" "$po"
    else
        python3 po/compile-mo.py "$po" "locale/$lang/LC_MESSAGES/gdrive-sync.mo"
    fi
done

%install
# Python package in a private dir, importable via the wrapper scripts.
mkdir -p %{buildroot}%{_prefix}/lib/gdrive-sync
cp -r gdrive_sync %{buildroot}%{_prefix}/lib/gdrive-sync/
find %{buildroot}%{_prefix}/lib/gdrive-sync -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

mkdir -p %{buildroot}%{_bindir}
cat > %{buildroot}%{_bindir}/gdrive-sync <<'WRAP'
#!/usr/bin/python3
import sys
sys.path.insert(0, "/usr/lib/gdrive-sync")
from gdrive_sync.gui.main import main
sys.exit(main())
WRAP
cat > %{buildroot}%{_bindir}/gdrive-sync-daemon <<'WRAP'
#!/usr/bin/python3
import sys
sys.path.insert(0, "/usr/lib/gdrive-sync")
from gdrive_sync.daemon.main import main
sys.exit(main())
WRAP
chmod 755 %{buildroot}%{_bindir}/gdrive-sync %{buildroot}%{_bindir}/gdrive-sync-daemon

install -Dm644 data/%{appid}.desktop \
    %{buildroot}%{_datadir}/applications/%{appid}.desktop
install -Dm644 data/%{appid}.Daemon.desktop \
    %{buildroot}%{_datadir}/applications/%{appid}.Daemon.desktop
install -Dm644 data/%{appid}.gschema.xml \
    %{buildroot}%{_datadir}/glib-2.0/schemas/%{appid}.gschema.xml
install -Dm644 data/%{appid}.Daemon.service \
    %{buildroot}%{_datadir}/dbus-1/services/%{appid}.Daemon.service
install -Dm644 data/gdrive-sync-daemon.service \
    %{buildroot}%{_prefix}/lib/systemd/user/gdrive-sync-daemon.service
install -Dm644 data/%{appid}.metainfo.xml \
    %{buildroot}%{_datadir}/metainfo/%{appid}.metainfo.xml
install -Dm644 data/icons/hicolor/scalable/apps/%{appid}.svg \
    %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/%{appid}.svg
for icon in data/icons/hicolor/symbolic/apps/*.svg; do
    install -Dm644 "$icon" \
        "%{buildroot}%{_datadir}/icons/hicolor/symbolic/apps/$(basename "$icon")"
done

for mo in locale/*/LC_MESSAGES/gdrive-sync.mo; do
    lang=$(echo "$mo" | cut -d/ -f2)
    install -Dm644 "$mo" \
        "%{buildroot}%{_datadir}/locale/$lang/LC_MESSAGES/gdrive-sync.mo"
done

%post
# Leap/Tumbleweed handle these via file triggers; keep as harmless fallback.
glib-compile-schemas %{_datadir}/glib-2.0/schemas >/dev/null 2>&1 || :
update-desktop-database %{_datadir}/applications >/dev/null 2>&1 || :

%postun
glib-compile-schemas %{_datadir}/glib-2.0/schemas >/dev/null 2>&1 || :
update-desktop-database %{_datadir}/applications >/dev/null 2>&1 || :

%files
%license LICENSE
%doc README.md README-IT.md
%{_bindir}/gdrive-sync
%{_bindir}/gdrive-sync-daemon
%{_prefix}/lib/gdrive-sync/
%{_datadir}/applications/%{appid}.desktop
%{_datadir}/applications/%{appid}.Daemon.desktop
%{_datadir}/glib-2.0/schemas/%{appid}.gschema.xml
%{_datadir}/dbus-1/services/%{appid}.Daemon.service
%{_prefix}/lib/systemd/user/gdrive-sync-daemon.service
%{_datadir}/metainfo/%{appid}.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/%{appid}.svg
%{_datadir}/icons/hicolor/symbolic/apps/%{appid}*-symbolic.svg
%{_datadir}/locale/*/LC_MESSAGES/gdrive-sync.mo

%changelog
