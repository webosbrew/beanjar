#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The core package beanjar (host/core) never loads GTK: every one of its modules imports with
gi.repository's Gtk, Gdk, Adw (and the rest of GTK's stack) and the windows' package beanjar_ui
blocked, and none of them is loaded afterwards. Gio and GLib stay allowed (dbus.py's, for the VMs'
bus). No display, no VM.

The checks run in a fresh interpreter (this file as a program): another test of the same pytest
process may already have loaded beanjar_ui or GTK."""

import importlib
import importlib.abc
import json
import os
import pkgutil
import subprocess
import sys

TOP = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(TOP, "host", "core"))
sys.dont_write_bytecode = True

BLOCKED = ("Gtk", "Gdk", "Adw", "GdkPixbuf", "Gsk", "Graphene", "Pango")
BLOCKED_MODULES = {f"gi.repository.{n}" for n in BLOCKED} | {"beanjar_ui"}
SKIP = {"beanjar.__main__"}  # runs the CLI


class Blocker(importlib.abc.MetaPathFinder):
    def __init__(self):
        self.asked = []

    def find_spec(self, name, path=None, target=None):
        if name in BLOCKED_MODULES or name.split(".")[0] == "beanjar_ui":
            self.asked.append(name)
            raise ImportError(f"{name} is blocked: the core must not import it")
        return None


def loaded():
    return sorted(n for n in sys.modules if n in BLOCKED_MODULES or n.split(".")[0] == "beanjar_ui")


def import_every_core_module():
    """Every beanjar module imported with the blocker in place: (modules, failures, asked, loaded)."""
    before = loaded()
    blocker = Blocker()
    sys.meta_path.insert(0, blocker)
    try:
        import beanjar

        names = sorted(
            m.name
            for m in pkgutil.walk_packages(beanjar.__path__, "beanjar.")
            if m.name not in SKIP
        )
        failed = {}
        for name in names:
            try:
                importlib.import_module(name)
            except ImportError as e:
                failed[name] = str(e)
    finally:
        sys.meta_path.remove(blocker)
    return before, names, failed, blocker.asked, loaded()


def block_gtk():
    """The finder sits before gi's own importer: Gtk cannot be imported past it."""
    blocker = Blocker()
    sys.meta_path.insert(0, blocker)
    try:
        importlib.import_module("gi.repository.Gtk")
        raised = False
    except ImportError:
        raised = True
    finally:
        sys.meta_path.remove(blocker)
    return raised, blocker.asked


def fresh(check):
    r = subprocess.run(
        [sys.executable, __file__, check], capture_output=True, text=True, timeout=120
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_every_core_module_imports_without_gtk():
    before, names, failed, asked, after = fresh("core")
    assert before == [], "loaded before the test"
    assert len(names) > 50
    assert failed == {}
    assert asked == []
    assert after == []


def test_the_blocker_blocks():
    raised, asked = fresh("blocker")
    assert raised
    assert asked == ["gi.repository.Gtk"]


if __name__ == "__main__":
    check = {"core": import_every_core_module, "blocker": block_gtk}[sys.argv[1]]
    print(json.dumps(check()))
