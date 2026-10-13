#!/bin/bash
# Minimal fallback. Needs: ffmpeg, pactl, optional zenity.
set -euo pipefail

die() { echo "$*" >&2; exit 1; }
command -v ffmpeg >/dev/null || die "ffmpeg not found"
command -v pactl >/dev/null || die "pactl not found"

# Audio monitors
mapfile -t ALINES < <(pactl list short sources | grep '\.monitor' || true)
(( ${#ALINES[@]} > 0 )) || die "No monitor sources found"

ASOURCES=()
ALABELS=()
for i in "${!ALINES[@]}"; do
  read -r _ name _ _ state <<< "${ALINES[$i]}"
  ASOURCES+=("$name")
  ALABELS+=("$name  [$state]")
done

pick_audio() {
  if command -v zenity >/dev/null 2>&1; then
    local args=()
    for i in "${!ALABELS[@]}"; do
      args+=(FALSE "$i" "${ALABELS[$i]}")
    done
    args[0]=TRUE
    local idx
    idx=$(zenity --list --title="Anything Recorder" \
      --text="Select audio monitor" --radiolist \
      --hide-column=1 --print-column=1 \
      --column=idx --column=Source --height=300 --width=700 \
      "${args[@]}") || return 1
    echo "${ASOURCES[$idx]}"
  else
    echo "Audio monitors:" >&2
    for i in "${!ALABELS[@]}"; do
      printf "  %2d  %s\n" "$i" "${ALABELS[$i]}" >&2
    done
    local idx=0
    read -r -p "Number [0]: " idx || return 1
    echo "${ASOURCES[${idx:-0}]}"
  fi
}

AUD=$(pick_audio) || exit 1

# Video: simple X11 primary for now
VID=":0.0"
if [[ -n "${WAYLAND_DISPLAY:-}" ]]; then
  echo "Wayland detected — this fallback only does audio. Use the GTK app or gpu-screen-recorder for video." >&2
fi

STAMP=$(date +%Y%m%d-%H%M%S)
SUGGEST="$HOME/recording-$STAMP.mp4"
if command -v zenity >/dev/null 2>&1; then
  OUT=$(zenity --file-selection --save --confirm-overwrite \
    --title="Save MP4" --filename="$SUGGEST") || exit 1
else
  read -r -p "Save as [$SUGGEST]: " OUT
  OUT=${OUT:-$SUGGEST}
fi
[[ "$OUT" == *.mp4 ]] || OUT="$OUT.mp4"

echo "Recording video $VID + audio $AUD → $OUT"
echo "Press q then Enter to stop."
ffmpeg -hide_banner -loglevel info -y \
  -f x11grab -framerate 30 -i "$VID" \
  -f pulse -i "$AUD" \
  -c:v libx264 -preset veryfast -crf 20 \
  -c:a aac -b:a 192k \
  -shortest \
  "$OUT"
echo "Saved $OUT"
