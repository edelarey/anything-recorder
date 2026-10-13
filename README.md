# anything-recorder

Record anything you're watching (screen / HDMI output + matching audio).

A simple Linux GTK 3 app in the same spirit as [headset-recorder](https://github.com/edelarey/headset-recorder).  
It captures a monitor (what is going out over HDMI or being cast) plus a PipeWire/Pulse **monitor** source and writes an MP4.

## Backends

- **Preferred**: `gpu-screen-recorder` (X11 + Wayland, GPU encode, portal support).  
  The app detects it automatically if it is on PATH.
- **Fallback**: ffmpeg + x11grab (X11 only).

## Current scope (v0.2)

- Video sources: portal, entire screen, named monitors (via gsr or xrandr).
- Audio: PipeWire/Pulse `.monitor` sources.
- Small X11 still-frame preview for concrete monitor names.
- Same simple interface as headset-recorder.

## Dependencies

```bash
sudo apt install build-essential pkg-config libgtk-3-dev ffmpeg
# optional but recommended for Wayland / better quality:
# install gpu-screen-recorder (flatpak, AUR, or from source)
```

`pactl` must be available.

## Build

```bash
./build.sh
./anything-recorder
```

## Install (GNOME / Ubuntu dock)

```bash
./install.sh
```

## Usage

1. Select a **video** source (portal / screen / monitor name).
2. Select an **audio monitor** source.
3. Set folder + name.
4. Record / Stop → MP4.

Shell fallback: `./anything-recorder.sh`

## Notes

- On Wayland, choose **Portal** (or a named monitor if gsr lists them). The portal dialog appears when recording starts.
- Preview is X11-only for now and only for concrete monitor names (not “portal” or “screen”).
- Stop sends SIGINT so both backends exit cleanly.

## License

MIT
