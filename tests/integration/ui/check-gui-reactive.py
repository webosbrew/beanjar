#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The VM manager's window follows its library whoever changes it: the CLI (beanjar create, start,
stop, settings set, delete) in another process, a VM folder moved in or removed, a vm.toml that does not
read, a run/ made anew; and lists the VMs running elsewhere (another library's), read-only, with a
Shut Down that goes to their library. Against the fakes of tests/fakes, on a private Xvfb.

    xvfb-run -a tests/integration/ui/check-gui-reactive.py      (pytest tests/integration/ui)

Exit status 77 (skip) without GTK 4 and libadwaita for Python or without an X display.
"""

import os
import shutil
import subprocess
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
    from gi.repository import Adw, GLib
except (ImportError, ValueError) as e:
    print(f"SKIP: no GTK 4 and libadwaita for Python: {e}")
    sys.exit(77)
if not os.environ.get("DISPLAY"):
    print("SKIP: no X display (run under xvfb-run -a)")
    sys.exit(77)

from beanjar import base  # noqa: E402
from beanjar.vm import library as vm_library  # noqa: E402
from beanjar.runtime import unit  # noqa: E402
from beanjar_ui.manager import gui  # noqa: E402
import testunits  # noqa: E402

FW = "05.06.10.01-HE_DTV_WT1H_AFAAABAA"
WITHIN = 3.0  # seconds a change may take to show: "about a second", with room for a slow host


def main():
    tmp = tempfile.mkdtemp(prefix="beanjar-gui-")
    lib_dir, other_dir = os.path.join(tmp, "lib"), os.path.join(tmp, "other")
    repo = os.path.join(tmp, "repo")
    os.makedirs(os.path.join(repo, "build"))
    # the target's always-used layers, which create checks (compat.py)
    for d in ("webos", "soc", "common"):
        os.makedirs(os.path.join(repo, "build", "layers", "h13-afro", d))
    board = "guest/layers/soc/h13/rootfs/etc/emulator/board.cmdline"  # the model's hwopt: its RAM
    os.makedirs(os.path.dirname(os.path.join(repo, board)))
    shutil.copyfile(os.path.join(TOP, board), os.path.join(repo, board))
    fw = os.path.join(tmp, "fws", FW)
    os.makedirs(fw)
    shutil.copyfile(
        os.path.join(TOP, "tests", "fakes", "trust-rootfs.pak"), os.path.join(fw, "rootfs.pak")
    )
    os.environ.update(
        BEANJAR_LAUNCHER=os.path.join(TOP, "tests", "fakes", "fake-vm"),
        BEANJAR_VIEWER=os.path.join(TOP, "tests", "fakes", "fake-beanjar-viewer"),
        FAKE_UNIT_PREFIX=testunits.PREFIX,
        FAKE_BOOT_DELAY="0.5",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONPATH=os.path.join(TOP, "host", "core")
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
    )
    lib = vm_library.Library(lib_dir, repo)

    def cli(*args, library=lib_dir):
        """The CLI in a process of its own, as from a shell."""
        p = subprocess.run(
            [sys.executable, "-m", "beanjar", "--library", library, "--repo", repo, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=60,
        )
        if p.returncode != 0:
            raise AssertionError(f"beanjar {' '.join(args)}: exit {p.returncode}\n{p.stdout}")
        print(f"$ beanjar {' '.join(args)}")

    def cleanup():
        for root in (lib_dir, other_dir):
            if os.path.isdir(os.path.join(root, "vms")):
                for vm in vm_library.Library(root, repo).vms():
                    if vm.pid():
                        testunits.end(vm.pid())
        testunits.stop_ours()
        shutil.rmtree(tmp, ignore_errors=True)

    testunits.install(cleanup)
    # what reads the VMs' units (max_running, VMs of other libraries) needs a user systemd
    user_systemd = unit.available()
    app = Adw.Application(application_id=None)
    result = {"rc": 1}
    m = None

    def check(cond, what):
        if not cond:
            raise AssertionError(what)
        print(f"ok: {what}")

    def rows():
        return {m.store.get_item(i).name: m.store.get_item(i) for i in range(m.store.get_n_items())}

    def phase(name):
        item = rows().get(name)
        return item.phase if item else None

    def timer_alive():
        return GLib.main_context_default().find_source_by_id(m.timer) is not None

    def script():
        cli("create", "a", "--import", "--firmware", fw)
        yield lambda: phase("a") == base.OFF
        check(True, "a VM the CLI made shows")
        cli("start", "a")
        yield lambda: phase("a") == base.RUNNING
        check(True, "the CLI's start shows: Running")
        cli("stop", "a")
        yield lambda: phase("a") == base.OFF
        check(True, "the CLI's stop shows: Off")
        cli("config", "set", "a", "smp=2")
        yield lambda: "2 CPUs" in rows()["a"].specs
        check(True, f"the CLI's set shows ({rows()['a'].specs})")

        # a vm.toml that does not read (a folder half copied in, a file a newer version wrote): that
        # VM says why, the others and the timer go on
        bad = os.path.join(lib_dir, "vms", "b")
        os.makedirs(bad)
        with open(os.path.join(bad, "vm.toml"), "w") as f:
            f.write('[network]\nlink = "nat"\nunknown = 1\n')
        yield lambda: phase("b") == gui.BROKEN
        check(
            "unknown setting network.unknown" in rows()["b"].state and timer_alive(),
            f"a vm.toml that does not read: its row says why ({rows()['b'].state}), the timer runs on",
        )
        m.select("b")
        yield 0.2
        check(
            not m.enabled("start")
            and not m.enabled("settings")
            and m.enabled("delete")
            and m.enabled("folder")
            and "unknown setting" in m.summary_text(),
            "its actions: none that need its settings; Delete",
        )
        banner = m.win.summary_banner
        check(
            banner.get_revealed()
            and "unknown setting network.unknown" in banner.get_title()
            and banner.get_button_label() == "Open VM Folder"
            and banner.get_action_name() == "win.folder"
            and m.summary_view.tiles["state"].value.get_text() == gui.BROKEN,
            f"the Summary's banner says why, with Open VM Folder ({banner.get_title()})",
        )
        if os.environ.get("CHECK_GUI_SHOTS"):
            m.win.set_default_size(1100, 760)
            yield 0.5
            from gi.repository import Gtk  # the window as rendered, as check-gui.py's shot

            snap = Gtk.Snapshot()
            Gtk.WidgetPaintable.new(m.win).snapshot(snap, m.win.get_width(), m.win.get_height())
            node = snap.to_node()
            if node is not None:
                m.win.get_renderer().render_texture(node, None).save_to_png(
                    os.environ["CHECK_GUI_SHOTS"] + "-summary-broken.png"
                )
        cli("start", "a")
        yield lambda: phase("a") == base.RUNNING
        check(phase("b") == gui.BROKEN, "the other VMs still follow")
        cli("stop", "a")
        yield lambda: phase("a") == base.OFF
        shutil.copyfile(os.path.join(lib_dir, "vms", "a", "vm.toml"), os.path.join(bad, "vm.toml"))
        yield lambda: phase("b") == base.OFF
        check(
            m.summary_view.problem is None or m.summary_view.problem.kind != "broken",
            "fixed, it reads again; the banner no longer says it does not",
        )

        # folders moved in and removed
        cli("create", "c", "--import", "--firmware", fw, library=other_dir)
        os.rename(os.path.join(other_dir, "vms", "c"), os.path.join(lib_dir, "vms", "c"))
        yield lambda: phase("c") == base.OFF
        check(True, "a VM folder moved into vms/ shows")
        shutil.rmtree(bad)
        yield lambda: "b" not in rows()
        check(
            not os.path.exists(bad),
            "a VM folder removed goes, and is not made again (no run/ left behind)",
        )

        # run/ made anew: its monitor is armed again. Without the timer, only the monitors refresh.
        run = os.path.join(lib_dir, "vms", "c", "run")
        shutil.rmtree(run, ignore_errors=True)
        os.makedirs(run)
        yield 0.5
        GLib.source_remove(m.timer)
        st = os.stat(run)
        check(
            m.watches[("run", "c")][1] == (st.st_dev, st.st_ino)
            and m.watches[("run", "c")][2] is not None,
            "a run/ made anew is watched again",
        )
        cli("start", "c")
        yield lambda: phase("c") in (base.STARTING, base.RUNNING)
        check(True, "the CLI's start shows through the monitors alone")
        m.timer = GLib.timeout_add(1000, m._tick)
        armed = m.tick
        cli("stop", "c")
        yield lambda: phase("c") == base.OFF

        # another library's VM, started by its CLI: listed elsewhere, read-only, with Shut Down. It is
        # found by its unit, so this needs a user systemd
        if user_systemd:
            cli("create", "o", "--import", "--firmware", fw, library=other_dir)
            cli("start", "o", library=other_dir)

            def elsewhere(name):
                return next((r for r in m.elsewhere if r["name"] == name), None)

            yield lambda: elsewhere("o") is not None and "Running" in elsewhere("o")["state"]
            r = elsewhere("o")
            row = m.win.elsewhere_list.get_first_child()
            while row is not None and row.elsewhere["name"] != "o":
                row = row.get_next_sibling()
            check(
                r["library"] is not None
                and r["library"].root == other_dir
                and "o" not in rows()
                and m.win.elsewhere_group.get_visible()
                and r["address"],
                f"another library's VM is listed elsewhere, not in the library's list ({r['state']}, {r['address']}, {r['where']})",
            )
            check(
                row is not None
                and getattr(row, "stop_button", None) is not None
                and not row.get_activatable(),
                "its row is read-only but for Shut Down",
            )
            m.stop_elsewhere(r)
            m.dialog.submit()
            yield (lambda: elsewhere("o") is None and r["key"] not in m.ops), 30
            # (the group itself may stay: another test's VMs, run at the same time, are elsewhere too)
            check(
                vm_library.Library(other_dir, repo).vm("o").pid() is None
                and m.win.elsewhere_group.get_visible() == bool(m.elsewhere),
                "Shut Down stops it through its library; its row goes",
            )
        else:
            print("skip: no user systemd: another library's VM")

        cli("delete", "a")
        # the timer ticks again once armed anew, however quickly the steps above went
        yield lambda: "a" not in rows() and m.tick > armed
        check(
            timer_alive(),
            f"the CLI's delete shows; the timer runs on ({m.tick - armed} ticks since armed anew)",
        )
        result["rc"] = 0

    def run_script():
        gen = script()

        def step(value):
            limit = WITHIN
            if isinstance(value, tuple):  # (condition, its own limit): an operation of the window's
                value, limit = value
            if callable(value):
                start = time.monotonic()
                deadline = start + 30

                def poll():
                    try:
                        ok = value()
                    except Exception:
                        traceback.print_exc()
                        app.quit()
                        return False
                    if ok:
                        took = time.monotonic() - start
                        if took > limit:
                            print(f"FAIL: took {took:.1f} s (more than {limit} s)", file=sys.stderr)
                            traceback.print_stack(gen.gi_frame)
                            app.quit()
                            return False
                        advance()
                        return False
                    if time.monotonic() > deadline:
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
        # the Summary's picture: a fake VM has no screen (no headless viewer left behind)
        m.pictures.command = lambda _name, _words: ["false"]
        m.win.present()
        GLib.timeout_add(300, lambda: (run_script(), False)[1])

    app.connect("activate", activate)
    try:
        app.run([])
    finally:
        cleanup()
    print("PASS" if result["rc"] == 0 else "FAIL")
    return result["rc"]


if __name__ == "__main__":
    sys.exit(main())
