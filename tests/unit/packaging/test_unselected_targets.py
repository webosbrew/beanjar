# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""packaging/debian/build.py refuses a guest package that leaves a target out (NetCast 4.5's was
missing from an installed payload because its build had never run)."""

import importlib.util
import json
import sys
from pathlib import Path

TOP = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(TOP / "host/core"))
from beanjar.firmware import targets

spec = importlib.util.spec_from_file_location("debian_build", TOP / "packaging/debian/build.py")
debian = importlib.util.module_from_spec(spec)
spec.loader.exec_module(debian)

ALL = [targets.name(t) for t in targets.all_targets() if targets.name(t) != "virt-testguest"]


def publish(root, selected):
    by_release = {}
    for name in selected:
        by_release.setdefault(name.split("-", 1)[1], {})[name] = "0" * 64
    for release, sel in by_release.items():
        d = root / "release" / release
        d.mkdir(parents=True)
        (d / "selections.json").write_text(json.dumps(sel))


def test_every_target_selected(tmp_path):
    publish(tmp_path, ALL)
    assert debian.unselected_targets(tmp_path) == []


def test_netcast_left_out(tmp_path):
    publish(tmp_path, [n for n in ALL if n != "lm14-netcast45"])
    assert debian.unselected_targets(tmp_path) == ["lm14-netcast45"]


def test_test_guest_not_needed(tmp_path):
    publish(tmp_path, ALL)
    assert "virt-testguest" not in debian.unselected_targets(tmp_path)
