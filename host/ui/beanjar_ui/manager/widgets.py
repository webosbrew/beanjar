# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The VM manager's window and dialogs as GTK templates: their layouts are host/ui/data/manager/*.blp
(ui.py loads them), what they do is gui.Manager's. Imported by gui.Manager, after --ui-dir."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, GObject, Graphene, Gtk  # noqa: E402

from . import ui  # noqa: E402
from beanjar.core import Error  # noqa: E402


@ui.template("window")
class ManagerWindow(Adw.ApplicationWindow):
    """ui/window.blp. on_secondary_click(row, x, y): the list was right-clicked on row (x, y in the
    list's coordinates); on_activate(row): a row was activated (double click, Enter; one click
    while the sidebar overlays the details)."""

    __gtype_name__ = "ManagerWindow"
    toasts = Gtk.Template.Child()
    window_title = Gtk.Template.Child()
    busy = Gtk.Template.Child()
    sidebar_button = Gtk.Template.Child()
    new_button = Gtk.Template.Child()
    stack = Gtk.Template.Child()
    empty = Gtk.Template.Child()
    empty_new = Gtk.Template.Child()
    split = Gtk.Template.Child()
    vm_list = Gtk.Template.Child()
    elsewhere_group = Gtk.Template.Child()
    elsewhere_list = Gtk.Template.Child()
    vm_title = Gtk.Template.Child()
    details = Gtk.Template.Child()
    summary = Gtk.Template.Child()
    console = Gtk.Template.Child()
    output = Gtk.Template.Child()
    narrow = Gtk.Template.Child()
    medium = Gtk.Template.Child()
    summary_banner = Gtk.Template.Child()
    state_button = Gtk.Template.Child()
    vm_menu_button = Gtk.Template.Child()
    main_menu_button = Gtk.Template.Child()
    vm_context_menu = Gtk.Template.Child()
    start_menu = Gtk.Template.Child()
    running_menu = Gtk.Template.Child()
    viewer_menu = Gtk.Template.Child()
    unplug_menu = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.on_secondary_click = None
        self.on_activate = None

    @Gtk.Template.Callback()
    def on_list_secondary_click(self, gesture, _n, x, y):
        row = self.vm_list.get_row_at_y(int(y))
        if row is not None and self.on_secondary_click:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self.on_secondary_click(row, x, y)

    @Gtk.Template.Callback()
    def on_row_activated(self, _list, row):
        if self.on_activate:
            self.on_activate(row)


@ui.template("vm-row")
class VMRow(Gtk.ListBoxRow):
    """A VM in the window's list (ui/vm-row.blp), showing item (a gui.VMItem): its labels follow
    the item's properties, the dot's colour its phase (PHASE_CLASS)."""

    __gtype_name__ = "ManagerVMRow"
    icon = Gtk.Template.Child()
    title = Gtk.Template.Child()
    specs = Gtk.Template.Child()
    dot = Gtk.Template.Child()
    state = Gtk.Template.Child()
    activity = Gtk.Template.Child()
    viewer = Gtk.Template.Child()

    # the dot's style class per VMItem.phase; Off is dimmed
    PHASE_CLASS = {
        "Running": "success",
        "Starting": "warning",
        "Stopping": "warning",
        "Error": "error",
    }

    def __init__(self, item):
        super().__init__()
        self.item = item
        sync = GObject.BindingFlags.SYNC_CREATE
        for prop, widget, target in (
            ("name", self.title, "label"),
            ("specs", self.specs, "label"),
            ("fw", self.specs, "tooltip-text"),
            ("state", self.state, "label"),
            ("activity", self.activity, "label"),
            ("viewer", self.viewer, "visible"),
        ):
            item.bind_property(prop, widget, target, sync)
        item.bind_property("activity", self.activity, "visible", sync, lambda _b, v: bool(v))
        item.connect("notify::phase", lambda *_: self._phase())
        self._phase()

    def _phase(self):
        cls = self.PHASE_CLASS.get(self.item.phase, "dimmed")
        for c in set(self.PHASE_CLASS.values()) | {"dimmed"}:
            if c == cls:
                self.dot.add_css_class(c)
            else:
                self.dot.remove_css_class(c)
        if self.item.phase == "Off":
            self.icon.add_css_class("dimmed")
        else:
            self.icon.remove_css_class("dimmed")


STAY = object()  # what on_ok returns to keep a VMForm open without an error (its progress is shown)


class _Submit:
    """What Confirm and VMForm share: on_ok(dialog) returns an error message to show (the dialog
    stays), STAY (it stays, without one), or None to close it; an Error it raises is shown the
    same way. closed: set once the dialog was closed."""

    closed = False

    def _setup(self, on_ok):
        self.on_ok = on_ok
        self.fields = {}
        self.connect("closed", self._closed)

    def _closed(self, _dialog):
        self.closed = True

    def run_ok(self):
        """None, STAY, or the Error to show (a message returned by on_ok becomes one without a key)."""
        try:
            err = self.on_ok(self)
        except Error as e:
            return e
        return err if err is STAY else Error(err) if err else None


@ui.template("confirm")
class Confirm(_Submit, Adw.AlertDialog):
    """A confirmation (ui/confirm.blp): heading, body, Cancel and ok_label (destructive, or
    suggested). add_switch() puts a switch row under the body, add_radio() a radio row; fields:
    key -> that row (the radio's button). An error from on_ok is shown in a toast of the parent
    window (on_error)."""

    __gtype_name__ = "ManagerConfirm"
    options = Gtk.Template.Child()

    def __init__(self, heading, body, ok_label, on_ok, destructive=True, on_error=None):
        super().__init__(heading=heading, body=body)
        self._setup(on_ok)
        self.on_error = on_error
        self.set_response_label("ok", ok_label)
        self.set_response_appearance(
            "ok",
            Adw.ResponseAppearance.DESTRUCTIVE if destructive else Adw.ResponseAppearance.SUGGESTED,
        )
        if not destructive:
            self.set_default_response("ok")

    def add_radio(self, key, title, subtitle=None, active=False):
        """A row with a radio button (a Gtk.CheckButton grouped with the dialog's other radios),
        under the body; fields[key]: its button, whose row is its .row."""
        check = Gtk.CheckButton(active=active, valign=Gtk.Align.CENTER)
        first = next((f for f in self.fields.values() if isinstance(f, Gtk.CheckButton)), None)
        if first is not None:
            check.set_group(first)
        row = Adw.ActionRow(title=title, activatable_widget=check)
        if subtitle:
            row.set_subtitle(subtitle)
        row.add_prefix(check)
        self.options.append(row)
        self.options.set_visible(True)
        self.fields[key] = check
        check.row = row
        return row

    def add_switch(self, key, title, subtitle=None, active=False):
        row = Adw.SwitchRow(title=title, active=active)
        if subtitle:
            row.set_subtitle(subtitle)
        self.options.append(row)
        self.options.set_visible(True)
        self.fields[key] = row
        return row

    def add_entry(self, key, title, text=""):
        """A text row (Adw.EntryRow) under the body; Enter in it is a click on ok."""
        row = Adw.EntryRow(title=title, text=text)
        row.connect("entry-activated", lambda *_: self.submit())
        self.options.append(row)
        self.options.set_visible(True)
        self.fields[key] = row
        return row

    @Gtk.Template.Callback()
    def on_response(self, _dialog, response):
        if response == "ok":
            err = self.run_ok()
            if err and self.on_error:
                self.on_error(str(err))

    def submit(self):
        """As a click on the ok response (the tests)."""
        if self.get_response_enabled("ok"):
            self.emit("response", "ok")
            self.force_close()


@ui.template("layer-row")
class LayerRow(Adw.ActionRow):
    """A layer on the Layers page (ui/layer-row.blp): name, its line, Up and Down, a switch. A fixed
    row (a layer the VM always gets) has no buttons and a switch that only shows."""

    __gtype_name__ = "ManagerLayerRow"
    order = Gtk.Template.Child()
    up = Gtk.Template.Child()
    down = Gtk.Template.Child()
    switch = Gtk.Template.Child()

    def __init__(self, name, subtitle="", active=False, fixed=False, on_move=None):
        super().__init__(title=name, subtitle=subtitle)
        self.name = name
        self.on_move = on_move
        self.switch.set_active(active)
        if fixed:
            self.order.set_visible(False)
            self.switch.set_sensitive(False)
            self.set_activatable_widget(None)

    @property
    def active(self):
        return self.switch.get_active()

    @Gtk.Template.Callback()
    def on_up_clicked(self, _button):
        self.on_move(self, -1)

    @Gtk.Template.Callback()
    def on_down_clicked(self, _button):
        self.on_move(self, 1)


# The pages of VMForm (ui/vm-form.blp's sidebar rows and stack pages), and the page of each key an
# Error can name (core.Error.key: vm.toml's keys and "name").
PAGES = ("general", "hardware", "display", "network", "layers")
KEY_PAGE = {
    "name": "general",
    "fw": "general",
    "mem": "hardware",
    "smp": "hardware",
    "free_page_reporting": "hardware",
    "display": "display",
    "audio": "display",
    "link": "network",
    "mode": "network",
    "mac": "network",
    "serial": "network",
    "listen": "network",
    "address": "network",
    "mitm_flows": "network",
    "mitm_flow_bytes": "network",
    "model_key": "network",
    "layers": "layers",
}


@ui.template("vm-form")
class VMForm(_Submit, Adw.Dialog):
    """New VM and Settings (ui/vm-form.blp): a page per category (PAGES), picked in the sidebar.
    fields: key -> its row (name, fw: the firmware's source, fwdir, epk, fw_import (New VM: the
    firmware imported, not used in place), mem, smp, free_page_reporting
    (free_page_reporting_group, Settings only), link, mode, mac and
    serial (identity_group, Settings only), listen (listen_group, Settings only, link nat), address
    (address_group, Settings only, link lan), display, audio; layers: the list of LayerRows, add_layer(); fixed_rows: those of
    add_fixed_layer()). The model key's row (model_key_group, Settings only; show_model_key()) says
    whether the VM has one, never its value; model_key_change is what Save does to it: None,
    ("install", path) (pick_model_key(), after on_browse_model_key() let the user choose a file) or
    ("clear", None). on_browse(row): a chooser button of fwdir or epk was clicked; on_change():
    a setting of the firmware (source, directory, file, root filesystem) changed. An error is
    shown in the banner, prefixed with its page's title, and that page is shown and marked in the
    sidebar until one of its settings changes."""

    __gtype_name__ = "ManagerVMForm"
    split = Gtk.Template.Child()
    categories = Gtk.Template.Child()
    page = Gtk.Template.Child()
    pages = Gtk.Template.Child()
    error = Gtk.Template.Child()
    ok = Gtk.Template.Child()
    sidebar_ok = Gtk.Template.Child()
    cancel = Gtk.Template.Child()
    sidebar_cancel = Gtk.Template.Child()
    name_group = Gtk.Template.Child()
    name_entry = Gtk.Template.Child()
    fw_group = Gtk.Template.Child()
    fw = Gtk.Template.Child()
    fwdir = Gtk.Template.Child()
    fwdir_browse = Gtk.Template.Child()
    epk = Gtk.Template.Child()
    epk_browse = Gtk.Template.Child()
    fw_import = Gtk.Template.Child()
    mem = Gtk.Template.Child()
    smp = Gtk.Template.Child()
    free_page_reporting_group = Gtk.Template.Child()
    free_page_reporting = Gtk.Template.Child()
    link = Gtk.Template.Child()
    mode = Gtk.Template.Child()
    address_group = Gtk.Template.Child()
    inspector_group = Gtk.Template.Child()
    mitm_flows = Gtk.Template.Child()
    mitm_flow_bytes = Gtk.Template.Child()
    address = Gtk.Template.Child()
    identity_group = Gtk.Template.Child()
    mac = Gtk.Template.Child()
    serial = Gtk.Template.Child()
    listen_group = Gtk.Template.Child()
    listen = Gtk.Template.Child()
    model_key_group = Gtk.Template.Child()
    model_key = Gtk.Template.Child()
    model_key_install = Gtk.Template.Child()
    model_key_clear = Gtk.Template.Child()
    display = Gtk.Template.Child()
    audio = Gtk.Template.Child()
    layer_list = Gtk.Template.Child()
    always_group = Gtk.Template.Child()
    warn_general = Gtk.Template.Child()
    warn_hardware = Gtk.Template.Child()
    warn_display = Gtk.Template.Child()
    warn_network = Gtk.Template.Child()
    warn_layers = Gtk.Template.Child()

    def __init__(self, title, ok_label, on_ok):
        super().__init__(title=title)
        self._setup(on_ok)
        self.error_page = None
        self.fixed_rows = []
        self.on_browse = None
        self.on_browse_model_key = None
        self.on_change = None
        self.model_key_present = False
        self.model_key_change = None
        self.ok.set_label(ok_label)
        self.sidebar_ok.set_label(ok_label)
        self.fields["name"] = self.name_entry
        self.settings = False  # Settings (listen and address shown by link), not New VM
        for key in (
            "fw",
            "fwdir",
            "epk",
            "fw_import",
            "mem",
            "smp",
            "free_page_reporting",
            "link",
            "mode",
            "mac",
            "serial",
            "listen",
            "address",
            "mitm_flows",
            "mitm_flow_bytes",
            "display",
            "audio",
        ):
            self.fields[key] = getattr(self, key)
        self.fields["layers"] = self.layer_list
        touched = {
            "name_entry": ("changed", "general"),
            "fw": ("notify::selected", "general"),
            "fwdir": ("changed", "general"),
            "epk": ("changed", "general"),
            "fw_import": ("notify::active", "general"),
            "mem": ("notify::value", "hardware"),
            "smp": ("notify::value", "hardware"),
            "free_page_reporting": ("notify::active", "hardware"),
            "display": ("notify::selected", "display"),
            "audio": ("notify::selected", "display"),
            "link": ("notify::selected", "network"),
            "mode": ("notify::selected", "network"),
            "mac": ("changed", "network"),
            "serial": ("changed", "network"),
            "listen": ("changed", "network"),
            "address": ("changed", "network"),
            "mitm_flows": ("notify::value", "network"),
            "mitm_flow_bytes": ("changed", "network"),
        }
        for attr, (signal, page) in touched.items():
            getattr(self, attr).connect(signal, lambda *_a, p=page: self.touched(p))
        self.link.connect("notify::selected", lambda *_a: self.link_changed())
        self.mode.connect("notify::selected", lambda *_a: self.link_changed())
        for attr, signal in (
            ("fw", "notify::selected"),
            ("fwdir", "changed"),
            ("epk", "changed"),
            ("fw_import", "notify::active"),
        ):
            getattr(self, attr).connect(signal, lambda *_a: self.on_change and self.on_change())
        self.show_page("general", navigate=False)

    def link_value(self):
        item = self.link.get_selected_item()
        return item.get_string() if item else "nat"

    def link_changed(self):
        """mode means nothing with link off; Settings shows listen with link nat, address with lan,
        and the inspector's limits with mode mitm."""
        link = self.link_value()
        self.mode.set_sensitive(link != "off")
        item = self.mode.get_selected_item()
        self.inspector_group.set_visible(
            self.settings and link != "off" and (item.get_string() if item else "mitm") == "mitm"
        )
        self.listen_group.set_visible(self.settings and link == "nat")
        self.address_group.set_visible(self.settings and link == "lan")

    # -- pages --
    def page_title(self, name):
        return self.pages.get_page(self.pages.get_child_by_name(name)).get_title()

    def current_page(self):
        return self.pages.get_visible_child_name()

    def show_page(self, name, navigate=True):
        """Show page name (and, while collapsed, go to it from the sidebar)."""
        self.pages.set_visible_child_name(name)
        self.page.set_title(self.page_title(name))
        self.categories.select_row(self.categories.get_row_at_index(PAGES.index(name)))
        if navigate:
            self.split.set_show_content(True)
        self._banner_button()

    def scroll_to(self, widget):
        """Scroll widget's page to it (a group of a long page), once the page is laid out."""

        def scroll(*_args):
            view = widget.get_ancestor(Gtk.ScrolledWindow)
            if view is None or not widget.get_mapped():
                return GLib.SOURCE_CONTINUE  # not laid out yet
            ok, point = widget.compute_point(view.get_child(), Graphene.Point())
            if ok:
                view.get_vadjustment().set_value(point.y)
            return GLib.SOURCE_REMOVE

        widget.add_tick_callback(lambda *_a: scroll())

    @Gtk.Template.Callback()
    def on_category_activated(self, _list, row):
        self.show_page(row.get_name())

    # -- errors --
    def show_error(self, message, page=None):
        self.clear_error()
        self.error_page = page
        self.error.set_title(f"{self.page_title(page)}: {message}" if page else message)
        self.error.set_revealed(True)
        if page:
            getattr(self, "warn_" + page).set_visible(True)
            self.show_page(page)

    def clear_error(self):
        self.error.set_revealed(False)
        self.error_page = None
        for p in PAGES:
            getattr(self, "warn_" + p).set_visible(False)

    def touched(self, page):
        """A setting on page changed: an error about it (or about no page) may be stale."""
        if self.error.get_revealed() and self.error_page in (None, page):
            self.clear_error()

    def _banner_button(self):
        p = self.error_page
        self.error.set_button_label(
            f"Show {self.page_title(p)}" if p and p != self.current_page() else None
        )

    @Gtk.Template.Callback()
    def on_error_button_clicked(self, _banner):
        if self.error_page:
            self.show_page(self.error_page)

    # -- layers --
    def add_layer(self, name, subtitle="", active=False):
        """A row at the end of the optional layers: its switch puts it in the VM's layers, at its place."""
        row = LayerRow(name, subtitle, active, on_move=self.move_layer)
        row.switch.connect("notify::active", lambda *_: self.touched("layers"))
        self.layer_list.append(row)
        self._order_buttons()
        return row

    def add_fixed_layer(self, name, subtitle="", active=True):
        """A row of the layers the VM always gets."""
        row = LayerRow(name, subtitle, active, fixed=True)
        self.always_group.add(row)
        self.fixed_rows.append(row)
        return row

    def layer_rows(self):
        rows, row = [], self.layer_list.get_first_child()
        while row is not None:
            rows.append(row)
            row = row.get_next_sibling()
        return rows

    def layer_row(self, name):
        return next((r for r in self.layer_rows() if r.name == name), None)

    def layers_value(self):
        """The VM's layers: the names of the rows switched on, in the list's order."""
        return [r.name for r in self.layer_rows() if r.active]

    def move_layer(self, row, delta):
        i = row.get_index() + delta
        if not 0 <= i < len(self.layer_rows()):
            return
        focus = row.up if delta < 0 else row.down
        self.layer_list.remove(row)
        self.layer_list.insert(row, i)
        self._order_buttons()
        (focus if focus.get_sensitive() else (row.down if delta < 0 else row.up)).grab_focus()
        self.touched("layers")

    def _order_buttons(self):
        rows = self.layer_rows()
        for i, r in enumerate(rows):
            r.up.set_sensitive(i > 0)
            r.down.set_sensitive(i < len(rows) - 1)

    # -- the model key --
    def show_model_key(self, present):
        """The model key's group, for a VM that has a key (present) or not."""
        self.model_key_group.set_visible(True)
        self.model_key_present = present
        self.model_key_change = None
        self._model_key_row()

    def pick_model_key(self, path):
        """Save installs the key in path (checked then, as `beanjar key model set` does)."""
        self.model_key_change = ("install", path)
        self._model_key_row()
        self.touched("network")

    def _model_key_row(self):
        change, path = self.model_key_change or (None, None)
        if change == "install":
            text = f"From {path}, at Save"
        elif change == "clear":
            text = "Cleared at Save: the built-in model secret"
        elif self.model_key_present:
            text = "Installed (value not shown)"
        else:
            text = "None: the built-in model secret"
        self.model_key.set_subtitle(GLib.markup_escape_text(text))
        self.model_key_clear.set_sensitive(
            change == "install" or (change is None and self.model_key_present)
        )

    @Gtk.Template.Callback()
    def on_model_key_install_clicked(self, _button):
        if self.on_browse_model_key:
            self.on_browse_model_key()

    @Gtk.Template.Callback()
    def on_model_key_clear_clicked(self, _button):
        self.model_key_change = ("clear", None) if self.model_key_present else None
        self._model_key_row()
        self.touched("network")

    @Gtk.Template.Callback()
    def on_browse_clicked(self, button):
        if self.on_browse:
            self.on_browse(self.epk if button is self.epk_browse else self.fwdir)

    # -- ok, cancel --
    def submit(self):
        err = self.run_ok()
        if err is STAY:
            return
        if err:
            self.show_error(str(err), KEY_PAGE.get(err.key))
        else:
            self.close()

    @Gtk.Template.Callback()
    def on_ok_clicked(self, _button):
        self.submit()

    @Gtk.Template.Callback()
    def on_cancel_clicked(self, _button):
        self.close()


@ui.template("progress")
class ProgressDialog(Adw.Dialog):
    """A New VM's progress (ui/progress.blp). operation: the running python3 -m beanjar.newvm (a
    gui.Operation), None once it has ended (finished()): until then Cancel and closing the dialog
    cancel it."""

    __gtype_name__ = "ManagerProgressDialog"
    head = Gtk.Template.Child()
    spinner = Gtk.Template.Child()
    log = Gtk.Template.Child()
    cancel_button = Gtk.Template.Child()
    close_button = Gtk.Template.Child()

    closed = False

    def __init__(self, heading, cancellable=True, title=None):
        """cancellable False: work that must finish (Settings' board data): no Cancel, and closing
        the dialog waits for it."""
        super().__init__()
        self.operation = None
        self.cancellable = cancellable
        self.cancel_button.set_visible(cancellable)
        if title:
            self.set_title(title)
        self.head.set_text(heading)
        self.connect("closed", lambda _d: setattr(self, "closed", True))

    def finished(self, heading):
        self.operation = None
        self.head.set_text(heading)
        self.spinner.set_visible(False)
        self.cancel_button.set_sensitive(False)
        self.close_button.set_sensitive(True)
        self.set_can_close(True)
        self.set_default_widget(self.close_button)
        self.close_button.grab_focus()

    @Gtk.Template.Callback()
    def on_cancel_clicked(self, _button):
        if self.operation:
            self.operation.cancel()

    @Gtk.Template.Callback()
    def on_close_clicked(self, _button):
        self.close()

    @Gtk.Template.Callback()
    def on_close_attempt(self, _dialog):
        if self.operation and self.cancellable:
            self.operation.cancel()
