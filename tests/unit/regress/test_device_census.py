#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Counter/log parsing from source-emitted formats and BusyBox collector failures."""

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

HERE = Path(__file__).resolve().parent
TOP = HERE.parent.parent.parent
spec = importlib.util.spec_from_file_location(
    "device_census", TOP / "tests/regress/lib/device_census.py"
)
census = importlib.util.module_from_spec(spec)
spec.loader.exec_module(census)

# Formats: SOURCES points to each emitter; unsupported log comes from
# guest/kernel/modules/lgemu/drv.c:lgemu_ioctl_unhandled.
COUNTERS = {
    "mstar-ioctls": "system_lg 0x80044c02 7 not-emulated\nmpool 0x40104d00 9\n",
    "utopia": "module 20 command 1: 50\n",
    "mstar-tee": "HDCP command 7: 4, failed (first from caller)\nHDCP query 0: 3, done (first from caller)\n",
    "webos-properties": "get 0x80046f52 8 not-emulated\n",
    "rtk-ioctls": "video ioctl 0xc0405602: 3\n",
    "rtk-rpc": "program 100 procedure 5: 9 (4 bytes)\nprogram 100 procedure 6: 2 (0 bytes, not given)\n",
    "rtk-tee": "PlayReady open: 4 (0)\nPlayReady command 0x10: 3 (0xffff0008)\n",
}


def snapshot(root, tables=COUNTERS):
    (root / ".done").touch()
    (root / "dmesg.txt").write_text("")
    for name in ("var-log", "tmp-logs"):
        (root / name).mkdir()
    directory = root / "device-counters" / "outer"
    directory.mkdir(parents=True)
    inventory = []
    for key, (proc, _) in census.SOURCES.items():
        status = "available" if key in tables else "absent"
        inventory.append(f"{proc}\t{status}\t{key}.txt\n")
        if key in tables:
            (directory / f"{key}.txt").write_text(tables[key])
    (directory / "inventory.tsv").write_text("".join(inventory))


def test_counts_are_observations_not_success(tmp_path):
    root = tmp_path
    snapshot(root)
    result = census.report(root)
    assert result["status"] == "captured-evidence"
    tables = {t["path"]: t for t in result["counters"]}
    rows = tables["/proc/mstar-utopia/ioctls"]["rows"]
    assert rows[0]["not_emulated"]
    assert rows[0]["count"] == 7
    assert "PCMCIA" in rows[0]["disposition"]["meaning"]
    assert not rows[1]["not_emulated"]
    assert "unclassified" in rows[1]["interpretation"]
    rpc = tables["/proc/rtk/rpc"]["rows"]
    assert rpc[0]["response_table_entry"]
    assert not rpc[1]["response_table_entry"]
    assert tables["/proc/mstar-utopia/tee"]["rows"][0]["outcome"] == "failed"
    assert tables["/proc/rtk/tee"]["rows"][1]["result"] == "0xffff0008"
    assert result["unknown_commands"] == []


def test_live_rtk_labels_outside_safe_domain_remain_unparsed(tmp_path):
    # K6's captured ioctl table includes these labels. Preserve the evidence
    # gap rather than accepting control characters or inventing a disposition.
    root = tmp_path
    snapshot(
        root,
        {
            "rtk-ioctls": (
                "\x01 ioctl 0x80685600: 2\n"
                "add@/devices/virtual/input/input4 ioctl 0x80685600: 2\n"
                "video28 ioctl 0x80685600: 4\n"
            )
        },
    )
    result = census.report(root)
    table = next(t for t in result["counters"] if t["path"] == "/proc/rtk/ioctl")
    assert result["status"] == "partial-evidence"
    assert table["unparsed_lines"] == 2
    assert [(r["interface"], r["command"], r["count"]) for r in table["rows"]] == [
        ("video28", "0x80685600", 4)
    ]
    assert {
        "file": "device-counters/outer/rtk-ioctls.txt",
        "status": "unparsed-counter-lines",
    } in result["issues"]
    encoded = json.dumps(result)
    assert "\x01" not in encoded
    assert "\\u0001" not in encoded
    assert "add@/devices/virtual/input/input4" not in encoded


def test_explicit_log_categories_and_no_raw_line_copy(tmp_path):
    root = tmp_path
    snapshot(root, {})
    line = "mstar_utopia: system_lg: ioctl 0x80044c02 ('L' 2, 4 bytes) not emulated: 0"
    (root / "dmesg.txt").write_text(
        "\n".join(
            [
                line,
                line,
                "mstar-utopia: mpool: ioctl 0x12 not emulated: -25 (count table full)",
                'tee: session "HDCP" command 7 (from thread) not implemented: failed',
                "KADP_DE_Open : can't open DE device (/dev/lg/de0)",
                "/proc/thermalinfo/soc_temperature open failed(No such file or directory)",
                "/proc/fault_noti/kernel access Error",
                "/dev/mmcblk0p12: Can't lookup blockdev",
                "/dev/lg/sdec0: No such device or address: HAL_SDEC_Initialize fails (-1)",
                "Failed to open RFKILL control device",
                "stat /dev/no_stat failed",
                "mount /dev/no_mount failed",
                "secret_id=NOT_FOR_REPORT",
            ]
        )
    )
    result = census.report(root)
    assert result["status"] == "captured-evidence"
    assert len(result["unknown_commands"]) == 3
    assert result["unknown_commands"][0]["log_occurrences"] == 2
    assert result["unknown_commands"][0]["reported_result"] == 0
    assert {x["path"] for x in result["failed_device_opens"]} == {
        "/dev/lg/de0",
        "/proc/thermalinfo/soc_temperature",
    }
    assert {x["operation"] for x in result["logged_interface_errors"]} == {
        "access",
        "lookup",
        "device-error",
    }
    assert "NOT_FOR_REPORT" not in str(result)
    assert "/dev/rfkill" not in str(result)


def test_atv_expected_return_requires_observed_minus_25(tmp_path):
    row = census.counter_line("webos-properties", "atv 0xc0405602 2 not-emulated")
    assert row["disposition"]["observed_result"] == "not-recorded"
    root = tmp_path
    snapshot(root, {})
    (root / "dmesg.txt").write_text(
        "webos_tv: atv: ioctl 0xc0405602 not emulated: -25\n"
        "webos_tv: atv: ioctl 0x80045626 not emulated: 0\n"
    )
    records = census.report(root)["unknown_commands"]
    assert len(records) == 2
    assert records[0]["disposition"]["observed_result"] == "matches"
    assert records[1]["disposition"]["observed_result"] == "discrepancy"
    assert "not selected W22H" in records[0]["disposition"]["vendor_scope"]


def test_missing_and_bad_evidence_explicit(tmp_path):
    root = tmp_path
    result = census.report(root)
    assert result["status"] == "partial-evidence"
    assert any((t["status"] == "not-collected" for t in result["counters"]))
    snapshot(root, {"utopia": "not a counter row\n"})
    result = census.report(root)
    assert any((i["status"] == "unparsed-counter-lines" for i in result["issues"]))
    inventory = root / "device-counters/outer/inventory.tsv"
    inventory.write_text("/proc/utopia\tavailable\t../../dmesg.txt\n")
    result = census.report(root)
    assert (
        next((t for t in result["counters"] if t["path"] == "/proc/utopia"))["status"]
        == "invalid-inventory-path"
    )


def test_bounded_reproducible_and_symlinks_not_followed(tmp_path):
    root = tmp_path
    snapshot(root, {})
    (root / "var-log/link").symlink_to("/etc/passwd")
    (root / "dmesg.txt").write_text("x" * 200)
    with patch.object(census, "MAX_BYTES", 64):
        result = census.report(root)
        assert result == census.report(root)
    assert any((i["status"] == "truncated" for i in result["issues"]))
    assert not any(("passwd" in str(i) for i in result["inputs"]))
    for n in range(65):
        (root / "var-log" / str(n)).touch()
    result = census.report(root)
    assert any((i["status"] == "file-limit" for i in result["issues"]))
    assert not any((i["file"].startswith("var-log/") for i in result["inputs"]))


def test_latest_completed_snapshot_only(tmp_path):
    parent = tmp_path
    for name in ("regress-check-m1", "20261006-end", "zzz-unfinished"):
        (parent / name).mkdir()
    old = parent / "regress-check-m1/.done"
    latest = parent / "20261006-end/.done"
    old.touch()
    latest.touch()
    os.utime(old, ns=(1000000000, 1000000000))
    os.utime(latest, ns=(2000000000, 2000000000))
    selected, error = census.select_latest(parent)
    assert selected.name == "20261006-end"
    assert error is None
    os.utime(old, ns=(2000000000, 2000000000))
    assert census.select_latest(parent)[0].name == "regress-check-m1"
    with patch.object(census, "MAX_FILES", 2):
        assert census.select_latest(parent) == (None, "snapshot-selection-limit")


def test_bad_completion_metadata_is_not_silently_ignored(tmp_path):
    parent = tmp_path
    (parent / "valid").mkdir()
    (parent / "valid/.done").touch()
    (parent / "bad").mkdir()
    (parent / "bad/.done").symlink_to(parent / "valid/.done")
    assert census.select_latest(parent) == (None, "completion-metadata-invalid")
    (parent / "bad/.done").unlink()
    (parent / "bad/.done").mkdir()
    assert census.select_latest(parent) == (None, "completion-metadata-invalid")
    (parent / "bad/.done").rmdir()
    real_stat = Path.lstat

    def inaccessible(path, *args, **kwargs):
        if path == parent / "bad/.done":
            raise PermissionError("fixture denied")
        return real_stat(path, *args, **kwargs)

    with patch.object(Path, "lstat", inaccessible):
        assert census.select_latest(parent) == (None, "completion-metadata-unavailable")


BUSYBOX = pytest.mark.skipif(
    not os.access("/bin/busybox", os.X_OK), reason="no /bin/busybox (the guest's shell)"
)


def run_collector(root, style, fail=None):
    proc = root / "proc"
    proc.mkdir()
    for key, (path, _) in census.SOURCES.items():
        file = proc / path.removeprefix("/proc/")
        file.parent.mkdir(parents=True, exist_ok=True)
        if key != "rtk-tee":
            file.write_text(COUNTERS[key])
    commands = root / "bin"
    commands.mkdir()
    timeout = commands / "timeout"
    if style == "absent":
        timeout.write_text("#!/bin/sh\nexit 127\n")
    else:
        test = (
            '[ "$1" = -t ] || exit 2; shift 2'
            if style == "old"
            else '[ "$1" != -t ] || exit 2; shift'
        )
        timeout.write_text("#!/bin/sh\n" + test + '\nexec "$@"\n')
    timeout.chmod(0o755)
    cat = commands / "cat"
    cat.write_text(
        '#!/bin/sh\ncase "$1" in */utopia) [ "$FAIL_READ" != 1 ] || { echo partial; exit 1; };; esac\nexec /bin/cat "$@"\n'
    )
    cat.chmod(0o755)
    if fail == "write":
        mv = commands / "mv"
        mv.write_text("#!/bin/sh\nexit 1\n")
        mv.chmod(0o755)
    env = dict(
        os.environ,
        PATH=str(commands) + ":" + os.environ["PATH"],
        FAIL_READ="1" if fail == "read" else "0",
    )
    out = root / "out"
    subprocess.run(
        [
            "/bin/busybox",
            "sh",
            str(TOP / "guest/scripts/save-device-counters.sh"),
            str(out),
            str(proc),
        ],
        env=env,
        check=True,
    )
    records = dict(
        (fields[0], fields[1])
        for fields in (
            line.split("\t") for line in (out / "inventory.tsv").read_text().splitlines()
        )
    )
    return out, records


@BUSYBOX
@pytest.mark.parametrize("style", ["old", "modern"])
def test_old_and_modern_timeout_syntax(tmp_path, style):
    out, records = run_collector(tmp_path, style)
    assert records["/proc/utopia"] == "available"
    assert (out / "utopia.txt").read_text() == COUNTERS["utopia"]
    assert records["/proc/rtk/tee"] == "absent"


@BUSYBOX
@pytest.mark.parametrize("failure", ["read", "write"])
def test_read_and_write_failures_do_not_publish_partial_snapshot(tmp_path, failure):
    out, records = run_collector(tmp_path, "modern", failure)
    assert records["/proc/utopia"] == failure + "-failed"
    assert not (out / "utopia.txt").exists()
    assert not (out / "utopia.tmp").exists()


@BUSYBOX
def test_legacy_firmware_without_luna_does_not_reenter_namespace(tmp_path):
    root = tmp_path
    host = root / "share"
    (host / "out").mkdir(parents=True)
    (host / "guest/scripts").mkdir(parents=True)
    proc = root / "proc"
    proc.mkdir()
    (proc / "meminfo").write_text("MemTotal: 1 kB\n")
    (proc / "cmdline").write_text("fixture\n")
    logs = root / "varlog"
    logs.mkdir()
    (logs / "messages").write_text("legacy log\n")
    commands = root / "bin"
    commands.mkdir()
    for name, body in {
        "dmesg": "echo fixture-kernel",
        "ps": "echo legacy-app",
        "webos-enter": 'touch "$REENTERED"; exit 1',
        "initctl": "exit 127",
        "PmLogCtl": "exit 127",
    }.items():
        program = commands / name
        program.write_text("#!/bin/sh\n" + body + "\n")
        program.chmod(0o755)
    # Only replace fixed filesystem roots to run the actual collector flow
    # against a fixture; control flow and luna absence are unchanged.
    import re

    substitutions = {
        "/mnt/host": str(host),
        "/host": str(root / "outer"),
        "/usr/bin/luna-send": str(root / "missing-luna"),
        "/proc": str(proc),
        "/var/log": str(logs),
        "/tmp/": str(root / "tmp") + "/",
        "/run/": str(root / "run") + "/",
        "/dev/msmailbox": str(root / "missing-mailbox"),
    }
    pattern = re.compile(
        "|".join(re.escape(k) for k in sorted(substitutions, key=len, reverse=True))
    )
    program = root / "save-logs.sh"
    program.write_text(
        pattern.sub(lambda m: substitutions[m[0]], (TOP / "guest/scripts/save-logs.sh").read_text())
    )
    (host / "guest/scripts/save-device-counters.sh").symlink_to(
        TOP / "guest/scripts/save-device-counters.sh"
    )
    env = dict(
        os.environ,
        PATH=str(commands) + ":" + os.environ["PATH"],
        REENTERED=str(root / "reentered"),
    )
    subprocess.run(
        ["/bin/busybox", "sh", str(program), "legacy", "part"],
        env=env,
        check=True,
        capture_output=True,
    )
    saved = host / "out/logs/legacy"
    assert not (root / "reentered").exists()
    assert (saved / "var-log/messages").read_text() == "legacy log\n"
    assert (saved / "bootstatus-unavailable.txt").exists()
    assert (saved / "device-counters/firmware/inventory.tsv").exists()
    assert not (saved / ".done").exists()  # outer caller finalizes it


@BUSYBOX
def test_no_working_timeout_is_explicit(tmp_path):
    out, records = run_collector(tmp_path, "absent")
    assert records["/proc/utopia"] == "timeout-unavailable"
    assert not (out / "utopia.txt").exists()
