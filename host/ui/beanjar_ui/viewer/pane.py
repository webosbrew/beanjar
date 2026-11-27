# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""ViewerPane (ui/pane.blp): the TV view over the status bar, embeddable (e.g.
in the manager). The remote is a separate window (remote_window.RemoteWindow),
made when first shown. The window around the pane gives it its controls
(attach): the Remote toggle, the remote's keys, a toast overlay for say(),
and on_fullscreen (F11)."""

import os
import sys
import tempfile
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

from . import ui  # noqa: E402
from .keys import HOME_KEYS  # noqa: E402
from .elements_layer import ElementsLayer  # noqa: E402
from .remote import LAYOUTS, RemotePanel, find  # noqa: E402
from .remote_window import RemoteWindow  # noqa: E402
from .stats import Stats, front_led, panel_off  # noqa: E402
from .statusbar import StatusBar  # noqa: E402
from .view import TvView  # noqa: E402

PORTRAIT = 270  # the angle the Rotate toggle's portrait is (what msc reports as portrait)
# The set's own keys, the micom's local key codes (include/webos-ir.h WEBOS_LOCAL_*). The joystick's
# directions (channel up/down, volume right/left) are inferred from LG's usual layout, not verified.
LOCAL_PRESS, LOCAL_UP, LOCAL_DOWN, LOCAL_RIGHT, LOCAL_LEFT = 0xEA, 0x00, 0x01, 0x02, 0x03

CSS = """
.tv-view { background-color: black; }
.standby { background-color: black; }
button.tv-key.led-red > image,
menubutton.tv-key.led-red > button image { color: #e53935; }
window.remote-window, window.remote-window > contents { background: none; box-shadow: none; }
"""


def _css(data):
    provider = Gtk.CssProvider()
    provider.load_from_string(data)
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )


@ui.template("pane")
class ViewerPane(Gtk.Box):
    """The TV view over the status bar; the remote is a separate window (RemoteWindow), made when
    first shown. core: a beanjar_ui.viewer.core.Core with its options set; stats_dir: where qemu.pid
    and viewer-qmp.sock are (None: no stats); remote: show the remote's window once mapped;
    statusbar: show the bar; remote_layout: the initial remote; firmware_layout: the firmware's normal remote,
    limiting choices to it and the service remote and determining the header keys.
    The window around it gives it its controls (attach): the Remote toggle, the remote's keys, a
    toast overlay for say(), and on_fullscreen (F11)."""

    __gtype_name__ = "ViewerPane"

    def __init__(
        self,
        core,
        stats_dir,
        remote=False,
        statusbar=True,
        on_quit=None,
        remote_layout="normal",
        firmware_layout=None,
        local_key=None,
        touch=False,
        portrait=False,
        vm_name=None,
    ):
        super().__init__()
        _css(CSS)
        ui.icons(Gdk.Display.get_default())
        self.core = core
        self.keys = 0
        self.menu_down = None  # the Menu key's last press, while down (key_controller)
        self.f11_down = False
        self._on_quit = on_quit
        self._finished = None
        self.view = TvView(core, self._quit, touch=touch)
        self.view.add_css_class("tv-view")
        self.remote_layouts = (firmware_layout, "service") if firmware_layout else tuple(LAYOUTS)
        if remote_layout not in self.remote_layouts:
            raise ValueError(f"remote layout {remote_layout!r} does not match this firmware")
        self.system_layout = LAYOUTS[
            firmware_layout or (remote_layout if remote_layout != "service" else "normal")
        ]
        self.remote = RemotePanel(send=self.ir, layout=remote_layout)
        self.home_code = self.system_layout.keys[find("HOME", self.system_layout)].code
        self.remote_ever_shown = False
        self.remote_window = None
        # the Elements layer (More > Show planes > Elements): screen recognition over the picture
        self.elements = ElementsLayer(
            core, self.view, say=self.say, turned=lambda: self.rotation != 0
        )
        self.picture_overlay = Gtk.Overlay(hexpand=True, vexpand=True)
        self.picture_overlay.set_child(self.view)
        self.picture_overlay.add_overlay(self.elements)
        self.append(self.picture_overlay)
        self.standby = False  # the SoC suspended (QMP's standby chardev)
        self.picture_off = False  # the panel off: that, or the micom's warm standby
        self.stats = (
            Stats(stats_dir, self._stats_changed, self._standby_changed) if stats_dir else None
        )
        self.bar = StatusBar(self.stats) if statusbar else None
        if self.bar:
            self.append(self.bar)
        self._start_remote = remote
        self.remote_toggle = None  # the window's controls (attach)
        self.key_held = None  # the IR code of the header key held down
        self.local_key = (
            local_key  # the model's local key (vm-info.toml): "onekey", "joystick", ... or None
        )
        self.local_held = None  # the local key code held down
        self.key_leds = ()  # the header's TV key controls and their action tooltips
        self.toasts = None
        self.on_fullscreen = None
        self.said = []  # every message, the last one last
        # a set that turns to portrait (vm-info.toml's portrait): the guest's rotation setting as
        # it reports it (run/rotation, beanjar/devices/rotation.py), the picture turned to match; rotate()
        # turns the set through `beanjar sensor set rotate` where the VM is a library's (vm_name)
        self.run_dir = stats_dir if portrait else None
        self.vm_name = vm_name
        self.rotation = 0
        self.rotating = None  # the beanjar sensor set rotate in progress
        self.on_turned = None  # the window's: the picture turned to portrait (True) or back
        self.on_rotation = None  # the window's: the guest's rotation angle, at each change
        if self.run_dir:
            GLib.timeout_add(500, self._rotation_poll)

    def attach(self, remote_toggle=None, keys=None, toasts=None, on_fullscreen=None):
        """The window's Remote toggle (kept in step with the remote's window), remote keys ({name
        of a system-layout key: button}, each held while the mouse is down on it), toast overlay (say())
        and fullscreen toggle (F11)."""
        self.remote_toggle = remote_toggle
        self.toasts, self.on_fullscreen = toasts, on_fullscreen
        for name, button in (keys or {}).items():
            code = self.system_layout.keys[find(name, self.system_layout)].code
            # Capture phase: before the button's own click gesture, which claims on release.
            gesture = Gtk.GestureClick(
                button=Gdk.BUTTON_PRIMARY, propagation_phase=Gtk.PropagationPhase.CAPTURE
            )
            gesture.connect("pressed", lambda *_, code=code: self.key_down(code))
            gesture.connect("released", lambda *_: self.key_up())
            gesture.connect("end", lambda *_: self.key_up())
            button.add_controller(gesture)

    def attach_local_key(self, buttons):
        """The set's own key: {local key code: button}, each held while the mouse is down on it."""
        for code, button in buttons.items():
            gesture = Gtk.GestureClick(
                button=Gdk.BUTTON_PRIMARY, propagation_phase=Gtk.PropagationPhase.CAPTURE
            )
            gesture.connect("pressed", lambda *_, code=code: self.local_down(code))
            gesture.connect("released", lambda *_: self.local_up())
            gesture.connect("end", lambda *_: self.local_up())
            button.add_controller(gesture)

    def attach_key_leds(self, buttons):
        """The header's TV key controls, whose power icons mirror the micom's front LED."""
        self.key_leds = [(button, button.get_tooltip_text()) for button in buttons]
        self.update_key_leds()

    def update_key_leds(self):
        state, label = front_led(self.stats.micom if self.stats else None)
        for button, action in self.key_leds:
            for old in ("off", "red", "unknown"):
                button.remove_css_class(f"led-{old}")
            button.add_css_class(f"led-{state}")
            tooltip = f"{action} · {label}"
            button.set_tooltip_text(tooltip)
            button.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])

    def local_down(self, code):
        """A local key down, to the micom, which tells a short press from a long one (and reports
        it in its warm standby too). With the SoC suspended (standby) the micom's always-on part
        behind ir.sock (beanjar's ir_relay) wakes it on the local key, as on the remote's power
        key."""
        self.local_up()
        self.core.local_key(code, True)
        self.local_held = code

    def local_up(self):
        if self.local_held is not None:
            code, self.local_held = self.local_held, None
            self.core.local_key(code, False)

    def key_down(self, code):
        """A header key: the remote's IR button down (a held one released first)."""
        self.key_up()
        self.ir(code, True)
        self.key_held = code

    def key_up(self):
        if self.key_held is not None:
            code, self.key_held = self.key_held, None
            self.ir(code, False)

    def ir(self, code, down):
        """A remote button edge to the micom over webos.ir; in standby the micom's always-on part
        behind ir.sock (beanjar's ir_relay) drops the keys and wakes the guest on POWER."""
        self.core.ir(code, down)

    def key_evdev(self, code, down):
        if code in HOME_KEYS:
            self.ir(self.home_code, down)
        else:
            self.core.key_evdev(code, down)

    # ---- rotation ----
    def _rotation_poll(self):
        if self._finished is not None:
            return False
        from beanjar.devices import rotation

        angle = rotation.current(self.run_dir) or 0
        if angle != self.rotation:
            self.rotation = angle
            if self.on_rotation:
                self.on_rotation(angle)
            turn = rotation.picture_turn(angle)
            self.core.turn(turn)
            self.view.queue_render()
            if self.on_turned:
                self.on_turned(turn % 180 == 90)
        return True

    def can_rotate(self):
        """Whether rotate() can turn the set: a library VM that turns to portrait."""
        return bool(self.run_dir and self.vm_name)

    def rotate_target(self):
        """The angle Rotate goes to: portrait (270) from landscape, landscape (0) from any other."""
        return PORTRAIT if self.rotation == 0 else 0

    def rotate(self):
        """Switches the set between landscape (0) and portrait (270; any other angle goes to
        landscape): `beanjar sensor set rotate NAME ANGLE`; the picture follows when the guest reports it."""
        if not self.can_rotate() or self.rotating:
            return
        from beanjar.devices import rotation

        from .stats import CORE

        library = os.path.dirname(os.path.dirname(os.path.dirname(self.run_dir)))
        angle = self.rotate_target()
        launcher = Gio.SubprocessLauncher.new(
            Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_PIPE
        )
        path = os.environ.get("PYTHONPATH")
        launcher.setenv("PYTHONPATH", CORE + (os.pathsep + path if path else ""), True)
        argv = [
            sys.executable,
            "-m",
            "beanjar",
            "--library",
            library,
            "sensor",
            "set",
            "rotate",
            self.vm_name,
        ]
        try:
            self.rotating = launcher.spawnv(argv + [str(angle)])
        except GLib.Error as e:
            self.say(f"Rotate failed: {e.message}")
            return

        def done(proc, result):
            self.rotating = None
            try:
                _ok, _out, err = proc.communicate_utf8_finish(result)
            except GLib.Error as e:
                self.say(f"Rotate failed: {e.message}")
                return
            if not proc.get_successful():
                lines = (err or "").strip().splitlines()
                self.say(lines[-1] if lines else "Rotate failed")

        self.rotating.communicate_utf8_async(None, None, done)

    # ---- standby ----
    def _stats_changed(self):
        if self.bar:
            self.bar.update()
        self.update_key_leds()
        self._update_picture()

    def _standby_changed(self, standby):
        """The SoC went into standby (suspended) or out of it."""
        self.standby = standby
        self._update_picture()

    def _update_picture(self):
        """The panel is off in standby and in the micom's warm standby (webOS's Active Standby,
        the SoC running; micom-status): the picture is black meanwhile, and LG's cursor is not
        drawn, as the guest never hides it."""
        off = self.standby or panel_off(self.stats.micom if self.stats else None)
        if off == self.picture_off:
            return
        self.picture_off = off
        self.core.standby(off)
        self.view.set_opacity(0.0 if off else 1.0)
        (self.add_css_class if off else self.remove_css_class)("standby")

    # ---- remote ----
    def remote_shown(self):
        """Whether the remote's window is on screen: shown and not minimized (suspended)."""
        w = self.remote_window
        return bool(w and w.get_visible() and not w.is_suspended())

    def toggle_remote(self):
        self.show_remote(not self.remote_shown())

    def show_remote(self, show):
        if self._finished is None and show != self.remote_shown():
            if show:
                if self.remote_window is None:
                    self.remote_window = RemoteWindow(self)
                    # Kept above the TV window: an unrelated toplevel goes behind it at the
                    # first click on the picture and looks hidden while it is still shown.
                    root = self.get_root()
                    if isinstance(root, Gtk.Window):
                        self.remote_window.set_transient_for(root)
                self.remote_ever_shown = True
                self.remote_window.present()
            else:
                self.remote.release()
                self.remote_window.set_visible(False)
        self.sync_remote_toggle()

    _syncing = False

    def sync_remote_toggle(self):
        """The Remote toggle set to whether the remote is on screen, without calling back."""
        toggle = self.remote_toggle
        if toggle and toggle.get_active() != self.remote_shown():
            self._syncing = True
            toggle.set_active(self.remote_shown())
            self._syncing = False

    def remote_toggled(self, active):
        """The Remote toggle was clicked (not set by show_remote)."""
        if not self._syncing:
            self.show_remote(active)

    def set_remote_layout(self, name):
        """A compatible remote (remote.LAYOUTS); what is held is released."""
        if name not in self.remote_layouts:
            raise ValueError(f"remote layout {name!r} does not match this firmware")
        self.remote.set_layout(name)

    def menu_open(self):
        return self.remote.menu_open()

    def release(self):
        self.key_up()
        self.local_up()
        self.core.release()
        self.remote.release()

    # ---- lifetime ----
    def realized_start(self):
        if self._start_remote:
            self.show_remote(True)

    def _quit(self):
        if self._on_quit:
            self._on_quit()

    def screenshot(self):
        """Save Screenshot: the guest picture (no chrome) as PNG in ~/Pictures (or ~)."""
        folder = GLib.get_user_special_dir(
            GLib.UserDirectory.DIRECTORY_PICTURES
        ) or os.path.expanduser("~")
        path = os.path.join(folder, time.strftime("webos-%Y%m%d-%H%M%S.png"))
        with tempfile.TemporaryDirectory() as tmp:
            ppm = os.path.join(tmp, "shot.ppm")
            if not self.view.screenshot(ppm):
                self.say("Screenshot failed")
                return None
            try:
                GdkPixbuf.Pixbuf.new_from_file(ppm).savev(path, "png", [], [])
            except GLib.Error:
                path = path[:-4] + ".ppm"
                os.replace(ppm, path)
        self.say(f"Saved {path}")
        print(f"viewer: screenshot {path}", file=sys.stderr)
        return path

    def say(self, text):
        """A toast over the picture."""
        self.said.append(text)
        if self.toasts:
            self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=3))

    def finish(self):
        """Once: release, stop the core (GL context current), print the counters."""
        if self._finished is not None:
            return self._finished
        self.key_up()
        self.local_up()
        self.remote.release()
        if self.stats:
            self.stats.close()
        self.elements.set_active(False)
        extra = f" keys={self.keys}"
        if self.remote_ever_shown:
            extra += f" remote_presses={self.remote.presses}"
        self.view.make_current()
        self._finished = self.core.finish(extra)
        self.view.stopped()
        if self.remote_window:
            self.remote_window.set_child(None)
            self.remote_window.destroy()
            self.remote_window = None
        return self._finished
