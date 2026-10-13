#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

APP_NAME="anything-recorder"
DESKTOP="$HOME/.local/share/applications/${APP_NAME}.desktop"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
ICON="$ICON_DIR/${APP_NAME}.svg"

mkdir -p "$(dirname "$DESKTOP")" "$ICON_DIR"

# Simple icon (placeholder)
cat > "$ICON" << 'EOF'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="12" fill="#2d2d2d"/>
  <circle cx="32" cy="32" r="14" fill="#e74c3c"/>
  <rect x="22" y="18" width="20" height="28" rx="2" fill="#fff"/>
</svg>
EOF

cat > "$DESKTOP" << EOF
[Desktop Entry]
Name=Anything Recorder
Comment=Record screen + system audio
Exec=$(pwd)/anything-recorder
Icon=${APP_NAME}
Terminal=false
Type=Application
Categories=AudioVideo;Recorder;
EOF

update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
echo "Installed launcher. You can pin it from the app grid."
