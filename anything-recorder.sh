#!/bin/bash
# Shell fallback. Prefers gpu-screen-recorder when it is on PATH.
# Otherwise records through the same backend as the GTK app, so on this
# GNOME session you still pick one display and one audio monitor.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$DIR/anything_recorder.py"

die() { echo "$*" >&2; exit 1; }
command -v python3 >/dev/null || die "python3 not found"
[[ -f "$PY" ]] || die "Missing $PY"

mapfile -t ROWS < <(python3 "$PY" --list)

VIDS=()
VLABS=()
AUDS=()
ALABS=()
DEF_VIDEO=""
DEF_AUDIO=""
while IFS=$'\t' read -r kind id label; do
  case "$kind" in
    video) VIDS+=("$id"); VLABS+=("$label") ;;
    audio) AUDS+=("$id"); ALABS+=("$label") ;;
    default-video) DEF_VIDEO="$id" ;;
    default-audio) DEF_AUDIO="$id" ;;
  esac
done < <(printf '%s\n' "${ROWS[@]}")

(( ${#VIDS[@]} > 0 )) || die "No video sources found"
(( ${#AUDS[@]} > 0 )) || die "No audio monitor sources found"

pick() {
  local title="$1"
  shift
  local -n ids=$1
  local -n labels=$2
  local def="$3"
  if command -v zenity >/dev/null 2>&1; then
    local args=() i
    for i in "${!labels[@]}"; do
      if [[ "${ids[$i]}" == "$def" ]]; then
        args+=(TRUE "$i" "${labels[$i]}")
      else
        args+=(FALSE "$i" "${labels[$i]}")
      fi
    done
    if [[ ${args[0]} != TRUE ]]; then
      args[0]=TRUE
    fi
    local idx
    idx=$(zenity --list --title="Anything Recorder" --text="$title" --radiolist \
      --hide-column=1 --print-column=1 \
      --column=idx --column=Source --height=360 --width=720 \
      "${args[@]}") || return 1
    echo "${ids[$idx]}"
  else
    echo "$title" >&2
    local i
    for i in "${!labels[@]}"; do
      printf "  %2d  %s\n" "$i" "${labels[$i]}" >&2
    done
    local idx=0
    read -r -p "Number [0]: " idx || return 1
    echo "${ids[${idx:-0}]}"
  fi
}

VID=$(pick "Display to record" VIDS VLABS "$DEF_VIDEO") || exit 1
AUD=$(pick "Audio to record with it (independent of the display)" AUDS ALABS "$DEF_AUDIO") || exit 1

STAMP=$(date +%Y%m%d-%H%M%S)
SUGGEST="$HOME/Videos/recording-$STAMP.mp4"
[[ -d "$HOME/Videos" ]] || SUGGEST="$HOME/recording-$STAMP.mp4"
if command -v zenity >/dev/null 2>&1; then
  OUT=$(zenity --file-selection --save --confirm-overwrite \
    --title="Save MP4" --filename="$SUGGEST") || exit 1
else
  read -r -p "Save as [$SUGGEST]: " OUT
  OUT=${OUT:-$SUGGEST}
fi
[[ "$OUT" == *.mp4 ]] || OUT="$OUT.mp4"

if command -v gpu-screen-recorder >/dev/null 2>&1 \
   && [[ "$VID" != v4l:* ]]; then
  TARGET="$VID"
  case "$VID" in
    monitor:*) TARGET="${VID#monitor:}" ;;
    desktop) TARGET="screen" ;;
    portal) TARGET="portal" ;;
  esac
  echo "gpu-screen-recorder: display $TARGET + audio $AUD → $OUT"
  echo "Press Ctrl+C to stop."
  exec gpu-screen-recorder -w "$TARGET" -f 30 -a "device:$AUD" -o "$OUT"
fi

exec python3 "$PY" --record --video "$VID" --audio "$AUD" --output "$OUT"
