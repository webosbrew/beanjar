# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The TV window (ui/window.blp) around a ViewerPane: a libadwaita header bar
with the set's own key (its icon shows the front LED on a model with a local key), the remote's Back, Home and
Settings keys, the VM's name, Save
Screenshot, Fullscreen and the Remote toggle, and toasts for the results.
ViewerPane and RemoteWindow live in pane.py and remote_window.py; they are
re-exported here for their existing importers (main.py, tests)."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import ui  # noqa: E402
from .keys import key_controller  # noqa: E402
from .pane import ViewerPane  # noqa: E402, re-exported
from .remote import find  # noqa: E402
from .remote_window import RemoteWindow  # noqa: E402, re-exported

# More's planes: the action's name, the core's WV_PLANE_* bit (core.h)
PLANES = (("ui", 2), ("video", 1), ("cursor", 4))

__all__ = ["ViewerPane", "RemoteWindow", "ViewerWindow"]


class Title:
    """The header bar's title and subtitle (two labels, as Adw.WindowTitle's API)."""

    def __init__(self, title, subtitle):
        self._title, self._subtitle = title, subtitle

    def get_title(self):
        return self._title.get_label()

    def set_title(self, text):
        self._title.set_label(text or "")

    def get_subtitle(self):
        return self._subtitle.get_label() if self._subtitle.get_visible() else None

    def set_subtitle(self, text):
        self._subtitle.set_label(text or "")
        self._subtitle.set_visible(bool(text))


@ui.template("window")
class ViewerWindow(Adw.ApplicationWindow):
    """The TV window (ui/window.blp) around pane: its header bar shows title (the VM's platform and
    its version) and subtitle (the VM's name, board and firmware version; main.titles) between the
    remote's keys and the window's buttons; window_title is the window manager's. Prefers the dark
    style."""

    __gtype_name__ = "ViewerWindow"
    toolbar = Gtk.Template.Child()
    title_label = Gtk.Template.Child()
    subtitle_label = Gtk.Template.Child()
    back_button = Gtk.Template.Child()
    home_button = Gtk.Template.Child()
    settings_button = Gtk.Template.Child()
    screenshot_button = Gtk.Template.Child()
    rotate_button = Gtk.Template.Child()
    mouse_button = Gtk.Template.Child()
    input_button = Gtk.Template.Child()
    more_button = Gtk.Template.Child()
    rotate_separator = Gtk.Template.Child()
    fullscreen_button = Gtk.Template.Child()
    remote_button = Gtk.Template.Child()
    local_key_group = Gtk.Template.Child()
    local_key_button = Gtk.Template.Child()
    joystick_button = Gtk.Template.Child()
    joystick_popover = Gtk.Template.Child()
    joystick_up = Gtk.Template.Child()
    joystick_down = Gtk.Template.Child()
    joystick_left = Gtk.Template.Child()
    joystick_right = Gtk.Template.Child()
    joystick_press = Gtk.Template.Child()
    toasts = Gtk.Template.Child()

    def __init__(self, app, pane, size, title=None, subtitle=None, window_title=None):
        Gtk.Window.set_default_icon_name(ui.APP_ID)  # this window's and the remote's (ui.icons)
        super().__init__(application=app)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.PREFER_DARK)
        self.pane = pane
        self.title = Title(self.title_label, self.subtitle_label)
        self._size = size
        self._fitted = False
        if title:
            self.title.set_title(title)
        if window_title:
            self.set_title(window_title)
        if subtitle:
            self.title.set_subtitle(subtitle)
        self.toasts.set_child(pane)
        keys = {"BACK": self.back_button, "HOME": self.home_button, "MENU": self.settings_button}
        if pane.system_layout.name == "netcast":
            self.home_button.set_icon_name("view-grid-symbolic")
            self.home_button.set_tooltip_text("NetCast (Super)")
        if pane.system_layout.name in ("netcast", "simplesmart"):
            self.settings_button.set_tooltip_text("Menu")
        pane.attach(self.remote_button, keys, self.toasts, self.toggle_fullscreen)
        # the set's own key, on a model with one: a onekey set's key, else a joystick
        if pane.local_key:
            from .pane import LOCAL_DOWN, LOCAL_LEFT, LOCAL_PRESS, LOCAL_RIGHT, LOCAL_UP

            self.local_key_group.set_visible(True)
            onekey = pane.local_key == "onekey"
            self.local_key_button.set_visible(onekey)
            self.joystick_button.set_visible(not onekey)
            pane.attach_local_key(
                {LOCAL_PRESS: self.local_key_button}
                if onekey
                else {
                    LOCAL_UP: self.joystick_up,
                    LOCAL_DOWN: self.joystick_down,
                    LOCAL_LEFT: self.joystick_left,
                    LOCAL_RIGHT: self.joystick_right,
                    LOCAL_PRESS: self.joystick_press,
                }
            )
            pane.attach_key_leds((self.local_key_button, self.joystick_button))
        self.add_controller(key_controller(pane))
        for name, fn in (
            ("fullscreen", lambda *_: self.toggle_fullscreen()),
            ("screenshot", lambda *_: pane.screenshot()),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", fn)
            self.add_action(action)
        # Rotate: a toggle between landscape and portrait, its state the guest's reported rotation
        self.rotate_action = Gio.SimpleAction.new_stateful(
            "rotate", None, GLib.Variant.new_boolean(False)
        )
        self.rotate_action.connect("change-state", lambda *_: pane.rotate())
        self.add_action(self.rotate_action)
        w, h = size
        bar_h = pane.bar.measure(Gtk.Orientation.VERTICAL, -1)[1] if pane.bar else 0
        self.set_default_size(w, h + bar_h)
        # a set that turns to portrait (vm-info.toml's portrait): its rotate button, where the
        # VM is a library's (beanjar sensor set rotate NAME), and the window turned with the picture
        self.rotate_button.set_visible(pane.can_rotate())
        self.rotate_separator.set_visible(pane.can_rotate())
        # the planes shown (More), a view setting of this window: the core leaves the hidden ones
        # out of its picture and Save Screenshot, never out of the guest's capture or `beanjar
        # screenshot`; from --hide-planes at start, not kept
        hidden = pane.core.planes()
        for name, bit in PLANES:
            action = Gio.SimpleAction.new_stateful(
                f"plane-{name}", None, GLib.Variant.new_boolean(not hidden & bit)
            )
            action.connect("change-state", self.plane_changed, bit)
            self.add_action(action)
        # Elements (More): screen recognition over the picture, a debug layer of this window,
        # off at start unless --elements (main.py)
        self.elements_action = Gio.SimpleAction.new_stateful(
            "layer-elements", None, GLib.Variant.new_boolean(False)
        )
        self.elements_action.connect(
            "change-state", lambda a, v: self.set_elements(v.get_boolean())
        )
        self.add_action(self.elements_action)
        pane.on_turned = self.turned
        pane.on_rotation = self.rotation_reported
        # a model with a touch screen (vm-info.toml's touch): the Mouse toggle, pointer or finger
        # (TvView.set_mouse_touch), kept for this viewer's session; the pointer at start
        self.mouse_action = Gio.SimpleAction.new_stateful(
            "mouse-touch", None, GLib.Variant.new_boolean(False)
        )
        self.mouse_action.connect(
            "change-state", lambda a, v: self.set_mouse_touch(v.get_boolean())
        )
        self.add_action(self.mouse_action)
        self.mouse_button.set_visible(pane.view.touch)
        self.input_button.set_visible(pane.view.touch)

    def plane_changed(self, action, value, bit):
        """A plane of More shown (value true) or hidden, in this window's picture."""
        hidden = self.pane.core.planes()
        hidden = hidden & ~bit if value.get_boolean() else hidden | bit
        action.set_state(GLib.Variant.new_boolean(not self.pane.core.planes(hidden) & bit))

    def set_elements(self, on):
        """The Elements layer shown (on) or hidden; stays off when go-textshot is missing."""
        on = self.pane.elements.set_active(on)
        self.elements_action.set_state(GLib.Variant.new_boolean(on))
        return on

    MOUSE_TIPS = {
        False: "Mouse: pointer (click to make it a finger on the touch screen)",
        True: "Mouse: touch (its left button is a finger; click for the pointer)",
    }

    def set_mouse_touch(self, on):
        """The Mouse toggle: the mouse's left button a finger on the touch screen (on) or the
        Magic Remote's pointer; only on a model with a touch screen."""
        on = bool(on and self.pane.view.touch)
        self.pane.view.set_mouse_touch(on)
        self.mouse_action.set_state(GLib.Variant.new_boolean(on))
        self.mouse_button.set_tooltip_text(self.MOUSE_TIPS[on])

    def rotation_reported(self, angle):
        """The Rotate toggle shows the set: portrait at any angle but 0."""
        self.rotate_action.set_state(GLib.Variant.new_boolean(angle != 0))
        self.rotate_button.set_tooltip_text("Portrait" if angle else "Landscape")

    def turned(self, odd):
        """The picture turned to portrait (odd) or back: the window's picture area turned with it,
        unless the window is maximized or fullscreen."""
        if not self._fitted or self.is_maximized() or self.is_fullscreen():
            return
        vw, vh = self.pane.view.get_width(), self.pane.view.get_height()
        if vw <= 0 or vh <= 0 or (vh > vw) == odd:
            return
        w, h = self.get_default_size()  # GTK keeps it the window's size as it is resized
        self.set_default_size(w - vw + vh, h - vh + vw)

    def toggle_fullscreen(self):
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()

    def set_chrome(self, shown):
        """The header bar and the status bar: hidden in fullscreen."""
        self.toolbar.set_reveal_top_bars(shown)
        if self.pane.bar:
            self.pane.bar.set_visible(shown)

    @Gtk.Template.Callback()
    def on_fullscreened(self, _w, _pspec):
        self.set_chrome(not self.is_fullscreen())

    @Gtk.Template.Callback()
    def on_remote_toggled(self, button):
        self.pane.remote_toggled(button.get_active())

    @Gtk.Template.Callback()
    def on_active_changed(self, _w, _pspec):
        if not self.is_active():
            self.pane.release()

    @Gtk.Template.Callback()
    def on_map(self, _w):
        # One correction once laid out: the picture area gets --size (titlebars differ).
        def fit():
            if self._fitted:
                return False
            vw, vh = self.pane.view.get_width(), self.pane.view.get_height()
            if vw <= 0 or vh <= 0:
                return True
            self._fitted = True
            if not (self.is_maximized() or self.is_fullscreen()):
                w, h = self.get_default_size()
                self.set_default_size(w + self._size[0] - vw, h + self._size[1] - vh)
            GLib.idle_add(lambda: (self.pane.realized_start(), False)[1])
            return False

        GLib.timeout_add(50, fit)

    @Gtk.Template.Callback()
    def on_close_request(self, _w):
        self.pane.finish()
        return False
