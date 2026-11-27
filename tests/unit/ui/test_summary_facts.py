#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The VM Summary's facts (beanjar_ui/manager/summary_facts.py): relative times, the last run, the
hardware rows worth a line, memory, and the banner's one problem. No GTK or display."""

import datetime
import os
import sys

TOP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
for package in ("ui", "core"):
    sys.path.append(os.path.join(TOP, "host", package))
from beanjar_ui.manager import summary_facts as f  # noqa: E402

NOW = datetime.datetime(2026, 11, 12, 15, 0).timestamp()


def test_relative_time():
    assert f.relative_time(None) == "Never"
    assert f.relative_time(NOW - 20, NOW) == "just now"
    assert f.relative_time(NOW + 30, NOW) == "just now"  # a clock that moved back
    assert f.relative_time(NOW - 5 * 60, NOW) == "5 min ago"
    assert f.relative_time(NOW - 2 * 3600 - 59, NOW) == "2 h ago"
    assert f.relative_time(NOW - 30 * 3600, NOW) == "yesterday"
    assert f.relative_time(NOW - 3 * 86400, NOW) == "3 days ago"
    old = NOW - 40 * 86400
    assert f.relative_time(old, NOW) == datetime.datetime.fromtimestamp(old).strftime("%Y-%m-%d")


def test_last_run(tmp_path):
    assert f.last_run(str(tmp_path)) is None
    console, qemu = tmp_path / "console.log", tmp_path / "qemu.out"
    qemu.write_text("")
    os.utime(qemu, (1000, 1000))
    assert f.last_run(str(tmp_path)) == 1000
    console.write_text("")
    os.utime(console, (2000, 2000))
    (tmp_path / "viewer.log").write_text("")  # a viewer's log is not a run
    os.utime(tmp_path / "viewer.log", (3000, 3000))
    assert f.last_run(str(tmp_path)) == 2000


def test_hardware_defaults():
    # vmconfig.py's: audio follows the display, free page reporting off
    assert f.audio_default("dbus") == "dbus" and f.audio_default("egl-headless") == "none"
    assert f.shown_hardware({}, "dbus") == {"audio": False, "free_page_reporting": False}
    assert f.shown_hardware({"audio": "dbus", "free_page_reporting": "off"}, "dbus") == {
        "audio": False,
        "free_page_reporting": False,
    }
    assert f.shown_hardware({"audio": "none"}, "dbus")["audio"]
    assert not f.shown_hardware({"audio": "off"}, "none")["audio"]  # off and none: no sound
    assert not f.shown_hardware({"audio": "none"}, "egl-headless")["audio"]
    assert f.shown_hardware({"free_page_reporting": "on"}, "dbus")["free_page_reporting"]


def test_fmt_gib():
    assert f.fmt_gib(512) == "512 MiB"
    assert f.fmt_gib(1024) == "1 GiB"
    assert f.fmt_gib(1946) == "1.9 GiB"
    assert f.fmt_gib(3072) == "3 GiB"


def test_first_sentence():
    # core.running_refusal's and capacity.admission's messages
    text = (
        "cannot start VM tv1: 2 VMs already running and the limit is 2 (max_running): a, b. "
        "Stop one, or raise the limit: beanjar host config set max_running=3"
    )
    assert f.first_sentence(text) == (
        "Cannot start VM tv1: 2 VMs already running and the limit is 2 (max_running): a, b"
    )
    memory = (
        "not enough free memory to start VM tv1: it needs 3072 MiB and 1024 MiB of headroom, "
        "and the host has 2000 MiB available (stop another VM, or give this one less memory)"
    )
    assert f.first_sentence(memory) == (
        "Not enough free memory to start VM tv1: it needs 3072 MiB and 1024 MiB of headroom, "
        "and the host has 2000 MiB available"
    )


def test_problem_none():
    assert f.problem() is None
    assert f.problem(qmp_detail="standby") is None  # Quick Start+: normal
    assert f.problem(qmp_detail="running") is None


def test_problem_each():
    p = f.problem(broken="vm.toml: line 7: expected '='")
    assert (p.kind, p.button, p.action) == ("broken", "Open VM Folder", "folder")
    p = f.problem(qmp_detail="paused")
    assert (p.kind, p.title, p.button) == ("qemu", "QEMU is paused", None)
    assert f.problem(qmp_detail="shutdown").kind == "qemu"
    p = f.problem(failed=("start", 1))
    assert (p.title, p.button, p.action) == ("Start failed (exit 1)", "Show Output", "show-output")
    assert f.problem(failed=("shut down", 2)).title == "Shut down failed (exit 2)"
    assert f.problem(failed=("unplug", 143)).title == "Unplug failed (exit 143)"
    p = f.problem(firmware_missing="~/fw/x")
    assert (p.kind, p.button, p.action) == ("firmware", "Settings…", "settings")
    assert "~/fw/x" in p.title
    full = "cannot start VM tv1: 2 VMs already running. Stop one"
    p = f.problem(blocked=full)
    assert (p.kind, p.title, p.button, p.tooltip) == (
        "blocked",
        "Cannot start VM tv1: 2 VMs already running",
        None,
        full,
    )


def test_problem_order():
    """One problem at a time, the most urgent first."""
    every = dict(
        broken="bad",
        qmp_detail="paused",
        failed=("start", 1),
        firmware_missing="~/fw",
        blocked="cannot start",
    )
    order = []
    while (p := f.problem(**every)) is not None:
        order.append(p.kind)
        key = {"broken": "broken", "qemu": "qmp_detail", "failed": "failed"}.get(p.kind)
        every.pop(key or {"firmware": "firmware_missing", "blocked": "blocked"}[p.kind])
    assert order == ["broken", "qemu", "failed", "firmware", "blocked"]


def test_catches_signal(tmp_path):
    """The picture's "Show the Viewer" sends SIGUSR1 only to a viewer that catches it
    (hostprocs.catches_signal, from /proc/PID/status's SigCgt)."""
    import signal

    from beanjar.runtime import hostprocs

    (tmp_path / "7").mkdir()
    status = tmp_path / "7" / "status"
    status.write_text("Name:\tbeanjar-viewer\nSigCgt:\t0000000000000200\n")  # SIGUSR1 (10) only
    assert hostprocs.catches_signal(7, signal.SIGUSR1, str(tmp_path))
    assert not hostprocs.catches_signal(7, signal.SIGUSR2, str(tmp_path))
    assert not hostprocs.catches_signal(8, signal.SIGUSR1, str(tmp_path))  # no such process


def test_picture_turn():
    """A portrait model's picture turned as the web list turns it (style.css: rotation 90 is
    rotate(270deg), a quarter counter-clockwise; 270 a quarter clockwise)."""
    import pytest

    gi = pytest.importorskip("gi")
    for name, version in (("GdkPixbuf", "2.0"), ("Gdk", "4.0"), ("Gtk", "4.0")):
        gi.require_version(name, version)
    from gi.repository import GdkPixbuf, GLib

    from beanjar_ui.manager.picture import turn

    a, b = b"\xff\x00\x00", b"\x00\x00\xff"  # red, blue: a 2x1 picture, red at the left
    pix = GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(a + b), GdkPixbuf.Colorspace.RGB, False, 8, 2, 1, 6
    )

    def column(p):
        data, stride = p.get_pixels(), p.get_rowstride()
        return [bytes(data[y * stride : y * stride + 3]) for y in range(p.get_height())]

    assert turn(pix, 0) is pix
    assert column(turn(pix, 90)) == [b, a]  # counter-clockwise: the right end on top
    assert column(turn(pix, 270)) == [a, b]  # clockwise: the left end on top
    assert bytes(turn(pix, 180).get_pixels()[:3]) == b
