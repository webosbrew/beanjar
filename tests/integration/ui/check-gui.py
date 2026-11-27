#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The VM manager's GTK window against the fake launcher, QEMU, viewer and epk2extract of
tests/fakes: no VM, never the desktop's display.

    xvfb-run -a tests/integration/ui/check-gui.py [--shots PREFIX]      (pytest tests/integration/ui)

Drives beanjar_ui.manager.gui's Manager with direct calls, as the buttons would, in a temporary library
and checkout: shows the empty state (a status page with New VM) and About, creates a VM from a .epk through
New VM (a missing file refused on General, a failed and a cancelled extraction leaving nothing, its
progress over the dialog, then the firmware in that VM's folder), creates one from that VM's
firmware/ as a directory through the New VM dialog (hard-linked into its own folder; its pages, wide
and collapsed; the banner and the page it names; the layer list, reordered), shows that VM's own
firmware in its Settings and a directory used in place, edits a second VM's
layers in Settings (an unknown layer shown, not saved), picks a VM in the narrow window's overlaid list,
starts the first by activating
its row (Starting, then Running, its row updated in place), checks the memory admission on the
second, opens the viewer (activating the row again), shuts the VM down and starts it again (the
viewer is reopened: auto-reconnect; the Shut Down dialog without the power-off signal), disconnects,
shows the dialog with the signal and the one after a power-off that timed out, unplugs it, checks
the Wipe dialog's defaults (nvram and eeprom off), gives the second a new identity through its
confirmation, renames it (a taken name refused) and back, checks the list's context menu and deletes a VM. The second VM's model key is
installed and cleared through Settings, a bad file refused.
--shots PREFIX writes PREFIX-empty.png, -about.png (the About dialog), -fw-epk.png, -fw-extracting.png, -fw-dir.png (New VM's
firmware: a .epk chosen, its extraction's progress, a directory hard-linked into the VM's folder),
-fw-settings.png (Settings' firmware: a directory used in place),
-new-vm.png, -list-narrow.png, -running.png,
-console.png, -summary-off.png and -summary-failed.png (the Summary of a VM that is off, its
last picture kept, and after a failed start), -settings-model-key.png, -shut-down.png, -shut-down-unavailable.png,
-not-powered-off.png, -wipe.png and -new-identity.png (the window, with the dialog over it), and -vmform-<page>-wide.png,
-vmform-error-wide.png and -vmform-{sidebar,<page>}-narrow.png (New VM on each of its pages, the
window wide and narrow).

Exit status 77 (skip) without GTK 4 and libadwaita for Python or without an X display.
"""

import argparse
import base64
import glob
import os
import re
import shutil
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(TOP, "host", "core"))
sys.path.insert(1, os.path.join(TOP, "host", "ui"))
sys.path.insert(2, os.path.join(TOP, "tests", "lib"))
os.environ["GDK_BACKEND"] = "x11"
os.environ.pop("WAYLAND_DISPLAY", None)  # never the desktop's compositor
try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, GLib, Gtk
except (ImportError, ValueError) as e:
    print(f"SKIP: no GTK 4 and libadwaita for Python: {e}")
    sys.exit(77)
if not os.environ.get("DISPLAY"):
    print("SKIP: no X display (run under xvfb-run -a)")
    sys.exit(77)

from beanjar import base, core, hostconfig, unit, vmsettings  # noqa: E402
from beanjar.firmware import targets  # noqa: E402
from beanjar_ui.manager import gui, ui  # noqa: E402
import testunits  # noqa: E402


def icon_names(patterns):
    """Every "...-symbolic" icon name in the files matching patterns (TOP-relative globs)."""
    text = "".join(open(p).read() for g in patterns for p in glob.glob(os.path.join(TOP, g)))
    return sorted(set(re.findall(r'"([a-z0-9-]+-symbolic)"', text)))


FW = "05.06.10.01-HE_DTV_WT1H_AFAAABAA"


def slurp(path):
    with open(path, errors="replace") as f:
        return f.read()


def menu_actions(model):
    """The action names of a Gio.MenuModel's items, its sections' included."""
    names = []
    for i in range(model.get_n_items()):
        action = model.get_item_attribute_value(i, "action", None)
        if action is not None:
            names.append(action.get_string())
        section = model.get_item_link(i, "section")
        if section is not None:
            names += menu_actions(section)
    return names


def descendants(widget):
    """Every widget under widget (its children's children included)."""
    child = widget.get_first_child()
    while child is not None:
        yield child
        yield from descendants(child)
        child = child.get_next_sibling()


def dismiss_toasts(m):
    """Close the toasts of earlier operations (libadwaita 1.7), so a screenshot shows the dialog's."""
    if hasattr(m.win.toasts, "dismiss_all"):
        m.win.toasts.dismiss_all()


def shot(widget, path):
    """The widget as rendered, to PNG, through its own renderer."""
    # a widget waiting for its next layout (the Summary's tiles change every second) renders
    # nothing: let the frame clock lay it out first
    for _ in range(100):
        w, h = widget.get_width(), widget.get_height()
        snap = Gtk.Snapshot()
        Gtk.WidgetPaintable.new(widget).snapshot(snap, w, h)
        node = snap.to_node()
        if node is not None:
            break
        time.sleep(0.01)
        while GLib.MainContext.default().iteration(False):
            pass
    if node is None:
        raise AssertionError(f"nothing rendered for {path} ({w}x{h}, mapped={widget.get_mapped()})")
    widget.get_native().get_renderer().render_texture(node, None).save_to_png(path)
    print(f"screenshot {path} ({w}x{h})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", help="write PNG screenshots to PREFIX-<step>.png")
    a = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix="beanjar-gui-")
    lib_dir = os.path.join(tmp, "lib")
    repo = os.path.join(tmp, "repo")
    os.makedirs(os.path.join(repo, "build"))
    # the checkout's layers, as Library.layers() finds them: source-only, generated, not yet built
    for d in (
        "guest/layers/normal",
        "guest/layers/headless",
        "guest/layers/common",
        "guest/layers/soc/h13",
        "guest/layers/webos/afro",
        "guest/layers/gl",
        "guest/layers/devmode",
        "build/layers/h13-afro/gl",
        "build/layers/h13-afro/webos",
        "build/layers/h13-afro/soc",
        "build/layers/h13-afro/common",
    ):
        os.makedirs(os.path.join(repo, d))
    board = "guest/layers/soc/h13/rootfs/etc/emulator/board.cmdline"  # the model's hwopt: its RAM
    os.makedirs(os.path.dirname(os.path.join(repo, board)))
    shutil.copyfile(os.path.join(TOP, board), os.path.join(repo, board))
    for d in (
        "guest/layers/gl",
        "guest/layers/devmode",
        "guest/layers/common",
        "guest/layers/soc/h13",
        "guest/layers/webos/afro",
    ):
        open(os.path.join(repo, d, "meson.build"), "w").close()
    # a usable mitmweb (mitmtool.py), whatever this host has; the Summary's no-mitmweb case
    # points NETNS_MITMWEB elsewhere
    mitmweb = os.path.join(tmp, "mitmweb")
    with open(mitmweb, "w") as f:
        f.write("#!/bin/sh\necho 'Mitmproxy: 12.3.1'\n")
    os.chmod(mitmweb, 0o755)
    epk = os.path.join(tmp, "fw.epk")
    with open(epk, "wb") as f:
        f.write(b"EPK2" * 1000)
    os.environ.update(
        BEANJAR_LAUNCHER=os.path.join(TOP, "tests", "fakes", "fake-vm"),
        BEANJAR_VIEWER=os.path.join(TOP, "tests", "fakes", "fake-beanjar-viewer"),
        BEANJAR_EPK2EXTRACT=os.path.join(TOP, "tests", "fakes", "fake-epk2extract"),
        FAKE_UNIT_PREFIX=testunits.PREFIX,
        FAKE_BOOT_DELAY="1.5",
        PYTHONDONTWRITEBYTECODE="1",
        NETNS_MITMWEB=mitmweb,
    )
    lib = core.Library(lib_dir, repo)

    def cleanup():
        for vm in lib.vms():
            for pid in (vm.viewer_pid(), vm.pid()):
                if pid:
                    testunits.end(pid)
        testunits.stop_ours()
        shutil.rmtree(tmp, ignore_errors=True)

    testunits.install(cleanup)
    # what reads the VMs' units (max_running, VMs of other libraries) needs a user systemd
    user_systemd = unit.available()
    app = Adw.Application(application_id=None)
    result = {"rc": 1}
    m_holder = {}

    def check(cond, what):
        if not cond:
            raise AssertionError(what)
        print(f"ok: {what}")

    def state(name):
        return m.states.get(name, ("?",))[0]

    def item(name):
        r = row(name)
        return r.item if r else None

    def row(name):
        """VM name's row in the list (a widgets.VMRow)."""
        for i in range(m.store.get_n_items()):
            if m.store.get_item(i).name == name:
                return m.vm_list.get_row_at_index(i)
        return None

    # the TV picture's `beanjar screenshot` (a fake VM has no screen): a PNG per mode, from files
    shots = {"live": os.path.join(tmp, "live.png"), "last": os.path.join(tmp, "last.png")}
    shot_runs = []

    def fake_screenshot(name, words):
        shot_runs.append((name, tuple(words)))
        path = shots["last" if "--last" in words else "live"]
        return ["cat", path] if os.path.exists(path) else ["false"]

    def png(path, rgb):
        from gi.repository import GdkPixbuf

        p = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 64, 36)
        p.fill(rgb << 8 | 0xFF)
        p.savev(path, "png", [], [])

    def script():
        widgets = m.widgets
        m.pictures.command = fake_screenshot
        # 0. the empty library: a status page offering New VM
        check(m.win.stack.get_visible_child_name() == "empty", "empty library: the status page")
        check(m.details.get_visible_child_name() == "summary", "the details open on the Summary")
        check(
            m.win.empty_new.has_css_class("suggested-action")
            and "import" not in m.actions
            and not hasattr(m.win, "empty_import"),
            "New VM is its action; there is no Import Firmware",
        )
        check(
            not any(m.enabled(k) for k in ("start", "connect", "delete", "settings", "folder")),
            "no VM actions without a VM",
        )
        if a.shots:
            shot(m.win, f"{a.shots}-empty.png")
        theme = Gtk.IconTheme.get_for_display(m.win.get_display())
        check(
            theme.has_icon("org.webosbrew.beanjar")
            and Gtk.Window.get_default_icon_name() == "org.webosbrew.beanjar"
            and m.win.get_title() == "Beanjar",
            "the window: Beanjar, the application icon in the theme and the windows' default",
        )
        # the header: +, the title (the library's path in its tooltip), the icon-only state button,
        # ⋮ (Settings… first) and ☰ (no New VM); no Settings gear
        bar = m.win.state_button.get_ancestor(Adw.HeaderBar)
        header_icons = [
            w.get_icon_name()
            for w in descendants(bar)
            if isinstance(w, (Gtk.Button, Gtk.MenuButton)) and w.get_icon_name()
        ]
        check(
            "applications-system-symbolic" not in header_icons
            and not hasattr(m.win, "settings_button"),
            f"the header has no Settings gear ({header_icons})",
        )
        check(
            m.win.window_title.get_title() == "Beanjar"
            and not m.win.window_title.get_subtitle()
            and m.win.window_title.get_tooltip_text()
            in (m.lib.root, "~" + m.lib.root[len(os.path.expanduser("~")) :]),
            f"the title is Beanjar, the library's path its tooltip ({m.win.window_title.get_tooltip_text()})",
        )
        check(
            menu_actions(m.win.vm_menu_button.get_menu_model())[0] == "win.settings",
            "⋮ starts with Settings…",
        )
        main = menu_actions(m.win.main_menu_button.get_menu_model())
        check(
            "win.new" not in main and main == ["win.auto-reconnect", "win.about"],
            f"☰ has no New VM: the reconnect option and About ({main})",
        )
        app = m.win.get_application()

        def accels(names):
            return [tuple(Gtk.accelerator_parse(n)) for n in names]

        check(
            all(
                accels(app.get_accels_for_action(f"win.{k}")) == accels(v)
                for k, v in gui.ACCELS.items()
            )
            and accels(app.get_accels_for_action("win.new")) == accels(["<Control>n"])
            and accels(app.get_accels_for_action("win.settings")) == accels(["<Alt>Return"]),
            "every accelerator is set, Ctrl+N and Alt+Return included",
        )
        check(
            m.win.state_button.get_icon_name() == "media-playback-start-symbolic"
            and m.win.state_button.get_tooltip_text() == "Start"
            and not m.win.state_button.get_label(),
            "the state button shows an icon only, its tooltip naming the action (Start)",
        )
        used = icon_names(["host/ui/data/manager/*.blp", "host/ui/beanjar_ui/manager/*.py"])
        missing = [n for n in used if not theme.has_icon(n)]
        check(
            len(used) > 15 and not missing,
            f"every icon the manager names is in the theme ({len(used)} names; missing {missing})",
        )
        # the desktop's icon theme not installed (the Pi's 'PiXtrix'): Adwaita instead; one installed kept
        settings = Gtk.Settings.get_for_display(m.win.get_display())
        settings.set_property("gtk-icon-theme-name", "NoSuchTheme-beanjar")
        lost = [n for n in used if not theme.has_icon(n)]
        replaced = ui.theme_fallback(m.win.get_display())
        missing = [n for n in used if not theme.has_icon(n)]
        check(
            lost
            and replaced == "NoSuchTheme-beanjar"
            and theme.get_theme_name() == "Adwaita"
            and not missing,
            f"a theme that is not installed falls back to Adwaita, and the icons resolve ({len(lost)} lost before)",
        )
        settings.set_property("gtk-icon-theme-name", "hicolor")
        check(
            ui.theme_fallback(m.win.get_display()) is None and theme.get_theme_name() == "hicolor",
            "an installed theme (hicolor) is left alone",
        )
        settings.set_property("gtk-icon-theme-name", "Adwaita")
        m.about()
        about = m.dialog
        check(
            (about.get_application_name(), about.get_application_icon())
            == ("Beanjar", "org.webosbrew.beanjar"),
            "About: Beanjar, its icon",
        )
        if a.shots:
            yield 0.5
            shot(m.win, f"{a.shots}-about.png")
        about.close()
        yield 0.3

        # 1. New VM from a .epk: refused without one, a failure and a cancel leave nothing, then made
        m.new_vm()
        form = m.dialog
        yield 0.3
        f = form.fields
        check(
            m.fw_source(form) == ("epk", "")
            and f["epk"].get_visible()
            and not f["fwdir"].get_visible()
            and [k for _l, k, _v in form.sources] == ["epk", "dir"]
            and "copy" not in f,
            f"New VM offers a .epk first, then a directory; nothing else ({form.sources})",
        )
        check(
            not form.identity_group.get_visible(),
            "New VM has no MAC or serial to give: they are made",
        )
        f["name"].set_text("tv0")
        form.submit()
        check(
            not form.closed
            and form.error.get_title() == "General: choose a firmware file (.epk)"
            and form.warn_general.get_visible(),
            f"no .epk is refused on General ({form.error.get_title()})",
        )
        f["epk"].set_text(epk)
        check(not form.error.get_revealed(), "choosing one clears it")
        desc = form.fw_group.get_description()
        check(
            "Extracted into the VM's folder when it is created: about 4.0 KB, the size of the .epk"
            in desc
            and "free in the library" in desc,
            f"the firmware group says where it goes and what it costs ({desc})",
        )
        if a.shots:
            yield 0.4
            shot(m.win, f"{a.shots}-fw-epk.png")

        def scratch():
            return os.listdir(os.path.join(lib_dir, "vms"))

        os.environ["FAKE_EPK_FAIL"] = "1"
        form.submit()
        check(
            not form.closed
            and m.create_op is not None
            and not m.enabled("new")
            and m.win.busy.get_visible(),
            "a .epk is extracted in the background: New VM stays open, New disabled, the spinner shown",
        )
        yield lambda: m.create_op is None
        win = m.progress
        check(
            win.head.get_text() == "Failed: epk2extract failed (exit 1)"
            and win.close_button.get_sensitive()
            and form.error.get_title() == "General: epk2extract failed (exit 1)",
            f"a failed extraction is shown, and on General ({form.error.get_title()})",
        )
        check(scratch() == [], f"and leaves nothing ({scratch()})")
        del os.environ["FAKE_EPK_FAIL"]
        win.close()
        yield lambda: win.closed
        os.environ["FAKE_EPK_SLEEP"] = "30"
        form.submit()
        yield lambda: m.progress is not win
        win = m.progress
        buf = win.log.get_buffer()
        yield lambda: "sleeping" in buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        check(
            win.head.get_text() == "Extracting fw.epk for tv0"
            and not win.close_button.get_sensitive()
            and any(n.startswith(".new-tv0-") for n in scratch()),
            "extracting: its progress over New VM",
        )
        yield 0.4
        if a.shots:
            shot(m.win, f"{a.shots}-fw-extracting.png")
        win.cancel_button.emit("clicked")
        yield lambda: m.create_op is None
        check(
            win.head.get_text() == "Cancelled: nothing was created"
            and scratch() == []
            and not form.closed,
            f"Cancel ends it, nothing left, New VM still open ({scratch()})",
        )
        del os.environ["FAKE_EPK_SLEEP"]
        win.close()
        yield lambda: win.closed
        form.submit()
        yield lambda: form.closed and m.create_op is None
        check(
            m.messages[-1] == "Created tv0" and m.progress.closed,
            "made: both dialogs close, a toast says so",
        )
        tv0_fw = os.path.join(lib_dir, "vms", "tv0", "firmware")
        check(
            os.path.isfile(os.path.join(tv0_fw, "manifest.toml"))
            and "fw" not in lib.vm("tv0").config
            and not os.path.exists(os.path.join(lib_dir, "firmware")),
            "the firmware is in the VM's folder, its own; there is no firmware library",
        )
        check(
            "mem" not in lib.vm("tv0").config and lib.vm("tv0").mem() == 3072,
            "Memory left as it was: no mem, the VM has its model's RAM (the 49UB850V's 3 GB)",
        )
        check(m.win.stack.get_visible_child_name() == "vms", "the list replaced the status page")
        # a firmware collection of the host's (firmware_dirs) with tv0's firmware in it
        coll_fw = os.path.join(tmp, "collection", lib.vm("tv0").firmware_name())
        shutil.copytree(
            tv0_fw, coll_fw, copy_function=os.link, ignore=shutil.ignore_patterns("cache")
        )
        hostconfig.set_global_option("firmware_dirs", os.path.dirname(coll_fw))

        # 2. New VM dialog: its pages, the banner, the layers
        m.new_vm()
        form = m.dialog
        check(isinstance(form, Adw.Dialog), "New VM is an Adw.Dialog")
        yield 0.3
        check(
            not form.split.get_collapsed()
            and form.current_page() == "general"
            and form.cancel.get_visible()
            and not form.sidebar_cancel.get_visible(),
            "wide: split, on General, Cancel and Create over the page",
        )
        form.submit()
        check(
            not form.closed
            and form.error.get_revealed()
            and form.error.get_title().startswith("General: invalid VM name"),
            f"an empty name is refused on General ({form.error.get_title()})",
        )
        check(
            form.warn_general.get_visible() and not form.warn_layers.get_visible(),
            "General's sidebar row is marked",
        )
        for i, page in enumerate(widgets.PAGES):
            form.categories.emit("row-activated", form.categories.get_row_at_index(i))
            check(
                form.current_page() == page and form.page.get_title() == form.page_title(page),
                f"the sidebar shows {page}",
            )
        check(
            form.error.get_revealed() and form.error.get_button_label() == "Show General",
            "the banner stays on the other pages and leads back to General",
        )
        if a.shots:
            yield 0.4
            shot(m.win, f"{a.shots}-vmform-error-wide.png")
        form.error.emit("button-clicked")
        check(form.current_page() == "general", "its button shows General")
        form.fields["name"].set_text("tv1")
        check(
            not form.error.get_revealed() and not form.warn_general.get_visible(),
            "editing the name clears its error: no stale banner over a new name",
        )
        yield 0.3
        check(
            m.fw_source(form) == ("place", coll_fw)
            and gui.dropdown_value(form.fields["fw"]) == "WT1H 05.06.10.01"
            and form.fields["fw_import"].get_visible()
            and not form.fields["fw_import"].get_active()
            and form.fw_group.get_description().startswith("Used in place, never written"),
            f"the collection's firmware first, used in place ({form.fw_group.get_description()})",
        )
        form.fields["fw"].set_selected(2)
        check(
            gui.dropdown_value(form.fields["fw"]) == "Directory…"
            and form.fields["fwdir"].get_visible()
            and not form.fields["epk"].get_visible(),
            "a directory: its row shown, the .epk's hidden",
        )
        form.fields["fw_import"].set_active(True)
        form.fields["fwdir"].set_text(tv0_fw)
        check(m.fw_source(form) == ("dir", tv0_fw), m.fw_source(form))
        check(
            form.fw_group.get_description().startswith(
                "The VM's own firmware: hard links to the directory's paks, no more space"
            ),
            f"the directory's paks are linked into the VM's folder ({form.fw_group.get_description()})",
        )
        check(
            int(form.fields["mem"].get_value()) == 3072
            and "3072 MiB" in form.fields["mem"].get_subtitle(),
            f"Memory shows the directory's model's RAM ({form.fields['mem'].get_subtitle()})",
        )
        if a.shots:
            yield 0.4
            shot(m.win, f"{a.shots}-fw-dir.png")
        names = [r.name for r in form.layer_rows()]
        check(
            names == ["normal", "gl", "headless", "devmode"]
            and form.layers_value() == ["normal", "gl"],
            f"the optional layers, normal and gl on: {names}",
        )
        check("not built" in form.layer_row("devmode").get_subtitle(), "a layer not built says so")
        fixed = {r.name: r for r in form.fixed_rows}
        check(
            list(fixed) == list(targets.ALWAYS_LAYERS)
            and all(
                r.switch.get_active() and not r.switch.get_sensitive() and not r.order.get_visible()
                for r in fixed.values()
            ),
            "the always-used layers: on, fixed",
        )
        form.layer_row("headless").switch.set_active(True)
        form.move_layer(form.layer_row("headless"), -1)
        check(
            form.layers_value() == ["normal", "headless", "gl"], f"reordered: {form.layers_value()}"
        )
        yield 0.4
        if a.shots:
            shot(m.win, f"{a.shots}-new-vm.png")
            for page in widgets.PAGES:
                form.show_page(page)
                yield 0.4
                shot(m.win, f"{a.shots}-vmform-{page}-wide.png")
            form.show_page("general")
        # narrow: the split view collapses to the sidebar, then one page at a time
        m.win.set_default_size(480, 760)
        yield lambda: form.split.get_collapsed()
        form.split.set_show_content(False)
        check(
            form.sidebar_ok.get_visible()
            and form.sidebar_cancel.get_visible()
            and not form.cancel.get_visible(),
            "collapsed: Cancel and Create on the sidebar, Create on each page",
        )
        yield 0.4
        if a.shots:
            shot(m.win, f"{a.shots}-vmform-sidebar-narrow.png")
        for i, page in enumerate(widgets.PAGES):
            form.categories.emit("row-activated", form.categories.get_row_at_index(i))
            check(
                form.split.get_show_content() and form.current_page() == page,
                f"collapsed: {page} shown",
            )
            if a.shots:
                yield 0.4
                shot(m.win, f"{a.shots}-vmform-{page}-narrow.png")
            form.split.set_show_content(False)
        m.win.set_default_size(1100, 760)
        yield lambda: not form.split.get_collapsed()
        form.submit()
        yield lambda: form.closed
        print("ok: New VM dialog closed")
        check(
            m.messages[-1] == "Created tv1" and m.create_op is None, "made at once: a toast says so"
        )
        check(
            lib.vm("tv1").config["layers"] == ["normal", "headless", "gl"],
            "vm.toml has the layers in the list's order",
        )
        own_rootfs = os.path.join(lib_dir, "vms", "tv1", "firmware", "rootfs.pak")
        check(
            "fw" not in lib.vm("tv1").config
            and os.path.samefile(own_rootfs, os.path.join(tv0_fw, "rootfs.pak")),
            "its firmware is in its folder, hard links of the directory's",
        )
        tv1_row = row("tv1")
        lib.create("tv2", fw=tv0_fw, place=False, display="none")
        yield lambda: item("tv2") is not None
        check(
            m.selected() == "tv1" and row("tv1") is tv1_row, "the new VM is selected, its row kept"
        )
        check(
            isinstance(tv1_row, widgets.VMRow)
            and tv1_row.title.get_label() == "tv1"
            and tv1_row.specs.get_label()
            == f"webOS 1 · WT1H 05.06.10.01 · {gui.fmt_mem(3072)} · 4 CPUs"
            and tv1_row.specs.get_tooltip_text() == FW,
            f"the row: name, then platform, firmware, memory and CPUs ({tv1_row.specs.get_label()})",
        )
        check(
            item("tv1").get_property("state") == "Off"
            and tv1_row.state.get_label() == "Off"
            and tv1_row.dot.has_css_class("dimmed")
            and not tv1_row.activity.get_visible()
            and not tv1_row.viewer.get_visible(),
            "tv1 Off: a dimmed dot, no uptime, no viewer",
        )
        check(m.enabled("start"), "Start enabled")
        check(not m.enabled("connect"), "Connect disabled while off")

        # 2b. Settings: the VM's own copy of its firmware; its layers (an unknown one shown, not saved), an error on
        # Display & Audio
        m.select("tv1")
        yield 0.2
        m.settings()
        form = m.dialog
        yield 0.3
        check(
            m.fw_source(form) == ("own", "")
            and [k for _l, k, _v in form.sources] == ["own", "dir"]
            and gui.dropdown_value(form.fields["fw"]) == "In the VM's Folder: WT1H 05.06.10.01"
            and form.fields["fw"].get_subtitle() == FW
            and not form.fields["fwdir"].get_visible(),
            f"Settings shows tv1's own firmware, or a directory; no .epk ({form.sources})",
        )
        check(
            ", in the VM's folder. " in form.fw_group.get_description(),
            form.fw_group.get_description(),
        )
        form.fields["fw"].set_selected(1)
        form.fields["fwdir"].set_text(tv0_fw)
        check(
            gui.dropdown_value(form.fields["fw"]) == "Directory in Place…"
            and form.fw_group.get_description().startswith("Used in place, never written: "),
            f"Settings: a directory is used in place ({form.fw_group.get_description()})",
        )
        if a.shots:
            yield 0.4
            shot(m.win, f"{a.shots}-fw-settings.png")
        form.close()
        yield lambda: form.closed
        cfg = lib.vm("tv2").config
        lib.vm("tv2").save_config(dict(cfg, layers=["rootssh", "mystery", "normal", "soc"]))
        m.select("tv2")
        yield 0.3
        m.settings()
        form = m.dialog
        yield 0.3
        rows = form.layer_rows()
        check(
            [(r.name, r.active) for r in rows[:5]]
            == [
                ("rootssh", True),
                ("mystery", True),
                ("normal", True),
                ("soc", True),
                ("gl", False),
            ],
            "Settings lists the VM's layers first, in its order, unknown ones too",
        )
        check(
            "Not in this checkout" in form.layer_row("mystery").get_subtitle()
            and "always used" in form.layer_row("soc").get_subtitle(),
            "and says what they are",
        )
        check(not form.name_group.get_visible(), "Settings does not rename")
        tv2 = lib.vm("tv2").config
        check(
            form.identity_group.get_visible()
            and form.fields["mac"].get_text() == tv2["mac"]
            and form.fields["serial"].get_text() == tv2["serial"],
            "Settings shows the TV's MAC and serial",
        )
        form.fields["mac"].set_text("01:00:5e:00:00:01")
        form.submit()
        check(
            not form.closed
            and form.error.get_title().startswith("Network: mac")
            and form.current_page() == "network"
            and form.warn_network.get_visible(),
            f"a multicast MAC is refused on Network ({form.error.get_title()})",
        )
        form.fields["mac"].set_text("02:00:00:ab:cd:ef")
        check(not form.error.get_revealed(), "editing the MAC clears the error")
        form.fields["audio"].set_selected(vmsettings.AUDIOS.index("dbus"))
        form.submit()
        check(
            not form.closed
            and form.error.get_title().startswith("Display & Audio: audio=dbus")
            and form.current_page() == "display"
            and form.warn_display.get_visible(),
            f"a display error points to Display & Audio ({form.error.get_title()})",
        )
        form.show_page("layers")
        form.layer_row("mystery").switch.set_active(False)
        check(form.error.get_revealed(), "a change on another page leaves the error")
        form.layer_row("mystery").switch.set_active(True)
        form.move_layer(form.layer_row("normal"), -1)
        form.move_layer(form.layer_row("normal"), -1)
        form.show_page("display")
        form.fields["audio"].set_selected(vmsettings.AUDIOS.index("none"))
        check(not form.error.get_revealed(), "fixing it clears the error")
        form.submit()
        check(
            not form.closed
            and form.error.get_title().startswith("Layers: unknown layer mystery")
            and form.current_page() == "layers"
            and form.warn_layers.get_visible(),
            f"a layer without a layer.toml is shown but not saved ({form.error.get_title()})",
        )
        form.layer_row("mystery").switch.set_active(False)
        form.submit()
        yield lambda: form.closed
        check(
            lib.vm("tv2").config["layers"] == ["normal", "rootssh", "soc"],
            f"Save took the new order without the unknown layer ({lib.vm('tv2').config['layers']})",
        )
        check(
            lib.vm("tv2").config["mac"] == "02:00:00:ab:cd:ef"
            and lib.vm("tv2").config["serial"] == tv2["serial"],
            "Save kept the new MAC and the serial",
        )

        # 2b-prep. a settings change that has the board data prepared: in the background, with
        # its progress over Settings; the main loop keeps running; an error is shown in Settings
        ticks = []
        GLib.timeout_add(50, lambda: ticks.append(time.monotonic()) or True)
        calls = os.path.join(lib.vm("tv2").dir, "prepared.calls")
        before_calls = slurp(calls).count("\n") if os.path.exists(calls) else 0
        os.environ["FAKE_PREPARE_DELAY"] = "2"
        m.settings()
        form = m.dialog
        yield 0.3
        form.show_page("network")
        form.fields["serial"].set_text("SER0000199")
        t0 = time.monotonic()
        form.submit()
        used = time.monotonic() - t0
        check(used < 1.0, f"Save returns at once, not after the preparation ({used:.2f} s)")
        yield 0.5
        prog = m.progress
        check(
            m.prepare_op is not None
            and prog.head.get_text() == "Preparing board data…"
            and prog.spinner.get_visible()
            and not prog.cancel_button.get_visible()
            and not form.closed,
            "the progress is shown over Settings",
        )
        if a.shots:
            shot(m.win, f"{a.shots}-settings-preparing.png")
        prog.close()  # a close attempt: it waits for the work
        yield 0.3
        check(m.prepare_op is not None and not prog.closed, "closing it half-way does not close it")
        yield lambda: form.closed
        check(
            m.prepare_op is None
            and prog.closed
            and lib.vm("tv2").config["serial"] == "SER0000199"
            and slurp(calls).count("\n") == before_calls + 1
            and slurp(calls).splitlines()[-1] == "ensure SER0000199",
            "done: both closed, the settings saved and the data prepared once",
        )
        spans = [b - a_ for a_, b in zip(ticks, ticks[1:])]
        check(
            len(ticks) > 20 and max(spans) < 0.5,
            f"the main loop kept running while it prepared ({len(ticks)} ticks, longest gap {max(spans):.2f} s)",
        )
        os.environ["FAKE_PREPARE_DELAY"] = "0"
        open(os.path.join(lib.vm("tv2").dir, "fail-prepare"), "w").close()
        m.settings()
        form = m.dialog
        yield 0.3
        form.show_page("network")
        form.fields["serial"].set_text("SER0000200")
        form.submit()
        yield lambda: m.prepare_op is None
        check(
            not form.closed
            and form.error.get_revealed()
            and "board data could not be prepared" in form.error.get_title()
            and m.progress.head.get_text().startswith("Failed:")
            and lib.vm("tv2").config["serial"] == "SER0000200",
            f"an error is shown in Settings ({form.error.get_title()})",
        )
        m.progress.close()
        yield lambda: m.progress.closed
        form.close()
        yield lambda: form.closed
        os.unlink(os.path.join(lib.vm("tv2").dir, "fail-prepare"))

        # 2b'. link and mode (Network): listen with nat, the LAN address with lan, mode unused with off
        m.settings()
        form = m.dialog
        yield 0.3
        form.show_page("network")
        check(
            form.listen_group.get_visible()
            and not form.address_group.get_visible()
            and form.mode.get_sensitive(),
            "nat: the listen address, no LAN address",
        )
        form.fields["link"].set_selected(vmsettings.LINKS.index("lan"))
        check(
            not form.listen_group.get_visible() and form.address_group.get_visible(),
            "lan: the LAN address, no listen",
        )
        form.fields["address"].set_text("10.0.0.5")
        form.submit()
        check(
            not form.closed
            and form.error.get_title().startswith("Network: address")
            and form.current_page() == "network",
            f"a LAN address without its prefix is refused ({form.error.get_title()})",
        )
        form.fields["address"].set_text("10.0.0.5/24,10.0.0.1")
        form.fields["mode"].set_selected(vmsettings.MODES.index("direct"))
        form.submit()
        yield lambda: form.closed
        cfg = lib.vm("tv2").config
        check(
            (cfg["link"], cfg["mode"], cfg.get("address"), cfg.get("listen"))
            == ("lan", "direct", "10.0.0.5/24,10.0.0.1", None),
            f"Save took link lan, mode direct and the address ({cfg})",
        )
        yield 0.3
        check(
            m.summary_view.fields["network"].get_text() == "LAN · Direct",
            "the summary shows LAN · Direct",
        )
        m.settings()
        form = m.dialog
        yield 0.3
        form.fields["link"].set_selected(vmsettings.LINKS.index("off"))
        check(
            not form.mode.get_sensitive() and not form.address_group.get_visible(),
            "off: no mode, no address",
        )
        form.fields["link"].set_selected(vmsettings.LINKS.index("nat"))
        form.fields["mode"].set_selected(vmsettings.MODES.index("mitm"))
        form.submit()
        yield lambda: form.closed
        cfg = lib.vm("tv2").config
        check(
            (cfg["link"], cfg["mode"], "address" in cfg) == ("nat", "mitm", False),
            f"back to nat: the address cleared ({cfg})",
        )

        # 2b''. the inspector's limits (Network, mode mitm only): a bad size refused, then saved and shown
        m.settings()
        form = m.dialog
        yield 0.3
        form.show_page("network")
        check(
            form.inspector_group.get_visible()
            and form.fields["mitm_flows"].get_value() == vmsettings.MITM_FLOWS
            and form.fields["mitm_flow_bytes"].get_text() == "",
            "mitm: the inspector's limits, the defaults",
        )
        form.fields["mode"].set_selected(vmsettings.MODES.index("direct"))
        check(not form.inspector_group.get_visible(), "direct: no inspector limits")
        form.fields["mode"].set_selected(vmsettings.MODES.index("mitm"))
        form.fields["mitm_flows"].set_value(200)
        form.fields["mitm_flow_bytes"].set_text("lots")
        form.submit()
        check(
            not form.closed
            and form.error.get_title().startswith("Network: mitm_flow_bytes")
            and form.current_page() == "network",
            f"a bad body size is refused ({form.error.get_title()})",
        )
        form.fields["mitm_flow_bytes"].set_text("200m")
        form.submit()
        yield lambda: form.closed
        cfg = lib.vm("tv2").config
        check(
            (cfg.get("mitm_flows"), cfg.get("mitm_flow_bytes")) == (200, "200M"),
            f"Save took the limits ({cfg})",
        )
        yield 0.3
        check(
            m.summary_view.fields["mitm_flows"].get_text() == "the last 200 flows"
            and m.summary_view.fields["mitm_flow_bytes"].get_text() == "200 MiB",
            "the summary shows the inspector's limits",
        )
        m.settings()
        form = m.dialog
        yield 0.3
        form.fields["mitm_flows"].set_value(vmsettings.MITM_FLOWS)
        form.fields["mitm_flow_bytes"].set_text("")
        form.submit()
        yield lambda: form.closed
        cfg = lib.vm("tv2").config
        check(
            "mitm_flows" not in cfg and "mitm_flow_bytes" not in cfg,
            f"the defaults again, not written ({cfg})",
        )

        # 2c. the model key, in Settings (Network): never its value; installed and cleared at Save, a bad
        # file refused before anything is saved
        from beanjar import sestore

        key_path = os.path.join(lib_dir, "vms", "tv2", "identity", "sestore.toml")
        good = os.path.join(tmp, "model.key")
        with open(good, "w") as fh:
            fh.write(base64.b64encode(os.urandom(32)).decode())
        bad = os.path.join(tmp, "bad.key")
        with open(bad, "w") as fh:
            fh.write("not a key")
        m.settings()
        form = m.dialog
        yield 0.3
        check(
            form.model_key_group.get_visible()
            and form.model_key.get_subtitle() == "None: the built-in model secret"
            and not form.model_key_clear.get_sensitive(),
            "Settings: no model key, nothing to clear",
        )
        form.fields["mem"].set_value(1792)
        form.pick_model_key(bad)
        form.submit()
        check(
            not form.closed
            and form.error.get_title().startswith("Network: " + bad)
            and form.current_page() == "network"
            and not os.path.exists(key_path)
            and lib.vm("tv2").config.get("mem") != 1792,
            f"a bad key file is refused on Network, nothing saved ({form.error.get_title()})",
        )
        form.pick_model_key(good)
        check(
            not form.error.get_revealed()
            and form.model_key.get_subtitle() == f"From {good}, at Save"
            and form.model_key_clear.get_sensitive(),
            "another file clears the error; Clear takes it back",
        )
        form.submit()
        yield lambda: form.closed
        check(
            os.path.isfile(key_path)
            and os.stat(key_path).st_mode & 0o777 == 0o600
            and sestore.load(os.path.dirname(key_path))[1][sestore.MODEL]
            == core.read_model_key(good)
            and lib.vm("tv2").config["mem"] == 1792
            and m.messages[-1] == "tv2: settings saved, model key installed",
            "Save installed it (0600), as model-key does",
        )
        yield 0.3
        check(
            "Model key: Installed (value not shown)" in m.summary_text(),
            "the summary says there is one",
        )
        check(
            m.summary_view.fields["model_key"].get_text() == "Custom installed",
            "the visible identity card reports the installed model key",
        )
        m.settings()
        form = m.dialog
        yield 0.3
        form.show_page("network")
        check(
            form.model_key.get_subtitle() == "Installed (value not shown)"
            and form.model_key_clear.get_sensitive()
            and core.read_model_key(good).decode() not in form.model_key.get_subtitle(),
            "Settings: installed, the value not shown",
        )
        if a.shots:
            yield 0.4
            shot(m.win, f"{a.shots}-settings-model-key.png")
        form.model_key_clear.emit("clicked")
        check(
            form.model_key.get_subtitle() == "Cleared at Save: the built-in model secret"
            and os.path.isfile(key_path),
            "Clear: at Save",
        )
        form.submit()
        yield lambda: form.closed
        check(
            not lib.vm("tv2").has_model_key()
            and m.messages[-1] == "tv2: settings saved, model key cleared",
            "Save cleared it",
        )

        # 2c'. the Summary cards' Edit opens Settings at their page
        for edit, page in zip(m.summary_view.edits, ("hardware", "network", "network")):
            check(edit.get_sensitive(), f"Edit ({page}) is sensitive while the VM is off")
            edit.emit("clicked")
            yield 0.3
            form = m.dialog
            check(
                isinstance(form, m.widgets.VMForm) and form.current_page() == page,
                f"Edit opens Settings at {page}",
            )
            form.close()
            yield lambda form=form: form.closed

        # 2d. narrow: the list overlays the details; picking a VM there hides it again
        m.win.set_default_size(480, 760)
        yield lambda: m.win.split.get_collapsed()
        check(
            m.win.sidebar_button.get_visible() and m.vm_list.get_activate_on_single_click(),
            "narrow: the list collapses behind a header toggle, one click picks",
        )
        check(
            menu_actions(m.win.vm_menu_button.get_menu_model())[0] == "win.settings"
            and m.win.new_button.get_visible(),
            "narrow: ⋮ still starts with Settings…; + stays (☰ has no New VM)",
        )
        m.win.sidebar_button.set_active(True)
        yield 0.4
        check(m.win.split.get_show_sidebar(), "the toggle shows the list")
        if a.shots:
            shot(m.win, f"{a.shots}-list-narrow.png")
        m.vm_list.emit("row-activated", row("tv1"))
        check(
            m.selected() == "tv1"
            and not m.win.split.get_show_sidebar()
            and state("tv1") == base.OFF,
            "a row picked there is selected and the list goes; nothing starts",
        )
        m.win.set_default_size(1100, 760)
        yield lambda: not m.win.split.get_collapsed()
        yield 0.3

        # 3. start by activating the row: Starting, then Running; others may start beside it, as
        # free memory allows
        m.vm_list.emit("row-activated", tv1_row)
        check(not m.enabled("start") and "tv1" in m.ops, "activating an Off VM's row starts it")
        check(
            m.win.state_button.get_icon_name() == "media-playback-start-symbolic"
            and m.win.state_button.get_tooltip_text() == "Starting…"
            and not m.win.state_button.get_sensitive()
            and m.win.busy.get_visible(),
            "while the start runs the state button keeps its icon, insensitive, its tooltip "
            "Starting…; the header's spinner shows",
        )
        yield lambda: state("tv1") == base.STARTING and "tv1" not in m.ops
        check(
            tv1_row.dot.has_css_class("warning") and item("tv1").phase == base.STARTING,
            "Starting: an amber dot",
        )
        yield lambda: state("tv1") == base.RUNNING
        check(
            row("tv1") is tv1_row
            and m.selected() == "tv1"
            and tv1_row.dot.has_css_class("success")
            and tv1_row.state.get_label() == "Running",
            "Running: a green dot, in the same row, still selected",
        )
        check(m.enabled("shutdown"), "Shut Down enabled")
        check(
            not m.enabled("wipe") and not m.enabled("delete") and not m.enabled("new-identity"),
            "Wipe/New Identity/Delete disabled",
        )
        check(
            m.win.state_button.get_action_name() == "win.connect"
            and m.win.state_button.get_icon_name() == "video-display-symbolic"
            and m.win.state_button.get_tooltip_text() == "Connect"
            and m.win.state_button.has_css_class("suggested-action")
            and m.win.state_button.get_menu_model() is m.win.running_menu
            and not m.enabled("settings")
            and all(
                e.get_tooltip_text() == "Shut down to change settings" for e in m.summary_view.edits
            )
            and not any(e.get_sensitive() for e in m.summary_view.edits),
            "running: the state button connects, its tooltip Connect (Shut Down… and Unplug… in "
            "its dropdown); Settings… (⋮) and the cards' Edit insensitive",
        )
        check(
            f"Address: {lib.vm('tv1').address()}" in m.summary_text()
            and lib.vm("tv1").address().startswith("127.0.1."),
            "the summary has its address",
        )
        # the tiles while it runs: what its row shows each second, and QEMU's memory on the host
        view = m.summary_view
        tiles = view.tiles
        yield lambda: tiles["memory"].value.get_text().endswith("iB")
        check(
            [t.title.get_text() for t in tiles.values()] == ["State", "Uptime", "CPU", "Memory"]
            and tiles["state"].value.get_text() == "Running"
            and tiles["state"].dot.has_css_class("success")
            and ":" in tiles["time"].value.get_text()
            and tiles["memory"].caption.get_text().startswith("on host")
            and not view.problem,
            "running: State, Uptime, CPU and Memory on the host; no banner "
            f"({[t.value.get_text() for t in tiles.values()]})",
        )
        # the Network card: the address with its forwarded ports, the inspector's limits
        address = lib.vm("tv1").address()
        ports = " ".join(str(h) for h in sorted({h for h, _g, _n in lib.vm("tv1").forwards()}))

        def shown(key):
            row = view.rows[key]
            return row.get_visible() and row.get_parent().get_visible()

        check(
            ports
            and view.fields["address"].get_text() == f"{address} · ports {ports}"
            and view.values["address"] == address
            and shown("address")
            and not shown("lan_address")
            and view.fields["mitm_flows"].get_text() == f"the last {vmsettings.MITM_FLOWS} flows"
            and view.fields["mitm_flow_bytes"].get_text() == "no limit"
            and shown("mitm_flows"),
            f"Network: the host address with its ports, copied without them, and the inspector's "
            f"limits ({view.fields['address'].get_text()})",
        )
        check(
            not shown("free_page_reporting") and view.fields["gpu"].get_text() in ("On", "Off"),
            "Hardware: GPU on or off, free page reporting only when on",
        )
        # the picture: live while it runs, the Summary shown and the window active, every 5 s
        picture = view.picture
        png(shots["live"], 0x2060C0)
        m.details.set_visible_child_name("summary")  # the start showed its output
        if not m.win.is_active():
            print("note: the window is not active under this X server: the picture waits")
        yield lambda: picture.kind == "live" or not m.win.is_active()
        if m.win.is_active():
            check(
                picture.picture.get_paintable() is not None
                and not picture.badge.get_visible()
                and picture.overlay.get_tooltip_text() == "Connect"
                and ("tv1", ("--frame", "384x216")) in shot_runs,
                "running: the picture is live (beanjar screenshot --frame 384x216), a click connects",
            )
            m.details.set_visible_child_name("console")
            n = len(shot_runs)
            yield 6
            check(len(shot_runs) == n, "no picture taken while the Summary is not shown")
            m.details.set_visible_child_name("summary")
            yield lambda: len(shot_runs) > n
            n = len(shot_runs)
            yield 2
            check(len(shot_runs) == n, "at most one picture in 5 s")
        # QEMU paused: a banner, without a button, and the State tile says it
        st = m.states["tv1"]
        m.states["tv1"] = (st[0], "paused", st[2], st[3])
        m.update_details()
        check(
            m.win.summary_banner.get_revealed()
            and m.win.summary_banner.get_title() == "QEMU is paused"
            and not m.win.summary_banner.get_button_label()
            and tiles["state"].caption.get_text() == "Paused",
            "QEMU paused: the banner says so, the State tile too",
        )
        m.refresh(qmp=True)
        check(not m.win.summary_banner.get_revealed(), "and the banner goes with it")
        meminfo = os.path.join(lib_dir, "meminfo")  # the host's free memory, as the test says

        def available(mib):
            with open(meminfo, "w") as f:
                f.write(f"MemTotal:  67108864 kB\nMemAvailable:  {mib * 1024} kB\n")

        os.environ["BEANJAR_MEMINFO"] = meminfo
        m.select("tv2")
        yield 0.1
        # off: State, Last run, CPUs and Memory as set; its last picture, or none yet
        check(
            [t.title.get_text() for t in tiles.values()] == ["State", "Last run", "CPUs", "Memory"]
            and tiles["state"].value.get_text() == "Off"
            and tiles["state"].dot.has_css_class("dimmed")
            and tiles["time"].value.get_text() == "Never"
            and tiles["cpu"].value.get_text() == str(lib.vm("tv2").config.get("smp"))
            and tiles["memory"].value.get_text() == "1792 MiB",
            f"off: State, Last run, CPUs, Memory ({[t.value.get_text() for t in tiles.values()]})",
        )
        yield lambda: ("tv2", ("--last",)) in shot_runs and not m.pictures.entry("tv2").pending
        check(
            picture.kind == "none"
            and picture.badge.get_text() == "No picture yet"
            and picture.outline.get_visible()
            and picture.overlay.get_tooltip_text() == "Start",
            "off without a picture kept: the TV outline, No picture yet; a click starts",
        )
        png(shots["last"], 0x30A050)
        m.pictures.forget("tv2")
        m.update_picture()
        yield lambda: picture.kind == "last"
        check(
            picture.badge.get_text() == "Last picture"
            and picture.overlay.has_css_class("dim")
            and picture.picture.get_paintable() is not None,
            "off: the last picture kept, dimmed, Last picture",
        )
        if a.shots:
            yield 0.5
            shot(m.win, f"{a.shots}-summary-off.png")
        # the Inspector card while off: one line
        check(
            m.inspector.status.get_text() == "Starts with the VM (MITM)"
            and not m.inspector.details.get_visible(),
            "off: the inspector card is one line",
        )
        # a start that fails: the banner says so until the next one succeeds
        m.run("tv2", "start", ["start", "tv2", "--no-such-option"])
        yield lambda: "tv2" not in m.ops
        banner = m.win.summary_banner
        check(
            banner.get_revealed()
            and banner.get_title() == "Start failed (exit 2)"
            and banner.get_button_label() == "Show Output",
            f"a failed start: the banner, Start failed (exit 2), Show Output ({banner.get_title()})",
        )
        m.details.set_visible_child_name("summary")
        if a.shots:
            yield 0.5
            shot(m.win, f"{a.shots}-summary-failed.png")
        next(w for w in descendants(banner) if isinstance(w, Gtk.Button)).emit("clicked")
        yield 0.1
        check(m.details.get_visible_child_name() == "output", "Show Output shows the output")
        m.details.set_visible_child_name("summary")
        m.select("tv1")
        yield 0.1
        check(not banner.get_revealed(), "another VM's failure is not this one's")
        m.select("tv2")
        yield 0.1
        check(banner.get_revealed(), "and it stays with its VM")
        m.run("tv2", "start", ["list"])  # stands in for a start that succeeds (exit 0)
        yield lambda: "tv2" not in m.ops
        m.details.set_visible_child_name("summary")
        check(not banner.get_revealed(), "the next success clears it")
        # its firmware gone: the banner offers Settings…
        fw_dir = lib.vm("tv2").firmware_dir()
        os.rename(fw_dir, fw_dir + ".away")
        try:
            m.refresh()
            check(
                view.problem is not None
                and view.problem.kind == "firmware"
                and banner.get_button_label() == "Settings…"
                and banner.get_action_name() == "win.settings",
                f"the firmware missing: the banner, Settings… ({banner.get_title()})",
            )
        finally:
            os.rename(fw_dir + ".away", fw_dir)
        m.refresh()
        check(not banner.get_revealed(), "the firmware back: no banner")
        available(32768)
        yield lambda: m.enabled("start")  # the admission is taken off the main loop (_poll)
        check(m.enabled("start"), "Start enabled on tv2 while tv1 runs")
        available(1024)
        yield lambda: not m.enabled("start")
        check(
            not m.enabled("start")
            and "not enough free memory" in (m.win.state_button.get_tooltip_text() or ""),
            "Start disabled without the memory for tv2, and says why",
        )
        banner = m.win.summary_banner
        check(
            banner.get_revealed()
            and m.summary_view.problem.kind == "blocked"
            and banner.get_title().startswith("Not enough free memory to start VM tv2")
            and "(stop another VM" not in banner.get_title()
            and banner.get_tooltip_text() == m.win.state_button.get_tooltip_text()
            and not banner.get_button_label(),
            f"the banner says it, the first sentence, the full text its tooltip ({banner.get_title()})",
        )
        available(32768)
        yield lambda: m.enabled("start")
        check(m.enabled("start"), "and enabled again with it")
        del os.environ["BEANJAR_MEMINFO"]
        # the machine's max_running (the test's own XDG_CONFIG_HOME: testunits.isolate), tv1 running;
        # the running VMs are counted by their units, so it needs a user systemd
        if user_systemd:
            hostconfig.set_global_option("max_running", "1")
            yield lambda: not m.enabled("start")
            tip = m.win.state_button.get_tooltip_text() or ""
            check(
                not m.enabled("start")
                and "cannot start VM tv2: 1 VM already running and the limit is 1 (max_running): tv1 of"
                in tip
                and "beanjar host config set max_running=2" in tip,
                f"Start disabled at max_running, and says why ({tip})",
            )
            check(
                banner.get_revealed()
                and banner.get_title().startswith("Cannot start VM tv2: 1 VM already running")
                and "raise the limit" not in banner.get_title()
                and banner.get_tooltip_text() == tip,
                f"the banner names max_running ({banner.get_title()})",
            )
            hostconfig.set_global_option("max_running", "")
            yield lambda: m.enabled("start")
            check(
                m.enabled("start") and not banner.get_revealed(),
                "and enabled again without the limit, the banner gone",
            )
        else:
            print("skip: no user systemd: max_running")
        m.select("tv1")
        yield 0.1

        # 4. viewer (activating the row again), uptime and CPU%, console tail
        check(m.enabled("connect"), "Connect enabled")
        m.vm_list.emit("row-activated", tv1_row)
        vm = lib.vm("tv1")
        yield lambda: vm.viewer_pid() is not None
        print("ok: activating a running VM's row connects")
        yield lambda: item("tv1").viewer and "% CPU" in item("tv1").activity
        check(
            tv1_row.viewer.get_visible() and tv1_row.activity.get_visible(),
            f"the row shows the viewer, uptime and CPU ({tv1_row.activity.get_label()})",
        )
        check(
            not m.enabled("connect") and m.enabled("disconnect"),
            "Disconnect enabled, Connect disabled",
        )
        m.update_details()
        n = len(shot_runs)
        yield 6
        check(
            tiles["state"].caption.get_text() == "Viewer open"
            and picture.overlay.get_tooltip_text() == "Show the Viewer"
            and not [r for r in shot_runs[n:] if "--frame" in r[1]],
            "the viewer open: State says so, the picture shows it, no picture taken meanwhile",
        )
        check(
            m.win.state_button.get_icon_name() == "system-shutdown-symbolic"
            and m.win.state_button.get_tooltip_text() == "Shut Down…"
            and m.win.state_button.get_action_name() == "win.shutdown"
            and not m.win.state_button.has_css_class("suggested-action")
            and m.win.state_button.get_menu_model() is m.win.viewer_menu,
            "with the viewer open the state button shuts down (Disconnect and Unplug… in its "
            "dropdown)",
        )
        buf = m.console.get_buffer()
        check(
            "debug shell" in buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False),
            "console tail shown",
        )

        def console_text():
            return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)

        # what the guest writes is appended, the tail kept to TAIL_BYTES in whole lines
        with open(vm.console_log, "a") as f:
            f.write("appended line one\r\nappended line two\n")
        yield lambda: "appended line two" in console_text()
        check(
            "debug shell" in console_text() and "\r" not in console_text(),
            "console lines written later are appended to the tail",
        )
        with open(vm.console_log, "a") as f:
            for n in range(gui.TAIL_BYTES // 40):
                f.write(f"console filler line {n:06d} ..............\n")
            f.write("last of the filler\n")
        yield lambda: "last of the filler" in console_text()
        text = console_text()
        check(
            len(text) <= gui.TAIL_BYTES
            and text.startswith("console filler line ")
            and "debug shell" not in text,
            f"the tail keeps its last {gui.TAIL_BYTES} bytes in whole lines ({len(text)} chars)",
        )
        # a log made anew (the VM started again) replaces the tail; it is at its debug shell again,
        # or the next whole refresh would read the VM as Starting
        new_log = "a new console log\nStarting the initramfs debug shell\n"
        os.unlink(vm.console_log)
        with open(vm.console_log, "w") as f:
            f.write(new_log)
        yield lambda: console_text() == new_log
        check(True, "a console log made anew replaces the tail")
        m.details.set_visible_child_name("summary")
        yield lambda: "State: Running" in m.summary_text() and "viewer pid" in m.summary_text()
        check(
            "State: Running" in m.summary_text()
            and "viewer pid" in m.summary_text()
            and f"{FW} (in the VM's folder)" in m.summary_text()
            and "Platform: webOS 1" in m.summary_text()
            and "GPU: " in m.summary_text()
            and "Layers: normal, headless, gl" in m.summary_text(),
            f"the summary rows: state, pids, platform, firmware, GPU and layers ({m.summary_text()!r})",
        )
        check(
            not m.summary_view.runtime.get_expanded()
            and str(vm.pid()) not in m.summary_view.state.get_text(),
            "process IDs are in folded runtime details rather than the status banner",
        )
        check(
            m.summary_view.fields["qemu_pid"].get_text() == str(vm.pid())
            and m.summary_view.fields["viewer_pid"].get_text() == str(vm.viewer_pid()),
            "runtime details retain the actual VM and viewer process IDs",
        )
        copied = {}

        def clipboard_done(clipboard, result):
            copied["text"] = clipboard.read_text_finish(result)

        m.summary_view.copy("mac", "MAC")
        m.summary_view.get_clipboard().read_text_async(None, clipboard_done)
        yield lambda: "text" in copied
        check(
            copied["text"] == vm.config["mac"], "identity copy uses the selected VM's current MAC"
        )
        m.open_vm_folder()
        check(m.messages[-1] == f"VM folder: {vm.dir}", "VM folder action targets the selected VM")
        if a.shots:
            shot(m.win, f"{a.shots}-running.png")
        yield 0.3
        # the traffic inspector: a group of the selected VM's summary, its own mitmweb (mode mitm only)
        parent = m.inspector.get_parent()
        while parent is not None and parent is not m.summary:
            parent = parent.get_parent()
        check(
            parent is m.summary
            and m.inspector.get_visible()
            and "shared by all of its VMs" in m.inspector.description.get_text()
            and not m.inspector.lan_row.get_visible(),
            "the inspector is a group of the VM's summary; it says the CA is the library's",
        )
        check(
            not m.inspector.open.get_sensitive() and not m.inspector.copy.get_sensitive(),
            "fake VM without a proxy cannot open an unrelated inspector",
        )
        from beanjar_ui.manager import inspector_ui

        inspector_state = m.inspector.state
        m.select("tv2")
        m.refresh()
        check(
            m.inspector.get_visible()
            and m.inspector.status.get_text() == inspector_ui.OFF_TEXT
            and not m.inspector.address.get_visible()
            and not m.inspector.copy.get_visible()
            and not m.inspector.open.get_visible()
            and not m.inspector.details.get_visible()
            and m.inspector.password.get_text() == "—",
            "a stopped VM's inspector is one line, Starts with the VM (MITM): no stale URL",
        )
        # without a usable mitmweb it says the inspector will be off, why and what to install
        os.environ["NETNS_MITMWEB"] = os.path.join(tmp, "no-mitmweb")
        m.inspector.next_check = 0
        m.refresh()
        yield lambda: m.inspector.problem.get_visible()
        check(
            m.inspector.status.get_text() == "Off: mitmproxy not installed"
            and "pipx install mitmproxy" in m.inspector.problem.get_text()
            and m.inspector.status.get_tooltip_text() == m.inspector.problem.get_text()
            and not m.inspector.details.get_visible(),
            "no mitmweb: Off: mitmproxy not installed, and the install command below it",
        )
        if a.shots:
            yield 0.5
            shot(m.inspector, f"{a.shots}-inspector-off.png")
        os.environ["NETNS_MITMWEB"] = mitmweb
        m.inspector.next_check = 0
        m.refresh()
        yield lambda: not m.inspector.problem.get_visible()
        check(
            m.inspector.status.get_text() == inspector_ui.OFF_TEXT,
            "a usable mitmweb again: Starts with the VM (MITM)",
        )
        lib.configure("tv2", mode="direct")
        m.refresh()
        check(not m.inspector.get_visible(), "a VM with mode direct has none")
        lib.configure("tv2", mode="mitm")
        m.select("tv1")
        m.refresh()
        # a running inspector with its password (inspector.snapshot reads it from netns.py's file)
        from beanjar.network import inspector

        locked = inspector.State(
            "mitm",
            "Running",
            "http://127.0.1.1:18083/",
            True,
            password="pw/+ x",
            password_file="/run/vm/mitmweb.password",
        )
        m.inspector.show_state(locked)
        check(
            m.inspector.password.get_text() == "pw/+ x"
            and m.inspector.copy_password.get_sensitive()
            and "pw/+" not in repr(locked),
            "the inspector shows its password, copyable, never in a repr",
        )
        copied.clear()
        m.inspector.copy_secret()
        m.inspector.get_clipboard().read_text_async(None, clipboard_done)
        yield lambda: "text" in copied
        check(
            copied["text"] == "pw/+ x" and m.messages[-1] == "Inspector password copied",
            "Copy Password puts the password on the clipboard",
        )
        copied.clear()
        m.inspector.copy_url()
        m.inspector.get_clipboard().read_text_async(None, clipboard_done)
        yield lambda: "text" in copied
        check(
            copied["text"] == "http://127.0.1.1:18083/",
            "Copy URL copies the address without the password",
        )
        opened, launch = [], inspector_ui.Gio.AppInfo.launch_default_for_uri
        inspector_ui.Gio.AppInfo.launch_default_for_uri = lambda uri, _ctx: opened.append(uri)
        try:
            m.inspector.open_browser()
        finally:
            inspector_ui.Gio.AppInfo.launch_default_for_uri = launch
        check(
            opened == ["http://127.0.1.1:18083/?token=pw%2F%2B%20x"]
            and not m.inspector.local_row.get_visible(),
            f"Open in Browser logs in by itself ({opened})",
        )
        # link lan: the LAN URL is shown and copied; this host's browser opens the VM's own address
        lan = inspector.State(
            "mitm",
            "Running",
            "http://192.168.1.157:18083/",
            True,
            password="pw",
            local_url="http://127.0.1.1:18083/",
        )
        m.inspector.show_state(lan)
        copied.clear()
        m.inspector.copy_url()
        m.inspector.get_clipboard().read_text_async(None, clipboard_done)
        yield lambda: "text" in copied
        opened.clear()
        inspector_ui.Gio.AppInfo.launch_default_for_uri = lambda uri, _ctx: opened.append(uri)
        try:
            m.inspector.open_browser()
        finally:
            inspector_ui.Gio.AppInfo.launch_default_for_uri = launch
        check(
            m.inspector.address.get_text() == "http://192.168.1.157:18083/"
            and m.inspector.local_row.get_visible()
            and m.inspector.local_url.get_text() == "http://127.0.1.1:18083/"
            and copied["text"] == "http://192.168.1.157:18083/"
            and opened == ["http://127.0.1.1:18083/?token=pw"],
            f"a link lan VM's inspector: the LAN URL, and this host's ({copied}, {opened})",
        )
        if a.shots:
            shot(m.win, f"{a.shots}-inspector.png")
        m.inspector.show_state(inspector_state)
        # VNC: a group after the inspector, shown only while beanjar vnc serves the VM
        check(
            m.vnc_panel.get_parent() is m.summary_view and not m.vnc_panel.get_visible(),
            "no VNC group while beanjar vnc does not serve the VM",
        )
        secret = os.path.join(
            os.environ.get("TMPDIR") or "/tmp", f"check-gui-vnc-{os.getpid()}.password"
        )
        with open(secret, "w") as f:
            f.write("vnc secret\n")
        try:
            m.vnc_panel.show_info(
                {
                    "pid": 4242,
                    "url": "vnc://192.168.1.157:5900",
                    "local_url": "vnc://127.0.1.1:5900",
                    "password_file": secret,
                }
            )
            check(
                m.vnc_panel.get_visible()
                and m.vnc_panel.address.get_text() == "vnc://192.168.1.157:5900"
                and m.vnc_panel.local_row.get_visible()
                and m.vnc_panel.local_url.get_text() == "vnc://127.0.1.1:5900"
                and m.vnc_panel.password.get_text() == "vnc secret",
                "a link lan VM's VNC: the LAN address, this host's, and the password from its file",
            )
            copied.clear()
            m.vnc_panel.copy_address()
            m.vnc_panel.get_clipboard().read_text_async(None, clipboard_done)
            yield lambda: "text" in copied
            check(
                copied["text"] == "vnc://192.168.1.157:5900"
                and m.messages[-1] == "VNC address copied",
                "Copy Address copies the VNC address",
            )
            copied.clear()
            m.vnc_panel.copy_secret()
            m.vnc_panel.get_clipboard().read_text_async(None, clipboard_done)
            yield lambda: "text" in copied
            check(
                copied["text"] == "vnc secret" and m.messages[-1] == "VNC password copied",
                "Copy Password copies the VNC password",
            )
            check(
                not m.inspector.details.get_expanded() and not m.vnc_panel.details.get_expanded(),
                "Inspector and VNC secondary details start folded",
            )
            if a.shots:
                # Keep the synthetic endpoints stable while the window is resized and rendered.
                inspector_refresh, vnc_refresh = m.inspector.refresh, m.vnc_panel.refresh
                m.inspector.refresh = m.vnc_panel.refresh = lambda _vm: None
                try:
                    m.inspector.show_state(lan)
                    m.win.set_default_size(1100, 1200)
                    if hasattr(m.win.toasts, "dismiss_all"):
                        m.win.toasts.dismiss_all()
                    yield 0.5
                    shot(m.win, f"{a.shots}-vnc.png")
                    shot(m.win, f"{a.shots}-summary-wide-window.png")
                    shot(m.summary_view, f"{a.shots}-summary-wide.png")
                    m.win.set_default_size(480, 1400)
                    yield 0.5
                    shot(m.win, f"{a.shots}-summary-narrow-window.png")
                    shot(m.summary_view, f"{a.shots}-summary-narrow.png")
                    m.win.set_default_size(480, 760)
                    yield 0.5
                    shot(m.win, f"{a.shots}-summary-narrow-short.png")
                finally:
                    m.inspector.refresh, m.vnc_panel.refresh = inspector_refresh, vnc_refresh
                    m.win.set_default_size(1100, 760)
                    m.inspector.show_state(inspector_state)
                yield 0.5
        finally:
            os.unlink(secret)
        m.vnc_panel.refresh(core.VM(lib, "tv1"))
        check(
            not m.vnc_panel.get_visible(),
            "the VNC group follows the VM: hidden again without beanjar vnc",
        )
        m.details.set_visible_child_name("console")
        yield 0.3
        if a.shots:
            shot(m.win, f"{a.shots}-console.png")

        # 5. shut down and start again: the viewer is reopened. The fake's webOS has not started (no
        # run/micom-status with ready=1), so the power-off signal is unavailable and says why;
        # Shut Down then syncs and stops it.
        first_viewer = vm.viewer_pid()
        with open(vm.console_log, "a") as f:  # booted again (the log above was made anew)
            f.write("Starting the initramfs debug shell\n")
        form = m.shutdown()
        signal_row = form.fields["signal"].row
        check(
            isinstance(form, Adw.AlertDialog)
            and form.fields["signal"].get_active()
            and not form.fields["unplug"].get_active()
            and form.get_response_appearance("ok") == Adw.ResponseAppearance.SUGGESTED,
            "Shut Down asks how, the power-off signal chosen",
        )
        check(
            "Checking" not in signal_row.get_subtitle(),
            "the dialog opens with the answer: nothing to wait for",
        )
        check(
            not signal_row.get_sensitive()
            and "webOS is still starting" in signal_row.get_subtitle()
            and "Shut Down syncs the disks and stops the VM" in signal_row.get_subtitle(),
            f"the signal is unavailable and says why ({signal_row.get_subtitle()})",
        )
        dismiss_toasts(m)  # the last operation's, which is not this dialog's
        yield 0.8
        if a.shots:
            shot(m.win, f"{a.shots}-shut-down-unavailable.png")
        form.submit()
        yield lambda: state("tv1") == base.OFF and "tv1" not in m.ops
        out = m.output.get_buffer()
        text = out.get_text(out.get_start_iter(), out.get_end_iter(), False)
        check(
            "no power-off signal (webOS is still starting)" in text and "SAVE_LOGS=1" in text,
            "without the signal, Shut Down syncs and stops it",
        )
        check(not base.proc_alive(first_viewer, "beanjar-viewer"), "viewer closed with the VM")
        check("tv1" in m.want_viewer, "viewer still wanted")
        check(m.auto_reconnect(), "auto-reconnect on by default")
        m.start()
        yield lambda: vm.viewer_pid() is not None and vm.viewer_pid() != first_viewer
        print("ok: viewer reopened after the restart")

        # 6. disconnect, then the Shut Down dialog with the signal available (the check stubbed:
        # the fake guest has no webOS), a power-off that timed out, and Unplug
        m.disconnect()
        yield lambda: vm.viewer_pid() is None and "tv1" not in m.ops
        check("tv1" not in m.want_viewer, "a disconnected viewer is not reopened")
        refusal = core.VM.power_off_refusal
        core.VM.power_off_refusal = lambda self: None
        try:
            form = m.shutdown()
            signal_row = form.fields["signal"].row
            check(signal_row.get_sensitive(), "the signal is available at once")
        finally:
            core.VM.power_off_refusal = refusal
        check(
            "webOS powers the TV off" in signal_row.get_subtitle()
            and form.fields["signal"].get_active(),
            "the signal is available, and chosen",
        )
        dismiss_toasts(m)  # the last operation's, which is not this dialog's
        yield 0.8
        if a.shots:
            shot(m.win, f"{a.shots}-shut-down.png")
        form.fields["unplug"].set_active(True)
        check(
            not form.fields["signal"].get_active()
            and form.get_response_appearance("ok") == Adw.ResponseAppearance.DESTRUCTIVE
            and form.get_response_label("ok") == "_Unplug",
            "choosing Unplug makes OK destructive",
        )
        form.force_close()
        yield 0.3
        with open(vm.power_off_file, "w") as f:  # as a stop that sent the signal leaves it
            f.write(f"{vm.pid()}\n")
        d = m.stop_timed_out("tv1", lib, "tv1")
        check(
            d.has_response("wait")
            and d.has_response("unplug")
            and not d.has_response("sync")
            and d.get_response_appearance("unplug") == Adw.ResponseAppearance.DESTRUCTIVE,
            "a power-off that timed out offers Wait Longer or Unplug",
        )
        dismiss_toasts(m)  # the last operation's, which is not this dialog's
        yield 0.8
        if a.shots:
            shot(m.win, f"{a.shots}-not-powered-off.png")
        d.force_close()
        # a `stop --no-fallback` that fails (here: no such VM, exit 1) hands its result to the
        # dialog, and says nothing as a toast: not "done", not "failed" behind the dialog
        m.messages.clear()
        m.run(
            "tv1",
            "shut down",
            ["stop", "no-such-vm", "--no-fallback"],
            done=lambda rc: m.stop_timed_out("tv1", lib, "tv1") if rc else None,
            library=lib,
        )
        yield lambda: "tv1" not in m.ops
        check(
            isinstance(m.dialog, Adw.AlertDialog) and m.dialog.has_response("wait"),
            "a timed-out signal shows its dialog",
        )
        check(
            not any("shut down" in t for t in m.messages),
            f"and no toast says it was done or failed ({m.messages})",
        )
        m.dialog.force_close()
        os.unlink(vm.power_off_file)
        d = m.stop_timed_out("tv1", lib, "tv1")
        check(
            d.has_response("sync") and not d.has_response("wait"),
            "a stop refused the signal offers Shut Down without it, or Unplug",
        )
        d.force_close()
        form = m.unplug()
        check(
            form.fields["unplug"].get_active() and form.get_response_label("ok") == "_Unplug",
            "Unplug… opens the Shut Down dialog with Unplug chosen",
        )
        form.submit()
        yield lambda: state("tv1") == base.OFF and "tv1" not in m.ops
        out = m.output.get_buffer()
        text = out.get_text(out.get_start_iter(), out.get_end_iter(), False)
        check("--unplug" in text and "SAVE_LOGS=0" in text, "Unplug kills it at once")
        check(
            not os.path.exists(os.path.join(lib_dir, "vms", "tv1", "start.lock")), "lock released"
        )

        # 7. the Wipe dialog's defaults
        m.select("tv2")
        yield 0.5
        m.wipe()
        form = m.dialog
        yield 0.6
        ticked = {p: form.fields[p].get_active() for p in targets.state_parts(("h13", "afro"))}
        check(
            ticked
            == {p: p not in ("nvram", "eeprom") for p in targets.state_parts(("h13", "afro"))},
            f"Wipe ticks all but nvram and eeprom: {ticked}",
        )
        if a.shots:
            shot(m.win, f"{a.shots}-wipe.png")
        for p in targets.state_parts(("h13", "afro")):
            form.fields[p].set_active(False)
        check(not form.get_response_enabled("ok"), "Wipe is disabled with no disk chosen")
        for p in targets.state_parts(("h13", "afro")):
            form.fields[p].set_active(p not in targets.settings_parts(("h13", "afro")))
        check(form.get_response_enabled("ok"), "and enabled again")
        state_dir = os.path.join(lib_dir, "vms", "tv2", "state")
        os.makedirs(state_dir, exist_ok=True)
        for p in targets.state_parts(("h13", "afro")):
            open(os.path.join(state_dir, p + ".img"), "w").close()
        form.submit()
        yield lambda: "tv2" not in m.ops
        check(
            sorted(os.listdir(state_dir)) == ["eeprom.img", "nvram.img"],
            "wipe kept nvram and eeprom",
        )

        # 7b. New Identity: a factory reset with a new MAC, serial and keys; the model key and the CA kept
        check(m.enabled("new-identity"), "New Identity enabled while off")
        with open(good) as fh:
            key = fh.read()
        lib.set_model_key("tv2", good)
        ident = os.path.join(lib_dir, "vms", "tv2", "identity")
        before = lib.vm("tv2").config
        old_keys = {
            n: slurp(os.path.join(ident, n))
            for n in ("id_ed25519", "rootssh_host_key", "devmode_rsa")
        }
        ca = slurp(os.path.join(lib_dir, "ca", "ca.pem"))
        for p in targets.state_parts(("h13", "afro")):
            open(os.path.join(state_dir, p + ".img"), "w").close()
        m.new_identity()
        form = m.dialog
        check(
            isinstance(form, Adw.AlertDialog)
            and form.get_response_appearance("ok") == Adw.ResponseAppearance.DESTRUCTIVE
            and "factory state" in form.get_body()
            and "new MAC address and serial number" in form.get_body()
            and "model key is kept" in form.get_body(),
            "New Identity asks first, and says what it does",
        )
        yield 0.6
        if a.shots:
            shot(m.win, f"{a.shots}-new-identity.png")
        form.submit()
        yield lambda: "tv2" not in m.ops
        after = lib.vm("tv2").config
        check(
            after["mac"] != before["mac"]
            and after["mac"].startswith(lib.mac_prefix + ":")
            and after["serial"] != before["serial"],
            f"a new MAC and serial ({after['mac']}, {after['serial']})",
        )
        check(all(slurp(os.path.join(ident, n)) != v for n, v in old_keys.items()), "new keys")
        check(
            sestore.load(os.path.dirname(key_path))[1][sestore.MODEL].decode() == key.strip()
            and slurp(os.path.join(lib_dir, "ca", "ca.pem")) == ca,
            "the model key and the library's CA kept",
        )
        check(
            not os.path.exists(state_dir) or os.listdir(state_dir) == [], "every state disk wiped"
        )
        check(m.messages[-1] == "tv2: new identity done", m.messages[-1])

        # 7b. Rename: a taken name refused in a toast, then tv2 as tv2b and back, selected each time
        check(m.enabled("rename"), "Rename enabled while off")
        mac = lib.vm("tv2").config["mac"]
        m.rename()
        entry = m.dialog.fields["name"]
        check(not m.dialog.get_response_enabled("ok"), "Rename needs another name")
        entry.set_text("tv1")
        m.dialog.submit()
        check("VM tv1 exists" in m.messages[-1], m.messages[-1])
        m.rename()
        m.dialog.fields["name"].set_text("tv2b")
        m.dialog.submit()
        yield lambda: item("tv2b") is not None and item("tv2") is None
        check(
            m.messages[-1] == "tv2 renamed to tv2b" and m.selected() == "tv2b",
            f"tv2 renamed, selected ({m.messages[-1]}, {m.selected()})",
        )
        check(lib.vm("tv2b").config["mac"] == mac, "the same TV")
        m.rename()
        m.dialog.fields["name"].set_text("tv2")
        m.dialog.submit()
        yield lambda: item("tv2") is not None and m.selected() == "tv2"

        # 7c. Clone: a free name offered, a taken one refused; the copy is another TV, selected
        check(m.enabled("clone"), "Clone enabled while off")
        form = m.clone()
        check(
            form.fields["name"].get_text() == "tv2-2" and form.get_response_enabled("ok"),
            "Clone offers a free name",
        )
        form.fields["name"].set_text("tv1")
        check(not form.get_response_enabled("ok"), "Clone refuses a taken name")
        form.fields["name"].set_text("tv2c")
        form.submit()
        yield lambda: "tv2" not in m.ops and item("tv2c") is not None and m.selected() == "tv2c"
        check(
            m.messages[-1] == "tv2 cloned to tv2c"
            and lib.vm("tv2c").config["mac"] != lib.vm("tv2").config["mac"],
            f"tv2 cloned as another TV, selected ({m.messages[-1]})",
        )
        lib.delete("tv2c")
        yield lambda: item("tv2c") is None

        # 7d. Start with Reason…: the reason on the Summary's state line. The VM's unit holds it, so
        # it needs a user systemd (without one, beanjar start --reason refuses)
        if user_systemd:
            m.select("tv2")
            yield lambda: m.enabled("start-reason")
            form = m.start_reason()
            check(not form.get_response_enabled("ok"), "Start with Reason needs a reason")
            form.fields["reason"].set_text("gui check")
            form.submit()
            yield lambda: state("tv2") == base.RUNNING and "tv2" not in m.ops
            reason = lib.vm("tv2").reason()
            m.refresh()
            check(
                reason is not None
                and reason["text"] == "gui check"
                and m.summary_view.reason.get_visible()
                and m.summary_view.reason_text.get_text() == "gui check",
                "the reason shows on the Summary's state line",
            )
            form = m.unplug()
            form.submit()
            yield lambda: state("tv2") == base.OFF and "tv2" not in m.ops
            check(not m.summary_view.reason.get_visible(), "and goes when the VM stops")
        else:
            print("skip: no user systemd: Start with Reason")

        # 8. the context menu, then Delete through its confirmation
        m.select("tv1")
        yield 0.3
        check(
            menu_actions(m.win.vm_menu_button.get_menu_model())
            == [
                "win.settings",
                "win.rename",
                "win.clone",
                "win.wipe",
                "win.new-identity",
                "win.delete",
            ],
            "the VM menu (⋮) has Settings… and the rare actions only",
        )
        context = menu_actions(m.win.vm_context_menu)
        check(
            all(
                f"win.{k}" in context
                for k in ("start", "shutdown", "unplug", "connect", "disconnect", "settings")
            )
            and context[-6:] == menu_actions(m.win.vm_menu_button.get_menu_model()),
            "the context menu has every action, the VM menu's last",
        )
        m.context_menu("tv2", 20, 20)
        yield 0.3
        check(
            m.selected() == "tv2" and m.vm_menu.get_visible(),
            "the context menu selects its VM and opens",
        )
        m.vm_menu.popdown()
        yield 0.3
        m.delete()
        m.dialog.submit()
        yield lambda: item("tv2") is None
        check(
            m.messages[-1] == "Deleted tv2" and m.selected() == "tv1", "tv2 deleted, tv1 selected"
        )
        result["rc"] = 0

    def run_script():
        gen = script()
        deadline = {"t": 0}

        def step(value):
            if callable(value):
                deadline["t"] = time.monotonic() + 30

                def poll():
                    try:
                        ok = value()
                    except Exception:
                        traceback.print_exc()
                        app.quit()
                        return False
                    if ok:
                        advance()
                        return False
                    if time.monotonic() > deadline["t"]:
                        print("FAIL: timed out waiting", file=sys.stderr)
                        traceback.print_stack(gen.gi_frame)
                        app.quit()
                        return False
                    return True

                GLib.timeout_add(50, poll)
            else:
                GLib.timeout_add(int(value * 1000), lambda: (advance(), False)[1])

        def advance():
            try:
                step(next(gen))
            except StopIteration:
                app.quit()
            except Exception:
                traceback.print_exc()
                app.quit()

        advance()

    def activate(app):
        nonlocal m
        try:
            m = gui.Manager(app, lib, open_folders=False)
        except Exception:
            traceback.print_exc()
            app.quit()
            return
        m_holder["m"] = m
        m.win.present()
        GLib.timeout_add(300, lambda: (run_script(), False)[1])

    m = None
    app.connect("activate", activate)
    try:
        app.run([])
    finally:
        cleanup()
    print("PASS" if result["rc"] == 0 else "FAIL")
    return result["rc"]


if __name__ == "__main__":
    sys.exit(main())
