# anything-recorder

Record anything you're watching (screen / HDMI output + matching audio).

A simple Linux GTK 3 app in the same spirit as [headset-recorder](https://github.com/edelarey/headset-recorder).  
It captures a monitor (what is going out over HDMI or being cast) plus a PipeWire/Pulse **monitor** source (system audio) and writes an MP4 via ffmpeg.

## Current scope (v0.1)

- X11: full screen or specific monitor capture + audio monitor.
- Small live preview of the selected monitor (X11 only for now).
- Same simple interface style as headset-recorder: source pickers, save folder, name, Record/Stop, status.
- Wayland: audio monitor works; video capture currently falls back to a note (portal / gpu-screen-recorder path planned).

## Dependencies

```bash
sudo apt install build-essential pkg-config libgtk-3-dev ffmpeg
```

`pactl` (PipeWire/Pulse) must be available. On X11, `xrandr` is used to list monitors.

## Build

```bash
./build.sh
# or
make
./anything-recorder
```

## Install (GNOME / Ubuntu dock)

```bash
./install.sh
```

## Usage

1. Select a **video source** (monitor). On X11 a small preview appears below.
2. Select an **audio monitor** source (what the speakers/HDMI are playing).
3. Choose save folder and name the take.
4. Record / Stop. Output is an MP4.

There is also a no-compiler fallback: `./anything-recorder.sh` (needs ffmpeg, pactl, optional zenity).

## Notes

- Preview is intentionally lightweight (periodic still frames) so it doesn't interfere with recording.
- For best results on modern Wayland desktops, a future version will use the xdg-desktop-portal ScreenCast path (or wrap `gpu-screen-recorder`).

## License

MIT
