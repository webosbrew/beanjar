# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""beanjar-viewer's GTK 4 front end (host/viewer/README.md, docs/dev/architecture/viewer.md)."""

import glob
import os
import signal
import tomllib
from urllib.parse import unquote

USAGE = """usage: beanjar-viewer BUS_ADDRESS [--size WxH] [--remote] [--remote-layout auto|normal|netcast|simplesmart|japan|service]
  [--no-statusbar] [--seconds N] [--straight-alpha] [--cursor-socket PATH] [--video-socket PATH]
  [--ir-socket PATH] [--media-socket PREFIX] [--capture-socket PATH]
  [--no-media] [--hwdec auto|none|TYPE] [--media-wav OUT.wav] [--media-latency MS] [--media-volume 0..2] [--media-mute]
  [--no-audio] [--audio-wav OUT.wav] [--sdl-audio] [--media-silent] [--ui-dir DIR]
  [--capture OUT.ppm --capture-every MS] [--capture-control FILE --capture-every MS]
  [--hide-planes video,ui,cursor] (planes left out at start; More shows or hides them)
  [--elements] (the Elements layer on at start) [--grab OUT.png --grab-every MS]
or: beanjar-viewer BUS_ADDRESS --headless [--seconds N] [--replay FILE] [--screenshot OUT.ppm
  [--screenshot-every MS] [--osd-alpha]] [--no-osd] ...   (viewer-headless)
or: beanjar-viewer --remote-preview OUT.ppm [--remote-layout normal|netcast|simplesmart|japan|service] [--remote-press KEY]
  [--seconds N]
"""
VALUED = (
    "cursor-socket",
    "video-socket",
    "ir-socket",
    "media-socket",
    "capture-socket",
    "hwdec",
    "media-wav",
    "media-latency",
    "media-volume",
    "audio-wav",
    "hide-planes",
)
FLAGS = ("straight-alpha", "no-media", "no-audio", "sdl-audio", "media-silent", "media-mute")


def parse(args):
    o = {
        "address": None,
        "size": (1280, 720),
        "remote": False,
        "statusbar": True,
        "seconds": 0.0,
        "core": [],
        "preview": None,
        "press": None,
        "ui_dir": None,
        "layout": "auto",
        "shot": None,
        "shot_every": 0,
        "ctl": None,
        "elements": False,
        "grab": None,
        "grab_every": 0,
    }
    i = 0
    while i < len(args):
        a = args[i]
        name = a[2:] if a.startswith("--") else None
        value = args[i + 1] if i + 1 < len(args) else None
        if name in VALUED and value is not None:
            o["core"].append((name, value))
            i += 2
            continue
        if name in FLAGS:
            o["core"].append((name, None))
            i += 1
            continue
        if name == "size" and value:
            try:
                w, h = (int(x) for x in value.split("x"))
            except ValueError:
                raise SystemExit("viewer: invalid window size")
            if not (64 <= w <= 8192 and 64 <= h <= 8192):
                raise SystemExit("viewer: invalid window size")
            o["size"] = (w, h)
            i += 2
            continue
        if name == "seconds" and value:
            o["seconds"] = float(value)
            i += 2
            continue
        if name == "remote":
            o["remote"] = True
            i += 1
            continue
        if name == "capture" and value:
            o["shot"] = value
            i += 2
            continue
        if name == "capture-control" and value:
            o["ctl"] = value
            i += 2
            continue
        if name == "capture-every" and value:
            o["shot_every"] = int(value)
            i += 2
            continue
        if name == "elements":
            o["elements"] = True
            i += 1
            continue
        if name == "grab" and value:
            o["grab"] = value
            i += 2
            continue
        if name == "grab-every" and value:
            o["grab_every"] = int(value)
            i += 2
            continue
        if name == "no-statusbar":
            o["statusbar"] = False
            i += 1
            continue
        if name == "ui-dir" and value:
            o["ui_dir"] = value
            i += 2
            continue
        if name == "remote-preview" and value:
            o["preview"] = value
            i += 2
            continue
        if name == "remote-layout" and value:
            if value not in ("auto", "normal", "netcast", "simplesmart", "japan", "service"):
                raise SystemExit(
                    "viewer: --remote-layout is auto, normal, netcast, simplesmart, japan or service"
                )
            o["layout"] = value
            i += 2
            continue
        if name == "remote-press" and value:
            o["press"] = value
            i += 2
            continue
        if name in ("help", "h") or a == "-h":
            print(USAGE, end="")
            raise SystemExit(0)
        if not a.startswith("-") and o["address"] is None:
            o["address"] = a
            i += 1
            continue
        raise SystemExit(USAGE.rstrip())
    if o["shot_every"] and not o["shot"] and not o["ctl"]:
        raise SystemExit(
            "viewer: --capture-every needs --capture OUT.ppm or --capture-control FILE"
        )
    if bool(o["grab"]) != bool(o["grab_every"]):
        raise SystemExit("viewer: --grab OUT.png and --grab-every MS go together")
    return o


def grab_name(grab, ms, suffix=".png"):
    """--grab-every's files for ms after the window was shown: OUT-<ms>.png (the picture as the
    window shows it, with the Elements layer), OUT-<ms>.frame.png and OUT-<ms>.elements.json (the
    frame the layer's reading shown was taken from, and that reading, as `beanjar elements
    --json` prints it)."""
    base = grab[:-4] if grab.endswith(".png") else grab
    return f"{base}-{ms:06d}{suffix}"


def grab(pane, path):
    """The pane's picture (the TV view and the layers over it, no status bar) as PNG; False when
    it has not been drawn yet."""
    import gi

    gi.require_version("Graphene", "1.0")
    from gi.repository import Graphene, Gtk

    widget = pane.picture_overlay
    w, h = widget.get_width(), widget.get_height()
    native = widget.get_native()
    if w <= 0 or h <= 0 or native is None:
        return False
    snapshot = Gtk.Snapshot()
    Gtk.WidgetPaintable.new(widget).snapshot(snapshot, w, h)
    node = snapshot.to_node()
    if node is None:
        return False
    texture = native.get_renderer().render_texture(node, Graphene.Rect().init(0, 0, w, h))
    return texture.save_to_png(path)


def shot_name(shot, ms):
    """--capture-every's file for ms after the first scanout: OUT-<ms>.ppm, as headless names its
    --screenshot-every captures (--screenshot itself selects the headless program)."""
    base, ext = os.path.splitext(shot)
    return f"{base or shot}-{ms:06d}.ppm" if ext and "/" not in ext else f"{shot}-{ms:06d}.ppm"


def socket_dir(address):
    """The launcher's sockets (qemu.pid, viewer-qmp.sock, ...) sit next to the D-Bus socket."""
    prefix = "unix:path="
    if not address.startswith(prefix):
        return None
    path = unquote(address[len(prefix) :]).split(",")[0]
    return os.path.dirname(path)


def vm_name(sockets):
    """A library VM's name, its directory's (<vm>/run/ holds the sockets, beside <vm>/vm.toml); None
    otherwise (the checkout's own VM, build/)."""
    if sockets and os.path.basename(sockets) == "run":
        vm = os.path.dirname(sockets)
        if os.path.isfile(os.path.join(vm, "vm.toml")):
            return os.path.basename(vm)
    return None


def vm_info(sockets):
    """The launch's vm-info.toml (platform, version, board, soc, release, firmware, local_key on
    a model with a local key and touch on one with a touch screen) next to the
    sockets, which the launcher writes at each start (beanjar/paths.py vm_info); {} if there is none."""
    try:
        with open(os.path.join(sockets, "vm-info.toml"), "rb") as f:
            return tomllib.load(f)
    except (OSError, ValueError, TypeError):
        return {}


def remote_layout(requested, info):
    """Use the selected target's remote by default; an explicit layout takes precedence
    (remote_layouts.remote_layout, shared with the manager's beanjar remote layout)."""
    from beanjar.remote_layouts import remote_layout as choose

    try:
        return choose(requested, info)
    except ValueError as e:
        raise SystemExit(f"viewer: {e}") from None


def titles(name, info):
    """(title, subtitle, window title) of a VM named name (None: the checkout's) whose launch wrote
    info (vm_info). The header bar's title is the platform with its version ("webOS 4.5"), its
    subtitle the VM's name, board and firmware version ("tv1 · W19P 04.10.20.01"); the window's
    title says it all in one line, with the release's codename and the SoC: "tv1 - webOS 4.5
    (goldilocks2) - W19P k5lp 04.10.20.01 - Beanjar Viewer". Without a platform in info the
    header's title is the VM's name, else the program's."""

    def words(*keys):
        return " ".join(str(info[k]) for k in keys if info.get(k))

    platform = words("platform", "version")
    title = platform or name or "Beanjar Viewer"
    subtitle = (
        " · ".join(x for x in (name if platform else None, words("board", "firmware")) if x) or None
    )
    release = f"({info['release']})" if info.get("release") else ""
    window = " - ".join(
        x
        for x in (
            name,
            " ".join(x for x in (platform, release) if x),
            words("board", "soc", "firmware"),
            "Beanjar Viewer",
        )
        if x
    )
    return title, subtitle, window


def gpu_rendernode():
    """Whether a render node with a GPU behind it exists: vgem's (loaded on WSL for VA-API,
    docs/dev/architecture/vnc.md) is none, as beanjar/qemu.py's is_vgem."""
    for n in sorted(glob.glob("/dev/dri/renderD*")):
        dev = os.path.realpath(f"/sys/class/drm/{os.path.basename(n)}/device")
        if os.path.basename(dev) != "vgem":
            return True
    return False


def prefer_d3d12():
    """On WSL (/dev/dxg, no GPU render node), Mesa 25 (Debian 13) draws with llvmpipe unless
    GALLIUM_DRIVER names d3d12, as the launcher (beanjar/qemu.py) does for QEMU. Set before GDK starts."""
    if os.path.exists("/dev/dxg") and not gpu_rendernode():
        os.environ.setdefault("GALLIUM_DRIVER", "d3d12")


def main(args):
    o = parse(args)
    if o["preview"] is None and not o["address"]:
        raise SystemExit(USAGE.rstrip())
    prefer_d3d12()
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk  # noqa: F401
    except (ImportError, ValueError) as e:
        raise SystemExit(
            f"viewer: GTK 4 and libadwaita for Python are not available ({e}); install "
            "python3-gi, gir1.2-gtk-4.0 and gir1.2-adw-1, or run with --headless"
        )
    if o["preview"] is not None:
        from .preview import remote_preview

        return remote_preview(
            o["preview"], o["press"], o["seconds"], remote_layout(o["layout"], {})
        )
    from .. import no_display

    missing = no_display("viewer", "use --headless")
    if missing:
        raise SystemExit(missing)
    return run(o)


def run(o):
    from gi.repository import Adw, Gio, GLib
    from . import core as corelib
    from . import ui

    if o["ui_dir"]:
        ui.use_dir(o["ui_dir"])  # before the window's templates are loaded
    from .window import ViewerPane, ViewerWindow

    sockets = socket_dir(o["address"])
    info = vm_info(sockets or "")
    selected_layout = remote_layout(o["layout"], info)

    # The core's SIGINT/SIGTERM handling (clean quit, then a hard stop); PyGObject must not
    # install its own SIGINT fallback over it.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    corelib.install_signals()
    core = corelib.Core()
    core.option("address", o["address"])
    w, h = o["size"]
    core.option("size", f"{w}x{h}")
    for name, value in o["core"]:
        try:
            core.option(name, value)
        except ValueError as e:
            raise SystemExit(f"viewer: {e}")
    result = {"failed": False}
    # NON_UNIQUE: one viewer per VM, each its own process.
    app = Adw.Application(application_id=ui.APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
    window = {}

    def quit_requested():
        win = window.get("w")
        if win is not None:
            win.close()
        else:
            app.quit()

    def activate(app):
        pane = ViewerPane(
            core,
            sockets,
            remote=o["remote"],
            remote_layout=selected_layout,
            firmware_layout=remote_layout("auto", info) if info else None,
            statusbar=o["statusbar"],
            local_key=info.get("local_key"),
            touch=bool(info.get("touch")),
            portrait=bool(info.get("portrait")),
            vm_name=vm_name(sockets),
            on_quit=lambda: GLib.idle_add(lambda: (quit_requested(), False)[1]),
        )
        title, subtitle, window_title = titles(vm_name(sockets), info)
        win = ViewerWindow(
            app, pane, o["size"], title=title, subtitle=subtitle, window_title=window_title
        )
        window["w"] = win
        win.connect("close-request", lambda _w: result.update(failed=pane.finish()) or False)
        win.present()
        # the manager's "Show the Viewer" (manager/gui.py present_viewer): SIGUSR1 brings it up
        GLib.unix_signal_add(
            GLib.PRIORITY_DEFAULT, signal.SIGUSR1, lambda: (win.present(), True)[1]
        )
        if o["elements"]:
            GLib.idle_add(lambda: (win.set_elements(True), False)[1])
        if o["grab_every"]:
            start = GLib.get_monotonic_time() // 1000
            grabbed = {"taken": None}

            def grab_tick():
                ms = GLib.get_monotonic_time() // 1000 - start
                if not grab(pane, grab_name(o["grab"], ms)):
                    return True
                shown = pane.elements.last()
                if shown and shown["taken"] != grabbed["taken"]:
                    import json

                    grabbed["taken"] = shown["taken"]
                    with open(grab_name(o["grab"], ms, ".frame.png"), "wb") as f:
                        f.write(shown["png"])
                    with open(grab_name(o["grab"], ms, ".elements.json"), "w") as f:
                        json.dump(shown["data"], f, indent=2, ensure_ascii=False)
                return True

            GLib.timeout_add(o["grab_every"], grab_tick)
        if o["seconds"] > 0:
            GLib.timeout_add(int(o["seconds"] * 1000), lambda: (quit_requested(), False)[1])
        if o["shot_every"]:
            # as headless --screenshot-every: OUT-<ms after the first scanout>.ppm at 960x540 (the
            # composite: the decoded video under the OSD), whatever the window's size; with
            # --capture-control FILE, only while FILE exists, into the OUT.ppm its first line names,
            # OUT-<ms since then>.ppm at 480x270 (scripts/regress's decode cases)
            first = {}

            def control():
                try:
                    with open(o["ctl"]) as f:
                        return f.readline().strip()
                except OSError:
                    return ""

            def shoot():
                now = GLib.get_monotonic_time() // 1000
                if (core.counter("valid") or 0) <= 0:
                    return True
                if o["ctl"]:
                    if now - first.get("checked", 0) >= 100:
                        first["checked"] = now
                        out = control()
                        if out != first.get("out"):
                            first.update(out=out, t=now, next=0)
                    out, size = first.get("out"), (480, 270)
                    if not out:
                        return True
                else:
                    first.setdefault("t", now)
                    out, size = o["shot"], (960, 540)
                ms = now - first["t"]
                if ms >= first.get("next", 0):
                    core.screenshot(shot_name(out, ms - ms % o["shot_every"]), *size)
                    first["next"] = ms - ms % o["shot_every"] + o["shot_every"]
                return True

            GLib.timeout_add(50, shoot)

    app.connect("activate", activate)
    app.run([])
    return 1 if result["failed"] else 0
