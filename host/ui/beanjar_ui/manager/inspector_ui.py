# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The selected VM's traffic inspector, a card of its Summary: its own mitmweb (each VM with mode
mitm has one, on its own port and with its own password; the CA it signs with is the library's).
One heading row: its status and URL, Copy URL and Open in Browser (logged in); under it one folded
"Connection details and credentials" with the password (Copy Password), with link lan the VM's
leased LAN address and "On this host", the mitmproxy it runs (its version, or what is wrong and
how to install it: mitmtool.py) and the proxy's last lines. While the VM is off the card is one line:
"Starts with the VM (MITM)", or, without a usable mitmweb, "Off: mitmproxy not installed" (or too
old) with the reason and the install command below it. mitmtool.check() runs on a worker thread:
its first look at a mitmweb may run `mitmweb --version`. The full mitmweb UI
opens in the system browser. With link lan the URL is the LAN one, for other machines; this host
cannot reach its own macvlan children, so its browser opens the inspector at the VM's own address,
shown as "On this host"."""

import threading
import time

from gi.repository import Gio, GLib, Gtk, Pango

from beanjar import base, inspector, mitmtool, vmsettings

DESCRIPTION = (
    "This VM's own mitmweb, on its own port and with its own password, while it runs. "
    "The CA certificate it signs with is the library's, shared by all of its VMs."
)
OFF_TEXT = "Starts with the VM (MITM)"
OFF_PROBLEM_TEXT = "Off: {}"  # mitmtool.Tool.brief(): the VM will start without its inspector


def detail_row(title, value, *suffix):
    """A "title value [suffix...]" row of the details, the value selectable."""
    row = Gtk.Box(spacing=8)
    label = Gtk.Label(label=title, xalign=0)
    label.add_css_class("dim-label")
    row.append(label)
    row.append(value)
    for widget in suffix:
        row.append(widget)
    return row


def value_label(mono=False):
    label = Gtk.Label(
        xalign=0,
        hexpand=True,
        selectable=True,
        ellipsize=Pango.EllipsizeMode.END,
        max_width_chars=32,
    )
    if mono:
        label.add_css_class("monospace")
    return label


class Panel(Gtk.Box):
    def __init__(self, manager):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("card")
        self.manager = manager
        self.key = None
        self.generation = 0
        self.next_check = 0
        self.pending = False
        self.closed = False
        self.vm_name = None
        self.tool = None  # mitmtool.check()'s last answer, None until the first one
        self.tool_pending = False
        self.state = inspector.State("mitm", "Not running")
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
        title = Gtk.Label(label="Traffic Inspector", xalign=0, valign=Gtk.Align.CENTER)
        title.add_css_class("heading")
        # "Running · http://...": its status, then its URL (dim; the URL wraps when narrow)
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
            icon_name="edit-copy-symbolic", tooltip_text="Copy URL", valign=Gtk.Align.CENTER
        )
        self.copy.add_css_class("flat")
        self.copy.connect("clicked", self.copy_url)
        self.open = Gtk.Button(label="Open", valign=Gtk.Align.CENTER)
        self.open.connect("clicked", self.open_browser)
        for widget in (title, self.status, self.address, self.copy, self.open):
            heading.append(widget)
        content.append(heading)
        # off without a usable mitmweb: why, and what to install (mitmtool.Tool.problem)
        self.problem = Gtk.Label(
            xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True, visible=False
        )
        self.problem.add_css_class("dim-label")
        content.append(self.problem)
        self.details = Gtk.Expander(label="Connection details and credentials")
        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=10)
        self.details.set_child(details)
        content.append(self.details)
        self.description = Gtk.Label(
            label=DESCRIPTION, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR
        )
        self.description.add_css_class("dim-label")
        details.append(self.description)
        # the inspector's password (netns.py makes one per start); Open in Browser logs in with it
        self.password = value_label(mono=True)
        self.copy_password = Gtk.Button(label="Copy Password")
        self.copy_password.connect("clicked", self.copy_secret)
        self.credentials = detail_row("Password", self.password, self.copy_password)
        details.append(self.credentials)
        # link lan: the address the VM leased on the LAN (its ports, and the inspector's, are there)
        self.lan_address = value_label()
        self.lan_row = detail_row("LAN address", self.lan_address)
        details.append(self.lan_row)
        # link lan: the URL that works from this host (the one above is the LAN's), which Open uses
        self.local_url = value_label()
        self.local_row = detail_row("On this host", self.local_url)
        details.append(self.local_row)
        # the user's own mitmproxy (mitmtool.py): its version, or why it cannot serve
        self.installation = Gtk.Label(xalign=0, hexpand=True, wrap=True, selectable=True)
        self.installation.add_css_class("dim-label")
        details.append(self.installation)
        diagnostics = Gtk.Label(label="Proxy diagnostics", xalign=0)
        diagnostics.add_css_class("dim-label")
        details.append(diagnostics)
        self.log = Gtk.TextView(
            editable=False,
            cursor_visible=False,
            monospace=True,
            wrap_mode=Gtk.WrapMode.CHAR,
            top_margin=8,
            bottom_margin=8,
            left_margin=8,
            right_margin=8,
        )
        self.scroll = Gtk.ScrolledWindow(min_content_height=100, max_content_height=100)
        self.scroll.set_child(self.log)
        details.append(self.scroll)

    def show_state(self, state):
        self.state = state
        running = state.status != "Not running"
        # off: one line, what the VM's mode means for its next start; without a usable mitmweb,
        # that it will be off and (a second line) why and what to install
        broken = not running and self.tool is not None and not self.tool.usable
        if running:
            self.status.set_text(state.status + (" ·" if state.url else ""))
        elif broken:
            self.status.set_text(OFF_PROBLEM_TEXT.format(self.tool.brief()))
        else:
            self.status.set_text(OFF_TEXT)
        self.status.set_tooltip_text(self.tool.problem if broken else None)
        self.problem.set_visible(broken)
        self.problem.set_text(self.tool.problem if broken else "")
        self.address.set_visible(bool(state.url))
        self.address.set_text(state.url or "")
        for widget in (self.copy, self.open, self.details):
            widget.set_visible(running)
        self.local_row.set_visible(bool(state.local_url))
        self.local_url.set_text(state.local_url)
        self.open.set_sensitive(state.ready)
        self.open.set_tooltip_text(
            f"Open in Browser: {state.here_url}" if state.here_url else "Open in Browser"
        )
        self.copy.set_sensitive(bool(state.url))
        self.password.set_text(state.password or ("Password unavailable" if state.url else "—"))
        self.password.set_tooltip_text(state.password_file or None)
        self.copy_password.set_sensitive(bool(state.password))

    def refresh(self, vm):
        """Follow vm, the selected VM (None: none, or one that does not read): shown only while
        its network is mitm, with its endpoint while it runs."""
        try:
            mitm_vm = (
                vm is not None
                and vmsettings.guest_net(vm.config.get("link"), vm.config.get("mode")) == "mitm"
            )
        except base.Error:
            mitm_vm = False
        self.set_visible(mitm_vm)
        if not mitm_vm:
            vm = None
        lan = vm is not None and vm.config.get("link") == "lan"
        self.lan_row.set_visible(lan)
        if lan:
            self.lan_address.set_text(vm.lan_address() or "Not running")
        state = inspector.snapshot(vm)
        key = (vm.name if vm else None, vm.pid() if vm else None, state)
        now = time.monotonic()
        if key != self.key:
            self.key = key
            self.generation += 1
            self.next_check = 0
            self.show_state(state)
        mitm = state.mode == "mitm"
        if now < self.next_check:
            return
        self.next_check = now + 5
        self.check_tool()
        text = inspector.log_tail(vm)
        text = text or "No proxy diagnostics yet."
        buffer = self.log.get_buffer()
        if buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False) != text:
            buffer.set_text(text)
        if mitm and state.url and not self.pending:
            self.pending = True
            generation = self.generation

            def worker():
                result = inspector.probe(state)
                GLib.idle_add(self.checked, generation, result)

            threading.Thread(target=worker, daemon=True).start()

    def check_tool(self):
        """mitmtool.check() off the main thread (a new mitmweb's version may need a `mitmweb
        --version`); tool_checked shows the answer."""
        if self.tool_pending:
            return
        self.tool_pending = True

        def worker():
            tool = mitmtool.check()
            GLib.idle_add(self.tool_checked, tool)

        threading.Thread(target=worker, daemon=True).start()

    def tool_checked(self, tool):
        self.tool_pending = False
        if self.closed:
            return False
        self.installation.set_text(tool.describe())
        if tool != self.tool:
            self.tool = tool
            self.show_state(self.state)
        return False

    def checked(self, generation, state):
        self.pending = False
        if not self.closed and generation == self.generation:
            self.show_state(state)
        return False

    def open_browser(self, *_):
        if not self.state.ready:
            return
        try:
            # the browser runs on this host, which reaches a link lan VM only at its local_url
            Gio.AppInfo.launch_default_for_uri(self.state.here_login_url, None)
        except GLib.Error as exc:
            self.manager.say(f"Could not open inspector: {exc.message}")

    def copy_url(self, *_):
        # the plain address: a pasted link does not carry the password
        if self.state.url:
            self.get_clipboard().set(self.state.url)
            self.manager.say("Inspector URL copied")

    def copy_secret(self, *_):
        if self.state.password:
            self.get_clipboard().set(self.state.password)
            self.manager.say("Inspector password copied")
