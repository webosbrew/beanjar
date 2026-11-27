#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The manager header's state button by VM state (beanjar_ui/manager/header.py), no GTK or display."""

import os
import re
import sys

import pytest

TOP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
for package in ("ui", "core"):
    sys.path.append(os.path.join(TOP, "host", package))
from beanjar import base  # noqa: E402
from beanjar_ui.manager import header  # noqa: E402
from beanjar_ui.manager.header import slot  # noqa: E402


def test_phases_are_cores():
    assert (header.OFF, header.STARTING, header.RUNNING, header.STOPPING) == (
        base.OFF,
        base.STARTING,
        base.RUNNING,
        base.STOPPING,
    )


def test_off_starts():
    s = slot(header.OFF)
    assert (s.label, s.action, s.menu, s.suggested, s.tooltip) == (
        "Start",
        "start",
        "start_menu",
        True,
        "Start",
    )
    # no VM, or one whose vm.toml does not read: the same, its action insensitive
    assert slot(None) == s


def test_blocked_says_why():
    s = slot(header.OFF, blocked="cannot start VM tv2: not enough free memory")
    assert s.action == "start" and s.tooltip == "cannot start VM tv2: not enough free memory"


@pytest.mark.parametrize(
    "busy, label",
    [
        ("start", "Starting…"),
        ("shut down", "Shutting Down…"),
        ("unplug", "Unplugging…"),
        ("clone", "Cloning…"),
        ("something new", "Working…"),
    ],
)
def test_busy_is_insensitive(busy, label):
    for phase in (header.OFF, header.STARTING, header.RUNNING):
        s = slot(phase, busy=busy, pid=True)
        assert (s.label, s.action, s.suggested) == (label, None, False)
        assert s.tooltip == label  # the icon-only button's tooltip says what runs


def test_stopping():
    s = slot(header.STOPPING, pid=True)
    assert s.action is None and s.label == s.tooltip == "Shutting Down…"


@pytest.mark.parametrize("phase", [header.STARTING, header.RUNNING])
def test_running_connects(phase):
    s = slot(phase, pid=True)
    assert (s.label, s.action, s.menu, s.suggested) == ("Connect", "connect", "running_menu", True)


def test_viewer_open_shuts_down():
    s = slot(header.RUNNING, pid=True, viewer=True)
    assert (s.label, s.action, s.menu, s.suggested) == (
        "Shut Down…",
        "shutdown",
        "viewer_menu",
        False,
    )


@pytest.mark.parametrize("display", ["egl-headless", "none"])
def test_no_dbus_display_shuts_down(display):
    s = slot(header.RUNNING, pid=True, display=display)
    assert (s.action, s.menu, s.suggested) == ("shutdown", "unplug_menu", False)
    assert f"display {display}: the viewer needs dbus" in s.tooltip


def test_no_qemu_yet_shuts_down():
    s = slot(header.STARTING, pid=False)
    assert (s.action, s.menu) == ("shutdown", "unplug_menu")


@pytest.mark.parametrize(
    "kwargs, action",
    [
        ({"phase": header.OFF}, "Start"),
        ({"phase": header.RUNNING, "pid": True}, "Connect"),
        ({"phase": header.RUNNING, "pid": True, "viewer": True}, "Shut Down…"),
        ({"phase": header.STARTING, "pid": False}, "Shut Down…"),
        ({"phase": header.RUNNING, "pid": True, "display": "none"}, "Shut Down…"),
    ],
)
def test_tooltip_names_the_action(kwargs, action):
    """The button shows only an icon: its tooltip and label (the accessible label) name the action."""
    s = slot(**kwargs)
    assert s.label == action and s.tooltip.startswith(action)


def test_menus_and_actions_exist():
    """Every menu and action a slot names is the window's (window.blp, gui.Manager's actions)."""
    with open(os.path.join(TOP, "host", "ui", "data", "manager", "window.blp")) as f:
        menus = set(re.findall(r"^menu (\w+) \{", f.read(), re.MULTILINE))
    with open(os.path.join(TOP, "host", "ui", "beanjar_ui", "manager", "gui.py")) as f:
        actions = set(re.findall(r'^\s+"([a-z-]+)": (?:self\.|lambda)', f.read(), re.MULTILINE))
    slots = [
        slot(header.OFF),
        slot(header.RUNNING, pid=True),
        slot(header.RUNNING, pid=True, viewer=True),
        slot(header.RUNNING, pid=True, display="none"),
    ]
    for s in slots:
        assert s.menu in menus, s
        assert s.action in actions, s
