#!/bin/bash
# Install Anything Recorder for this user and pin it to the GNOME dock.
set -euo pipefail
cd "$(dirname "$0")"

PREFIX="${PREFIX:-$HOME/.local}"
BINDIR="$PREFIX/bin"
APPDIR="$PREFIX/share/applications"
ICONDIR="$PREFIX/share/icons/hicolor/scalable/apps"
DESKTOP_ID="anything-recorder.desktop"
PIN=1
UNINSTALL=0

usage() {
  cat <<EOF
Usage: $0 [--prefix DIR] [--no-pin] [--uninstall]
  --prefix DIR   Install prefix (default: \$HOME/.local)
  --no-pin       Do not add the app to the GNOME dock
  --uninstall    Remove a previous user install and unpin it
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) PREFIX="$2"; BINDIR="$PREFIX/bin"; APPDIR="$PREFIX/share/applications"
              ICONDIR="$PREFIX/share/icons/hicolor/scalable/apps"; shift 2 ;;
    --no-pin) PIN=0; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

unpin() {
  command -v gsettings >/dev/null 2>&1 || return 0
  python3 - "$DESKTOP_ID" <<'PY'
import ast, subprocess, sys
desktop_id = sys.argv[1]
raw = subprocess.check_output(["gsettings", "get", "org.gnome.shell", "favorite-apps"], text=True)
favs = ast.literal_eval(raw)
if desktop_id not in favs:
    raise SystemExit(0)
favs = [x for x in favs if x != desktop_id]
out = "[" + ", ".join("'" + x + "'" for x in favs) + "]"
subprocess.check_call(["gsettings", "set", "org.gnome.shell", "favorite-apps", out])
PY
}

if [[ $UNINSTALL -eq 1 ]]; then
  unpin
  rm -f "$BINDIR/anything-recorder" \
        "$BINDIR/anything-recorder.py" \
        "$APPDIR/$DESKTOP_ID" \
        "$ICONDIR/anything-recorder.svg"
  update-desktop-database "$APPDIR" 2>/dev/null || true
  gtk-update-icon-cache -f -t "$PREFIX/share/icons/hicolor" 2>/dev/null || true
  echo "Removed Anything Recorder from $PREFIX"
  exit 0
fi

python3 - <<'PY'
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gst", "1.0")
from gi.repository import Gtk, Gst
print("gtk", Gtk.get_major_version(), "gst-import-ok")
PY

install -d "$BINDIR" "$APPDIR" "$ICONDIR"
install -m 644 anything_recorder.py "$BINDIR/anything-recorder.py"
install -m 644 anything-recorder.svg "$ICONDIR/anything-recorder.svg"

# Wrapper drops snap/IDE library paths that crash GTK, and keeps the
# session bus, Wayland socket, and ~/.local/bin (ffmpeg, pactl).
cat > "$BINDIR/anything-recorder" <<EOF
#!/bin/bash
exec env -i \\
  HOME="\$HOME" \\
  USER="\${USER:-}" \\
  LOGNAME="\${LOGNAME:-\${USER:-}}" \\
  LANG="\${LANG:-C.UTF-8}" \\
  DISPLAY="\${DISPLAY:-}" \\
  WAYLAND_DISPLAY="\${WAYLAND_DISPLAY:-}" \\
  XAUTHORITY="\${XAUTHORITY:-}" \\
  XDG_RUNTIME_DIR="\${XDG_RUNTIME_DIR:-}" \\
  XDG_CURRENT_DESKTOP="\${XDG_CURRENT_DESKTOP:-}" \\
  XDG_SESSION_TYPE="\${XDG_SESSION_TYPE:-}" \\
  XDG_SESSION_DESKTOP="\${XDG_SESSION_DESKTOP:-}" \\
  DBUS_SESSION_BUS_ADDRESS="\${DBUS_SESSION_BUS_ADDRESS:-}" \\
  XDG_ACTIVATION_TOKEN="\${XDG_ACTIVATION_TOKEN:-}" \\
  DESKTOP_STARTUP_ID="\${DESKTOP_STARTUP_ID:-}" \\
  GIO_LAUNCHED_DESKTOP_FILE="\${GIO_LAUNCHED_DESKTOP_FILE:-}" \\
  GIO_LAUNCHED_DESKTOP_FILE_PID="\${GIO_LAUNCHED_DESKTOP_FILE_PID:-}" \\
  XDG_DATA_DIRS="/usr/share/ubuntu:/usr/share/gnome:/usr/local/share:/usr/share:/var/lib/snapd/desktop" \\
  PATH="/usr/local/bin:/usr/bin:/bin:\$HOME/.local/bin" \\
  /usr/bin/python3 "$BINDIR/anything-recorder.py" "\$@"
EOF
chmod 755 "$BINDIR/anything-recorder"

cat > "$APPDIR/$DESKTOP_ID" <<EOF
[Desktop Entry]
Type=Application
Name=Anything Recorder
Comment=Record one display and one audio monitor to MP4
Exec=$BINDIR/anything-recorder
TryExec=$BINDIR/anything-recorder
Icon=$ICONDIR/anything-recorder.svg
Terminal=false
Categories=AudioVideo;Recorder;
Keywords=record;screen;hdmi;audio;video;monitor;
StartupNotify=true
StartupWMClass=za.delarey.anythingrecorder
EOF
chmod 644 "$APPDIR/$DESKTOP_ID"

if command -v desktop-file-validate >/dev/null 2>&1; then
  desktop-file-validate "$APPDIR/$DESKTOP_ID"
fi

update-desktop-database "$APPDIR" 2>/dev/null || true
gtk-update-icon-cache -f -t "$PREFIX/share/icons/hicolor" 2>/dev/null || true

if [[ $PIN -eq 1 ]] && command -v gsettings >/dev/null 2>&1; then
  python3 - "$DESKTOP_ID" <<'PY'
import ast, subprocess, sys
desktop_id = sys.argv[1]
raw = subprocess.check_output(["gsettings", "get", "org.gnome.shell", "favorite-apps"], text=True)
favs = ast.literal_eval(raw)
if desktop_id not in favs:
    favs.append(desktop_id)
    out = "[" + ", ".join("'" + x + "'" for x in favs) + "]"
    subprocess.check_call(["gsettings", "set", "org.gnome.shell", "favorite-apps", out])
print("pinned")
PY
fi

echo "Installed $BINDIR/anything-recorder"
echo "Launcher: $APPDIR/$DESKTOP_ID"
if [[ $PIN -eq 1 ]]; then
  echo "Pinned to the Ubuntu dock."
else
  echo "Search 'Anything Recorder' in Activities, then pin it to the dock."
fi
