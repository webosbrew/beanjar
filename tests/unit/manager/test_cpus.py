#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""beanjar/runtime/cpus.py (big.LITTLE placement) against a stand-in /sys/devices/system/cpu."""

import os
from unittest.mock import patch

import pytest

from beanjar.runtime import cpus


@pytest.fixture
def tree(tmp_path):
    n = 0

    def make(caps):
        nonlocal n
        n += 1
        root = tmp_path / f"cpu-tree-{n}"
        for cpu, cap in enumerate(caps):
            (root / f"cpu{cpu}").mkdir(parents=True)
            if cap is not None:
                (root / f"cpu{cpu}" / "cpu_capacity").write_text(f"{cap}\n")
        return str(root)

    return make


def test_rk3399(tree):
    root = tree([381, 381, 381, 381, 1024, 1024])
    with patch.object(os, "sched_getaffinity", return_value=set(range(6))):
        assert cpus.big_cpus(root) == [4, 5]
        with patch.object(cpus.shutil, "which", return_value="/usr/bin/taskset"):
            assert cpus.on_big(["beanjar-vnc", "x"], root) == [
                "/usr/bin/taskset",
                "-c",
                "4,5",
                "beanjar-vnc",
                "x",
            ]


def test_alike_or_unknown(tree):
    with patch.object(os, "sched_getaffinity", return_value=set(range(4))):
        assert cpus.big_cpus(tree([1024] * 4)) is None
        assert cpus.big_cpus(tree([None] * 4)) is None
        assert cpus.on_big(["a"], tree([1024] * 4)) == ["a"]
        assert not cpus.thread_on_big(1, tree([1024] * 4))


def test_outside_affinity(tree):
    root = tree([381, 381, 1024, 1024])
    with patch.object(os, "sched_getaffinity", return_value={0, 1}):
        assert cpus.big_cpus(root) is None
    with patch.object(os, "sched_getaffinity", return_value={0, 1, 3}):
        assert cpus.big_cpus(root) == [3]


def test_thread(tree):
    root = tree([381, 1024])
    with (
        patch.object(os, "sched_getaffinity", return_value={0, 1}),
        patch.object(os, "sched_setaffinity") as setaff,
    ):
        assert cpus.thread_on_big(1234, root)
        setaff.assert_called_once_with(1234, [1])
