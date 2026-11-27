# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The selected VM's VNC server, a card of its Summary after the traffic inspector, shown only while
`beanjar vnc` serves it (VM.vnc_info). One heading row: "Serving", its address, Copy Address and
Open (a VNC client, the system's handler of vnc:// addresses); under it one folded "Connection
details and credentials" with link lan "On this host" for this host's way in (its own address) and
its password (always one, random per start unless BEANJAR_VNC_PASSWORD chose it, in the owner-only
run/vnc.password) with Copy Password."""

from beanjar.network import inspector
from gi.repository import Gio, GLib, Gtk, Pango

from .inspector_ui import detail_row, value_label

DESCRIPTION = (
    "beanjar vnc serves this VM's screen to VNC clients, behind its own password (RSA-AES)."
)


class Panel(Gtk.Box):
    def __init__(self, manager):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("card")
        self.manager = manager
        self.info = None
        self.secret = ""
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        self.append(content)
        heading = Gtk.Box(spacing=8)
        title = Gtk.Label(label="VNC", xalign=0, valign=Gtk.Align.CENTER)
        title.add_css_class("heading")
        self.status = Gtk.Label(xalign=1, hexpand=True, valign=Gtk.Align.CENTER)
        self.status.add_css_class("dim-label")
        self.address = Gtk.Label(
            xalign=1,
            valign=Gtk.Align.CENTER,
            selectable=True,
            wrap=True,
            wrap_mode=Pango.WrapMode.CHAR,
            natural_wrap_mode=Gtk.NaturalWrapMode.NONE,
            max_width_chars=30,
            justify=Gtk.Justification.RIGHT,
        )
        self.address.add_css_class("dim-label")
        self.copy = Gtk.Button(
            icon_name="edit-copy-symbolic", tooltip_text="Copy Address", valign=Gtk.Align.CENTER
        )
        self.copy.add_css_class("flat")
        self.copy.connect("clicked", self.copy_address)
        self.open = Gtk.Button(
            label="Open", tooltip_text="Open in a VNC Client", valign=Gtk.Align.CENTER
        )
        self.open.connect("clicked", self.open_client)
        for widget in (title, self.status, self.address, self.copy, self.open):
            heading.append(widget)
        content.append(heading)
        self.details = Gtk.Expander(label="Connection details and credentials")
        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=10)
        self.details.set_child(details)
        content.append(self.details)
        description = Gtk.Label(
            label=DESCRIPTION, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR
        )
        description.add_css_class("dim-label")
        details.append(description)
        # link lan: this host's way in (the address above is the LAN's, which the host cannot reach)
        self.local_url = value_label()
        self.local_row = detail_row("On this host", self.local_url)
        details.append(self.local_row)
        self.password = value_label(mono=True)
        self.copy_password = Gtk.Button(label="Copy Password")
        self.copy_password.connect("clicked", self.copy_secret)
        details.append(detail_row("Password", self.password, self.copy_password))
        self.set_visible(False)

    def show_info(self, info):
        """info: VM.vnc_info(), or None (hidden)."""
        self.info = info
        self.set_visible(bool(info))
        if not info:
            self.secret = ""
            return
        self.secret = inspector.read_password(info.get("password_file"))
        self.status.set_text("Serving ·")
        self.status.set_tooltip_text(f"beanjar-vnc pid {info['pid']}")
        self.address.set_text(info["url"])
        self.local_row.set_visible(bool(info.get("local_url")))
        self.local_url.set_text(info.get("local_url") or "")
        self.password.set_text(self.secret or "Password unavailable")
        self.password.set_tooltip_text(info.get("password_file"))
        self.copy_password.set_sensitive(bool(self.secret))
        self.open.set_sensitive(Gio.AppInfo.get_default_for_uri_scheme("vnc") is not None)

    def refresh(self, vm):
        """Follow vm, the selected VM (None: none, or one that does not read)."""
        info = vm.vnc_info() if vm is not None else None
        if info != self.info:
            self.show_info(info)

    def copy_address(self, *_):
        if self.info:
            self.get_clipboard().set(self.info["url"])
            self.manager.say("VNC address copied")

    def copy_secret(self, *_):
        if self.secret:
            self.get_clipboard().set(self.secret)
            self.manager.say("VNC password copied")

    def open_client(self, *_):
        """The system's VNC client at this host's address (link lan: its own, "On this host")."""
        if not self.info:
            return
        try:
            Gio.AppInfo.launch_default_for_uri(self.info.get("local_url") or self.info["url"], None)
        except GLib.Error as exc:
            self.manager.say(f"Could not open a VNC client: {exc.message}")
