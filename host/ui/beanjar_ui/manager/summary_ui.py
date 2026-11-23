# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The selected VM's Summary, top to bottom: a problem banner (the window's, above the scrolled
page: set_problem), four stat tiles with the TV's picture beside them, the TV line (webOS, board
and version, firmware source), why it runs (its reason), its layers and tags; then the Hardware,
Identity and Network cards, each with an Edit that opens Settings at its page (win.settings-page,
insensitive while the VM runs); the traffic inspector's and VNC's cards (inspector_ui, vnc_ui,
inserted by gui.py at inspector_slot); and the files and runtime details, folded.

Running, the tiles are State (with QMP's detail and "Viewer open"), Uptime, CPU and Memory (QEMU's
footprint on the host, with the GPU's use); off, State, Last run, CPUs and Memory as set. Three
layouts follow the window (set_layout, from its breakpoints, follow): wide, the four tiles in a row
with the TV line and reason under them and the picture at their right; medium, the tiles 2x2 with
the picture at their right; narrow, the tiles 2x2 and the picture the full width under them, the
cards in one column."""

import datetime

from gi.repository import Gdk, GLib, GObject, Gtk, Pango

from . import picture

WIDE, MEDIUM, NARROW = "wide", "medium", "narrow"


def reason_note(reason, now=None):
    """When and by whom a reason was set: "set 10:58 by user" (today), "set 2026-11-11 10:58 by
    user, agent" (before)."""
    try:
        when = datetime.datetime.strptime(reason.get("set") or "", "%Y-%m-%dT%H:%M:%S%z")
        now = now or datetime.datetime.now(when.tzinfo)
        when = when.astimezone(now.tzinfo)
        stamp = when.strftime("%H:%M" if when.date() == now.date() else "%Y-%m-%d %H:%M")
    except ValueError:
        stamp = reason.get("set") or "?"
    who = ", ".join(filter(None, (reason.get("user"), reason.get("by"))))
    return f"set {stamp}" + (f" by {who}" if who else "")


CSS = b"""
.beanjar-layer-tag {
    padding: 2px 8px;
    border-radius: 999px;
    background-color: alpha(currentColor, 0.08);
}
.beanjar-user-tag {
    padding: 2px 8px;
    border-radius: 999px;
    background-color: alpha(@accent_bg_color, 0.14);
    color: @accent_color;
}
.beanjar-reason {
    color: @warning_color;
}
.beanjar-tile { padding: 10px 14px; }
.beanjar-tile-value { font-size: 1.35em; font-weight: 700; }
.beanjar-tile-dot { font-size: 0.8em; }
"""


class Tile(Gtk.Box):
    """A stat tile: a caption-sized title, a big value (after a state dot for State), a caption."""

    def __init__(self, dot=False):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self.add_css_class("card")
        self.add_css_class("beanjar-tile")
        self.title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.title.add_css_class("caption")
        self.title.add_css_class("dim-label")
        self.append(self.title)
        row = Gtk.Box(spacing=8)
        self.dot = None
        if dot:
            self.dot = Gtk.Label(label="●", valign=Gtk.Align.CENTER)
            self.dot.add_css_class("beanjar-tile-dot")
            row.append(self.dot)
        self.value = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.value.add_css_class("beanjar-tile-value")
        self.value.add_css_class("numeric")
        row.append(self.value)
        self.append(row)
        self.caption = Gtk.Label(label=" ", xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.caption.add_css_class("caption")
        self.caption.add_css_class("dim-label")
        self.append(self.caption)

    def set(self, title, value, caption=""):
        for label, text in (
            (self.title, title),
            (self.value, value),
            (self.caption, caption or " "),
        ):
            if label.get_text() != text:
                label.set_text(text)

    def set_phase(self, cls):
        """The dot's colour: widgets.VMRow.PHASE_CLASS's (success, warning, error) or dimmed."""
        for c in ("success", "warning", "error", "dimmed"):
            (self.dot.add_css_class if c == cls else self.dot.remove_css_class)(c)


class Summary(Gtk.Box):
    TILES = ("state", "time", "cpu", "memory")

    def __init__(self, manager, banner=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.manager = manager
        self.banner = banner if banner is not None else Gtk.Box()  # the window's Adw.Banner
        self.problem = None
        self.tag_style = Gtk.CssProvider()
        self.tag_style.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self.tag_style, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self.values = {}
        self.fields = {}
        self.layout = None
        # at a glance: the tiles and the picture, the TV line, the reason, the chips
        self.glance = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.append(self.glance)
        self.tiles = {key: Tile(dot=key == "state") for key in self.TILES}
        self.state = self.tiles["state"].value
        self.tile_grid = Gtk.Grid(column_spacing=10, row_spacing=10, hexpand=True)
        self.picture = picture.TvPicture()
        self.top = Gtk.Box(spacing=16)
        self.left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, hexpand=True)
        self.top.append(self.left)
        self.glance.append(self.top)
        self.firmware = Gtk.Label(
            xalign=0, selectable=True, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR
        )
        self.reason = Gtk.Box(spacing=6, visible=False)
        pin = Gtk.Image(icon_name="view-pin-symbolic")
        pin.add_css_class("beanjar-reason")
        self.reason_text = Gtk.Label(
            xalign=0, selectable=True, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR
        )
        self.reason_text.add_css_class("beanjar-reason")
        self.reason_set = Gtk.Label(xalign=0)
        self.reason_set.add_css_class("dim-label")
        for widget in (pin, self.reason_text, self.reason_set):
            self.reason.append(widget)
        # the layers, and the VM's tags beside them (under them, narrow)
        self.chips = Gtk.Box(spacing=24)
        layers, self.layers = self.chip_group("Layers", True)
        self.tags_group, self.tags = self.chip_group("Tags", False)
        self.tags_group.set_visible(False)
        self.chips.append(layers)
        self.chips.append(self.tags_group)
        self.set_layout(WIDE)
        # the settings, in cards with an Edit each
        self.cards = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.append(self.cards)
        self.edits = []
        self.rows = {}
        hardware = self.card("Hardware", "hardware")
        for key, title in (
            ("smp", "CPUs"),
            ("mem", "Memory"),
            ("gpu", "GPU"),
            ("display", "Display"),
            ("audio", "Audio"),
            ("free_page_reporting", "Free page reporting"),
        ):
            hardware.append(self.field(key, title))
        identity = self.card("Identity", "identity")
        for key, title in (("mac", "MAC"), ("serial", "Serial")):
            identity.append(self.field(key, title, copy=True))
        identity.append(self.field("model_key", "Model key"))
        network = self.card("Network", "network")
        # the address its ports are on, with them, over the card's whole width
        self.address_row = self.field("address", "Host address", copy=True, wide=True)
        self.lan_row = self.field("lan_address", "LAN address", copy=True, wide=True)
        box = network.get_parent()
        box.insert_child_after(self.address_row, None)
        box.insert_child_after(self.lan_row, self.address_row)
        for key, title in (
            ("network", "Connection mode"),
            ("listen", "Service ports on"),
            ("mitm_flows", "Inspector keeps"),
            ("mitm_flow_bytes", "Body size limit"),
        ):
            network.append(self.field(key, title))
        self.inspector_slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.append(self.inspector_slot)
        self.runtime = Gtk.Expander(label="Files and runtime details")
        files = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=10)
        for key, title, action, tooltip in (
            ("directory", "VM directory", "win.folder", "Open VM Folder"),
            ("logs", "Logs", "win.logs", "Open Log Folder"),
            ("firmware_path", "Firmware location", None, None),
            ("unit", "systemd unit", None, None),
            ("qemu_pid", "QEMU PID", None, None),
            ("viewer_pid", "Viewer PID", None, None),
            ("restarts", "Guest restarts", None, None),
        ):
            row = self.field(key, title, wrap=True, title_chars=16)
            if action:
                button = Gtk.Button(
                    label="Open", action_name=action, tooltip_text=tooltip, valign=Gtk.Align.CENTER
                )
                button.add_css_class("flat")
                row.append(button)
                # centred with the button (a taller line than the text), not on the first line
                self.fields[key].set_valign(Gtk.Align.CENTER)
                row.get_first_child().set_valign(Gtk.Align.CENTER)
            files.append(row)
        self.runtime.set_child(files)
        self.append(self.runtime)

    # -- layout --
    def follow(self, window):
        """Follow the window's breakpoints (window.blp: medium, narrow): the layout (set_layout),
        and narrow the tags under the layers."""
        vertical = GObject.Value(Gtk.Orientation, Gtk.Orientation.VERTICAL)
        window.narrow.add_setter(self.chips, "orientation", vertical)
        window.narrow.add_setter(self.chips, "spacing", GObject.Value(int, 8))

        def changed(*_):
            bp = window.get_current_breakpoint()
            self.set_layout(
                NARROW if bp is window.narrow else MEDIUM if bp is window.medium else WIDE
            )

        window.connect("notify::current-breakpoint", changed)
        changed()

    def set_layout(self, layout):
        """WIDE: four tiles in a row, the TV line and reason under them, the picture at their right
        and the chips under all; MEDIUM: the tiles 2x2 and the picture at their right, then the
        TV line, reason and chips; NARROW: the tiles 2x2, the picture the full width, then the rest."""
        if layout == self.layout:
            return
        self.layout = layout
        for widget in (self.tile_grid, self.firmware, self.reason, self.chips, self.picture):
            if widget.get_parent() is not None:
                widget.get_parent().remove(widget)
        for tile in self.tiles.values():
            if tile.get_parent() is not None:
                self.tile_grid.remove(tile)
        cols = 4 if layout == WIDE else 2
        self.tile_grid.set_column_homogeneous(cols == 2)
        self.tile_grid.set_row_homogeneous(True)
        for i, key in enumerate(self.TILES):
            self.tile_grid.attach(self.tiles[key], i % cols, i // cols, 1, 1)
        self.left.append(self.tile_grid)
        if layout == WIDE:
            self.left.append(self.firmware)
            self.left.append(self.reason)
            self.top.append(self.picture)
            self.picture.set_size(picture.WIDE)
            self.glance.append(self.chips)
        elif layout == MEDIUM:
            self.top.append(self.picture)
            self.picture.set_size(picture.MEDIUM)
            for widget in (self.firmware, self.reason, self.chips):
                self.glance.append(widget)
        else:
            self.left.append(self.picture)
            self.picture.set_size(None)
            for widget in (self.firmware, self.reason, self.chips):
                self.glance.append(widget)

    # -- pieces --
    def chip_group(self, title, expand):
        group = Gtk.Box(spacing=12, hexpand=expand)
        group.append(Gtk.Label(label=title, xalign=0, valign=Gtk.Align.CENTER))
        flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            homogeneous=False,
            min_children_per_line=1,
            max_children_per_line=12,
            column_spacing=6,
            row_spacing=6,
            hexpand=expand,
            halign=Gtk.Align.START,
        )
        group.append(flow)
        return group, flow

    @staticmethod
    def set_chips(flow, names, style):
        while child := flow.get_first_child():
            flow.remove(child)
        for name in names:
            chip = Gtk.Label(label=name, halign=Gtk.Align.START)
            chip.add_css_class(style)
            flow.append(chip)

    def set_edit_tooltip(self, text):
        for edit in self.edits:
            edit.set_tooltip_text(text)

    @staticmethod
    def flow():
        return Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            homogeneous=False,
            min_children_per_line=1,
            max_children_per_line=2,
            column_spacing=12,
            row_spacing=12,
        )

    def card(self, title, page=None):
        """A group with a heading (and with page, an Edit for Settings at that page) over a card;
        its fields' flow box."""
        edit = None
        if page:
            edit = Gtk.Button(
                label="Edit",
                valign=Gtk.Align.CENTER,
                action_name="win.settings-page",
                action_target=GLib.Variant.new_string(page),
            )
            edit.add_css_class("flat")
            self.edits.append(edit)
        group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        head = Gtk.Box(spacing=8)
        heading = Gtk.Label(label=title, xalign=0, hexpand=True)
        heading.add_css_class("heading")
        head.append(heading)
        if edit is not None:
            head.append(edit)
        group.append(head)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.add_css_class("card")
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        card.append(content)
        group.append(card)
        self.cards.append(group)
        fields = self.flow()
        content.append(fields)
        return fields

    def field(self, key, title, copy=False, wrap=False, title_chars=None, wide=False):
        """A "title value" row; copy: with a Copy button (copy()); wide: across the card, its value
        right-aligned and wrapping."""
        row = Gtk.Box(spacing=10, hexpand=True)
        if not wrap and not wide:
            row.set_size_request(260, -1)
        alignment = Gtk.Align.START if wrap else Gtk.Align.CENTER
        label = Gtk.Label(label=title, xalign=0, valign=alignment)
        if copy and not wide:
            label.set_width_chars(6)
        if title_chars:
            label.set_width_chars(title_chars)
        label.add_css_class("dim-label")
        value = Gtk.Label(
            xalign=0 if wrap or (copy and not wide) else 1,
            valign=alignment,
            hexpand=True,
            selectable=True,
            ellipsize=Pango.EllipsizeMode.NONE if wrap or wide else Pango.EllipsizeMode.END,
            wrap=wrap or wide,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            max_width_chars=24 if not wide else 48,
        )
        if wide:
            value.set_justify(Gtk.Justification.RIGHT)
        row.append(label)
        row.append(value)
        self.fields[key] = value
        self.rows[key] = row
        if copy:
            button = Gtk.Button(
                icon_name="edit-copy-symbolic",
                tooltip_text=f"Copy {title}",
                valign=Gtk.Align.CENTER,
            )
            button.add_css_class("flat")
            button.connect("clicked", lambda *_: self.copy(key, title))
            row.append(button)
        return row

    def show_row(self, key, shown):
        """Show or hide a field's row (in a flow box: its child)."""
        row = self.rows[key]
        parent = row.get_parent()
        (parent if isinstance(parent, Gtk.FlowBoxChild) else row).set_visible(shown)

    def copy(self, key, title):
        value = self.values.get(key)
        if value:
            self.get_clipboard().set(value)
            self.manager.say(f"{title} copied")

    # -- contents --
    def set_problem(self, problem):
        """The banner (summary_facts.problem), or None: hidden."""
        if problem == self.problem:
            return
        self.problem = problem
        b = self.banner
        if not hasattr(b, "set_revealed"):
            return
        if problem is None:
            b.set_revealed(False)
            return
        b.set_title(GLib.markup_escape_text(problem.title))
        b.set_button_label(problem.button)
        b.set_action_name(f"win.{problem.action}" if problem.action else None)
        b.set_tooltip_text(problem.tooltip)
        b.set_revealed(True)

    def set_tiles(self, tiles, phase_class=None):
        """tiles: {key: (title, value, caption)} for those of TILES that change; phase_class: the
        state dot's colour (widgets.VMRow.PHASE_CLASS's, or dimmed), None to keep it."""
        for key, (title, value, caption) in tiles.items():
            self.tiles[key].set(title, value, caption)
        if phase_class is not None:
            self.tiles["state"].set_phase(phase_class)

    def set_tv_line(self, tv, source):
        """The TV line: tv (webOS, board and version) then source (firmware, not tested), dimmed."""
        parts = [GLib.markup_escape_text(tv)] if tv else []
        if source:
            sep = " · " if tv else ""
            parts.append(f"<span alpha='55%'>{GLib.markup_escape_text(sep + source)}</span>")
        self.firmware.set_markup("".join(parts))

    def update(
        self,
        values,
        tv="",
        source="",
        layers=(),
        reason=None,
        tags=(),
        full=None,
        shown=None,
        copies=None,
    ):
        """Show values (key: text), the TV line (tv, source) and the chips; reason: why the VM runs
        (VM.reason: text, set, user, by), None if it has none; full: the values whose tooltip and
        copy are not the text shown (key: text, e.g. a path shown with ~); shown: {key: bool} for
        rows shown only sometimes (address, lan_address, listen, audio, free_page_reporting,
        restarts, mitm_flows, mitm_flow_bytes); copies: what a field's Copy copies when it is not
        its text (the address without its ports)."""
        full = full or {}
        self.values = {**values, **full, **(copies or {})}
        self.set_tv_line(tv, source)
        self.firmware.set_tooltip_text(values.get("fw") or None)
        for key, label in self.fields.items():
            text = values.get(key) or "—"
            if label.get_text() != text:
                label.set_text(text)
            whole = full.get(key)
            label.set_tooltip_text(whole if whole and whole != text else None)
        for key, on in (shown or {}).items():
            self.show_row(key, on)
        layer_names = tuple(layers)
        if getattr(self, "layer_names", None) != layer_names:
            self.layer_names = layer_names
            self.set_chips(self.layers, layer_names, "beanjar-layer-tag")
        tag_names = tuple(tags)
        if getattr(self, "tag_names", None) != tag_names:
            self.tag_names = tag_names
            self.set_chips(self.tags, tag_names, "beanjar-user-tag")
            self.tags_group.set_visible(bool(tag_names))
        self.chips.set_visible(bool(layer_names or tag_names))
        self.reason.set_visible(bool(reason))
        if reason:
            self.reason_text.set_text(reason["text"])
            self.reason_set.set_text(reason_note(reason))
            self.reason.set_tooltip_text(
                "Why it runs (beanjar status): Shut Down asks before overriding it"
            )
