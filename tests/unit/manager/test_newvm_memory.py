#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""create's default RAM is capped to the host's memory (newvm.cap_memory); an explicit one is not."""

import os
from pathlib import Path
from unittest import mock

import pytest

from beanjar.runtime import capacity, qemu
from beanjar import newvm
from beanjar.board import nvm

TARGET = ("x", "y")


@pytest.fixture
def run_cap(tmp_path):
    def run(total_mib, board, cfg, avail_mib=None, headroom=1024, accel="kvm"):
        lines = []
        meminfo = Path(tmp_path) / "meminfo"
        meminfo.write_text(
            f"MemTotal: {total_mib * 1024} kB\nMemAvailable: {(avail_mib if avail_mib is not None else total_mib) * 1024} kB\n"
        )
        with (
            mock.patch.dict(os.environ, {"BEANJAR_MEMINFO": str(meminfo)}),
            mock.patch.object(capacity, "memory_headroom_mib", return_value=headroom),
            mock.patch.object(newvm.targets, "board_broadcast", return_value="atsc"),
            mock.patch.object(nvm, "board_ram", return_value=board),
            mock.patch.object(qemu, "vm_accel", return_value=accel),
            mock.patch.object(qemu, "tcg_tb_mib", return_value=1024),
        ):
            newvm.cap_memory(None, None, TARGET, "/fw", cfg, lines.append)
        return cfg, lines

    return run


def test_rock_pi_idle(run_cap):
    # MemTotal 3851, MemAvailable 3662: admission takes mem <= 2638; rounded down to 2560
    cfg, lines = run_cap(3851, 4096, {}, avail_mib=3662)
    assert cfg["mem"] == 2560
    assert len(lines) == 1
    assert "2560" in lines[0]


def test_never_over_total_less_headroom(run_cap):
    cfg, _ = run_cap(2500, 4096, {}, avail_mib=2500)
    assert cfg["mem"] == 1280


def test_tcg_cache_subtracted(run_cap):
    cfg, _ = run_cap(3851, 4096, {}, avail_mib=3662, accel="tcg")
    assert cfg["mem"] == 1536


def test_default_that_fits_is_left_unset(run_cap):
    cfg, lines = run_cap(8000, 2048, {})
    assert "mem" not in cfg
    assert lines == []


def test_explicit_never_changed_only_warned(run_cap):
    cfg, lines = run_cap(2000, 4096, {"mem": 4096})
    assert cfg["mem"] == 4096
    assert len(lines) == 1
    assert "warning" in lines[0]


def test_explicit_that_fits_is_silent(run_cap):
    cfg, lines = run_cap(8000, 4096, {"mem": 1024})
    assert (cfg["mem"], lines) == (1024, [])


def test_unknown_host_memory_does_nothing():
    with mock.patch.dict(os.environ, {"BEANJAR_MEMINFO": "/nonexistent"}):
        cfg = {}
        newvm.cap_memory(None, None, TARGET, "/fw", cfg, lambda line: pytest.fail(line))
    assert cfg == {}
