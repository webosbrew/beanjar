# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""python3 -m beanjar_ui.manager: the VM manager's window (GTK 4, PyGObject), over the core package beanjar.

A libadwaita window after VirtualBox and GNOME Boxes: the VMs in a sidebar list (a status page while
there are none), each row with its firmware, memory, CPUs and state; the actions for the selection
in the header bar, its menus and the list's context menu, and beside the list the details: the VM's
settings, a live tail of its serial console and the output of the last operation. Results are
toasts. It never assumes it is the parent of a VM or a viewer: whoever started, stopped, made,
moved or changed one (the CLI, another window, an agent), its state is read back from the library
(pidfiles, console.log, start.lock, vm.toml, QMP) on every refresh. Gio.FileMonitors on vms/, each
VM's folder and run/ (armed again when one is made anew) and the selected console.log queue one
(the console's new lines are appended to its tail); a 1 s timer updates the running VMs' CPU% and
uptime, and every 5 s refreshes anyway (whatever a monitor missed). systemd is never asked on
the main loop: a worker thread takes a snapshot of the VMs' units (vmunits.vm_units) each second, which
the refresh reads, with the Start's memory admission and the VMs running elsewhere (other libraries,
the development and regress VMs: `beanjar list --all`), listed read-only below the library's. A VM
whose vm.toml does not read is shown with the reason, and a refresh that fails is logged, not fatal:
the timer always runs on.
Operations that take time (start, shut down, unplug, wipe, disconnect) run the CLI (python3 -m
beanjar) as a Gio.Subprocess and stream its output, so the main loop never blocks; so does a New VM
that extracts a .epk or copies a firmware into the VM's folder (python3 -m beanjar.vm.create, with a
progress dialog over New VM; one that does neither is made at once).

The viewer is a separate process (beanjar-viewer, run/viewer.pid), as with the CLI's `view`: the
viewer's core handles signals and SDL audio process-wide, so one embedded in this window would
take the manager down with it and allow only one viewer per manager.

The window's and dialogs' layouts are host/ui/data/manager/*.blp, as templates (widgets.py); this is
what they do.
"""

import argparse
import codecs
import os
import signal
import sys
import threading
import time
import traceback

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from beanjar import base, paths  # noqa: E402
from beanjar.vm import library as vm_library, create, settings as vm_settings  # noqa: E402
from beanjar.runtime import capacity, hostprocs, lifecycle, poweroff, vmunits  # noqa: E402
from beanjar.firmware import install, targets  # noqa: E402
from beanjar.board import nvm  # noqa: E402
from beanjar.display import gpu  # noqa: E402
from beanjar.devices import rotation  # noqa: E402
from beanjar.vm.library import Library  # noqa: E402
from beanjar.base import OFF, RUNNING, STARTING, Error  # noqa: E402
from beanjar.firmware.install import fmt_size  # noqa: E402

from .. import no_display  # noqa: E402
from . import header, picture, summary_facts  # noqa: E402

PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(vm_library.__file__)))  # beanjar's
TAIL_BYTES = 24 * 1024
QMP_EVERY = 5  # ticks between QMP cross-checks
CONSOLE_COALESCE_MS = 500  # a console monitor's events within this are one read (one redraw)
BROKEN = "Error"  # the phase of a VM whose vm.toml does not read


class VMItem(GObject.Object):
    """A VM of the list; its row (widgets.VMRow) shows these properties. phase: core's state, or
    Starting while a start runs; state: its text (with QMP's detail); fw: the firmware's name,
    specs: the platform (VM.platform_title), the firmware shortened (fw_label), memory and CPUs;
    activity: uptime and CPU% while it runs."""

    __gtype_name__ = "BeanjarVMItem"
    name = GObject.Property(type=str, default="")
    phase = GObject.Property(type=str, default=OFF)
    state = GObject.Property(type=str, default="")
    fw = GObject.Property(type=str, default="")
    specs = GObject.Property(type=str, default="")
    activity = GObject.Property(type=str, default="")
    viewer = GObject.Property(type=bool, default=False)

    def set(self, key, value):
        if self.get_property(key) != value:
            self.set_property(key, value)


class Operation:
    """The CLI (or module, e.g. beanjar.vm.create) as a Gio.Subprocess; on_line(line) per output line,
    on_done(exit status)."""

    def __init__(self, library, args, on_line, on_done, module="beanjar"):
        self.on_line, self.on_done = on_line, on_done
        launcher = Gio.SubprocessLauncher.new(
            Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE
        )
        path = os.environ.get("PYTHONPATH")
        launcher.setenv("PYTHONPATH", PKG_PARENT + (os.pathsep + path if path else ""), True)
        launcher.setenv("PYTHONUNBUFFERED", "1", True)
        self.argv = [
            sys.executable,
            "-m",
            module,
            "--library",
            library.root,
            *(["--repo", library.known_repo] if library.known_repo else []),
            *args,
        ]
        self.proc = launcher.spawnv(self.argv)
        self.stream = Gio.DataInputStream.new(self.proc.get_stdout_pipe())
        self.rc = None
        self._read()

    def _read(self):
        self.stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self._got)

    def _got(self, stream, res):
        try:
            line, _n = stream.read_line_finish_utf8(res)
        except GLib.Error:
            line = None
        if line is None:
            self.proc.wait_async(None, self._exited)
            return
        self.on_line(line)
        self._read()

    def _exited(self, proc, res):
        try:
            proc.wait_finish(res)
        except GLib.Error:
            pass
        self.rc = proc.get_exit_status() if proc.get_if_exited() else 128 + proc.get_term_sig()
        self.on_done(self.rc)

    def cancel(self):
        if self.rc is None:
            self.proc.send_signal(signal.SIGTERM)


def log_append(view, text, clear=False):
    """Append text to a log view (console, output, progress), or replace its text. Without Pango's
    hyphens: a log wrapped at any character must not look as if it had a hyphen there."""
    buf = view.get_buffer()
    tag = buf.get_tag_table().lookup("log") or buf.create_tag("log", insert_hyphens=False)
    if clear:
        buf.set_text("")
    buf.insert_with_tags(buf.get_end_iter(), text, tag)


def fw_label(fw):
    """A firmware as the list shows it: an imported one's name (<version>-<...>_DTV_<platform>_<...>,
    as epk2extract names it) as "<platform> <version>", anything else (a directory) as its name."""
    version, _sep, rest = fw.partition("-")
    parts = rest.split("_")
    if "DTV" in parts[:-1] and version.replace(".", "").isdigit():
        return f"{parts[parts.index('DTV') + 1]} {version}"
    return fw


def untested_suffix(fw_dir):
    """ " (not tested yet: may not boot)" after a firmware whose target has never booted
    (targets.tested), "" otherwise or if its target cannot be told."""
    try:
        target = paths.firmware_target(fw_dir)
    except ValueError:
        return ""
    return "" if targets.tested(target) else f" ({targets.UNTESTED})"


def widgets_phase(phase, widgets):
    """The state dot's style class for phase, as the VM list's rows colour it."""
    return widgets.VMRow.PHASE_CLASS.get(phase, "dimmed")


def fmt_mem(mib):
    return f"{mib // 1024} GiB" if mib >= 1024 and mib % 1024 == 0 else f"{mib} MiB"


def dropdown(d, options, value=None):
    """Fill drop-down d with options, value selected."""
    d.set_model(Gtk.StringList.new(list(options)))  # a Gtk.DropDown or an Adw.ComboRow
    if value in options:
        d.set_selected(list(options).index(value))
    return d


def dropdown_value(d):
    item = d.get_selected_item()
    return item.get_string() if item else None


home_path = vm_library.home_path


def elsewhere_rows(lib, units):
    """The VMs up in beanjar.slice that are not lib's, as `beanjar list --all` finds them (units: a
    vmunits.vm_units() snapshot): another library's (a development library's) VM, with its library, or
    a unit of another RUN_DIR (a regress target's), with that directory. Each a dict: key, name,
    state, address, where (the library or run directory, ~ shortened), tooltip, and library (the
    core.Library a Stop goes to; None for a unit of no library). Asks systemd: off the main loop."""
    from beanjar.cli.vms import unit_row

    shown = set()
    for vm in lib.vms():
        shown |= {vmunits.vm_unit_name(vm.dir), vm.unit_name(), f"webOS VM {vm.run_dir}"}
    rows = []
    for unit, props in sorted(units.items()):
        if unit in shown or props.get("Description") in shown:
            continue
        vm = vm_library.unit_vm(props)
        if vm is not None:
            try:
                st, detail = vm.state(units=units)
                address = vm.lan_address() or vm.address() or ""
            except Error as e:
                st, detail, address = BROKEN, str(e), ""
            rows.append(
                {
                    "key": unit,
                    "name": vm.name,
                    "state": st + (f" ({detail})" if detail else ""),
                    "address": address,
                    "where": home_path(vm.library.root),
                    "tooltip": f"{vm.name} of the library {vm.library.root} (unit {unit})",
                    "library": vm.library,
                }
            )
            continue
        r = unit_row(unit, props)
        rows.append(
            {
                "key": unit,
                "name": r["name"],
                "state": r["state"],
                "address": r["lan_address"] or r["address"] or "",
                "where": home_path(r["run_dir"]) if r["run_dir"] else unit,
                "tooltip": f"unit {unit}"
                + (f", run directory {r['run_dir']}" if r["run_dir"] else ""),
                "library": None,
            }
        )
    return rows


# The window's actions (win.<key>): what they run, and their keyboard shortcuts.
ACCELS = {
    "new": ["<Control>n"],
    "start": ["<Control>Return"],
    "shutdown": ["<Control><Shift>Delete"],  # opens the Shut Down dialog, which confirms
    "connect": ["<Control><Shift>Return"],
    "settings": ["<Alt>Return"],
    "rename": ["F2"],
    "delete": ["Delete"],
    "quit": ["<Control>q"],
    "show-output": [],
    "about": [],
}
SUMMARY = (
    ("state", "State"),
    ("platform", "Platform"),
    ("fw", "Firmware"),
    ("mem", "Memory (MiB)"),
    ("gpu", "GPU"),
    ("smp", "CPUs"),
    ("free_page_reporting", "Free page reporting"),
    ("layers", "Layers"),
    ("network", "Network"),
    ("mitm_flows", "Inspector keeps"),
    ("mitm_flow_bytes", "Inspector bodies"),
    ("address", "Address"),
    ("lan_address", "LAN address"),
    ("listen", "Service ports on"),
    ("mac", "MAC address"),
    ("serial", "Serial number"),
    ("model_key", "Model key"),
    ("display", "Display"),
    ("audio", "Audio"),
    ("directory", "Directory"),
)


class Manager:
    def __init__(self, app, library, open_folders=True):
        from . import widgets  # the templates: imported after main()'s --ui-dir

        self.widgets = widgets
        self.app = app
        self.lib = library
        self.open_folders = open_folders
        self.ops = {}  # VM name -> Operation (start/stop/wipe/unview)
        self.create_op = None  # the New VM being made (python3 -m beanjar.vm.create)
        self.progress = None  # its widgets.ProgressDialog
        self.prepare_op = (
            None  # the board data being prepared after a Settings save (beanjar prepare)
        )
        self.want_viewer = {}  # VM name -> QEMU pid the viewer was for (auto-reconnect)
        self.cpu = {}  # VM name -> (ticks, monotonic)
        self.cpu_text = {}  # VM name -> its last CPU% ("58%"), as its row shows it
        self.gpu = {}  # VM name -> (QEMU pid, its gpu.Sampler, its last status)
        self.watches = {}  # key -> (path, its stat signature, Gio.FileMonitor)
        self.console_monitor = None
        self.console_path = None
        self.console_sig = None  # console.log's (dev, ino, size, mtime) when last read
        self.console_read = 0  # how far into it the view has it
        self.console_shown = None  # the log the view has
        self.console_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.console_pending = None  # a monitor's read to come (_console_changed)
        self.units = {}  # the last snapshot of the VMs' units (vmunits.vm_units)
        self.blocked = {}  # VM name -> why it cannot start (capacity.admission), the selected one's
        self.broken = {}  # VM name -> why its vm.toml does not read
        self.failures = {}  # VM name -> (kind, exit status): its last start or stop, which failed
        self.displays = {}  # VM name -> its display (dbus, egl-headless, none), at the last refresh
        self.elsewhere = []  # the VMs running elsewhere (elsewhere_rows), as listed
        self.polling = False  # the worker thread (_poll) is running
        self.logged = set()  # the errors already written to stderr
        self.tick = 0
        self.dialog = None
        self.messages = []  # every toast's text, the last one last
        self.refresh_pending = False
        self.states = {}  # VM name -> (state, detail, QEMU pid, viewer pid)

        from . import ui

        ui.icons(Gdk.Display.get_default())
        Gtk.Window.set_default_icon_name(ui.APP_ID)
        self.win = self.widgets.ManagerWindow(application=app)
        self.store = Gio.ListStore(item_type=VMItem)
        self.vm_list = self.win.vm_list
        self.vm_list.bind_model(self.store, self.widgets.VMRow)
        self.vm_list.connect("row-selected", lambda *_: self.selection_changed())
        self.last_index = 0  # the selected row's, for the one to select after it goes
        self.win.on_secondary_click = lambda row, x, y: self.context_menu(row.item.name, x, y)
        self.win.on_activate = self.activate
        self.vm_menu = Gtk.PopoverMenu.new_from_model(self.win.vm_context_menu)
        self.vm_menu.set_parent(self.vm_list)
        self.vm_menu.set_has_arrow(False)
        self.vm_menu.set_halign(Gtk.Align.START)
        home = os.path.expanduser("~")
        root = self.lib.root
        # the library's path: the title's tooltip, so a narrow window does not cut the title
        self.win.window_title.set_tooltip_text(
            "~" + root[len(home) :] if root.startswith(home + os.sep) else root
        )
        self.actions = {}
        run = {
            "new": self.new_vm,
            "start": self.start,
            "start-connect": self.start_connect,
            "start-reason": self.start_reason,
            "shutdown": self.shutdown,
            "unplug": self.unplug,
            "connect": self.connect,
            "disconnect": self.disconnect,
            "wipe": self.wipe,
            "new-identity": self.new_identity,
            "rename": self.rename,
            "clone": self.clone,
            "settings": self.settings,
            "logs": self.open_logs,
            "folder": self.open_vm_folder,
            "delete": self.delete,
            "about": self.about,
            "show-output": lambda: self.details.set_visible_child_name("output"),
            "quit": self.win.close,
        }
        for key, cb in run.items():
            action = Gio.SimpleAction.new(key, None)
            action.connect("activate", lambda _a, _p, cb=cb: self.guard(cb))
            self.win.add_action(action)
            self.actions[key] = action
            if key in ACCELS:
                app.set_accels_for_action("win." + key, ACCELS[key])
        # Settings at one of its pages (the Summary cards' Edit: win.settings-page::network)
        action = Gio.SimpleAction.new("settings-page", GLib.VariantType.new("s"))
        action.connect(
            "activate", lambda _a, page: self.guard(lambda: self.settings(page.get_string()))
        )
        self.win.add_action(action)
        self.actions["settings-page"] = action
        # The reconnect option: its state is read from the library's config before it is
        # connected, so start-up does not write it.
        self.reconnect = Gio.SimpleAction.new_stateful(
            "auto-reconnect",
            None,
            GLib.Variant.new_boolean(bool(self.lib.config.get("auto_reconnect", True))),
        )
        self.reconnect.connect("change-state", self._reconnect_changed)
        self.win.add_action(self.reconnect)
        self.vm_title = self.win.vm_title
        self.summary, self.console, self.output = (
            self.win.summary,
            self.win.console,
            self.win.output,
        )
        from .summary_ui import Summary

        self.summary_view = Summary(self, self.win.summary_banner)
        self.summary.append(self.summary_view)
        self.summary_view.follow(self.win)
        self.summary_values = {}
        self.details = self.win.details
        from .inspector_ui import Panel

        self.inspector = Panel(self)
        self.summary_view.inspector_slot.append(self.inspector)
        # the TV picture: `beanjar screenshot` in a thread (picture.Pictures)
        self.pictures = picture.Pictures(
            lambda name, words: picture.beanjar_argv(self.lib, name, words),
            self._rotation,
            env=picture.cli_env(PKG_PARENT),
        )
        self.pictures.connect("changed", lambda _p, name: self.update_picture(name))
        self.summary_view.picture.on_click = lambda: self.guard(self.picture_clicked)
        self.details.connect("notify::visible-child-name", lambda *_: self.update_picture())
        self.win.connect("notify::is-active", lambda *_: self.update_picture())
        from . import vnc_ui

        self.vnc_panel = vnc_ui.Panel(self)
        self.summary_view.insert_child_after(self.vnc_panel, self.summary_view.inspector_slot)
        self.details.set_visible_child_name("summary")

        # Viewers and VMs already running are found from their pidfiles.
        for vm in self.lib.vms():
            if vm.viewer_pid() and vm.pid():
                self.want_viewer[vm.name] = vm.pid()
        self.safe_refresh(qmp=True)
        self._poll()
        self.timer = GLib.timeout_add(1000, self._tick)
        self.win.connect("close-request", self._closing)

    # -- helpers --
    def _monitor(self, path, cb):
        try:
            m = Gio.File.new_for_path(path).monitor(Gio.FileMonitorFlags.NONE, None)
        except GLib.Error:
            return None
        m.connect("changed", lambda *_: cb())
        return m

    def _watch(self, key, path):
        """A Gio.FileMonitor on directory path, queueing a refresh; armed again when path is made
        anew (a monitor follows the inode it was armed on, which a removed directory takes with it).
        A directory that is not there has none: its parent's monitor sees it come."""
        try:
            st = os.stat(path)
            sig = (st.st_dev, st.st_ino)
        except OSError:
            sig = None
        w = self.watches.get(key)
        if w and w[0] == path and w[1] == sig and (sig is None or w[2] is not None):
            return
        if w and w[2] is not None:
            w[2].cancel()
        m = None
        if sig is not None:
            try:
                m = Gio.File.new_for_path(path).monitor_directory(
                    Gio.FileMonitorFlags.WATCH_MOVES, None
                )
                m.connect("changed", self._changed, key)
            except GLib.Error:
                m = None
        self.watches[key] = (path, sig, m)

    def _changed(self, _monitor, file, _other, event, key):
        # the directory itself removed or moved away: its monitor is spent; the refresh arms another
        if (
            event
            in (
                Gio.FileMonitorEvent.DELETED,
                Gio.FileMonitorEvent.MOVED_OUT,
                Gio.FileMonitorEvent.UNMOUNTED,
            )
            and file is not None
            and key in self.watches
            and file.get_path() == self.watches[key][0]
        ):
            w = self.watches.pop(key)
            if w[2] is not None:
                w[2].cancel()
        self.queue_refresh()

    def _unwatch(self, keep):
        for key in [k for k in self.watches if k not in keep]:
            w = self.watches.pop(key)
            if w[2] is not None:
                w[2].cancel()

    def log_error(self, what, exc):
        """An error the window shows (or survives) rather than raises: on stderr once per message."""
        key = (what, str(exc))
        if key not in self.logged:
            self.logged.add(key)
            print(f"beanjar_ui.manager.gui: {what}:", file=sys.stderr)
            traceback.print_exception(exc, file=sys.stderr)

    def _closing(self, _win):
        GLib.source_remove(self.timer)
        if self.console_pending is not None:
            GLib.source_remove(self.console_pending)
            self.console_pending = None
        self.inspector.closed = True
        if self.create_op:  # a New VM being made is cancelled: it leaves nothing
            self.create_op.cancel()
        return False

    def enabled(self, key):
        return self.actions[key].get_enabled()

    def auto_reconnect(self):
        return self.reconnect.get_state().get_boolean()

    def _reconnect_changed(self, action, value):
        action.set_state(value)
        self.save_option("auto_reconnect", value.get_boolean())

    def activate(self, row):
        """A row activated: narrow, it was picked, so the list makes way for its details; else
        (double click, Enter) Connect, or Start while it cannot connect."""
        self.select(row.item.name)
        if self.win.split.get_collapsed():
            self.win.split.set_show_sidebar(False)
            return
        for key in ("connect", "start"):
            if self.enabled(key):
                self.actions[key].activate(None)
                return

    def context_menu(self, name, x, y):
        """The list's context menu (vm_menu) for VM name, at (x, y) of the list."""
        self.select(name)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        self.vm_menu.set_pointing_to(rect)
        self.vm_menu.popup()

    # -- state --
    def selected(self):
        row = self.vm_list.get_selected_row()
        return row.item.name if row else None

    def select(self, name):
        for i in range(self.store.get_n_items()):
            if self.store.get_item(i).name == name:
                self.vm_list.select_row(self.vm_list.get_row_at_index(i))
                return True
        return False

    def keep_selection(self):
        """A VM stays selected: the one after a removed one (or the new last), the first at start."""
        n = self.store.get_n_items()
        if n and self.vm_list.get_selected_row() is None:
            self.vm_list.select_row(self.vm_list.get_row_at_index(min(self.last_index, n - 1)))

    def queue_refresh(self):
        if not self.refresh_pending:
            self.refresh_pending = True
            GLib.timeout_add(100, self._queued)

    def _queued(self):
        self.refresh_pending = False
        self.safe_refresh()
        return False

    def _tick(self):
        """Each second, whatever happens: the rows' uptime and CPU%, the selected console's tail if
        it changed, and a new snapshot of the units (_poll); every QMP_EVERY ticks a whole refresh
        with QMP's check, for whatever the monitors and the units missed. Everything else is
        refreshed when a monitor or the units' snapshot says it changed: an idle window reads no
        vm.toml (a refresh of every VM each second took a core's fifth on the Pi)."""
        self.tick += 1
        if self.tick % QMP_EVERY == 0:
            self.safe_refresh(qmp=True)
        elif any(st[0] == base.STARTING and st[2] for st in self.states.values()):
            # QEMU is up and the guest is not yet at its debug shell: nothing watches the console
            # log's first line (only the selected VM's console is monitored, and that does not
            # change the state), so look each second, not at the next whole refresh
            self.safe_refresh()
        else:
            try:
                self._activities()
            except Exception as e:  # noqa: BLE001
                self.log_error("the activity", e)
        try:
            self._update_console(changed_only=True)
        except Exception as e:  # noqa: BLE001 - the timer must outlive any of it
            self.log_error("the console", e)
        try:
            self.update_picture()
        except Exception as e:  # noqa: BLE001
            self.log_error("the picture", e)
        self._poll()
        return True

    def safe_refresh(self, qmp=False):
        """refresh(), its errors logged: one that raises must stop neither the timer nor the monitors."""
        try:
            self.refresh(qmp=qmp)
        except Exception as e:  # noqa: BLE001
            self.log_error("refresh", e)

    def _poll(self):
        """A snapshot of what systemd knows, taken off the main loop (a thread; one at a time): the
        VMs' units (vmunits.vm_units), the selected VM's admission (the machine's max_running,
        core.running_refusal, then capacity.admission, which asks the units of the library's VMs) and
        the VMs running elsewhere (elsewhere_rows); _polled takes it."""
        if self.polling:
            return
        self.polling = True
        name = self.selected()
        name = (
            name
            if name and self.states.get(name, (OFF,))[0] == OFF and name not in self.broken
            else None
        )
        lib = self.lib

        def work():
            result = {}
            try:
                units = vmunits.vm_units()
                result["units"] = units
                if name:
                    try:
                        vm = lib.vm(name)
                        result["blocked"] = {
                            name: vm_library.running_refusal(
                                name, vm.run_dir, vmunits.vm_unit_name(vm.dir), units=units
                            )
                            or capacity.admission(vm)
                        }
                    except Exception:  # noqa: BLE001 - a VM that does not read cannot start anyway
                        result["blocked"] = {}
                result["elsewhere"] = elsewhere_rows(lib, units)
            except Exception as e:  # noqa: BLE001
                result["error"] = e
            GLib.idle_add(self._polled, result)

        threading.Thread(target=work, name="beanjar-gui-units", daemon=True).start()

    def _polled(self, result):
        self.polling = False
        if "error" in result:
            self.log_error("the units' snapshot", result["error"])
        changed = False
        if "units" in result:
            units = {
                n: (p.get("ActiveState"), p.get("MainPID")) for n, p in result["units"].items()
            }
            changed = units != {
                n: (p.get("ActiveState"), p.get("MainPID")) for n, p in self.units.items()
            }
            self.units = result["units"]
        if "blocked" in result and result["blocked"] != {
            k: v for k, v in self.blocked.items() if k in result["blocked"]
        }:
            self.blocked = result["blocked"]
            changed = True
        if "elsewhere" in result:
            try:
                self.update_elsewhere(result["elsewhere"])
            except Exception as e:  # noqa: BLE001
                self.log_error("the VMs running elsewhere", e)
        if changed:
            self.queue_refresh()
        return False

    def refresh(self, qmp=False):
        names = self.lib.names()
        for i in reversed(range(self.store.get_n_items())):
            if self.store.get_item(i).name not in names:
                self.store.remove(i)
        have = {self.store.get_item(i).name for i in range(self.store.get_n_items())}
        for n in names:
            if n not in have:
                pos = sum(1 for h in have if h < n)
                self.store.insert(pos, VMItem(name=n))
                have.add(n)
        self._unwatch({"vms"} | {(k, n) for n in names for k in ("vm", "run")})
        self._watch("vms", self.lib.vms_dir)
        old, self.states, self.broken = self.states, {}, {}
        for i in range(self.store.get_n_items()):
            item = self.store.get_item(i)
            vm = vm_library.VM(
                self.lib, item.name
            )  # not lib.vm: its vm.toml may go while this runs
            self._watch(("vm", item.name), vm.dir)
            self._watch(("run", item.name), vm.run_dir)
            try:
                self._refresh_vm(item, vm, old, qmp)
            except Exception as e:  # noqa: BLE001 - one VM must not take the others with it
                self._broken(item, vm, e)
        self.keep_selection()
        self.update_empty()
        self.update_actions()
        self.update_details()

    def _refresh_vm(self, item, vm, old, qmp):
        st, detail = vm.state(qmp=qmp, units=self.units)
        if not qmp and old.get(item.name, (None,))[0] == st:
            detail = old[item.name][1]  # QMP's detail holds until its next check
        pid, vpid = vm.pid(), vm.viewer_pid()
        op = self.ops.get(item.name)
        phase = STARTING if op and st == OFF and op[0] == "start" else st
        c = vm.config
        fw = vm.firmware_name()
        smp = int(c.get("smp", 0) or 0)
        specs = " · ".join(
            filter(
                None,
                (
                    vm.platform_title(),
                    fw_label(fw),
                    fmt_mem(vm.mem() or 0),
                    f"{smp} CPU{'s' if smp != 1 else ''}",
                ),
            )
        )
        self.states[item.name] = (st, detail, pid, vpid)
        self.displays[item.name] = vm.display
        item.set("phase", phase)
        item.set("state", phase + (f" ({detail})" if detail else ""))
        item.set("fw", fw)
        item.set("specs", specs)
        self._activity(item, pid)
        item.set("viewer", bool(vpid))
        self._reconnect(vm, st, pid, vpid)

    def _activity(self, item, pid):
        """A row's uptime, CPU and GPU use: the values that change each second."""
        cpu = self._cpu(item.name, pid)
        self.cpu_text[item.name] = cpu
        graphics = self._gpu(item.name, pid)
        headline = graphics["headline"]
        if item.name == self.selected() and self.summary_values:
            self.summary_values["gpu"] = gpu.label(graphics)
            self._live_tiles(pid, cpu, headline)
        activity = [
            v
            for v in (
                vm_library.fmt_duration(hostprocs.uptime_of(pid)) if pid else "",
                cpu and f"{cpu} CPU",
                headline is not None and f"{headline:.0f}% GPU",
            )
            if v
        ]
        item.set("activity", "· " + " · ".join(activity) if activity else "")

    def _live_tiles(self, pid, cpu, headline):
        """The selected running VM's Uptime, CPU and Memory tiles: what its row shows each second,
        and QEMU's resident memory on the host (with the GPU's use, when known)."""
        mem = capacity.rss_mib(pid)
        self.summary_view.set_tiles(
            {
                "time": ("Uptime", vm_library.fmt_duration(hostprocs.uptime_of(pid)) or "—", ""),
                "cpu": ("CPU", cpu or "—", ""),
                "memory": (
                    "Memory",
                    summary_facts.fmt_gib(mem) if mem else "—",
                    "on host" + (f" · GPU {headline:.0f}%" if headline is not None else ""),
                ),
            }
        )

    def _activities(self):
        """Each row's activity, from the QEMU pids the last refresh found."""
        for i in range(self.store.get_n_items()):
            item = self.store.get_item(i)
            st = self.states.get(item.name)
            if st and st[2] and item.name not in self.broken:
                self._activity(item, st[2])

    def _broken(self, item, vm, exc):
        """VM vm that does not read (its vm.toml, say, or one not written out yet): its row says why."""
        message = str(exc) or type(exc).__name__
        if not isinstance(exc, Error):
            self.log_error(f"VM {vm.name}", exc)
        short = message.removeprefix(vm.config_path + ": ")
        self.broken[item.name] = message
        pid = vm.pid()
        self.states[item.name] = (BROKEN, short, pid, None)
        item.set("phase", BROKEN)
        item.set("state", f"{BROKEN}: {short}")
        item.set("fw", message)
        item.set("specs", "vm.toml does not read" if isinstance(exc, Error) else "cannot be read")
        item.set("activity", "")
        item.set("viewer", False)

    def update_elsewhere(self, rows):
        """The VMs running elsewhere (elsewhere_rows), read-only below the library's: name, state,
        address and library or run directory; a library's VM has Shut Down (the CLI's stop, with
        --library), a unit of none (a regress target) nothing."""
        shown = [{k: v for k, v in r.items() if k != "library"} for r in rows]
        if shown == [{k: v for k, v in r.items() if k != "library"} for r in self.elsewhere]:
            return
        self.elsewhere = rows
        box = self.win.elsewhere_list
        while (child := box.get_first_child()) is not None:
            box.remove(child)
        for r in rows:
            row = Adw.ActionRow(
                title=GLib.markup_escape_text(r["name"]),
                tooltip_text=r["tooltip"],
                subtitle=GLib.markup_escape_text(
                    " · ".join(filter(None, (r["state"], r["address"], r["where"])))
                ),
                activatable=False,
                selectable=False,
            )
            row.add_css_class("property")
            row.elsewhere = r
            if r["library"] is not None:
                b = Gtk.Button(
                    icon_name="system-shutdown-symbolic",
                    valign=Gtk.Align.CENTER,
                    tooltip_text=f"Shut Down {r['name']} (of {r['where']})",
                )
                b.add_css_class("flat")
                b.set_sensitive(r["key"] not in self.ops)
                b.connect("clicked", lambda _b, r=r: self.guard(lambda: self.stop_elsewhere(r)))
                row.add_suffix(b)
                row.stop_button = b
            box.append(row)
        self.win.elsewhere_group.set_visible(bool(rows))
        self.update_empty()

    def stop_elsewhere(self, r):
        """Shut down a VM of another library (`beanjar --library <its library> stop <name>`), through
        the same dialog as the library's own."""
        self.shut_down(
            r["name"],
            r["library"],
            r["key"],
            f"{r['name']} is a VM of another library, {r['library'].root}.",
        )

    def update_empty(self):
        """The status page while the library has no VMs (and none runs elsewhere), with New VM."""
        if self.store.get_n_items() or self.elsewhere:
            self.win.stack.set_visible_child_name("vms")
            return
        self.win.empty.set_description(
            "Create a VM from a webOS firmware file (.epk) or extracted firmware."
        )
        self.win.stack.set_visible_child_name("empty")

    def _cpu(self, name, pid):
        t = hostprocs.proc_times(pid) if pid else None
        if t is None:
            self.cpu.pop(name, None)
            return ""
        now = time.monotonic()
        prev = self.cpu.get(name)
        if prev and prev[2] == pid and now - prev[1] < 0.2:
            # a refresh just after the tick's sample: keep that one (an empty CPU% flickers)
            return self.cpu_text.get(name, "")
        self.cpu[name] = (t[0], now, pid)
        if not prev or prev[2] != pid:
            return ""
        pct = (t[0] - prev[0]) / os.sysconf("SC_CLK_TCK") / (now - prev[1]) * 100
        return f"{pct:.0f}%"

    def _gpu(self, name, pid):
        if not pid:
            self.gpu.pop(name, None)
            return gpu.status_off()
        current = self.gpu.get(name)
        sampler = (
            current[1]
            if current and current[0] == pid
            else gpu.Sampler(pid, vm_library.VM(self.lib, name).config.get("layers"))
        )
        value = sampler.sample()
        self.gpu[name] = (pid, sampler, value)
        return value

    def _reconnect(self, vm, st, pid, vpid):
        """Auto-reconnect: the viewer quits when QEMU goes away; open it again once the VM runs
        again. A viewer closed while its VM still runs was closed by the user: leave it."""
        if vm.name not in self.want_viewer:
            return
        if vpid:
            if pid:
                self.want_viewer[vm.name] = pid
            return
        was = self.want_viewer[vm.name]
        if pid and pid == was:
            del self.want_viewer[vm.name]
        elif pid and st == RUNNING and self.auto_reconnect() and vm.name not in self.ops:
            try:
                self.lib.view(vm.name)
                self.want_viewer[vm.name] = pid
                self.say(f"{vm.name}: viewer reconnected")
            except Error as e:
                self.say(str(e))
                del self.want_viewer[vm.name]

    def update_actions(self):
        name = self.selected()
        st, detail, pid, vpid = self.states.get(name, (OFF, "", None, None))
        vm = vm_library.VM(self.lib, name) if name else None
        if name in self.broken:  # it does not read: only what does not need its settings
            vm = None
        busy = name in self.ops
        off = st == OFF and not busy
        # other VMs may run (each on its own address); a VM starts if fewer than max_running VMs run
        # and the host's memory covers it (core.running_refusal, capacity.admission, which ask systemd:
        # taken by _poll, off the main loop)
        blocked = self.blocked.get(name) if vm is not None and off else None
        dbus = vm is not None and vm.display == "dbus"
        start = vm is not None and off and not blocked and self.prepare_op is None
        shutdown = vm is not None and st in (STARTING, RUNNING) and not busy
        sens = {
            "new": self.create_op is None,
            "start": start,
            "start-connect": start and dbus,
            "start-reason": start,
            "shutdown": shutdown,
            "unplug": shutdown,
            "connect": vm is not None and dbus and pid is not None and not vpid and not busy,
            "disconnect": vm is not None and vpid is not None and not busy,
            "wipe": vm is not None and off,
            "new-identity": vm is not None and off,
            "rename": vm is not None and off,
            "clone": vm is not None and off,
            "settings": vm is not None and off and self.prepare_op is None,
            "settings-page": vm is not None and off and self.prepare_op is None,
            "logs": name is not None,
            "folder": name is not None,
            "delete": name is not None and (off or name in self.broken and not pid),
        }
        for k, on in sens.items():
            if self.actions[k].get_enabled() != on:
                self.actions[k].set_enabled(on)
        self.update_header(
            header.slot(
                st if vm is not None else None,
                self.ops[name][0] if busy else None,
                blocked,
                pid is not None,
                vpid is not None,
                vm.display if vm is not None else "dbus",
            )
        )
        settings_tip = (
            "Shut down to change settings" if vm is not None and st != OFF else "Settings…"
        )
        self.summary_view.set_edit_tooltip(settings_tip)
        self.win.vm_menu_button.set_sensitive(name is not None)
        self.win.busy.set_visible(
            bool(self.ops) or self.create_op is not None or self.prepare_op is not None
        )
        self.vm_title.set_text(name or "No VM selected")

    def update_header(self, slot):
        """The header's state button as header.slot says: its icon, its tooltip and accessible label
        (the action, or what runs), style, action and dropdown."""
        button = self.win.state_button
        button.set_icon_name(slot.icon)
        button.set_tooltip_text(slot.tooltip)
        button.update_property([Gtk.AccessibleProperty.LABEL], [slot.label])
        if slot.suggested != button.has_css_class("suggested-action"):
            (button.add_css_class if slot.suggested else button.remove_css_class)(
                "suggested-action"
            )
        button.set_action_name(f"win.{slot.action}" if slot.action else None)
        if slot.menu:
            menu = getattr(self.win, slot.menu)
            if button.get_menu_model() is not menu:
                button.set_menu_model(menu)
        # without an action (an operation runs) the whole button, dropdown included, is insensitive
        button.set_sensitive(slot.action is not None)

    def update_details(self):
        name = self.selected()
        try:  # the selected VM's traffic inspector, a card of its summary
            self.inspector.refresh(
                vm_library.VM(self.lib, name) if name and name not in self.broken else None
            )
        except Exception as e:  # noqa: BLE001
            self.log_error("the inspector", e)
        try:  # and its VNC server, while beanjar vnc serves it
            self.vnc_panel.refresh(
                vm_library.VM(self.lib, name) if name and name not in self.broken else None
            )
        except Exception as e:  # noqa: BLE001
            self.log_error("the VNC panel", e)
        view = self.summary_view
        if not name:
            self.summary_values = {}
            view.update({})
            view.set_tiles({k: ("", "—", "") for k in view.TILES}, "dimmed")
            view.set_problem(None)
            self.update_picture()
            self._set_console(None)
            return
        vm = vm_library.VM(self.lib, name)
        st, detail, pid, vpid = self.states.get(name, (OFF, "", None, None))
        if name in self.broken:
            self.summary_values = {"state": f"{BROKEN}: {self.broken[name]}", "directory": vm.dir}
            view.update(
                {
                    "directory": home_path(vm.dir),
                    "logs": home_path(vm.logs_dir),
                    "qemu_pid": str(pid) if pid else "",
                },
                full=dict(directory=vm.dir, logs=vm.logs_dir),
            )
            view.set_tiles(
                {
                    "state": ("State", BROKEN, ""),
                    "time": self._last_run_tile(vm),
                    "cpu": ("CPUs", "—", ""),
                    "memory": ("Memory", "—", ""),
                },
                widgets_phase(BROKEN, self.widgets),
            )
            short = self.broken[name].removeprefix(vm.config_path + ": ")
            view.set_problem(
                summary_facts.problem(broken=short)._replace(tooltip=self.broken[name])
            )
            self.update_picture()
            self._set_console(vm.console_log)
            return
        c = vm.config
        values = {
            k: ", ".join(v) if isinstance(v := c.get(k, ""), list) else str(v) for k, _t in SUMMARY
        }
        values["platform"] = vm.platform_title()
        values["mem"] = str(vm.mem() or "") + ("" if c.get("mem") else " (the model's)")
        values["state"] = (
            f"{st}{f' ({detail})' if detail else ''}"
            + (f", QEMU pid {pid}" if pid else "")
            + (f", viewer pid {vpid}" if vpid else "")
        )
        fw_dir = vm.firmware_dir()
        fw_found = bool(paths.root_pak(fw_dir))
        if not fw_found:
            values["fw"] = f"{fw_dir} (not found)"
        elif vm.firmware_external():
            values["fw"] = f"{vm.firmware_name()} (used in place: {fw_dir})"
        else:
            values["fw"] = f"{vm.firmware_name()} (in the VM's folder)"
        values["directory"] = vm.dir
        values["address"] = vm.address() or ""  # its ports' host address (its slot)
        link = c.get("link", "nat")
        values["network"] = vm_settings.network_label(link, c.get("mode"))
        # 3000, 3001, 6622, 9922, 9991: listen with link nat; the VM's own LAN address with lan
        lan = vm.lan_address()
        values["lan_address"] = (
            (lan or ("not running" if link == "lan" else "")) if link == "lan" else "—"
        )
        values["listen"] = (
            c.get("listen") or values["address"]
            if link == "nat"
            else "none (link off)"
            if link == "off"
            else f"its LAN address ({lan or (c.get('address') or 'dhcp').split('/')[0]})"
        )
        values["model_key"] = (
            "Installed (value not shown)"
            if vm.has_model_key()
            else "None: the built-in model secret"
        )
        graphics = self.gpu[name][2] if name in self.gpu and pid else gpu.status_off()
        values["gpu"] = gpu.label(graphics)
        self.summary_values = values
        displayed = dict(values)
        mib = vm.mem() or 0
        displayed["mem"] = fmt_mem(mib) + (" (model default)" if not c.get("mem") else "")
        layers = c.get("layers", [])
        displayed["gpu"] = "On" if (graphics["enabled"] if pid else "gl" in layers) else "Off"
        displayed["free_page_reporting"] = "On" if c.get("free_page_reporting") == "on" else "Off"
        displayed["model_key"] = "Custom installed" if vm.has_model_key() else "Built-in"
        displayed["display"] = {"dbus": "D-Bus", "egl-headless": "EGL headless", "none": "Off"}.get(
            vm.display, vm.display
        )
        audio = c.get("audio") or summary_facts.audio_default(vm.display)
        displayed["audio"] = {"dbus": "D-Bus", "none": "Off", "off": "Off"}.get(audio, audio)
        mode = {"mitm": "MITM", "direct": "Direct"}.get(c.get("mode", "mitm"), "—")
        displayed["network"] = "Off" if link == "off" else f"{link.upper()} · {mode}"
        mitm = vm_settings.guest_net(link, c.get("mode")) == "mitm"
        flows = c.get("mitm_flows", vm_settings.MITM_FLOWS)
        values["mitm_flows"] = displayed["mitm_flows"] = (
            "—" if not mitm else "all flows" if flows == 0 else f"the last {flows} flows"
        )
        values["mitm_flow_bytes"] = displayed["mitm_flow_bytes"] = (
            "—" if not mitm else vm_settings.flow_bytes_label(c.get("mitm_flow_bytes"))
        )
        # the address its forwarded ports are on, with them (VM.forwards: what ares and ssh use)
        try:
            ports = " ".join(str(port) for port in sorted({host for host, _g, _n in vm.forwards()}))
        except Error:
            ports = ""
        with_ports = (lambda a: f"{a} · ports {ports}" if ports else a) if link != "off" else str
        displayed["address"] = with_ports(values["address"]) if values["address"] else ""
        displayed["lan_address"] = with_ports(lan) if lan else values["lan_address"]
        unit = vm.unit_name() if pid else None
        restarts = lifecycle.restarts(vm.run_dir) if pid else 0
        displayed.update(
            directory=home_path(vm.dir),  # ~ shortened; the tooltip has the full path
            logs=home_path(vm.logs_dir),
            link=link,
            firmware_path=home_path(fw_dir),
            qemu_pid=str(pid) if pid else "",
            viewer_pid=str(vpid) if vpid else "",
            unit=unit or "",
            restarts=str(restarts),
        )
        shown = summary_facts.shown_hardware(c, vm.display)
        shown.update(
            address=link == "nat",
            lan_address=link == "lan",
            listen=link != "off",
            mitm_flows=mitm,
            mitm_flow_bytes=mitm,
            restarts=restarts > 0,
        )
        source = "External firmware" if vm.firmware_external() else "Managed firmware"
        if not fw_found:
            source = "Firmware missing"
        elif self._untested(fw_dir):
            source += " · not tested yet"
        view.update(
            displayed,
            tv=" · ".join(filter(None, (vm.platform_title(), fw_label(vm.firmware_name())))),
            source=source,
            layers=layers,
            reason=vm.reason() if st != OFF else None,
            tags=c.get("tags", []),
            full=dict(directory=vm.dir, logs=vm.logs_dir, firmware_path=fw_dir),
            shown=shown,
            copies=dict(address=values["address"], lan_address=lan or ""),
        )
        # the tiles: running, its live figures (_live_tiles, each second); off, its settings
        phase = self.store_phase(name) or st
        caption = " · ".join(
            filter(None, (detail.capitalize() if detail else "", "Viewer open" if vpid else ""))
        )
        tiles = {"state": ("State", phase, caption)}
        if not pid:
            smp = int(c.get("smp", 0) or 0)
            tiles.update(
                time=self._last_run_tile(vm),
                cpu=("CPUs", str(smp) if smp else "—", ""),
                memory=("Memory", fmt_mem(mib) if mib else "—", ""),  # as set: the row's form
            )
        view.set_tiles(tiles, widgets_phase(phase, self.widgets))
        if pid:
            self._live_tiles(pid, self.cpu_text.get(name, ""), graphics["headline"])
        view.set_problem(
            summary_facts.problem(
                qmp_detail=detail if pid else "",
                failed=self.failures.get(name),
                firmware_missing=None if fw_found else home_path(fw_dir),
                blocked=self.blocked.get(name) if st == OFF and name not in self.ops else None,
            )
        )
        self.update_picture()
        self._set_console(vm.console_log)

    def store_phase(self, name):
        """VM name's row phase (Starting while a start runs), or None."""
        for i in range(self.store.get_n_items()):
            item = self.store.get_item(i)
            if item.name == name:
                return item.phase
        return None

    @staticmethod
    def _last_run_tile(vm):
        """The Last run tile: when its console log or QEMU's output was last written."""
        when = summary_facts.last_run(vm.logs_dir)
        caption = "" if when is None else time.strftime("%Y-%m-%d %H:%M", time.localtime(when))
        return ("Last run", summary_facts.relative_time(when), caption)

    @staticmethod
    def _untested(fw_dir):
        try:
            return not targets.tested(paths.firmware_target(fw_dir))
        except ValueError:
            return False

    # -- the TV picture --
    def _rotation(self, name, live):
        """How far VM name's picture turns (a worker thread's): a portrait model's guest's last
        rotation report while it runs (live), the angle its set was left at when it is off."""
        try:
            vm = vm_library.VM(self.lib, name)
            if not vm.portrait():
                return 0
            angle = rotation.status(vm).get("rotation" if live else "saved")
        except Exception:  # noqa: BLE001 - no turn is the safe answer
            return 0
        return angle if angle in (90, 180, 270) else 0

    def update_picture(self, changed=None):
        """The selected VM's picture: taken anew (Pictures.update) at most every 5 s while it runs,
        the Summary is shown, the window active and no viewer open; its last kept one when it is
        off. changed: the VM a new picture came for (None: the selected one)."""
        name = self.selected()
        if changed is not None and changed != name:
            return
        view = self.summary_view.picture
        if not name or name in self.broken:
            view.show("none", None)
            view.set_action(None)
            return
        st, _detail, pid, vpid = self.states.get(name, (OFF, "", None, None))
        dbus = self.displays.get(name) == "dbus"
        mode = "live" if pid and st == RUNNING and dbus else "last" if st == OFF else None
        allowed = (
            self.details.get_visible_child_name() == "summary" and self.win.is_active() and not vpid
        )
        if changed is None:
            self.pictures.update(name, mode, allowed)
        kind, texture = self.pictures.picture(name)
        view.show(kind, texture)
        if vpid:
            action = "Show the Viewer"
        elif pid:
            action = "Connect" if self.actions["connect"].get_enabled() else None
        else:
            action = "Start" if self.actions["start"].get_enabled() else None
        view.set_action(action)

    def picture_clicked(self):
        """A click on the picture: the viewer shown (if open) or opened while it runs, a start
        when it is off; the header's state button does the same."""
        name = self.selected()
        _st, _detail, pid, vpid = self.states.get(name, (OFF, "", None, None))
        if vpid:
            self.present_viewer(name, vpid)
        elif pid:
            if self.actions["connect"].get_enabled():
                self.connect()
        elif self.actions["start"].get_enabled():
            self.start()

    def present_viewer(self, name, vpid):
        """Bring VM name's open viewer (pid vpid) to the front: SIGUSR1, which it presents its
        window on (viewer/main.py); a viewer that does not catch it (an older one) is not sent it,
        since SIGUSR1 would end it."""
        if hostprocs.catches_signal(vpid, signal.SIGUSR1):
            os.kill(vpid, signal.SIGUSR1)
        else:
            self.say(f"{name}: the viewer is already open (pid {vpid})")

    def summary_text(self):
        """The summary as "Title: value" lines (the tests)."""
        return "\n".join(f"{title}: {self.summary_values.get(key, '')}" for key, title in SUMMARY)

    def _set_console(self, path):
        if path != self.console_path:
            self.console_path = path
            if self.console_monitor is not None:
                self.console_monitor.cancel()
            self.console_monitor = self._monitor(path, self._console_changed) if path else None
            self._update_console()

    def _console_changed(self):
        """The console's monitor: one read for a burst of events (a guest writing line by line)."""
        if self.console_pending is None:
            self.console_pending = GLib.timeout_add(CONSOLE_COALESCE_MS, self._console_due)

    def _console_due(self):
        self.console_pending = None
        try:
            self._update_console(changed_only=True)
        except Exception as e:  # noqa: BLE001
            self.log_error("the console", e)
        return False

    def _update_console(self, changed_only=False):
        """The console's tail, its last TAIL_BYTES; changed_only (the timer, the monitor): only if
        the file is not as it was last read (its monitor misses a log made anew in a logs/ made
        anew). What a log the view already shows gained is appended, and as much dropped from the
        top: the view is set anew only for another file (a VM started again, another VM)."""
        try:
            st = os.stat(self.console_path) if self.console_path else None
            sig = st and (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        except OSError:
            sig = None
        if changed_only and sig == self.console_sig:
            return
        old, self.console_sig = self.console_sig, sig
        same = (
            changed_only
            and old
            and sig
            and old[:2] == sig[:2]
            and self.console_read <= sig[2] <= self.console_read + TAIL_BYTES
            and self.console_shown == self.console_path
        )
        buf = self.console.get_buffer()
        if same:
            try:
                with open(self.console_path, "rb") as f:
                    f.seek(self.console_read)
                    data = f.read()
                    self.console_read = f.tell()
            except OSError:
                return
            text = self.console_decoder.decode(data).replace("\r", "")
            if not text:
                return
            log_append(self.console, text)
            extra = buf.get_char_count() - TAIL_BYTES
            if extra > 0:  # whole lines off the top, as the full read drops its first partial one
                cut = buf.get_iter_at_offset(extra)
                if not cut.starts_line():
                    cut.forward_line()
                buf.delete(buf.get_start_iter(), cut)
        else:
            text, self.console_read = "", 0
            self.console_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            if self.console_path:
                try:
                    with open(self.console_path, "rb") as f:
                        f.seek(0, 2)
                        size = f.tell()
                        f.seek(max(0, size - TAIL_BYTES))
                        text = self.console_decoder.decode(f.read()).replace("\r", "")
                        self.console_read = f.tell()
                    if size > TAIL_BYTES:
                        text = text.split("\n", 1)[-1]
                except OSError:
                    text = ""
            self.console_shown = self.console_path
            if buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False) == text:
                return
            log_append(self.console, text, clear=True)
        buf.place_cursor(buf.get_end_iter())
        self.console.scroll_mark_onscreen(buf.get_insert())

    def selection_changed(self):
        row = self.vm_list.get_selected_row()
        if row is not None:
            self.last_index = row.get_index()
        self.update_actions()
        self.update_details()

    def say(self, text, output=False):
        """A toast; output: with a button that shows the Output page."""
        self.messages.append(text)
        toast = Adw.Toast(title=GLib.markup_escape_text(text), timeout=4)
        if output:
            toast.set_button_label("Show Output")
            toast.set_action_name("win.show-output")
        self.win.toasts.add_toast(toast)

    def save_option(self, key, value):
        self.lib.set_option(key, value)

    def guard(self, cb):
        try:
            cb()
        except Error as e:
            self.say(str(e))

    # -- long operations --
    def output_start(self, command, show=True):
        """The Output page for a new operation (shown), its command, then line(text) for each line."""
        buf = self.output.get_buffer()
        log_append(self.output, f"$ {command}\n", clear=True)
        if show:
            self.details.set_visible_child_name("output")

        def line(text):
            log_append(self.output, text + "\n")
            self.output.scroll_mark_onscreen(buf.get_insert())

        return line

    def run(self, name, kind, args, done=None, library=None):
        line = self.output_start(f"beanjar {' '.join(args)}")

        def finished(rc):
            self.ops.pop(name, None)
            line(f"[exit {rc}]")
            # the Summary's banner names a failed start or stop until the next one succeeds
            if kind in summary_facts.FAILED_KINDS and library in (None, self.lib):
                if rc:
                    self.failures[name] = (kind, rc)
                else:
                    self.failures.pop(name, None)
            # done may take the result over (return True: a dialog says more than a toast)
            if not (done and done(rc)):
                self.say(
                    f"{name or 'Beanjar'}: {kind} "
                    + ("done" if rc == 0 else f"failed (exit {rc})"),
                    output=rc != 0,
                )
            self.refresh()

        self.ops[name] = (kind, Operation(library or self.lib, args, line, finished))
        self.refresh()

    # -- actions --
    def start(self):
        self.start_vm(self.selected())

    def warn_mitm(self, name):
        """Say so when a mode=mitm VM will start without its traffic inspector (mitmtool.py)."""
        from beanjar.network import mitmtool

        c = self.lib.vm(name).config
        if vm_settings.guest_net(c.get("link"), c.get("mode")) == "mitm":
            tool = mitmtool.check()
            if not tool.usable:
                self.say(f"{name}: the traffic inspector will be off: {tool.problem}")

    def start_vm(self, name, reason=None, then=None):
        """Start VM name (`beanjar start`, with --reason: why it runs); then(), once it started."""
        self.warn_mitm(name)
        self.run(
            name,
            "start",
            ["start", name, *(["--reason", reason] if reason else [])],
            done=(lambda rc: then() if rc == 0 else None) if then else None,
        )

    def start_connect(self):
        """Start and Connect: the viewer opens once the start returns (QEMU runs)."""
        name = self.selected()
        self.start_vm(name, then=lambda: self.guard(lambda: self.connect_vm(name)))

    def start_reason(self):
        """Start with Reason…: why it runs (`beanjar start --reason`), which list, info and the
        viewers show and Stop asks about."""
        name = self.selected()

        def ok(form):
            self.start_vm(name, form.fields["reason"].get_text().strip())

        form = self.widgets.Confirm(
            f"Start {name}?",
            "Say why it runs: others see it in the list and the viewers, and Shut Down asks "
            "before overriding it. It lasts until the VM stops.",
            "_Start",
            ok,
            destructive=False,
            on_error=self.say,
        )
        entry = form.add_entry("reason", "Reason")
        entry.connect(
            "changed", lambda e: form.set_response_enabled("ok", bool(e.get_text().strip()))
        )
        form.set_response_enabled("ok", False)
        self.show(form)
        return form

    def shutdown(self):
        return self.shut_down(self.selected(), self.lib, self.selected())

    def unplug(self):
        """Unplug…: the Shut Down dialog with Unplug chosen."""
        return self.shut_down(self.selected(), self.lib, self.selected(), unplug=True)

    def shut_down(self, name, library, key, note=None, unplug=False):
        """Shut Down… (after VirtualBox's close dialog): the power-off signal (`beanjar stop
        --no-fallback`), the default, or Unplug (`beanjar stop --unplug`, QEMU killed at once).
        Whether the guest can take the signal is read when the dialog opens (VM.power_off_refusal:
        QMP and the micom's state, nothing typed on the console); where it cannot, its radio is
        insensitive and says why, and OK runs `beanjar stop`, which syncs the disks and stops the VM
        instead. A signal the VM has not powered off on in time, or that webOS refused, offers
        Unplug or waiting again (stop_timed_out). A VM running for a reason (VM.reason) has it in
        the dialog, which is the confirmation stop --force asks for."""
        vm = vm_library.VM(library, name)
        try:
            why = vm.power_off_refusal()
        except Exception as e:  # noqa: BLE001
            why = str(e)
        reason = vm.reason()
        if reason:
            note = " ".join(filter(None, (note, f"It runs for: {vmunits.reason_line(reason)}.")))

        def ok(form):
            if form.fields["unplug"].get_active():
                self.run(key, "unplug", ["stop", name, "--unplug", "--force"], library=library)
            elif why is None:
                self.run(
                    key,
                    "shut down",
                    ["stop", name, "--no-fallback", "--force"],
                    done=lambda rc: self.stop_timed_out(name, library, key) if rc else None,
                    library=library,
                )
            else:  # unavailable: stop falls back to the sync and SIGTERM
                self.run(key, "shut down", ["stop", name, "--force"], library=library)

        form = self.widgets.Confirm(
            f"Shut Down {name}?",
            " ".join(filter(None, (note, "How should the VM stop?"))),
            "_Shut Down",
            ok,
            destructive=False,
            on_error=self.say,
        )
        if why is None:
            subtitle = "webOS powers the TV off, and the VM stops by itself"
        else:
            why = why if why.startswith("webOS") else why[:1].upper() + why[1:]
            subtitle = f"{why}; Shut Down syncs the disks and stops the VM"
        signal_row = form.add_radio("signal", "Send power-off signal", subtitle, active=True)
        signal_row.set_sensitive(why is None)
        form.add_radio(
            "unplug",
            "Unplug (may lose data)",
            "Kills the VM at once, as a TV losing its power: what webOS has not written to its "
            "disks is lost",
        )

        def chosen(*_):
            unplug = form.fields["unplug"].get_active()
            form.set_response_appearance(
                "ok",
                Adw.ResponseAppearance.DESTRUCTIVE if unplug else Adw.ResponseAppearance.SUGGESTED,
            )
            form.set_response_label("ok", "_Unplug" if unplug else "_Shut Down")

        form.fields["unplug"].connect("toggled", chosen)
        if unplug:
            form.fields["unplug"].set_active(True)
        self.show(form)
        return form

    def stop_timed_out(self, name, library, key):
        """A `stop --no-fallback` that did not stop the VM: one that has the signal and has not
        powered off in time can be waited for again, or unplugged; one that could not take it
        (it changed since the dialog asked) can be shut down as stop does without it."""
        vm = vm_library.VM(library, name)
        if not vm.pid():
            return None
        sent = base.read_pid(vm.power_off_file) == vm.pid()
        d = Adw.AlertDialog(
            heading=f"{name} Has Not Powered Off",
            body=(
                f"It is still running {poweroff.POWER_OFF_TIMEOUT} s after the power-off signal."
                if sent
                else "It cannot take the power-off signal now."
            ),
        )
        d.add_response("cancel", "_Leave It Running")
        if sent:
            d.add_response("wait", "_Wait Longer")
        else:
            d.add_response("sync", "_Shut Down")
        d.add_response("unplug", "_Unplug")
        d.set_response_appearance("unplug", Adw.ResponseAppearance.DESTRUCTIVE)
        d.set_default_response("wait" if sent else "sync")
        d.set_close_response("cancel")

        def response(_d, r):
            if r == "wait":
                self.run(
                    key,
                    "shut down",
                    ["stop", name, "--no-fallback", "--force"],
                    done=lambda rc: self.stop_timed_out(name, library, key) if rc else None,
                    library=library,
                )
            elif r == "sync":
                self.run(key, "shut down", ["stop", name, "--force"], library=library)
            elif r == "unplug":
                self.run(key, "unplug", ["stop", name, "--unplug", "--force"], library=library)

        d.connect("response", response)
        self.show(d)
        return d

    def connect(self):
        self.connect_vm(self.selected())

    def connect_vm(self, name):
        pid = self.lib.view(name)
        self.want_viewer[name] = self.lib.vm(name).pid()
        self.say(f"{name}: viewer opened (pid {pid})")
        self.refresh()

    def disconnect(self):
        name = self.selected()
        self.want_viewer.pop(name, None)
        self.run(name, "disconnect", ["viewer", "close", name])

    def wipe(self):
        name = self.selected()
        try:
            target = self.lib.vm(name).target()
        except Error as e:
            self.say(f"{name}: {e}")
            return
        state_parts, settings_parts = targets.state_parts(target), targets.settings_parts(target)

        def ok(form):
            parts = [p for p in state_parts if form.fields[p].get_active()]
            args = ["wipe", name] + (
                [] if len(parts) == len(state_parts) else [a for p in parts for a in ("--part", p)]
            )
            self.run(name, "wipe", args)

        form = self.widgets.Confirm(
            f"Wipe {name}?",
            "The chosen state disks are deleted; the next boot formats them afresh.",
            "_Wipe",
            ok,
            on_error=self.say,
        )
        for p in state_parts:
            row = form.add_switch(
                p,
                p,
                "the TV's settings (tvservice's NVM)" if p in settings_parts else None,
                active=p not in settings_parts,
            )
            row.connect(
                "notify::active",
                lambda *_: form.set_response_enabled(
                    "ok", any(form.fields[q].get_active() for q in state_parts)
                ),
            )
        self.show(form)

    def new_identity(self):
        name = self.selected()
        self.confirm(
            f"Give {name} a New Identity?",
            "The TV is reset to its factory state: all of its state disks are deleted, its settings "
            "included. It gets a new MAC address and serial number, and so a new NDUID, and new SSH and "
            "developer mode keys: to apps and LG's servers it is another TV. A key fetched from it "
            "before no longer works. Its model key is kept.",
            "_New Identity",
            lambda: self.run(name, "new identity", ["identity", "reset", name]),
        )

    def rename(self):
        """Rename (beanjar rename): the VM, off, under another name; Library.rename refuses a
        front end still open or a name taken, shown in a toast."""
        name = self.selected()

        def ok(form):
            new = form.fields["name"].get_text().strip()
            vm = self.lib.rename(name, new)
            self.refresh()
            self.select(vm.name)
            lan = vm.lan_hostname()
            self.say(
                f"{name} renamed to {vm.name}"
                + (f"; its LAN hostname is {lan} from its next start" if lan else "")
            )

        form = self.widgets.Confirm(
            f"Rename {name}?",
            "It stays the same TV: its identity, address, settings, state and logs are kept. With "
            "link lan it sends its new name as its DHCP hostname from its next start.",
            "_Rename",
            ok,
            destructive=False,
            on_error=self.say,
        )
        entry = form.add_entry("name", "Name", name)
        entry.connect(
            "changed",
            lambda e: form.set_response_enabled(
                "ok",
                bool(base.NAME_RE.fullmatch(e.get_text().strip())) and e.get_text().strip() != name,
            ),
        )
        form.set_response_enabled("ok", False)
        self.show(form)

    def clone(self):
        """Clone… (`beanjar clone`): a copy of the VM, off, as another TV under a new name."""
        name = self.selected()

        def ok(form):
            new = form.fields["name"].get_text().strip()

            def done(rc):
                if rc == 0:
                    self.refresh()
                    self.select(new)
                    self.say(f"{name} cloned to {new}")
                    return True
                return False

            self.run(name, "clone", ["clone", name, new], done=done)

        form = self.widgets.Confirm(
            f"Clone {name}?",
            "The copy keeps its settings, state disks and model key, as another TV: with a new "
            "MAC address, serial number and SSH and developer mode keys.",
            "_Clone",
            ok,
            destructive=False,
            on_error=self.say,
        )
        taken = {vm.name for vm in self.lib.vms()}
        entry = form.add_entry("name", "Name", self.clone_name(name, taken))
        entry.connect(
            "changed",
            lambda e: form.set_response_enabled(
                "ok",
                bool(base.NAME_RE.fullmatch(e.get_text().strip()))
                and e.get_text().strip() not in taken,
            ),
        )
        self.show(form)
        return form

    @staticmethod
    def clone_name(name, taken):
        """A free name for a copy of VM name: name-2, name-3, ..."""
        base = name
        i = 2
        while f"{base}-{i}" in taken:
            i += 1
        return f"{base}-{i}"

    def _vm_form(self, title, ok_label, cfg, on_ok, new):
        """New VM (new) or Settings, filled in from cfg (vm.toml's settings; name)."""
        form = self.widgets.VMForm(title, ok_label, on_ok)
        f = form.fields
        f["name"].set_text(cfg.get("name", ""))
        form.name_group.set_visible(new)
        self._fw_sources(form, cfg.get("fw", ""), new, cfg.get("dir"))
        # mem: vm.toml's, else the model's (nvm.board_ram): a VM without one follows its model, and keeps
        # doing so unless the value is changed here (form.model_mem, _model_mem)
        form.model_mem = None
        if cfg.get("mem"):
            f["mem"].set_value(int(cfg["mem"]))
        elif not new:
            self._model_mem(form, self.lib.vm(cfg["name"]).mem())
        else:  # until a firmware names its model: the largest model's, as a placeholder
            f["mem"].set_value(
                max(
                    filter(None, map(self._board_ram, targets.all_targets())),
                    default=f["mem"].get_value(),
                )
            )
            self._model_mem(form, None)
        form.mem_initial = int(f["mem"].get_value())
        f["smp"].set_value(int(cfg.get("smp", 4)))
        form.free_page_reporting_group.set_visible(not new)  # a new VM has it off
        f["free_page_reporting"].set_active(cfg.get("free_page_reporting", "off") == "on")
        dropdown(f["link"], vm_settings.LINKS, cfg.get("link", "nat"))
        dropdown(f["mode"], vm_settings.MODES, cfg.get("mode", "mitm"))
        form.identity_group.set_visible(not new)  # a new VM's are made at create
        f["mac"].set_text(str(cfg.get("mac", "")))
        f["serial"].set_text(str(cfg.get("serial", "")))
        form.settings = not new
        f["listen"].set_text(str(cfg.get("listen", "")))
        f["address"].set_text(str(cfg.get("address", "")))
        f["mitm_flows"].set_value(int(cfg.get("mitm_flows", vm_settings.MITM_FLOWS)))
        f["mitm_flow_bytes"].set_text(str(cfg.get("mitm_flow_bytes", "")))
        form.link_changed()
        self._layer_rows(
            form, cfg.get("layers", vm_settings.DEFAULT_LAYERS), None if new else cfg["name"]
        )
        dropdown(f["display"], vm_settings.DISPLAYS, cfg.get("display", "dbus"))
        dropdown(f["audio"], vm_settings.AUDIOS, cfg.get("audio", "dbus"))
        if not new:
            form.show_model_key(self.lib.vm(cfg["name"]).has_model_key())
        form.on_browse = lambda row: self.browse(form, row)
        form.on_browse_model_key = lambda: self.browse_model_key(form)
        form.on_change = lambda: self._fw_hints(form)
        self._fw_hints(form)
        return form

    def _fw_sources(self, form, fw, new, vm_dir=None):
        """The firmware's sources (form.sources: (label, kind, value)). New VM: each firmware of the
        host's collections (newvm.collection; the first selected), used in place unless imported
        (fw_import), a .epk, extracted into the VM's folder, or a directory, used in place or
        imported. Settings: the VM's own firmware/ (labelled as the VM list does, fw_label) if it
        has one, or a directory used in place (fw: vm.toml's, selected)."""
        form.own_fw = None
        if new:
            sources = [
                (fw_label(n) + untested_suffix(d), "place", d) for n, d in create.collection()
            ]
            sources += [("Firmware File (.epk)…", "epk", None), ("Directory…", "dir", None)]
        else:
            sources = []
            if paths.root_pak(os.path.join(vm_dir, install.OWN_FW)):
                m = install.manifest(os.path.join(vm_dir, install.OWN_FW)) or {}
                form.own_fw = m.get("name", install.OWN_FW)
                sources.append((f"In the VM's Folder: {fw_label(form.own_fw)}", "own", ""))
            sources.append(("Directory in Place…", "dir", None))
        form.sources = sources
        form.vm_dir = None if new else vm_dir
        f = form.fields
        i = 0
        if fw and not new:
            i = len(sources) - 1
            f["fwdir"].set_text(fw)
        dropdown(f["fw"], [label for label, _k, _v in sources])
        f["fw"].set_selected(i)

    @staticmethod
    def fw_source(form):
        """(kind, value) of the firmware chosen: value "" for the VM's own firmware/, a collection's
        firmware's directory ("place"), or the directory's or .epk's path as typed."""
        f = form.fields
        i = f["fw"].get_selected()
        _label, kind, value = form.sources[i] if 0 <= i < len(form.sources) else ("", "dir", None)
        if kind == "dir":
            value = f["fwdir"].get_text().strip()
        elif kind == "epk":
            value = f["epk"].get_text().strip()
        return kind, value

    def _board_ram(self, target):
        """The RAM of target's model in the library's checkout (nvm.board_ram), None if it cannot say."""
        try:
            return nvm.board_ram(target, paths.Tree(self.lib.repo))
        except (Error, targets.Error, OSError):  # no checkout known: it cannot say
            return None

    @staticmethod
    def _model_mem(form, mib):
        """Memory as the model's RAM, mib (None: not known yet), while it was not changed by hand."""
        f = form.fields["mem"]
        untouched = (
            form.model_mem if form.model_mem is not None else getattr(form, "mem_initial", None)
        )
        if untouched is not None and int(f.get_value()) != untouched:
            return
        if mib:
            f.set_value(mib)
        elif getattr(form, "mem_initial", None):
            f.set_value(form.mem_initial)
        form.model_mem = int(f.get_value()) if mib else None
        f.set_subtitle(
            f"MiB; the model's RAM, {mib} MiB, unless changed"
            if mib
            else "MiB; the model's RAM unless changed"
        )

    def _fw_hints(self, form):
        """The firmware group: which rows show and what the choice costs (the group's description)."""
        f = form.fields
        kind, value = self.fw_source(form)
        if form.name_group.get_visible():
            target = None
            if kind in ("dir", "place") and value:
                try:
                    if install.has_root(value):
                        target = install.target_of(value)
                except Error:
                    pass
            self._model_mem(form, self._board_ram(target) if target else None)
        new = form.name_group.get_visible()
        f["fwdir"].set_visible(kind == "dir")
        f["epk"].set_visible(kind == "epk")
        f["fw_import"].set_visible(new and kind in ("dir", "place"))
        place = new and kind in ("dir", "place") and not f["fw_import"].get_active()
        full = (
            form.own_fw
            if kind == "own"
            else os.path.basename(value)
            if kind == "place" and value
            else None
        )
        f["fw"].set_subtitle(
            full if full and fw_label(full) != full else ""
        )  # the name the label shortens
        try:
            sp = create.space(
                self.lib,
                fw=value,
                vm_dir=form.vm_dir,
                place=place,
            )
        except OSError:
            sp = {}
        free = f"{fmt_size(sp['free'])} free in the library" if sp.get("free") is not None else ""
        size = fmt_size(sp["paks"]) if "paks" in sp else None
        if kind == "epk":
            what = "Extracted into the VM's folder when it is created: about " + (
                f"{fmt_size(sp['epk'])}, the size of the .epk"
                if "epk" in sp
                else "the size of the .epk"
            )
        elif kind == "own":
            what = f"{size or '?'}, in the VM's folder"
        elif place:
            what = "Used in place, never written; nothing is copied" + (f": {size}" if size else "")
        elif new:
            what = "The VM's own firmware: " + (
                "hard links to the directory's paks, no more space"
                if sp.get("copy") == 0
                else f"a copy of the directory's paks, {size}"
                if size
                else "a copy of the directory's paks"
            )
        else:
            what = "Used in place, never written" + (f": {size}" if size else "")
        form.fw_group.set_description(". ".join(x for x in (what, free) if x) + ".")

    def browse(self, form, row):
        """A chooser for the firmware's directory or .epk (row: form.fields' fwdir or epk)."""
        epk = row is form.fields["epk"]
        filters = Gio.ListStore(item_type=Gtk.FileFilter)
        filt = None
        if epk:
            filt = Gtk.FileFilter(name="webOS Firmware (*.epk)")
            filt.add_pattern("*.epk")
            filters.append(filt)
        chooser = Gtk.FileDialog(
            title="Firmware File" if epk else "Directory of Extracted Paks",
            modal=True,
            filters=filters,
            default_filter=filt,
        )
        current = row.get_text().strip()
        if current:
            chooser.set_initial_file(Gio.File.new_for_path(os.path.expanduser(current)))

        def picked(dlg, res):
            try:
                f = (dlg.open_finish if epk else dlg.select_folder_finish)(res)
            except GLib.Error:
                return  # cancelled
            if f and f.get_path():
                row.set_text(f.get_path())

        (chooser.open if epk else chooser.select_folder)(self.win, None, picked)

    def browse_model_key(self, form):
        """A chooser for the model key's file; Save installs it."""
        chooser = Gtk.FileDialog(title="Model Key File", modal=True)

        def picked(dlg, res):
            try:
                f = dlg.open_finish(res)
            except GLib.Error:
                return  # cancelled
            if f and f.get_path():
                form.pick_model_key(f.get_path())

        chooser.open(self.win, None, picked)

    def _layer_rows(self, form, layers, name=None):
        """The Layers page: the VM's layers in its order, then the other optional layers of the
        checkout; then the ones every VM gets, which cannot be switched off. Built for the firmware
        target of the VM name (Settings), or of the default firmware (New VM); the source layers
        only if that target is not known."""
        try:
            target = self.lib.vm(name).target() if name else paths.Tree(self.lib.repo).target()
        except (Error, ValueError):
            target = None
        avail = self.lib.layers(target)

        def line(name):
            base, _at, version = name.partition("@")
            info = avail.get(base)
            if info is None:
                return "Not in this checkout (no layer.toml): switch it off to save"
            text = info["description"] or info["path"]
            if info.get("versions"):
                # a versioned layer (hbchannel@VERSION): the version this row runs
                text += f"; version {version or info['default_version']}"
            rules = ([f"needs {', '.join(info['requires'])}"] if info.get("requires") else []) + (
                [f"not with {', '.join(info['conflicts'])}"] if info.get("conflicts") else []
            )
            if rules:
                text += f" ({'; '.join(rules)})"
            return text if info["built"] else f"{text} (not built: scripts/build.sh guest)"

        auto = set(targets.ALWAYS_LAYERS)
        for n, on in vm_settings.layer_list(layers, avail):
            form.add_layer(
                n, line(n) + (" (always used; here it takes this place)" if n in auto else ""), on
            )
        for n in targets.ALWAYS_LAYERS:
            form.add_fixed_layer(n, line(n))

    def _form_values(self, form):
        """The dialog's values: fw (New VM: a .epk too; Settings: "" for the VM's own), then the settings."""
        f = form.fields
        kind, value = self.fw_source(form)
        if not value and kind != "own":
            raise Error(
                "choose a firmware file (.epk)"
                if kind == "epk"
                else "give the directory of the firmware's paks",
                "fw",
            )
        v = dict(fw=value)
        if form.name_group.get_visible():  # New VM: in place, unless imported
            v["place"] = kind != "epk" and not f["fw_import"].get_active()
        mem = int(f["mem"].get_value())
        # unchanged from the model's (or, New VM, no firmware resolved yet and not touched): none, the
        # VM follows its model's RAM
        follow = form.model_mem == mem or (
            form.name_group.get_visible() and form.model_mem is None and mem == form.mem_initial
        )
        v.update(
            mem=None if follow else mem,
            smp=int(f["smp"].get_value()),
            layers=form.layers_value(),
            link=dropdown_value(f["link"]),
            mode=dropdown_value(f["mode"]),
            display=dropdown_value(f["display"]),
            audio=dropdown_value(f["audio"]),
        )
        if form.identity_group.get_visible():
            v.update(
                mac=f["mac"].get_text().strip() or None,
                serial=f["serial"].get_text().strip() or None,
            )
        if form.settings:
            # each only where its link has it, else cleared (vm.toml refuses listen with lan,
            # address without lan or with lan and direct)
            v.update(
                free_page_reporting="on" if f["free_page_reporting"].get_active() else "off",
                listen=f["listen"].get_text().strip()
                if v["link"] == "nat"
                else "",  # empty: the VM's own address
                address=f["address"].get_text().strip()
                if v["link"] == "lan" and v["mode"] == "mitm"
                else "",  # empty: dhcp
                # the inspector's limits, kept whatever the mode (configure checks them)
                mitm_flows=int(f["mitm_flows"].get_value()),
                mitm_flow_bytes=f["mitm_flow_bytes"].get_text().strip(),
            )  # empty: no limit
        return v

    def new_vm(self):
        def ok(form):
            name = form.fields["name"].get_text().strip()
            create.check_name(self.lib, name)
            v = self._form_values(form)
            if v["display"] != "dbus" and v["audio"] == "dbus":
                v["audio"] = "none"
            plan = create.check(self.lib, name, **v)
            if create.slow(self.lib, plan):
                self.create_async(form, name, v, plan["epk"])
                return self.widgets.STAY
            line = self.output_start(f"new VM {name}", show=False)
            self.lib.create(name, out=line, **v)
            self.created(name)

        self.show(self._vm_form("New VM", "_Create", {}, ok, True))

    def created(self, name):
        self.refresh()
        self.select(name)
        self.say(f"Created {name}")
        self.warn_mitm(name)

    def create_async(self, form, name, v, epk=None):
        """A New VM that takes time: python3 -m beanjar.vm.create with a progress dialog over New VM.
        Done, both close; cancelled or failed (nothing is left of it either way), Close goes back
        to New VM, whose banner has the refusal. epk: the .epk it extracts (newvm.check's plan)."""
        args = [
            name,
            "--firmware",
            v["fw"],
            *([] if v.get("place", True) else ["--import"]),
            *([f"--mem={v['mem']}"] if v["mem"] else []),
            f"--smp={v['smp']}",
            f"--layers={','.join(v['layers'])}",
            f"--link={v['link']}",
            f"--mode={v['mode']}",
            f"--display={v['display']}",
            f"--audio={v['audio']}",
        ]
        win = self.widgets.ProgressDialog(
            f"Extracting {os.path.basename(epk)} for {name}" if epk else f"Creating {name}"
        )
        output = self.output_start(f"new VM {name}")
        buf = win.log.get_buffer()
        refusal = {}

        def line(text):
            log_append(win.log, text + "\n")
            win.log.scroll_mark_onscreen(buf.get_insert())
            output(text)
            for prefix, key in (("beanjar-key: ", "key"), ("beanjar: ", "message")):
                if text.startswith(prefix):
                    refusal[key] = text[len(prefix) :]

        def done(rc):
            self.create_op = None
            if rc == 0:
                win.finished(f"Created {name}")
                win.close()
                form.close()
                self.created(name)
                return
            if rc == 130:
                win.finished("Cancelled: nothing was created")
            else:
                message = refusal.get("message") or f"failed (exit {rc})"
                win.finished(f"Failed: {message}")
                form.show_error(message, self.widgets.KEY_PAGE.get(refusal.get("key")))
            self.say(win.head.get_text())
            self.refresh()

        self.create_op = win.operation = Operation(
            self.lib, args, line, done, module="beanjar.vm.create"
        )
        self.progress = win
        self.show(win)
        self.update_actions()

    def prepare_async(self, form, name, saved):
        """The board data of a VM whose settings were just saved (beanjar prepare --if-stale), with a
        progress dialog over Settings that cannot be closed or cancelled until it ends, so the
        settings are never left saved with their data unprepared by a closed dialog. Done, both
        close; failed, Close goes back to Settings, whose banner has the refusal (the settings
        stay saved: a start prepares them again)."""
        win = self.widgets.ProgressDialog(
            "Preparing board data…", cancellable=False, title=f"Settings of {name}"
        )
        output = self.output_start(f"prepare {name}", show=False)
        buf = win.log.get_buffer()
        refusal = {}

        def line(text):
            log_append(win.log, text + "\n")
            win.log.scroll_mark_onscreen(buf.get_insert())
            output(text)
            if text.startswith("beanjar: "):
                refusal["message"] = text[len("beanjar: ") :]

        def done(rc):
            self.prepare_op = None
            if rc == 0:
                win.finished(f"{name}: board data prepared")
                win.close()
                form.close()
                self.say(saved)
                self.warn_mitm(name)
            else:
                message = refusal.get("message") or f"failed (exit {rc})"
                win.finished(f"Failed: {message}")
                form.show_error(
                    f"Settings saved, but the board data could not be prepared: {message}. "
                    "It is prepared again at the next start."
                )
                self.say(f"{name}: settings saved, board data not prepared")
            self.update_actions()

        self.prepare_op = win.operation = Operation(
            self.lib, ["prepare", name, "--if-stale"], line, done
        )
        self.progress = win
        self.show(win)
        self.update_actions()

    def settings(self, page=None):
        """Settings…, at page (a VMForm page, or "identity": the Network page's Identity group)."""
        name = self.selected()
        vm = self.lib.vm(name)
        cfg = dict(vm.config, name=name, dir=vm.dir)

        def ok(form):
            v = self._form_values(form)
            change, path = form.model_key_change or (None, None)
            if change == "install":
                vm_library.read_model_key(path)  # refused before anything is saved
            before = dict(self.lib.vm(name).config)
            self.lib.configure(name, prepare=False, **v)
            if change:
                self.lib.set_model_key(name, path)  # as `beanjar key model set` (None: unset)
            self.refresh()
            saved = f"{name}: settings saved" + {
                "install": ", model key installed",
                "clear": ", model key cleared",
            }.get(change, "")
            after = self.lib.vm(name).config
            if any(before.get(k) != after.get(k) for k in vm_library.BOARD_SETTINGS):
                # its board data comes from what changed: worked out in the background
                self.prepare_async(form, name, saved)
                return self.widgets.STAY
            self.say(saved)
            self.warn_mitm(name)

        form = self._vm_form(f"Settings of {name}", "_Save", cfg, ok, False)
        if page == "identity":
            form.show_page("network")
            form.scroll_to(form.identity_group)
        elif page:
            form.show_page(page)
        self.show(form)
        return form

    def delete(self):
        name = self.selected()

        def really():
            self.lib.delete(name)
            self.say(f"Deleted {name}")
            self.refresh()

        self.confirm(
            f"Delete {name}?",
            "The VM is removed with its state disks and logs. This cannot be undone.",
            "_Delete",
            really,
        )

    def open_folder(self, path):
        if self.open_folders:
            try:
                Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(path).get_uri(), None)
            except GLib.Error as exc:
                raise Error(f"Could not open folder: {exc.message}") from exc

    def open_vm_folder(self):
        vm = self.lib.vm(self.selected())
        self.open_folder(vm.dir)
        self.say(f"VM folder: {vm.dir}")

    def open_logs(self):
        vm = self.lib.vm(self.selected())
        os.makedirs(vm.logs_dir, exist_ok=True)
        self.open_folder(vm.logs_dir)
        self.say(f"Logs: {vm.logs_dir}")

    def confirm(self, heading, body, ok_label, action):
        self.show(
            self.widgets.Confirm(heading, body, ok_label, lambda _form: action(), on_error=self.say)
        )

    def about(self):
        from . import ui

        about = Adw.AboutDialog(
            application_name="Beanjar",
            application_icon=ui.APP_ID,
            comments="Runs webOS TV firmware in QEMU virtual machines: creates them from "
            "firmware files, starts, stops, wipes and views them.",
        )
        self.show(about)

    def show(self, dialog):
        self.dialog = dialog
        dialog.present(self.win)


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="beanjar_ui.manager",
        description="Beanjar, the VM manager's window (GTK 4, libadwaita)",
    )
    p.add_argument("--library", help=f"library directory (default {base.default_library()})")
    p.add_argument("--repo", help="the Beanjar checkout")
    p.add_argument(
        "--ui-dir",
        help="the layouts from DIR (.ui, or .blp compiled at start, e.g. "
        "host/ui/data/manager) instead of build/manager/beanjar.gresource",
    )
    a = p.parse_args(argv)
    missing = no_display("beanjar", "use the command line (beanjar --help)")
    if missing:
        print(missing, file=sys.stderr)
        return 1
    if a.ui_dir:
        from . import ui

        ui.use_dir(a.ui_dir)
    try:
        lib = Library(a.library, a.repo)
    except Error as e:
        return vm_library.main_error(e)
    # The application ID names the icon and the desktop entry; NON_UNIQUE: each run is its own
    # window (a second one, e.g. with another --library, is not handed to the first).
    from . import ui

    app = Adw.Application(application_id=ui.APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
    holder = {}
    app.connect(
        "activate",
        lambda app: (holder.setdefault("m", Manager(app, lib)), holder["m"].win.present()),
    )
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    return app.run([])


if __name__ == "__main__":
    sys.exit(main())
