#!/usr/bin/env python3
"""Record one display (or a capture device) and one audio monitor to MP4.

Video and audio are chosen separately. On this GNOME Wayland session the
display is captured with Mutter's screen cast, so each connected monitor can
be recorded on its own. The selected PipeWire/Pulse monitor is mixed in as
the audio track.
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

import cairo
import gi

# Mutter on Wayland ignores Gtk's keep-above flag. An XWayland window
# carries _NET_WM_STATE_ABOVE, which this compositor does honor. Set the
# backend before Gdk opens a display. Capture itself uses PipeWire.
if not os.environ.get("GDK_BACKEND") and os.environ.get("DISPLAY"):
    os.environ["GDK_BACKEND"] = "x11"

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Gst", "1.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gst, Gtk

APP_ID = "za.delarey.anythingrecorder"
APP_NAME = "Anything Recorder"

MON_RE = re.compile(
    r"^\s*\d+:\s+(?P<flags>\S+)\s+(?P<w>\d+)/\d+x(?P<h>\d+)/\d+\+(?P<x>-?\d+)\+(?P<y>-?\d+)\s+(?P<name>\S+)\s*$"
)
HIFI_RE = re.compile(r"HiFi__([A-Za-z0-9]+)__sink\.monitor")


class Cancelled(Exception):
    pass


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def is_wayland() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY"))


def run_text(argv: list[str]) -> str:
    try:
        return subprocess.check_output(argv, text=True, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return ""


def config_file() -> str:
    return os.path.join(GLib.get_user_config_dir(), "anything-recorder", "config.ini")


def load_config() -> dict[str, str]:
    cp = configparser.ConfigParser()
    path = config_file()
    if os.path.isfile(path):
        cp.read(path)
    if not cp.has_section("main"):
        return {}
    return dict(cp["main"])


def save_config(values: dict[str, str]) -> None:
    path = config_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cp = configparser.ConfigParser()
    cp["main"] = {k: v for k, v in values.items() if v is not None}
    with open(path, "w", encoding="utf-8") as fh:
        cp.write(fh)


def debug_log_path() -> str:
    return os.path.join(GLib.get_user_config_dir(), "anything-recorder", "debug.log")


_LOG_LOCK = threading.Lock()
_GST_LOG_READY = False


def log_event(event: str, **fields) -> None:
    """Append one line to the capture debug log. Safe to call from any thread."""
    parts = [time.strftime("%H:%M:%S"), event]
    for key, value in fields.items():
        text = str(value).replace("\n", " ")
        if len(text) > 360:
            text = text[:360] + "…"
        parts.append(f"{key}={text}")
    line = " ".join(parts) + "\n"
    path = debug_log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _LOG_LOCK:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
    except OSError:
        pass


def _rotate_debug_log() -> None:
    path = debug_log_path()
    try:
        if os.path.isfile(path) and os.path.getsize(path) > 1_500_000:
            os.replace(path, path + ".1")
    except OSError:
        pass


def _pw_dump() -> list:
    try:
        raw = subprocess.check_output(["pw-dump"], text=True, timeout=3)
        data = json.loads(raw)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        log_event("pw-dump-failed", err=exc)
        return []
    return data if isinstance(data, list) else []


def _fmt_param(value) -> str:
    if not value:
        return "-"
    try:
        text = json.dumps(value, separators=(",", ":"))
    except TypeError:
        text = str(value)
    return text if len(text) <= 280 else text[:280] + "…"


def log_pipewire_node(node_id: int, why: str) -> None:
    """Log the node we are about to capture, and every other live video node.

    A monitor can expose more than one stream. The one Mutter names is not
    always the one that carries frames.
    """
    data = _pw_dump()
    nodes = []
    ports = []
    for obj in data:
        kind = str(obj.get("type"))
        if kind.endswith("Node"):
            nodes.append(obj)
        elif kind.endswith("Port"):
            ports.append(obj)
    target = None
    others = []
    for obj in nodes:
        info = obj.get("info") or {}
        props = info.get("props") or {}
        media = str(props.get("media.class") or "")
        name = str(props.get("node.name") or "")
        interesting = (
            obj.get("id") == int(node_id)
            or "Video" in media
            or "screen" in name.lower()
            or "mutter" in name.lower()
        )
        if obj.get("id") == int(node_id):
            target = obj
        elif interesting:
            others.append(obj)
    if target is None:
        log_event("pw-node-missing", why=why, node=node_id, nodes=len(nodes))
        return
    info = target.get("info") or {}
    props = info.get("props") or {}
    node_ports = []
    for port in ports:
        pinfo = port.get("info") or {}
        pprops = pinfo.get("props") or {}
        if int(pprops.get("node.id") or -1) != int(node_id):
            continue
        params = pinfo.get("params") or {}
        node_ports.append(
            f"{port.get('id')}:{pinfo.get('direction')}:{pprops.get('port.name')} "
            f"fmt={_fmt_param(params.get('Format'))}"
        )
    log_event(
        "pw-node",
        why=why,
        id=node_id,
        serial=props.get("object.serial"),
        name=props.get("node.name"),
        media=props.get("media.name"),
        klass=props.get("media.class"),
        state=info.get("state"),
        error=info.get("error") or "",
        outs=info.get("n-output-ports"),
        ports="; ".join(node_ports) or "none",
    )
    for obj in others[:8]:
        oinfo = obj.get("info") or {}
        oprops = oinfo.get("props") or {}
        log_event(
            "pw-other-video",
            why=why,
            id=obj.get("id"),
            serial=oprops.get("object.serial"),
            name=oprops.get("node.name"),
            media=oprops.get("media.name"),
            klass=oprops.get("media.class"),
            state=oinfo.get("state"),
        )


def _install_gst_log() -> None:
    global _GST_LOG_READY
    if _GST_LOG_READY:
        return
    _GST_LOG_READY = True
    try:
        Gst.debug_set_active(True)
        Gst.debug_set_colored(False)
        # WARNING keeps the app quiet. DEBUG dumps every caps negotiation
        # to the terminal, which hid the useful lines.
        Gst.debug_set_threshold_for_name("pipewiresrc", Gst.DebugLevel.WARNING)
    except Exception as exc:
        log_event("gst-debug-setup-failed", err=exc)
        return

    def on_log(_cat, level, _file, _func, _line, _obj, message, _data) -> None:
        try:
            text = str(message)
        except Exception:
            return
        low = text.lower()
        if not any(word in low for word in ("connect", "target", "error", "stream", "format", "negot", "caps", "fail", "buffer")):
            return
        log_event("gst", lvl=int(level), msg=text)

    try:
        Gst.debug_add_log_function(on_log, None)
    except Exception as exc:
        log_event("gst-log-fn-failed", err=exc)


def _buffer_sample(buf) -> str:
    try:
        ok, mapped = buf.map(Gst.MapFlags.READ)
    except Exception as exc:
        return f"map-error {exc}"
    if not ok:
        return "unmapable"
    try:
        data = bytes(mapped.data)
    finally:
        buf.unmap(mapped)
    if not data:
        return "empty"
    step = max(1, len(data) // 64)
    sample = data[0:len(data):step][:64]
    mean = sum(sample) / len(sample)
    return f"bytes={len(data)} luma~{mean:.1f} head={data[:8].hex()}"


def parse_xrandr_monitors(text: str) -> list[dict]:
    found = []
    for line in text.splitlines():
        match = MON_RE.match(line)
        if not match:
            continue
        found.append(
            {
                "connector": match.group("name"),
                "x": int(match.group("x")),
                "y": int(match.group("y")),
                "w": int(match.group("w")),
                "h": int(match.group("h")),
                "primary": "*" in match.group("flags"),
            }
        )
    return found


def list_monitors() -> list[dict]:
    found = parse_xrandr_monitors(run_text(["xrandr", "--listmonitors"]))
    if found:
        return sorted(found, key=lambda m: (not m["primary"], m["x"], m["y"], m["connector"]))
    display = Gdk.Display.get_default()
    if display is None:
        return []
    found = []
    for i in range(display.get_n_monitors()):
        mon = display.get_monitor(i)
        geo = mon.get_geometry()
        name = mon.get_model() or f"display-{i}"
        found.append(
            {
                "connector": name,
                "x": int(geo.x),
                "y": int(geo.y),
                "w": int(geo.width),
                "h": int(geo.height),
                "primary": bool(mon.is_primary()),
            }
        )
    return sorted(found, key=lambda m: (not m["primary"], m["x"], m["y"], m["connector"]))


def monitor_bounds(monitors: list[dict]) -> tuple[int, int, int, int]:
    x0 = min(m["x"] for m in monitors)
    y0 = min(m["y"] for m in monitors)
    x1 = max(m["x"] + m["w"] for m in monitors)
    y1 = max(m["y"] + m["h"] for m in monitors)
    return x0, y0, x1 - x0, y1 - y0


def damage_origin(video_id: str) -> tuple[int, int] | None:
    """Top-left of the monitor or desktop being captured, in X11 pixels."""
    monitors = list_monitors()
    if video_id == "desktop":
        if not monitors:
            return None
        x, y, _w, _h = monitor_bounds(monitors)
        return x, y
    if not video_id.startswith("monitor:"):
        return None
    connector = video_id.split(":", 1)[1]
    match = next((m for m in monitors if m["connector"] == connector), None)
    if match is None:
        return None
    return int(match["x"]), int(match["y"])


def _clicks_pass_through(win: Gtk.Window) -> None:
    """Empty the X11 input shape so the corner window does not take clicks."""
    gdkwin = win.get_window()
    if gdkwin is None or not hasattr(gdkwin, "get_xid"):
        return
    try:
        gi.require_version("GdkX11", "3.0")
        from gi.repository import GdkX11
        import ctypes

        xdisplay = GdkX11.X11Display.get_xdisplay(gdkwin.get_display())
        xid = int(gdkwin.get_xid())
        address = int(xdisplay) if not isinstance(xdisplay, int) else xdisplay
        xext = ctypes.CDLL("libXext.so.6")
        xext.XShapeCombineRectangles.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int,
            ctypes.c_int, ctypes.c_int,
        ]
        # ShapeInput = 2, ShapeSet = 0, zero rectangles: no clickable area.
        xext.XShapeCombineRectangles(
            ctypes.c_void_p(address), ctypes.c_ulong(xid), 2,
            0, 0, None, 0, 0, 0,
        )
    except Exception:
        pass


def open_damage_window(x: int, y: int) -> Gtk.Window | None:
    """Map a clear always-on-top window so Mutter composites this monitor.

    A hardware video plane, or a picture that is not being redrawn, never
    damages the stage. Mutter then pushes no PipeWire buffers, the link
    stays active, and the recording file stays empty. An XWayland window
    on that output forces a composite for as long as the capture runs.
    """
    try:
        win = Gtk.Window(type=Gtk.WindowType.POPUP)
        win.set_decorated(False)
        win.set_accept_focus(False)
        win.set_focus_on_map(False)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        win.set_keep_above(True)
        # A fully clear window is culled and does not damage the output.
        # A small, faint square is enough for Mutter to composite the monitor.
        win.set_opacity(0.15)
        win.set_default_size(8, 8)
        win.move(int(x), int(y))
        win.show_all()
        gdkwin = win.get_window()
        if gdkwin is not None:
            gdkwin.set_keep_above(True)
        _clicks_pass_through(win)
        display = Gdk.Display.get_default()
        if display is not None:
            display.sync()
        # show_all is not painted until the GTK loop runs. Preview waits on
        # the pipeline bus, so paint once here before capture starts.
        context = GLib.MainContext.default()
        paint_until = time.monotonic() + 0.12
        while time.monotonic() < paint_until:
            while context.pending():
                context.iteration(False)
            time.sleep(0.01)
        log_event("damage-window", x=int(x), y=int(y))
        return win
    except Exception as exc:
        log_event("damage-window-failed", err=exc)
        return None


def close_damage_window(win: Gtk.Window | None) -> None:
    if win is None:
        return
    try:
        win.destroy()
    except Exception as exc:
        log_event("damage-window-close-failed", err=exc)


def friendly_audio(name: str) -> str:
    match = HIFI_RE.search(name)
    if not match:
        if name.endswith(".monitor"):
            return name[: -len(".monitor")]
        return name
    tag = match.group(1)
    if tag.lower() == "speaker":
        return "Speakers"
    hdmi = re.fullmatch(r"HDMI(\d+)", tag, re.IGNORECASE)
    if hdmi:
        return f"HDMI {hdmi.group(1)}"
    return tag


def audio_label(name: str, state: str) -> str:
    return f"{friendly_audio(name)}  [{state}]"


def parse_pactl_sources(text: str) -> list[dict]:
    found = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        name = parts[1]
        state = parts[-1]
        if not name.endswith(".monitor"):
            continue
        found.append({"name": name, "state": state, "label": audio_label(name, state)})
    rank = {"RUNNING": 0, "IDLE": 1, "SUSPENDED": 2}
    found.sort(key=lambda s: (rank.get(s["state"], 9), friendly_audio(s["name"])))
    return found


def list_audio() -> list[dict]:
    return parse_pactl_sources(run_text(["pactl", "list", "short", "sources"]))


def default_sink() -> str:
    for line in run_text(["pactl", "info"]).splitlines():
        if line.startswith("Default Sink:"):
            return line.split(":", 1)[1].strip()
    return ""


def default_audio_name(sources: list[dict] | None = None) -> str:
    sources = list_audio() if sources is None else sources
    want = default_sink()
    if want:
        want += ".monitor"
        if any(s["name"] == want for s in sources):
            return want
    return sources[0]["name"] if sources else ""


def list_v4l() -> list[dict]:
    found = []
    for path in sorted(p for p in os.listdir("/dev") if p.startswith("video")):
        dev = "/dev/" + path
        if not os.path.exists(dev):
            continue
        text = run_text(["udevadm", "info", "-q", "property", "-n", dev])
        caps = ""
        model = ""
        for line in text.splitlines():
            if line.startswith("ID_V4L_CAPABILITIES="):
                caps = line.split("=", 1)[1]
            elif line.startswith("ID_V4L_PRODUCT="):
                model = line.split("=", 1)[1]
        if ":capture:" not in caps:
            continue
        model = model.split(":")[0].replace("_", " ").strip() or "Capture card"
        found.append({"id": "v4l:" + dev, "label": f"{model} ({dev})"})
    return found


def video_choices() -> tuple[list[dict], list[dict]]:
    monitors = list_monitors()
    choices = []
    for mon in monitors:
        primary = " · primary" if mon["primary"] else ""
        choices.append(
            {
                "id": "monitor:" + mon["connector"],
                "label": f"{mon['connector']}  {mon['w']}×{mon['h']}{primary}",
            }
        )
    if monitors:
        choices.append({"id": "desktop", "label": "Entire desktop (every display)"})
    choices.append(
        {"id": "portal", "label": "Portal — pick a screen or window when recording starts"}
    )
    choices.extend(list_v4l())
    return choices, monitors


def default_video_id(choices: list[dict], monitors: list[dict]) -> str:
    for mon in monitors:
        if mon["primary"]:
            return "monitor:" + mon["connector"]
    if monitors:
        return "monitor:" + monitors[0]["connector"]
    for choice in choices:
        if choice["id"] == "portal":
            return "portal"
    return choices[0]["id"] if choices else ""


def fresh_name() -> str:
    return time.strftime("recording-%Y%m%d-%H%M%S")


def icon_path() -> str | None:
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "anything-recorder.svg")
    installed = os.path.expanduser("~/.local/share/icons/hicolor/scalable/apps/anything-recorder.svg")
    for path in (here, installed):
        if os.path.isfile(path):
            return path
    return None


def info_pixbuf(title: str, subtitle: str) -> GdkPixbuf.Pixbuf:
    width, height = 640, 180
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    cr = cairo.Context(surface)
    cr.set_source_rgb(0.15, 0.16, 0.18)
    cr.rectangle(0, 0, width, height)
    cr.fill()
    cr.set_source_rgb(0.93, 0.94, 0.95)
    cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    cr.set_font_size(20)
    cr.move_to(24, 78)
    cr.show_text(title)
    cr.set_source_rgb(0.75, 0.78, 0.82)
    cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    cr.set_font_size(14)
    cr.move_to(24, 112)
    cr.show_text(subtitle)
    return Gdk.pixbuf_get_from_surface(surface, 0, 0, width, height)


def gst_error(msg) -> str:
    err, debug = msg.parse_error()
    text = err.message if err else "unknown gstreamer error"
    if debug:
        text = f"{text} ({debug.splitlines()[-1][:240]})"
    return text


class MutterSession:
    """One GNOME screen-cast session for a single connector or a desktop area."""

    def __init__(self) -> None:
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.session = None
        self.stream_path = ""
        self.node = None
        self._sub = 0
        self._loop = None

    def _ensure(self) -> None:
        if self.session is not None:
            return
        proxy = Gio.DBusProxy.new_sync(
            self.bus,
            Gio.DBusProxyFlags.NONE,
            None,
            "org.gnome.Mutter.ScreenCast",
            "/org/gnome/Mutter/ScreenCast",
            "org.gnome.Mutter.ScreenCast",
            None,
        )
        path = proxy.call_sync(
            "CreateSession",
            GLib.Variant("(a{sv})", ({},)),
            Gio.DBusCallFlags.NONE,
            8000,
            None,
        ).unpack()[0]
        self.session = Gio.DBusProxy.new_sync(
            self.bus,
            Gio.DBusProxyFlags.NONE,
            None,
            "org.gnome.Mutter.ScreenCast",
            path,
            "org.gnome.Mutter.ScreenCast.Session",
            None,
        )

    def _listen(self) -> None:
        if self._sub:
            return
        self._sub = self.bus.signal_subscribe(
            "org.gnome.Mutter.ScreenCast",
            "org.gnome.Mutter.ScreenCast.Stream",
            "PipeWireStreamAdded",
            self.stream_path,
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_added,
            None,
        )

    def _on_added(self, _conn, _sender, _path, _iface, _signal, params, _data) -> None:
        self.node = int(params.unpack()[0])
        log_event("mutter-node", stream=self.stream_path, node=self.node)
        log_pipewire_node(self.node, "stream-added")
        if self._loop is not None and self._loop.is_running():
            self._loop.quit()

    def _start_stream(self) -> None:
        self._listen()
        self.session.call_sync("Start", None, Gio.DBusCallFlags.NONE, 8000, None)

    def open_monitor(self, connector: str) -> None:
        self._ensure()
        props = {"cursor-mode": GLib.Variant("u", 1)}
        self.stream_path = self.session.call_sync(
            "RecordMonitor",
            GLib.Variant("(sa{sv})", (connector, props)),
            Gio.DBusCallFlags.NONE,
            8000,
            None,
        ).unpack()[0]
        log_event("mutter-open", kind="monitor", connector=connector, stream=self.stream_path)
        self._start_stream()

    def open_area(self, x: int, y: int, width: int, height: int) -> None:
        self._ensure()
        props = {"cursor-mode": GLib.Variant("u", 1)}
        self.stream_path = self.session.call_sync(
            "RecordArea",
            GLib.Variant("(iiiia{sv})", (int(x), int(y), int(width), int(height), props)),
            Gio.DBusCallFlags.NONE,
            8000,
            None,
        ).unpack()[0]
        log_event("mutter-open", kind="area", x=x, y=y, w=width, h=height, stream=self.stream_path)
        self._start_stream()

    def wait_node(self, timeout: float = 4.0) -> int:
        if self.node is not None:
            return self.node
        self._loop = GLib.MainLoop()
        GLib.timeout_add(int(timeout * 1000), self._quit_wait)
        self._loop.run()
        self._loop = None
        if self.node is None:
            raise RuntimeError("The display did not produce a capture stream.")
        return int(self.node)

    def _quit_wait(self) -> bool:
        if self._loop is not None and self._loop.is_running():
            self._loop.quit()
        return False

    def close(self) -> None:
        if self._sub:
            self.bus.signal_unsubscribe(self._sub)
            self._sub = 0
        if self.session is not None:
            try:
                self.session.call_sync("Stop", None, Gio.DBusCallFlags.NONE, 3000, None)
            except GLib.Error:
                pass
        self.session = None
        self.node = None


_MUTTER_OK: bool | None = None


def pipewire_serial(node_id: int, timeout: float = 1.5) -> str:
    """Return the object serial WirePlumber uses to link this node.

    Mutter reports a node id. PipeWire recycles that id, and a numeric
    target.object is matched as object.serial, not as the node id.
    """
    deadline = time.monotonic() + timeout
    while True:
        serial = ""
        try:
            raw = subprocess.check_output(["pw-dump"], text=True, timeout=3)
            for obj in json.loads(raw):
                if obj.get("id") != int(node_id) or "Node" not in str(obj.get("type")):
                    continue
                serial = str((obj.get("info") or {}).get("props", {}).get("object.serial") or "")
                break
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            serial = ""
        if serial:
            log_event("pw-serial", node=node_id, serial=serial)
            return serial
        if time.monotonic() >= deadline:
            log_event("pw-serial-missing", node=node_id)
            return ""
        time.sleep(0.05)


def _capture_client(prefix: str) -> str:
    """Unique pipewiresrc node name so a leftover stream is not linked."""
    return f"{prefix}{os.getpid()}-{time.time_ns() % 1_000_000_000}"


def _screencast_ports(node_id: int, client_name: str) -> tuple[int | None, int | None]:
    """Return the Mutter output port id and the pipewiresrc input port id."""
    consumers: list[int] = []
    ports = []
    for obj in _pw_dump():
        kind = str(obj.get("type") or "")
        if kind.endswith("Node"):
            props = (obj.get("info") or {}).get("props") or {}
            if str(props.get("node.name") or "") == client_name:
                try:
                    consumers.append(int(obj["id"]))
                except (KeyError, TypeError, ValueError):
                    pass
        elif kind.endswith("Port"):
            ports.append(obj)
    consumer = max(consumers) if consumers else None
    out_id = None
    in_id = None
    for obj in ports:
        info = obj.get("info") or {}
        props = info.get("props") or {}
        try:
            owner = int(props.get("node.id"))
            pid = int(obj["id"])
        except (TypeError, ValueError):
            continue
        direction = str(info.get("direction") or "").lower()
        if owner == int(node_id) and direction == "output" and out_id is None:
            out_id = pid
        if consumer is not None and owner == consumer and direction == "input" and in_id is None:
            in_id = pid
    return out_id, in_id


def link_screencast(node_id: int, client_name: str, timeout: float = 2.0) -> None:
    """Connect the Mutter output to pipewiresrc with pw-link.

    target-object matches object.serial. On this PipeWire that lookup often
    fails with "stream error: target not found", and the pipeline then sits
    in PLAYING with zero buffers. Linking the two port ids delivers frames.
    "File exists" means the ports are already connected.
    """
    deadline = time.monotonic() + timeout
    last = "The display capture ports were not ready."
    last_log = 0.0
    while True:
        out_id, in_id = _screencast_ports(node_id, client_name)
        if out_id is not None and in_id is not None:
            try:
                proc = subprocess.run(
                    ["pw-link", str(out_id), str(in_id)],
                    text=True,
                    capture_output=True,
                    timeout=2,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                last = str(exc)
                log_event("pw-link", node=node_id, client=client_name, out=out_id, inp=in_id, err=exc)
            else:
                err = ((proc.stderr or "") + " " + (proc.stdout or "")).strip()
                log_event(
                    "pw-link",
                    node=node_id,
                    client=client_name,
                    out=out_id,
                    inp=in_id,
                    code=proc.returncode,
                    err=err,
                )
                if proc.returncode == 0 or "file exists" in err.lower():
                    return
                last = err or f"pw-link exited {proc.returncode}"
        else:
            now = time.monotonic()
            if now - last_log > 0.4:
                log_event(
                    "pw-link-wait",
                    node=node_id,
                    client=client_name,
                    out="" if out_id is None else out_id,
                    inp="" if in_id is None else in_id,
                )
                last_log = now
        if time.monotonic() >= deadline:
            log_event("pw-link-failed", node=node_id, client=client_name, err=last)
            raise RuntimeError(last)
        time.sleep(0.05)


def play_and_link(pipe, node_id: int, client_name: str, timeout: float = 2.0, fail_message: str = "Could not start the capture pipeline.") -> None:
    """Start the pipeline and link video on another thread.

    A video-only pipeline blocks inside set_state(PLAYING) until the first
    frame. The link has to be made from a second thread, or that wait never
    ends. If the state change is still blocked after the link attempt, it
    is aborted so the window does not stay frozen.
    """
    box: dict = {"error": None, "done": threading.Event()}

    def do_link() -> None:
        try:
            link_screencast(node_id, client_name, timeout=timeout)
        except Exception as exc:
            box["error"] = exc
        finally:
            box["done"].set()

    state_done = threading.Event()

    def abort() -> None:
        if state_done.wait(timeout + 1.5):
            return
        log_event("pipe-state-abort", node=node_id, client=client_name)
        try:
            pipe.set_state(Gst.State.NULL)
        except Exception as exc:
            log_event("pipe-state-abort-failed", err=exc)

    threading.Thread(target=do_link, daemon=True).start()
    threading.Thread(target=abort, daemon=True).start()
    try:
        ret = pipe.set_state(Gst.State.PLAYING)
    finally:
        state_done.set()
    log_event("pipe-playing", ret=ret.value_nick if hasattr(ret, "value_nick") else str(ret), client=client_name)
    if not box["done"].wait(0.3):
        box["done"].wait(timeout)
    if box["error"] is not None:
        pipe.set_state(Gst.State.NULL)
        raise box["error"]
    if not box["done"].is_set():
        pipe.set_state(Gst.State.NULL)
        raise RuntimeError("The display capture stream did not link in time.")
    if ret == Gst.StateChangeReturn.FAILURE:
        pipe.set_state(Gst.State.NULL)
        raise RuntimeError(fail_message)


def mutter_available() -> bool:
    global _MUTTER_OK
    if _MUTTER_OK is not None:
        return _MUTTER_OK
    try:
        session = MutterSession()
        session._ensure()
        session.close()
        _MUTTER_OK = True
    except (GLib.Error, RuntimeError, OSError):
        _MUTTER_OK = False
    return _MUTTER_OK


class PortalSession:
    def __init__(self) -> None:
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.sender = self.bus.get_unique_name()[1:].replace(".", "_")
        self.proxy = Gio.DBusProxy.new_sync(
            self.bus,
            Gio.DBusProxyFlags.NONE,
            None,
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.ScreenCast",
            None,
        )
        self.session_path = ""
        self.fd = None
        self.restore_token = ""
        self._n = 0

    def _token(self) -> str:
        self._n += 1
        return f"ar{os.getpid()}{self._n}{time.time_ns() % 1000000000}"

    def _request(self, method: str, token: str, variant: GLib.Variant):
        path = f"/org/freedesktop/portal/desktop/request/{self.sender}/{token}"
        done: dict = {}
        loop = GLib.MainLoop()

        def on_response(_c, _s, _p, _i, _sig, params, _data) -> None:
            response, results = params.unpack()
            done["response"] = response
            done["results"] = results
            if loop.is_running():
                loop.quit()

        sub = self.bus.signal_subscribe(
            "org.freedesktop.portal.Desktop",
            "org.freedesktop.portal.Request",
            "Response",
            path,
            None,
            Gio.DBusSignalFlags.NO_MATCH_RULE,
            on_response,
            None,
        )
        try:
            self.proxy.call_sync(method, variant, Gio.DBusCallFlags.NONE, 180000, None)
            if "response" not in done:
                GLib.timeout_add_seconds(180, lambda: loop.quit() or False)
                loop.run()
        finally:
            self.bus.signal_unsubscribe(sub)
        if "response" not in done:
            raise RuntimeError("The screen portal timed out.")
        if done["response"] == 1:
            raise Cancelled("Screen choice was cancelled.")
        if done["response"] != 0:
            raise RuntimeError(f"The screen portal failed (code {done['response']}).")
        return done["results"]

    def start(self, restore_token: str = "") -> tuple[int, int]:
        token = self._token()
        session_token = self._token()
        created = self._request(
            "CreateSession",
            token,
            GLib.Variant(
                "(a{sv})",
                (
                    {
                        "handle_token": GLib.Variant("s", token),
                        "session_handle_token": GLib.Variant("s", session_token),
                    },
                ),
            ),
        )
        self.session_path = created["session_handle"]
        select_token = self._token()
        options = {
            "handle_token": GLib.Variant("s", select_token),
            "types": GLib.Variant("u", 1 | 2),
            "multiple": GLib.Variant("b", False),
            "cursor_mode": GLib.Variant("u", 2),
            "persist_mode": GLib.Variant("u", 2),
        }
        if restore_token:
            options["restore_token"] = GLib.Variant("s", restore_token)
        self._request(
            "SelectSources",
            select_token,
            GLib.Variant("(oa{sv})", (self.session_path, options)),
        )
        start_token = self._token()
        started = self._request(
            "Start",
            start_token,
            GLib.Variant(
                "(osa{sv})",
                (self.session_path, "", {"handle_token": GLib.Variant("s", start_token)}),
            ),
        )
        self.restore_token = str(started.get("restore_token") or "")
        streams = started.get("streams") or []
        if not streams:
            raise RuntimeError("The portal did not share a screen.")
        node = int(streams[0][0])
        result, fd_list = self.proxy.call_with_unix_fd_list_sync(
            "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (self.session_path, {})),
            Gio.DBusCallFlags.NONE,
            30000,
            None,
            None,
        )
        del result
        if fd_list is None or fd_list.get_length() < 1:
            raise RuntimeError("The portal did not return a PipeWire connection.")
        self.fd = os.dup(fd_list.get(0))
        return node, self.fd

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        if not self.session_path:
            return
        try:
            sess = Gio.DBusProxy.new_sync(
                self.bus,
                Gio.DBusProxyFlags.NONE,
                None,
                "org.freedesktop.portal.Desktop",
                self.session_path,
                "org.freedesktop.portal.Session",
                None,
            )
            sess.call_sync("Close", None, Gio.DBusCallFlags.NONE, 3000, None)
        except GLib.Error:
            pass
        self.session_path = ""


class Engine:
    def __init__(self) -> None:
        self.pipe = None
        self.proc = None
        self.mutter = None
        self.portal = None
        self._damage = None
        self.running = False
        self.backend = ""
        self.on_finished = None
        self.on_error = None
        self._closed = False
        self._shutdown_started = False
        self._reader = None
        self.path = ""

    def start(self, video_id: str, audio: str, path: str) -> None:
        if self.running:
            raise RuntimeError("Already recording.")
        if not audio:
            raise RuntimeError("Select an audio source.")
        if not video_id:
            raise RuntimeError("Select a display.")
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._closed = False
        self._shutdown_started = False
        self.path = path
        self.running = False
        _rotate_debug_log()
        _install_gst_log()
        log_event(
            "record-start",
            video=video_id,
            audio=audio,
            path=path,
            monitors="; ".join(
                f"{m['connector']} {m['w']}x{m['h']}+{m['x']}+{m['y']}" for m in list_monitors()
            ),
        )
        if video_id.startswith("v4l:"):
            self._start_pipeline(path, audio, v4l=video_id.split(":", 1)[1])
            self.backend = "camera"
        elif video_id == "portal":
            if have("gpu-screen-recorder"):
                self._start_gsr("portal", audio, path)
            else:
                self._start_portal(audio, path)
        elif video_id == "desktop" or video_id.startswith("monitor:"):
            if have("gpu-screen-recorder"):
                target = "screen" if video_id == "desktop" else video_id.split(":", 1)[1]
                self._start_gsr(target, audio, path)
            elif mutter_available():
                self._start_mutter(video_id, audio, path)
            elif not is_wayland():
                self._start_x11(video_id, audio, path)
            else:
                raise RuntimeError(
                    "This Wayland session has no per-display capture. "
                    "Choose Portal, or install gpu-screen-recorder."
                )
        else:
            raise RuntimeError(f"Unknown video source: {video_id}")
        if not self._closed:
            self.running = True

    def _close_damage(self) -> None:
        win = self._damage
        self._damage = None
        close_damage_window(win)

    def _start_mutter(self, video_id: str, audio: str, path: str) -> None:
        monitors = list_monitors()
        origin = damage_origin(video_id)
        if origin is not None:
            self._damage = open_damage_window(*origin)
        error = "The display did not deliver video."
        try:
            self._mutter_attempts(video_id, audio, path, monitors, error)
        except Exception:
            self._close_damage()
            raise

    def _mutter_attempts(self, video_id: str, audio: str, path: str, monitors: list, error: str) -> None:
        for attempt in range(3):
            session = MutterSession()
            self.mutter = session
            try:
                if video_id == "desktop":
                    if not monitors:
                        raise RuntimeError("No displays are connected.")
                    x, y, w, h = monitor_bounds(monitors)
                    session.open_area(x, y, w, h)
                    self.backend = "gnome-desktop"
                else:
                    connector = video_id.split(":", 1)[1]
                    if monitors and not any(m["connector"] == connector for m in monitors):
                        raise RuntimeError(f"{connector} is not connected. Refresh the display list.")
                    session.open_monitor(connector)
                    self.backend = "gnome"
                node = session.wait_node()
                self._start_pipeline(path, audio, node=node, watch=False)
                # The cast often sends its first frame late, after Mutter
                # has inhibited the hardware cursor and repainted. One real
                # frame means the link is up; later frames follow damage.
                error = self._video_flowing(2.5) or ""
                log_event("mutter-attempt", n=attempt + 1, video=video_id, node=node, error=error or "frames")
                if not error:
                    self._arm_bus()
                    return
            except Exception as exc:
                error = str(exc)
                log_event("mutter-attempt-failed", n=attempt + 1, video=video_id, error=exc)
            self._discard_pipe()
            session.close()
            self.mutter = None
        raise RuntimeError(error or "The display did not deliver video.")

    def _start_portal(self, audio: str, path: str) -> None:
        portal = PortalSession()
        self.portal = portal
        try:
            node, fd = portal.start(load_config().get("restore_token", ""))
            if portal.restore_token:
                cfg = load_config()
                cfg["restore_token"] = portal.restore_token
                save_config(cfg)
            self._start_pipeline(path, audio, node=node, fd=fd)
            self.backend = "portal"
        except Exception:
            portal.close()
            self.portal = None
            raise

    def _start_pipeline(self, path: str, audio: str, node: int | None = None, fd: int | None = None, v4l: str | None = None, watch: bool = True) -> None:
        if v4l:
            # Cameras advertise a real frame rate. Locking that rate is fine.
            video = (
                "v4l2src name=vsrc do-timestamp=true ! videoconvert ! videorate ! "
                "video/x-raw,format=I420,framerate=30/1"
            )
        else:
            # Screen casts advertise framerate 0/1. Forcing 30 fps with
            # videorate waits forever for a second frame and Stop never returns.
            video = (
                "pipewiresrc name=vsrc do-timestamp=true ! "
                "videoconvert ! video/x-raw,format=I420"
            )
        desc = (
            f"{video} ! "
            "x264enc tune=zerolatency speed-preset=veryfast bitrate=12000 key-int-max=60 ! "
            "video/x-h264,stream-format=avc,alignment=au ! "
            "queue max-size-buffers=4 ! mux. "
            "pulsesrc name=asrc ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=2 ! "
            "avenc_aac bitrate=192000 ! aacparse ! queue ! mux. "
            "mp4mux name=mux fragment-duration=500 ! filesink name=sink sync=false async=false"
        )
        pipe = Gst.parse_launch(desc)
        pipe.get_by_name("asrc").set_property("device", audio)
        pipe.get_by_name("sink").set_property("location", path)
        link_client = ""
        if v4l:
            pipe.get_by_name("vsrc").set_property("device", v4l)
        else:
            src = pipe.get_by_name("vsrc")
            # Copy DMA-BUF frames so the encoder can hold them, and do not
            # offer a PipeWire clock: the pipeline stays paused until a
            # buffer arrives, and that clock never starts.
            src.set_property("always-copy", True)
            src.set_property("provide-clock", False)
            # Mutter pushes a buffer only when that monitor is repainted.
            # A still picture, or a video plane that does not damage the
            # stage, would leave the muxer holding one frame and the file
            # empty. Repeat the newest frame until the next real one.
            src.set_property("keepalive-time", 200)
            if fd is not None and fd >= 0:
                # The portal hands back a private PipeWire socket. On that
                # socket the node id is the connect target.
                src.set_property("fd", int(fd))
                src.set_property("path", str(int(node)))
                log_event("pipe-target", mode="portal-fd", node=node, fd=fd, path_prop=str(int(node)))
            else:
                link_client = _capture_client("ar")
                src.set_property("client-name", link_client)
                src.set_property("autoconnect", False)
                log_event(
                    "pipe-target",
                    mode="pw-link",
                    node=node,
                    client=link_client,
                    readback=src.get_property("client-name"),
                )
                log_pipewire_node(int(node), "before-playing")
        # Count frames from the first buffer. Installing the probe after
        # PLAYING misses the preroll, and a quiet display then looks dead.
        self._frames = {"n": 0}
        if not v4l:
            def on_buf(pad, info):
                buf = info.get_buffer()
                n = self._frames["n"]
                self._frames["n"] = n + 1
                if n == 0 and buf is not None:
                    caps = pad.get_current_caps()
                    log_event(
                        "first-buffer",
                        pts=buf.pts,
                        size=buf.get_size(),
                        caps=caps.to_string() if caps is not None else "",
                        sample=_buffer_sample(buf),
                    )
                return Gst.PadProbeReturn.OK

            pipe.get_by_name("vsrc").get_static_pad("src").add_probe(
                Gst.PadProbeType.BUFFER, on_buf
            )
        self.pipe = pipe
        try:
            if link_client:
                play_and_link(
                    pipe,
                    int(node),
                    link_client,
                    timeout=2.0,
                    fail_message="Could not start the capture pipeline. Check the audio source.",
                )
            else:
                ret = pipe.set_state(Gst.State.PLAYING)
                log_event("pipe-playing", ret=ret.value_nick if hasattr(ret, "value_nick") else str(ret))
                if ret == Gst.StateChangeReturn.FAILURE:
                    raise RuntimeError("Could not start the capture pipeline. Check the audio source.")
        except Exception:
            self.pipe = None
            pipe.set_state(Gst.State.NULL)
            raise
        if watch:
            self._arm_bus()

    def _arm_bus(self) -> None:
        if self.pipe is None:
            return
        bus = self.pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus)

    def _discard_pipe(self) -> None:
        pipe = self.pipe
        self.pipe = None
        if pipe is not None:
            pipe.set_state(Gst.State.NULL)
        if self.path and os.path.isfile(self.path) and os.path.getsize(self.path) < 8192:
            try:
                os.remove(self.path)
            except OSError:
                pass

    def _video_flowing(self, timeout: float) -> str | None:
        """Wait until one video frame arrives. None means a frame arrived.

        Mutter sends frames when the monitor is repainted. A hardware video
        plane, or a display that is sitting still, can take more than a
        second to deliver the first one, then continue. Requiring several
        frames inside a short window rejected those streams.
        """
        if self.pipe is None:
            return "Capture pipeline did not start."
        got = self._frames
        bus = self.pipe.get_bus()
        deadline = time.monotonic() + timeout
        context = GLib.MainContext.default()
        while time.monotonic() < deadline:
            msg = bus.pop_filtered(Gst.MessageType.ERROR)
            if msg is not None:
                text = gst_error(msg)
                log_event("video-error", buffers=got["n"], msg=text)
                return text
            if got["n"] >= 1:
                log_event("video-flowing", buffers=got["n"])
                return None
            context.iteration(False)
            time.sleep(0.02)
        log_event("video-stalled", buffers=got["n"], waited=timeout)
        if got["n"] >= 1:
            return None
        return "The display did not deliver video."

    def _start_x11(self, video_id: str, audio: str, path: str) -> None:
        monitors = list_monitors()
        if video_id == "desktop":
            if not monitors:
                raise RuntimeError("No displays are connected.")
            x, y, w, h = monitor_bounds(monitors)
        else:
            connector = video_id.split(":", 1)[1]
            match = next((m for m in monitors if m["connector"] == connector), None)
            if match is None:
                raise RuntimeError(f"{connector} is not connected.")
            x, y, w, h = match["x"], match["y"], match["w"], match["h"]
        display = os.environ.get("DISPLAY") or ":0"
        base = display if re.search(r"\.\d+$", display) else display + ".0"
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
            "-f", "x11grab", "-framerate", "30", "-video_size", f"{w}x{h}",
            "-i", f"{base}+{x},{y}",
            "-f", "pulse", "-i", audio,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            path,
        ]
        self._spawn(cmd)
        self.backend = "ffmpeg"

    def _start_gsr(self, target: str, audio: str, path: str) -> None:
        cmd = [
            "gpu-screen-recorder", "-w", target, "-f", "30",
            "-a", f"device:{audio}", "-o", path,
        ]
        self._spawn(cmd)
        self.backend = "gpu-screen-recorder"

    def _spawn(self, cmd: list[str]) -> None:
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        except FileNotFoundError as exc:
            raise RuntimeError(f"Could not start {cmd[0]}.") from exc

        def reader() -> None:
            err = ""
            if self.proc and self.proc.stderr:
                err = self.proc.stderr.read()
            code = self.proc.wait() if self.proc else 1
            GLib.idle_add(self._spawn_done, code, err)

        self._reader = threading.Thread(target=reader, daemon=True)
        self._reader.start()

    def _spawn_done(self, code: int, err: str) -> bool:
        if self._closed:
            return False
        tail = ""
        if err:
            lines = [ln.strip() for ln in err.splitlines() if ln.strip()]
            tail = lines[-1] if lines else ""
        if code == 0:
            self._finish(True, "Saved.")
        else:
            detail = f": {tail}" if tail else ""
            self._finish(False, f"{self.backend or 'recorder'} exited ({code}){detail}")
        return False

    def _on_bus(self, _bus, msg) -> None:
        if self._closed or self._shutdown_started or self.pipe is None:
            return
        if msg.type == Gst.MessageType.ERROR:
            self._finish(False, gst_error(msg))
        elif msg.type == Gst.MessageType.EOS:
            self._finish(True, "Saved.")

    def request_stop(self) -> None:
        if self._closed:
            return
        if self.pipe is not None:
            # send_event(EOS) blocks until every source returns. A display
            # that is not delivering frames never returns, and the call was
            # on the UI thread, so GNOME reported the window as not responding.
            self._shutdown_async(True, "Saved.")
            return
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)

    def _shutdown_async(self, ok: bool, message: str) -> None:
        if self._shutdown_started or self.pipe is None:
            return
        self._shutdown_started = True
        pipe = self.pipe

        def work() -> None:
            try:
                # EOS on the source pads flushes the muxer. Sending it to
                # the pipeline instead waits on the live sources and the
                # Stop button never returns.
                for name in ("vsrc", "asrc"):
                    element = pipe.get_by_name(name)
                    if element is None:
                        continue
                    pad = element.get_static_pad("src")
                    if pad is not None:
                        pad.push_event(Gst.Event.new_eos())
                bus = pipe.get_bus()
                if bus is not None:
                    bus.timed_pop_filtered(
                        2 * Gst.SECOND,
                        Gst.MessageType.EOS | Gst.MessageType.ERROR,
                    )
            except Exception:
                pass
            try:
                pipe.set_state(Gst.State.NULL)
            except Exception:
                pass

            def deliver() -> bool:
                self._finish(ok, message)
                return False

            GLib.idle_add(deliver)

        threading.Thread(target=work, daemon=True).start()

    def _finish(self, ok: bool, message: str) -> None:
        if self._closed:
            return
        self._closed = True
        self.running = False
        pipe = self.pipe
        self.pipe = None
        if pipe is not None and not self._shutdown_started:
            pipe.set_state(Gst.State.NULL)
        if self.mutter is not None:
            self.mutter.close()
            self.mutter = None
        if self.portal is not None:
            self.portal.close()
            self.portal = None
        self._close_damage()
        size = os.path.getsize(self.path) if self.path and os.path.isfile(self.path) else 0
        log_event("record-finish", ok=ok, bytes=size, message=message, backend=self.backend)
        if ok and size < 8192:
            ok = False
            message = "The recording file is empty. The display did not deliver video."
        cb = self.on_finished if ok else self.on_error
        if cb:
            cb(message)

    def force_close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
        if self.pipe is not None and not self._shutdown_started:
            self._shutdown_async(False, "Stopped.")
            return
        self._finish(False, "Stopped.")


def grab_still(video_id: str, dest: str) -> None:
    """Write one preview frame for a monitor, the whole desktop, or a camera."""
    session = None
    pipe = None
    damage = None
    origin = damage_origin(video_id)
    if origin is not None:
        damage = open_damage_window(*origin)
    try:
        if video_id.startswith("v4l:"):
            pipe = Gst.parse_launch(
                "v4l2src name=src num-buffers=1 ! videoconvert ! pngenc ! filesink name=sink"
            )
            pipe.get_by_name("src").set_property("device", video_id.split(":", 1)[1])
        elif video_id == "desktop" or video_id.startswith("monitor:"):
            session = MutterSession()
            if video_id == "desktop":
                monitors = list_monitors()
                if not monitors:
                    raise RuntimeError("No displays are connected.")
                x, y, w, h = monitor_bounds(monitors)
                session.open_area(x, y, w, h)
            else:
                session.open_monitor(video_id.split(":", 1)[1])
            node = session.wait_node()
            client = _capture_client("ap")
            pipe = Gst.parse_launch(
                "pipewiresrc name=src num-buffers=1 do-timestamp=true ! "
                "videoconvert ! pngenc ! filesink name=sink"
            )
            src = pipe.get_by_name("src")
            src.set_property("client-name", client)
            src.set_property("autoconnect", False)
            src.set_property("always-copy", True)
            src.set_property("provide-clock", False)
            # Same as recording: a monitor that is not being repainted
            # never pushes the single preview buffer on its own.
            src.set_property("keepalive-time", 200)
            log_event("preview-link", node=node, client=client)
        else:
            raise RuntimeError("No still preview for this source.")
        pipe.get_by_name("sink").set_property("location", dest)
        if video_id == "desktop" or video_id.startswith("monitor:"):
            play_and_link(pipe, node, client, timeout=4.0, fail_message="Preview pipeline failed to start.")
        elif pipe.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Preview pipeline failed to start.")
        # Poll the bus so the damage window can paint. A blocking pop
        # freezes GTK and Mutter never composites the monitor.
        bus = pipe.get_bus()
        context = GLib.MainContext.default()
        deadline = time.monotonic() + 5
        msg = None
        while time.monotonic() < deadline:
            msg = bus.pop_filtered(Gst.MessageType.EOS | Gst.MessageType.ERROR)
            if msg is not None:
                break
            while context.pending():
                context.iteration(False)
            time.sleep(0.02)
        if msg is None:
            raise RuntimeError("Preview timed out.")
        if msg.type == Gst.MessageType.ERROR:
            raise RuntimeError(gst_error(msg))
    finally:
        close_damage_window(damage)
        if pipe is not None:
            pipe.set_state(Gst.State.NULL)
        if session is not None:
            session.close()


class LevelMeter:
    def __init__(self, on_level) -> None:
        self.on_level = on_level
        self.pipe = None

    def set_device(self, device: str) -> None:
        self.stop()
        if not device:
            return
        pipe = Gst.parse_launch(
            "pulsesrc name=src ! audioconvert ! audio/x-raw,channels=1,rate=8000 ! "
            "level interval=80000000 post-messages=true ! fakesink sync=false"
        )
        pipe.get_by_name("src").set_property("device", device)
        bus = pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_msg)
        if pipe.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            pipe.set_state(Gst.State.NULL)
            return
        self.pipe = pipe

    def _on_msg(self, _bus, msg) -> None:
        if msg.type != Gst.MessageType.ELEMENT:
            return
        struct = msg.get_structure()
        if struct is None or struct.get_name() != "level":
            return
        rms = struct.get_value("rms")
        if not rms:
            return
        db = float(rms[0])
        if db != db:
            return
        frac = max(0.0, min(1.0, (db + 50.0) / 50.0))
        self.on_level(frac)

    def stop(self) -> None:
        if self.pipe is not None:
            self.pipe.set_state(Gst.State.NULL)
            self.pipe = None


class RecorderWindow:
    def __init__(self, app: Gtk.Application | None, *, live: bool = True) -> None:
        self.app = app
        self.live = live
        self.engine = Engine()
        self.engine.on_finished = self._on_saved
        self.engine.on_error = self._on_failed
        self.meter = LevelMeter(self._on_level)
        self._loading = False
        self._busy = False
        self.compact = False
        self._saved_size = (760, 720)
        self._started = 0.0
        self._tick = 0
        self._monitor_sig = ""

        if app is not None:
            self.window = Gtk.ApplicationWindow(application=app)
        else:
            self.window = Gtk.Window()
        self.window.set_title(APP_NAME)
        self.window.set_default_size(760, 720)
        self.window.set_position(Gtk.WindowPosition.CENTER)
        icon = icon_path()
        if icon:
            try:
                self.window.set_icon_from_file(icon)
            except GLib.Error:
                pass

        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.set_title(APP_NAME)
        header.set_subtitle("One display + one audio source")
        self.window.set_titlebar(header)

        self.top_switch = Gtk.Switch()
        self.top_switch.set_valign(Gtk.Align.CENTER)
        self.top_switch.set_tooltip_text("Keep this window above other windows")
        top_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        top_box.pack_start(Gtk.Label(label="Always on top"), False, False, 0)
        top_box.pack_start(self.top_switch, False, False, 0)
        self.min_btn = Gtk.Button.new_with_label("Minimise")
        self.min_btn.set_tooltip_text("Shrink to a small bar that stays on screen")
        self.restore_btn = Gtk.Button.new_with_label("Restore")
        self.restore_btn.set_tooltip_text("Show the full window. If it is on the dock, this brings it back.")
        self.restore_btn.set_sensitive(False)
        header.pack_end(self.restore_btn)
        header.pack_end(self.min_btn)
        header.pack_end(top_box)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.set_border_width(12)
        self.window.add(outer)

        self.hint = Gtk.Label()
        self.hint.set_xalign(0)
        self.hint.set_line_wrap(True)
        self.hint.set_max_width_chars(70)
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.main_box.pack_start(self.hint, False, False, 0)

        self.video_combo = Gtk.ComboBoxText()
        self.video_combo.set_hexpand(True)
        self.video_combo.set_tooltip_text("Which display to record. This does not change the audio.")
        video_refresh = Gtk.Button.new_with_label("Refresh")
        video_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        video_row.pack_start(Gtk.Label(label="Display:"), False, False, 0)
        video_row.pack_start(self.video_combo, True, True, 0)
        video_row.pack_start(video_refresh, False, False, 0)
        self.main_box.pack_start(video_row, False, False, 0)

        self.audio_combo = Gtk.ComboBoxText()
        self.audio_combo.set_hexpand(True)
        self.audio_combo.set_tooltip_text("Which audio monitor to record. Independent of the display.")
        audio_refresh = Gtk.Button.new_with_label("Refresh")
        audio_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        audio_row.pack_start(Gtk.Label(label="Audio:"), False, False, 0)
        audio_row.pack_start(self.audio_combo, True, True, 0)
        audio_row.pack_start(audio_refresh, False, False, 0)
        self.main_box.pack_start(audio_row, False, False, 0)

        self.level = Gtk.LevelBar()
        self.level.set_mode(Gtk.LevelBarMode.CONTINUOUS)
        self.level.set_min_value(0)
        self.level.set_max_value(1)
        self.level.set_value(0)
        self.level.set_size_request(-1, 16)
        level_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        level_row.pack_start(Gtk.Label(label="Level:"), False, False, 0)
        level_row.pack_start(self.level, True, True, 0)
        self.main_box.pack_start(level_row, False, False, 0)

        frame = Gtk.Frame(label="Settings")
        grid = Gtk.Grid()
        grid.set_row_spacing(6)
        grid.set_column_spacing(8)
        grid.set_border_width(8)
        frame.add(grid)
        self.folder_btn = Gtk.FileChooserButton.new("Save folder", Gtk.FileChooserAction.SELECT_FOLDER)
        self.folder_btn.set_hexpand(True)
        grid.attach(Gtk.Label(label="Folder:", xalign=0), 0, 0, 1, 1)
        grid.attach(self.folder_btn, 1, 0, 1, 1)
        self.name_entry = Gtk.Entry()
        self.name_entry.set_hexpand(True)
        self.name_entry.set_text(fresh_name())
        grid.attach(Gtk.Label(label="Name:", xalign=0), 0, 1, 1, 1)
        grid.attach(self.name_entry, 1, 1, 1, 1)
        grid.attach(Gtk.Label(label=".mp4"), 2, 1, 1, 1)
        self.main_box.pack_start(frame, False, False, 0)

        self.preview_frame = Gtk.Frame(label="Preview")
        self.preview = Gtk.Image()
        self.preview.set_size_request(640, 180)
        self.preview_frame.add(self.preview)
        self.main_box.pack_start(self.preview_frame, True, True, 0)
        outer.pack_start(self.main_box, True, True, 0)

        self.record_btn = Gtk.Button.new_with_label("Record")
        self.record_btn.set_size_request(-1, 42)
        self.record_btn.get_style_context().add_class("suggested-action")
        outer.pack_start(self.record_btn, False, False, 0)

        self.status = Gtk.Label(label="Starting…")
        self.status.set_xalign(0)
        self.status.set_line_wrap(True)
        self.status.set_max_width_chars(70)
        outer.pack_start(self.status, False, False, 0)

        self._apply_saved()
        self.reload_video()
        self.reload_audio()
        self._apply_top()
        self.update_hint()

        self.top_switch.connect("notify::active", self._on_top_toggled)
        self.window.connect("realize", lambda *_: self._apply_top())
        self.window.connect("map", lambda *_: self._apply_top())
        self.min_btn.connect("clicked", lambda *_: self.minimise())
        self.restore_btn.connect("clicked", lambda *_: self.restore())
        video_refresh.connect("clicked", lambda *_: self.reload_video())
        audio_refresh.connect("clicked", lambda *_: self.reload_audio())
        self.video_combo.connect("changed", self._on_video_changed)
        self.audio_combo.connect("changed", self._on_audio_changed)
        self.folder_btn.connect("file-set", lambda *_: self.save_ui())
        self.record_btn.connect("clicked", lambda *_: self.toggle_record())
        self.window.connect("delete-event", self._on_delete)

        if live:
            self._monitor_sig = self._signature()
            GLib.timeout_add_seconds(3, self._poll_monitors)
            GLib.idle_add(self.update_preview)
            audio = self.video_audio()[1]
            if audio:
                self.meter.set_device(audio)
            self.save_ui()

    def _apply_saved(self) -> None:
        cfg = load_config()
        folder = cfg.get("folder") or ""
        if not folder or not os.path.isdir(folder):
            videos = os.path.join(os.path.expanduser("~"), "Videos")
            folder = videos if os.path.isdir(videos) else os.path.expanduser("~")
        self.folder_btn.set_filename(folder)
        top = cfg.get("always_on_top", "true").strip().lower()
        self.top_switch.set_active(top not in ("0", "false", "no", "off"))

    def _apply_top(self) -> None:
        # Before the Gdk window exists this is a no-op, so realize and map
        # call it again. On XWayland that sets _NET_WM_STATE_ABOVE.
        above = self.top_switch.get_active()
        self.window.set_keep_above(above)
        gdk_window = self.window.get_window()
        if gdk_window is not None:
            gdk_window.set_keep_above(above)

    def _on_top_toggled(self, *_args) -> None:
        self._apply_top()
        self.save_ui()

    def _signature(self) -> str:
        mons = list_monitors()
        return ",".join(f"{m['connector']}:{m['w']}x{m['h']}@{m['x']},{m['y']}" for m in mons)

    def _poll_monitors(self) -> bool:
        if self.engine.running or self._busy:
            return True
        sig = self._signature()
        if sig != self._monitor_sig:
            self._monitor_sig = sig
            self.reload_video()
            n = len(list_monitors())
            self.set_status(f"Display list updated. {n} display{'s' if n != 1 else ''} connected.")
        return True

    def reload_video(self) -> None:
        cfg = load_config()
        current = self.video_combo.get_active_id() or cfg.get("video") or ""
        choices, monitors = video_choices()
        if not current or not any(c["id"] == current for c in choices):
            current = default_video_id(choices, monitors)
        self._loading = True
        self.video_combo.remove_all()
        for choice in choices:
            self.video_combo.append(choice["id"], choice["label"])
        if not self.video_combo.set_active_id(current):
            self.video_combo.set_active(0)
        self._loading = False
        self.update_hint()

    def reload_audio(self) -> None:
        cfg = load_config()
        current = self.audio_combo.get_active_id() or cfg.get("audio") or ""
        sources = list_audio()
        if not current or not any(s["name"] == current for s in sources):
            current = default_audio_name(sources)
        self._loading = True
        self.audio_combo.remove_all()
        for source in sources:
            self.audio_combo.append(source["name"], source["label"])
        if current and not self.audio_combo.set_active_id(current):
            self.audio_combo.set_active(0)
        elif not current:
            self.audio_combo.set_active(0)
        self._loading = False
        if self.live and not self.engine.running:
            _video, audio = self.video_audio()
            self.meter.set_device(audio)

    def update_hint(self) -> None:
        monitors = list_monitors()
        n = len(monitors)
        if n <= 1:
            displays = "One display is connected. Plug in another and press Refresh — it can be recorded on its own."
        else:
            names = ", ".join(m["connector"] for m in monitors)
            displays = f"{n} displays connected ({names}). Pick any one. The audio choice does not follow the display."
        backend = "GNOME screen capture" if mutter_available() else ("gpu-screen-recorder" if have("gpu-screen-recorder") else "ffmpeg")
        session = "Wayland" if is_wayland() else "X11"
        self.hint.set_text(
            "Display and audio are independent: record any display with whichever audio monitor you select "
            f"(speakers or HDMI).\n{displays}\nThis session: {session}, backend {backend}."
        )

    def video_audio(self) -> tuple[str, str]:
        video = self.video_combo.get_active_id() or ""
        audio = self.audio_combo.get_active_id() or ""
        return video, audio

    def save_ui(self) -> None:
        video, audio = self.video_audio()
        folder = self.folder_btn.get_filename() or ""
        cfg = load_config()
        cfg["folder"] = folder
        if video:
            cfg["video"] = video
        if audio:
            cfg["audio"] = audio
        cfg["always_on_top"] = "true" if self.top_switch.get_active() else "false"
        save_config(cfg)

    def _on_video_changed(self, *_args) -> None:
        if self._loading:
            return
        self.save_ui()
        self.update_hint()
        if self.live:
            self.update_preview()

    def _on_audio_changed(self, *_args) -> None:
        if self._loading:
            return
        self.save_ui()
        if self.live and not self.engine.running:
            _video, audio = self.video_audio()
            self.meter.set_device(audio)

    def _on_level(self, frac: float) -> None:
        self.level.set_value(frac)

    def set_status(self, text: str) -> None:
        self.status.set_text(text)

    def update_preview(self) -> bool:
        if self._busy or self.engine.running:
            return False
        video, _audio = self.video_audio()
        if video == "portal":
            self.preview_frame.set_label("Preview")
            self.preview.set_from_pixbuf(
                info_pixbuf("Portal", "The system dialog picks the screen when you press Record.")
            )
            return False
        if not video.startswith("monitor:") and video != "desktop" and not video.startswith("v4l:"):
            self.preview.set_from_pixbuf(info_pixbuf("No preview", "Choose a display."))
            return False
        if (video.startswith("monitor:") or video == "desktop") and not mutter_available() and is_wayland():
            self.preview.set_from_pixbuf(
                info_pixbuf("No live preview", "Install gpu-screen-recorder, or use Portal.")
            )
            return False
        self._busy = True
        self.record_btn.set_sensitive(False)
        self.set_status("Updating preview…")
        fd, dest = tempfile.mkstemp(prefix="anything-preview-", suffix=".png")
        os.close(fd)
        shown = False
        try:
            grab_still(video, dest)
            if self.video_combo.get_active_id() != video:
                GLib.idle_add(self.update_preview)
                return False
            pix = GdkPixbuf.Pixbuf.new_from_file(dest)
            fit = min(640 / pix.get_width(), 220 / pix.get_height(), 1)
            if fit < 1:
                pix = pix.scale_simple(
                    max(1, int(pix.get_width() * fit)),
                    max(1, int(pix.get_height() * fit)),
                    GdkPixbuf.InterpType.BILINEAR,
                )
            self.preview.set_from_pixbuf(pix)
            if video.startswith("monitor:"):
                self.preview_frame.set_label("Preview — " + video.split(":", 1)[1])
            elif video == "desktop":
                self.preview_frame.set_label("Preview — entire desktop")
            else:
                self.preview_frame.set_label("Preview")
            shown = True
        except (RuntimeError, GLib.Error, OSError) as exc:
            message = exc.message if isinstance(exc, GLib.Error) else str(exc)
            self.preview.set_from_pixbuf(info_pixbuf("Preview unavailable", message[:90]))
        finally:
            self._busy = False
            if not self.engine.running:
                self.record_btn.set_sensitive(True)
                if shown or not self.engine.running:
                    self.set_status(self._ready_text())
            try:
                os.remove(dest)
            except OSError:
                pass
        return False

    def _ready_text(self) -> str:
        video, audio = self.video_audio()
        vlabel = self.video_combo.get_active_text() or video or "no display"
        alabel = friendly_audio(audio) if audio else "no audio"
        return f"Ready. Display: {vlabel}. Audio: {alabel}. Always on top is {'on' if self.top_switch.get_active() else 'off'}."

    def minimise(self) -> None:
        if not self.compact:
            self._saved_size = self.window.get_size()
            self.compact = True
            self.main_box.hide()
            self.window.resize(480, 120)
        self.min_btn.set_sensitive(False)
        self.restore_btn.set_sensitive(True)
        self.window.present()
        self._apply_top()

    def restore(self) -> None:
        self.compact = False
        self.main_box.show_all()
        width, height = self._saved_size or (760, 720)
        self.window.resize(width, height)
        self.window.deiconify()
        self.window.present()
        self.min_btn.set_sensitive(True)
        self.restore_btn.set_sensitive(False)
        self._apply_top()

    def present(self) -> None:
        self.window.deiconify()
        self.window.present()
        self._apply_top()

    def output_path(self) -> str:
        folder = self.folder_btn.get_filename() or ""
        name = (self.name_entry.get_text() or "").strip()
        if name.lower().endswith(".mp4"):
            name = name[:-4]
        if not folder or not os.path.isdir(folder):
            raise RuntimeError("Choose a folder that exists.")
        if not name:
            raise RuntimeError("Choose a file name.")
        return os.path.join(folder, name + ".mp4")

    def toggle_record(self) -> None:
        if self._busy:
            return
        if self.engine.running:
            self.engine.request_stop()
            self.record_btn.set_sensitive(False)
            self.set_status("Finishing the file…")
            token = time.monotonic()
            self._stop_token = token

            def force_finish() -> bool:
                if getattr(self, "_stop_token", None) != token:
                    return False
                if self.engine.running:
                    self.engine.force_close()
                return False

            GLib.timeout_add_seconds(8, force_finish)
            return
        try:
            path = self.output_path()
        except RuntimeError as exc:
            self.set_status(str(exc))
            return
        if os.path.exists(path):
            dialog = Gtk.MessageDialog(
                transient_for=self.window,
                modal=True,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.YES_NO,
                text="Overwrite the existing file?",
            )
            dialog.format_secondary_text(path)
            response = dialog.run()
            dialog.destroy()
            if response != Gtk.ResponseType.YES:
                return
        video, audio = self.video_audio()
        if not video or not audio:
            self.set_status("Select a display and an audio source.")
            return
        self.save_ui()
        self.meter.stop()
        self._busy = True
        self.record_btn.set_sensitive(False)
        self.set_status("Starting…")
        try:
            self.engine.start(video, audio, path)
        except Cancelled as exc:
            self._busy = False
            self.record_btn.set_sensitive(True)
            self.set_status(str(exc))
            self._restart_meter()
            return
        except Exception as exc:  # noqa: BLE001 - surface dbus/gstreamer failures
            self._busy = False
            self.record_btn.set_sensitive(True)
            self.set_status(f"Could not start: {exc}")
            self._restart_meter()
            return
        self._busy = False
        if not self.engine.running:
            self.record_btn.set_sensitive(True)
            self._restart_meter()
            return
        self._started = time.monotonic()
        self._set_recording_ui(True)
        self._tick = GLib.timeout_add(500, self._on_tick)

    def _set_recording_ui(self, recording: bool) -> None:
        self.record_btn.set_sensitive(True)
        self.record_btn.set_label("Stop" if recording else "Record")
        style = self.record_btn.get_style_context()
        if recording:
            style.remove_class("suggested-action")
            style.add_class("destructive-action")
        else:
            style.remove_class("destructive-action")
            style.add_class("suggested-action")
        for widget in (self.video_combo, self.audio_combo, self.folder_btn, self.name_entry):
            widget.set_sensitive(not recording)

    def _on_tick(self) -> bool:
        if not self.engine.running:
            return False
        elapsed = int(time.monotonic() - self._started)
        video = self.video_combo.get_active_text() or ""
        _vid, audio = self.video_audio()
        self.set_status(
            f"Recording  {elapsed // 60:02d}:{elapsed % 60:02d}  ·  {video}  +  {friendly_audio(audio)}  ({self.engine.backend})"
        )
        return True

    def _on_saved(self, message: str) -> None:
        GLib.idle_add(self._finish_ui, True, message)

    def _on_failed(self, message: str) -> None:
        GLib.idle_add(self._finish_ui, False, message)

    def _finish_ui(self, ok: bool, message: str) -> bool:
        self._stop_token = None
        self.engine.running = False
        self._busy = False
        self._set_recording_ui(False)
        if ok:
            self.name_entry.set_text(fresh_name())
            self.set_status(message + "  Next take is ready.")
        else:
            self.set_status(message)
        self._restart_meter()
        return False

    def _restart_meter(self) -> None:
        if not self.live:
            return
        _video, audio = self.video_audio()
        if audio:
            self.meter.set_device(audio)

    def _on_delete(self, *_args) -> bool:
        if self.engine.running:
            self.engine.request_stop()
        self.meter.stop()
        self.save_ui()
        return False


def print_list() -> int:
    cfg = load_config()
    choices, monitors = video_choices()
    sources = list_audio()
    video = cfg.get("video") or default_video_id(choices, monitors)
    if not any(c["id"] == video for c in choices):
        video = default_video_id(choices, monitors)
    audio = cfg.get("audio") or default_audio_name(sources)
    if not any(s["name"] == audio for s in sources):
        audio = default_audio_name(sources)
    for choice in choices:
        print(f"video\t{choice['id']}\t{choice['label']}")
    for source in sources:
        print(f"audio\t{source['name']}\t{source['label']}")
    print(f"default-video\t{video}")
    print(f"default-audio\t{audio}")
    print(f"backend\t{'gsr' if have('gpu-screen-recorder') else 'gnome' if mutter_available() else 'ffmpeg'}")
    return 0


def cmd_record(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="anything-recorder --record")
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--video", required=True)
    parser.add_argument("--audio", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    engine = Engine()
    loop = GLib.MainLoop()
    result = {"code": 1, "msg": "Recording did not finish."}

    def finish(ok: bool, message: str) -> None:
        result["code"] = 0 if ok else 1
        result["msg"] = message
        if loop.is_running():
            loop.quit()

    engine.on_finished = lambda message: finish(True, message)
    engine.on_error = lambda message: finish(False, message)

    def stop(*_args) -> bool:
        engine.request_stop()
        return True

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, stop)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, stop)
    try:
        engine.start(args.video, args.audio, args.output)
    except (Cancelled, RuntimeError, GLib.Error) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"Recording {args.video} + {args.audio} → {args.output}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    loop.run()
    print(result["msg"])
    return result["code"]


class RecorderApp(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.ui = None
        self.connect("activate", self.on_activate)

    def on_activate(self, _app) -> None:
        if self.ui is not None:
            self.ui.present()
            return
        self.ui = RecorderWindow(self, live=True)
        self.ui.window.show_all()
        self.ui._apply_top()
        self.ui.set_status(self.ui._ready_text())


def self_test() -> int:
    sample = """Monitors: 2
 0: +*eDP-1 1920/340x1200/220+0+0  eDP-1
 1: +HDMI-1 2560/600x1440/340+1920+0  HDMI-1
"""
    mons = parse_xrandr_monitors(sample)
    assert [m["connector"] for m in mons] == ["eDP-1", "HDMI-1"], mons
    assert mons[0]["primary"] and not mons[1]["primary"]
    assert mons[1]["w"] == 2560 and mons[1]["x"] == 1920
    assert monitor_bounds(mons) == (0, 0, 1920 + 2560, 1440)
    assert friendly_audio("alsa_output.pci-0000_00_1f.3-platform-sof_sdw.HiFi__Speaker__sink.monitor") == "Speakers"
    assert friendly_audio("alsa_output.x.HiFi__HDMI2__sink.monitor") == "HDMI 2"
    live = list_monitors()
    assert any(m["connector"] == "eDP-1" for m in live), live
    sources = list_audio()
    assert any(s["name"].endswith("Speaker__sink.monitor") for s in sources), sources
    assert any("HDMI1" in s["name"] for s in sources)
    audio = default_audio_name(sources)
    assert audio.endswith("Speaker__sink.monitor"), audio
    choices, monitors = video_choices()
    assert any(c["id"] == "monitor:eDP-1" for c in choices)
    assert default_video_id(choices, monitors) == "monitor:eDP-1"

    app = Gtk.Application(application_id=APP_ID + ".test", flags=Gio.ApplicationFlags.NON_UNIQUE)
    holder: dict = {}

    def on_activate(application) -> None:
        ui = RecorderWindow(application, live=False)
        holder["ui"] = ui
        ui.window.show_all()
        GLib.idle_add(check_ui, ui, application)

    def check_ui(ui: RecorderWindow, application) -> bool:
        try:
            video, selected_audio = ui.video_audio()
            assert video == "monitor:eDP-1", video
            assert selected_audio.endswith("Speaker__sink.monitor"), selected_audio
            assert ui.top_switch.get_active()
            labels = []
            model = ui.video_combo.get_model()
            itr = model.get_iter_first()
            while itr is not None:
                labels.append(model.get_value(itr, 1))
                itr = model.iter_next(itr)
            assert "monitor:eDP-1" in labels, labels
            assert "desktop" in labels, labels
            ui.minimise()
            assert ui.compact and not ui.main_box.get_visible()
            ui.restore()
            assert not ui.compact and ui.main_box.get_visible()
            ui.top_switch.set_active(False)
            assert not ui.top_switch.get_active()
            ui.top_switch.set_active(True)
            assert ui.top_switch.get_active()
            print("ui-ok", video, friendly_audio(selected_audio), "displays", len(monitors))
            ui.window.destroy()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            print("ui-fail", repr(exc), file=sys.stderr)
            holder["code"] = 1
            try:
                ui.window.destroy()
            except Exception:
                pass
        application.quit()
        return False

    app.connect("activate", on_activate)
    app.run([])
    if holder.get("code"):
        return 1

    still = "/tmp/ar-selftest-preview.png"
    grab_still("monitor:eDP-1", still)
    pix = GdkPixbuf.Pixbuf.new_from_file(still)
    assert pix.get_width() >= 320, pix.get_width()
    print("preview-ok", pix.get_width(), pix.get_height(), os.path.getsize(still))

    out = "/tmp/ar-selftest.mp4"
    try:
        os.remove(out)
    except OSError:
        pass
    engine = Engine()
    loop = GLib.MainLoop()
    result = {"ok": False, "msg": "no result"}

    def finish(ok: bool, message: str) -> None:
        result["ok"] = ok
        result["msg"] = message
        if loop.is_running():
            loop.quit()

    engine.on_finished = lambda message: finish(True, message)
    engine.on_error = lambda message: finish(False, message)
    engine.start("monitor:eDP-1", audio, out)
    GLib.timeout_add_seconds(2, lambda: engine.request_stop() or False)
    GLib.timeout_add_seconds(20, lambda: loop.quit() or False)
    loop.run()
    size = os.path.getsize(out) if os.path.isfile(out) else 0
    print("record", result, "bytes", size)
    if not result["ok"] or size < 20000:
        return 1
    probe = subprocess.run(
        ["ffprobe", "-hide_banner", "-show_streams", "-of", "compact", out],
        text=True,
        capture_output=True,
    )
    text = probe.stdout
    if "codec_name=h264" not in text or "codec_name=aac" not in text:
        print(probe.stderr, file=sys.stderr)
        return 1
    print("streams-ok")
    return 0


def main(argv: list[str]) -> int:
    Gst.init(None)
    if "--self-test" in argv:
        return self_test()
    if "--list" in argv:
        return print_list()
    if "--record" in argv:
        return cmd_record(argv)
    app = RecorderApp()
    return app.run(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
