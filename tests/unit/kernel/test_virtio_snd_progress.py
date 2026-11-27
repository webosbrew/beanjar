#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Compile the shipped worker/pointer with pinned ALSA ring recovery and test locks."""

import os
import re
import resource
import subprocess
from pathlib import Path

import pytest
from linuxsrc import linux_source

TOP = Path(__file__).resolve().parents[3]
KERNEL = Path(os.environ.get("VIRTIO_TEST_KERNEL") or linux_source())
PATCH = TOP / "guest/kernel/patches/0013-virtio-snd-period-progress.patch"

pytestmark = pytest.mark.skipif(
    not (KERNEL / "sound/virtio").is_dir(), reason=f"no unpacked kernel source: {KERNEL}"
)


def function(text, name):
    match = re.search(r"^static (?:void|int|snd_pcm_uframes_t)\s+" + name + r"\(", text, re.M)
    if not match:
        raise ValueError("function definition missing: " + name)
    start = match.start()
    brace = text.index("{", start)
    depth = 1
    end = brace + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


def core_recovery():
    text = (KERNEL / "sound/core/pcm_lib.c").read_text()
    begin = text.index("\thw_base = runtime->hw_ptr_base;")
    end = text.index("\n\tif (runtime->no_period_wakeup)", begin)
    return text[begin:end]


def test_shipped_progress_and_previous_negative_control(tmp_path):
    worker = (KERNEL / "sound/virtio/virtio_pcm.c").read_text()
    ops = (KERNEL / "sound/virtio/virtio_pcm_ops.c").read_text()
    assert "hw_published" in worker
    harness = (Path(__file__).with_name("virtio-snd-progress-fixture.c")).read_text()
    directory = tmp_path
    source = directory / "fixture.c"
    code = harness.replace("/* CORE_RECOVERY */", core_recovery())
    code = code.replace("/* WORKER */", function(worker, "virtsnd_pcm_period_elapsed"))
    code = code.replace("/* POINTER */", function(ops, "virtsnd_pcm_pb_pointer"))
    code = code.replace("/* CANCEL_HELPER */", function(ops, "virtsnd_pcm_cancel_playback_work"))
    for name, marker in (
        ("virtsnd_pcm_prepare", "PREPARE"),
        ("virtsnd_pcm_hw_free", "HW_FREE"),
        ("virtsnd_pcm_close", "CLOSE"),
    ):
        code = code.replace("/* " + marker + " */", function(ops, name))
    source.write_text(code)
    binary = directory / "fixture"
    subprocess.run(
        [
            "cc",
            "-std=gnu11",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-Wno-sign-compare",
            "-Wno-unused-function",
            str(source),
            "-o",
            str(binary),
        ],
        check=True,
    )
    result = subprocess.run([str(binary)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "progress/locks/lifecycle regression PASS" in result.stdout
    # Reverse only the owned new patch in an isolated subset to recover
    # the preceding implementation; do not edit the fetched kernel.
    for relative in ("virtio_pcm.c", "virtio_pcm_ops.c", "virtio_pcm.h"):
        target = directory / "sound/virtio" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((KERNEL / "sound/virtio" / relative).read_bytes())
    subprocess.run(
        ["patch", "-R", "-p1", "-d", str(directory), "-i", str(PATCH)],
        env=dict(os.environ, TMPDIR=str(directory)),
        check=True,
        capture_output=True,
    )
    old_worker = (directory / "sound/virtio/virtio_pcm.c").read_text()
    old_ops = (directory / "sound/virtio/virtio_pcm_ops.c").read_text()
    old_code = harness.replace("/* CORE_RECOVERY */", core_recovery())
    old_code = old_code.replace("/* WORKER */", function(old_worker, "virtsnd_pcm_period_elapsed"))
    old_code = old_code.replace("/* POINTER */", function(old_ops, "virtsnd_pcm_pb_pointer"))
    old_code = old_code.replace(
        "/* CANCEL_HELPER */", function(ops, "virtsnd_pcm_cancel_playback_work")
    )
    for name, marker in (
        ("virtsnd_pcm_prepare", "PREPARE"),
        ("virtsnd_pcm_hw_free", "HW_FREE"),
        ("virtsnd_pcm_close", "CLOSE"),
    ):
        old_code = old_code.replace("/* " + marker + " */", function(ops, name))
    source.write_text(old_code)
    subprocess.run(
        [
            "cc",
            "-std=gnu11",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-Wno-sign-compare",
            "-Wno-unused-function",
            str(source),
            "-o",
            str(binary),
        ],
        check=True,
    )

    def no_core():
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    old = subprocess.run([str(binary)], capture_output=True, text=True, preexec_fn=no_core)
    assert old.returncode != 0, "preceding full-ring behavior must fail"
    assert "runtime.status->hw_ptr == runtime.buffer_size" in old.stderr


def test_prepare_start_and_capture_contracts():
    ops = (KERNEL / "sound/virtio/virtio_pcm_ops.c").read_text()
    prepare = function(ops, "virtsnd_pcm_prepare")
    assert "vss->hw_done = vss->hw_published = vss->hw_seen = 0;" in prepare
    assert prepare.index("virtsnd_pcm_cancel_playback_work(vss)") < prepare.index("vss->hw_done =")
    for name in ("virtsnd_pcm_hw_params", "virtsnd_pcm_hw_free", "virtsnd_pcm_close"):
        body = function(ops, name)
        assert "virtsnd_pcm_cancel_playback_work(vss);" in body
    cancel = function(ops, "virtsnd_pcm_cancel_playback_work")
    assert cancel.index("vss->xfer_enabled = false") < cancel.index("cancel_work_sync")
    trigger = function(ops, "virtsnd_pcm_trigger")
    assert "if (!rc && vss->direction == SNDRV_PCM_STREAM_PLAYBACK)" in trigger
    assert "schedule_work(&vss->elapsed_period);" in trigger
    # The new patch does not modify completion, capture pointer/ack or alias
    # ownership; those paths remain the prior reviewed kernel contract.
    patch = PATCH.read_text()
    assert "--- a/sound/virtio/virtio_pcm_msg.c" not in patch
    assert "virtsnd_pcm_cp_pointer" not in patch
    assert "virtsnd_pcm_cp_ack" not in patch
    assert "pcm_periods_min" not in patch
    assert "cmpxchg" not in patch
    messages = (KERNEL / "sound/virtio/virtio_pcm_msg.c").read_text()
    assert "msg->length += n;" in messages
    assert "if (msg->length == period_bytes)" in messages
    assert "vss->hw_done += msg->length;" in messages
    assert messages.index("if (msg->length == period_bytes)") < messages.index(
        "rc = virtqueue_add_sgs"
    )
