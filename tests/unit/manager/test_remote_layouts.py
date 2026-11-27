#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The remotes' layouts (host/core/beanjar/devices/remote_layouts.py) and the layout a VM gets
(pipe.vm_layout): the Japanese remote on an ARIB board, its keys' codes, the extended codes the
CLI's remote takes. No VM. `pytest tests/unit/manager/test_remote_layouts.py`."""

import os
import sys
from unittest import mock

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, os.path.join(TOP, "host", "core"))
from beanjar import base, pipe  # noqa: E402
from beanjar.devices import remote_layouts as rl  # noqa: E402


@pytest.mark.parametrize(
    "info, name",
    [
        ({"platform": "webOS", "release": "queue", "broadcast": "arib"}, "japan"),
        ({"platform": "webOS", "release": "queue", "broadcast": "atsc"}, "normal"),
        ({"platform": "webOS", "release": "queue", "broadcast": "dvb"}, "normal"),
        ({"platform": "webOS", "release": "queue"}, "normal"),
        ({"platform": "NetCast", "release": "netcast45", "broadcast": "arib"}, "netcast"),
        ({"platform": "SimpleSmart", "broadcast": "arib"}, "simplesmart"),
        ({}, "normal"),
    ],
)
def test_auto_layout(info, name):
    assert rl.remote_layout("auto", info) == name


def test_explicit_layout_on_arib():
    info = {"platform": "webOS", "broadcast": "arib"}
    assert rl.remote_layout("service", info) == "service"
    assert rl.remote_layout("japan", info) == "japan"
    with pytest.raises(ValueError):
        rl.remote_layout("normal", info)
    with pytest.raises(ValueError):
        rl.remote_layout("japan", {"platform": "webOS", "broadcast": "atsc"})


def codes(layout):
    return {k.name: k.code for k in layout.keys}


def test_japan_keys():
    jp = codes(rl.JAPAN)
    assert (jp["DATA"], jp["DISPLAY"], jp["THREE_DIGIT"]) == (0x9F, 0xAC, 0x23)
    assert [jp[n] for n in ("BS", "CS", "TERRESTRIAL", "BS4K")] == [0xDB00, 0xDB10, 0xDB30, 0xDB40]
    assert [jp[str(d)] for d in range(1, 13)] == [0x10 + d for d in range(1, 10)] + [
        0x10,
        0xDB3B,
        0xDB3C,
    ]
    # the normal remote's other keys keep their codes
    normal = codes(rl.NORMAL)
    for name, code in jp.items():
        if name in normal and not name.isdigit():
            assert code == normal[name], name
    assert "LIST" not in jp and "FLASHBACK" not in jp
    assert all(rl.code_valid(c) for c in jp.values() if c is not None)


def test_japan_fits_its_panel():
    layout = rl.JAPAN
    bx, by, bw, bh, _, _ = layout.body
    boxes = [(k.x, k.y, k.x + k.w, k.y + k.h) for k in layout.keys]
    for name, (x0, y0, x1, y1) in zip((k.name for k in layout.keys), boxes):
        assert bx <= x0 and x1 <= bx + bw and by <= y0 and y1 <= by + bh, name
    for i, a in enumerate(boxes):
        for b in boxes[i + 1 :]:
            assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1], (a, b)
    assert rl.as_json(layout)["name"] == "japan"


def test_other_layouts_unchanged():
    assert len(rl.NORMAL.keys) == 48 and rl.NORMAL.height == 652
    assert all(0 <= k.code <= 0xFF for lay in (rl.NORMAL, rl.NETCAST, rl.SERVICE) for k in lay.keys
               if k.code is not None)  # fmt: skip


def test_code_valid():
    assert rl.code_valid(0) and rl.code_valid(0xFF) and rl.code_valid(0xDB00)
    assert rl.code_valid(0xDBFF)
    assert not rl.code_valid(0x100) and not rl.code_valid(0xDC00) and not rl.code_valid(-1)


def vm(broadcast):
    v = mock.Mock()
    v.target.return_value = ("o22", "queue")
    return v, mock.patch("beanjar.tuner.antenna.vm_broadcast", return_value=broadcast)


def test_vm_layout_arib():
    v, patch = vm("arib")
    with patch, mock.patch("beanjar.targets.platform", return_value="webOS"):
        assert pipe.vm_layout(v) is rl.JAPAN
    v, patch = vm("atsc")
    with patch, mock.patch("beanjar.targets.platform", return_value="webOS"):
        assert pipe.vm_layout(v) is rl.NORMAL
    v = mock.Mock(target=mock.Mock(side_effect=base.Error("none")))
    assert pipe.vm_layout(v) is rl.NORMAL


def test_remote_code_takes_names_and_extended_codes():
    r = pipe.Remote.__new__(pipe.Remote)
    r.layout = rl.JAPAN
    assert r.code("DATA") == 0x9F and r.code("bs4k") == 0xDB40
    assert r.code("0xdb30") == 0xDB30 and r.code("0x9f") == 0x9F
    for bad in ("0x100", "0xdc00", "LIST"):
        with pytest.raises(base.Error):
            r.code(bad)
