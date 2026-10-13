# anything-recorder

Record anything you're watching.

A Linux GTK 3 app in the same spirit as [headset-recorder](https://github.com/edelarey/headset-recorder). Pick a display and, separately, a PipeWire/Pulse **monitor** source, then write an MP4. The picture does not force the sound: a second screen can be the video while the laptop speakers, or an HDMI output, are the audio.

## What you can record

- **One display.** Every connected monitor is listed by name (for example `eDP-1`, `HDMI-1`). Plug another in and it appears in Display within a few seconds, or press Refresh.
- **The whole desktop.** Every display in one frame.
- **Portal.** The system dialog asks for a screen or a window when recording starts.
- **A capture device.** Any `/dev/video*` node that can capture (webcam or HDMI capture card), with whichever audio monitor you select.

Audio choices are the `.monitor` sources (Speakers, HDMI 1, HDMI 2, HDMI 3, and so on). A level meter follows the one you pick. Idle HDMI outputs stay in the list.

## Controls

- **Always on top** keeps the window above other windows. The choice is remembered.
- **Minimise** shrinks the window to a small bar. The timer and Stop stay available.
- **Restore** opens the full window again.
- The title-bar minimise button hides the window to the dock. Click the Anything Recorder icon there to bring it back.

The window is part of the picture if it sits on the display you are recording. Minimise it, or move it to another display.

Last display, audio source, folder, and always-on-top choice are saved in `~/.config/anything-recorder/config.ini`. New takes are named `recording-YYYYMMDD-HHMMSS.mp4`. The first launch uses the primary display, the default sink's monitor, and `~/Videos`.

## Backends

The app picks the first one that can do the job:

1. **`gpu-screen-recorder`**, when it is on `PATH`.
2. **GNOME screen cast** on Wayland (per display, no extra package). This is what Ubuntu GNOME uses here.
3. **ffmpeg `x11grab`** on X11 only.
4. **Portal**, for a screen or window chosen in the system dialog.
5. **Video4Linux** for `/dev/video*` devices.

Preview is a still of the selected display or camera. Portal has no still; the system dialog is the picker. Stop finalises the MP4.

## Dependencies

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-libav gstreamer1.0-pulseaudio
```

`pactl` must be available. `ffmpeg` is used for the X11 fallback. Optional: `gpu-screen-recorder`, and `zenity` for the shell fallback.

No C compiler is required. `main.c` is an earlier prototype; the dock launches the Python app.

## Run

```bash
./anything-recorder
```

Shell fallback, with the same display list and the same separate audio choice:

```bash
./anything-recorder.sh
```

## Install (GNOME / Ubuntu dock)

```bash
./install.sh
```

Copies the app to `~/.local/bin`, adds a launcher, and pins **Anything Recorder** to the dock.

```bash
./install.sh --no-pin       # launcher only
./install.sh --uninstall    # remove the launcher and unpin it
```

## Usage

1. Select a **display** (or the whole desktop, portal, or a capture device).
2. Select an **audio monitor**. It does not have to match the display.
3. Set the folder and the name.
4. Record / Stop. The file is an H.264 + AAC MP4.

## License

MIT
