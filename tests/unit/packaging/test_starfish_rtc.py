#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""RTC calendar, failure and flat release-component fixtures; no device access."""

import contextlib
import errno
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

TOP = Path(__file__).resolve().parents[3]
ROOTFS = TOP / "guest/layers/webos/spacebuns/rtc-rootfs"
HELPER = "usr/libexec/beanjar/starfish-global-rtc"
DROPIN = "etc/systemd/system/starfish-global.service.d/emulator-rtc.conf"
# The helper lives in a staged rootfs tree. Importing it must not add a cache
# file to the payload that the staging/publication fixture subsequently copies.
sys.dont_write_bytecode = True
loader = importlib.machinery.SourceFileLoader("starfish_rtc", str(ROOTFS / HELPER))
spec = importlib.util.spec_from_loader(loader.name, loader)
rtc = importlib.util.module_from_spec(spec)
loader.exec_module(rtc)


def calendar(year=2026, month=10, day=8, hour=10, minute=19, second=47):
    return rtc.RTC_TIME.pack(second, minute, hour, day, month - 1, year - 1900, -1, -1, -1)


def test_verified_linux_rtc_abi():
    # Compiled against the pinned Linux 6.18.53 RTC and generated ARM64
    # ioctl headers: request 0x80247009, size 36, offsets 0,4,...,32.
    assert rtc.RTC_RD_TIME == 0x80247009
    assert rtc.RTC_TIME.size == 36
    assert rtc.RTC_TIME.unpack(bytes(36)) == (0,) * 9


def read(payload):
    def ioctl(fd, request, buffer, mutate):
        assert (fd, request, mutate) == (42, rtc.RTC_RD_TIME, True)
        buffer[:] = payload
        return 0

    with (
        mock.patch.object(rtc.os, "open", return_value=42) as opened,
        mock.patch.object(rtc.os, "close") as closed,
        mock.patch.object(rtc.fcntl, "ioctl", side_effect=ioctl),
    ):
        try:
            return rtc.read_rtc()
        finally:
            opened.assert_called_once_with("/dev/rtc0", os.O_RDONLY | os.O_CLOEXEC)
            closed.assert_called_once_with(42)


@pytest.mark.parametrize(
    "payload, expected",
    [
        (calendar(), "2026-10-08 10:19:47"),
        (calendar(2024, 2, 29, 23, 59, 59), "2024-02-29 23:59:59"),
        (calendar(2027, 1, 1, 0, 0, 0), "2027-01-01 00:00:00"),
        (calendar(2000, 2, 29), "2000-02-29 10:19:47"),
    ],
    ids=["2026-10-08", "2024-02-29", "2027-01-01", "2000-02-29"],
)
def test_calendar_conversion(payload, expected):
    assert read(payload) == expected


@pytest.mark.parametrize(
    "payload",
    [
        calendar(2026, 2, 29),
        calendar(2100, 2, 29),
        calendar(month=0),
        calendar(month=13),
        calendar(day=0),
        calendar(hour=24),
        calendar(minute=60),
        calendar(second=60),
    ],
    ids=[
        "2026-02-29",
        "2100-02-29",
        "month-0",
        "month-13",
        "day-0",
        "hour-24",
        "minute-60",
        "second-60",
    ],
)
def test_invalid_calendar_is_not_normalized(payload):
    with pytest.raises(ValueError):
        read(payload)


def test_stock_mktime_range():
    assert read(calendar(1970, 1, 1, 0, 0, 0)) == "1970-01-01 00:00:00"
    assert read(calendar(2038, 1, 19, 3, 14, 7)) == "2038-01-19 03:14:07"
    for payload in (calendar(1969, 12, 31, 23, 59, 59), calendar(2038, 1, 19, 3, 14, 8)):
        with pytest.raises(ValueError, match="stock mktime range"):
            read(payload)


@pytest.mark.parametrize(
    "error",
    [
        OSError(errno.ENOENT, "RTC missing"),
        OSError(errno.EIO, "RTC read failed"),
        ValueError("invalid RTC calendar"),
    ],
    ids=["missing", "read-failed", "invalid-calendar"],
)
def test_failures_are_explicit_and_never_exec_stock(error):
    with (
        mock.patch.object(rtc, "read_rtc", side_effect=error),
        mock.patch.object(rtc.os, "execve") as execute,
        contextlib.redirect_stderr(io.StringIO()) as diagnostic,
    ):
        assert rtc.main() == 1
        execute.assert_not_called()
        assert "starfish-global-rtc: failed:" in diagnostic.getvalue()


def test_ioctl_failure_closes_descriptor():
    with (
        mock.patch.object(rtc.os, "open", return_value=42),
        mock.patch.object(rtc.os, "close") as closed,
        mock.patch.object(rtc.fcntl, "ioctl", side_effect=OSError(errno.EIO, "read")),
    ):
        with pytest.raises(OSError):
            rtc.read_rtc()
        closed.assert_called_once_with(42)


@pytest.fixture
def restore_tz():
    yield
    time.tzset()


@pytest.mark.parametrize("timezone", ["JST-9", "EST5EDT"])
def test_exec_is_stock_with_scoped_utc(timezone, restore_tz):
    with (
        mock.patch.dict(os.environ, TZ=timezone),
        mock.patch.object(rtc, "read_rtc", return_value=read(calendar())),
        mock.patch.object(rtc.os, "execve") as execute,
    ):
        rtc.main()
        binary, arguments, environment = execute.call_args.args
        assert binary == "/usr/sbin/starfish-global"
        assert arguments == [binary, "--earliest_date", "2026-10-08 10:19:47"]
        assert environment == dict(os.environ, TZ="UTC")
        assert os.environ["TZ"] == timezone
        old_tz = os.environ["TZ"]
        try:
            os.environ["TZ"] = environment["TZ"]
            time.tzset()
            assert int(time.mktime(time.strptime(arguments[2], "%Y-%m-%d %H:%M:%S"))) == 1791454787
        finally:
            os.environ["TZ"] = old_tz
            time.tzset()


def test_exec_failure_is_not_success():
    with (
        mock.patch.object(rtc, "read_rtc", return_value="2026-10-08 10:19:47"),
        mock.patch.object(rtc.os, "execve", side_effect=OSError(errno.ENOENT, "stock missing")),
        contextlib.redirect_stderr(io.StringIO()),
    ):
        assert rtc.main() == 1


def test_exec_preserves_stock_exit_status():
    # Replace only the stock executable path in a child process. A real exec
    # must propagate its status, with no wrapper success or subprocess layer.
    code = """
import importlib.machinery, os
rtc = importlib.machinery.SourceFileLoader('rtc', {helper!r}).load_module()
rtc.read_rtc = lambda: '2026-10-08 10:19:47'
real_exec = os.execve
def execute(path, argv, env):
    assert path == '/usr/sbin/starfish-global'
    assert argv == [path, '--earliest_date', '2026-10-08 10:19:47']
    assert env['TZ'] == 'UTC'
    real_exec('/bin/sh', ['/bin/sh', '-c', 'exit 23'], env)
rtc.os.execve = execute
raise SystemExit(rtc.main())
""".format(helper=str(ROOTFS / HELPER))
    assert subprocess.run([sys.executable, "-B", "-c", code], capture_output=True).returncode == 23


def test_dropin_changes_only_execstart():
    assert (
        ROOTFS / DROPIN
    ).read_text() == "[Service]\nExecStart=\nExecStart=/usr/bin/python3 /" + HELPER + "\n"
    build = (TOP / "guest/layers/webos/spacebuns/meson.build").read_text()
    assert (
        "if soc == 'o22n2'\n  webos_layer_args += ['--tree', meson.current_source_dir() / 'rtc-rootfs', '.']\nendif"
        in build
    )


def test_flat_staging_and_publication(tmp_path):
    root = tmp_path
    layer = root / "layer"
    subprocess.run(
        [
            "python3",
            str(TOP / "meson/stage.py"),
            "--out",
            str(layer),
            "--stamp",
            str(root / "stage.stamp"),
            "--depfile",
            str(root / "stage.d"),
            "--tree",
            str(ROOTFS),
            ".",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "python3",
            str(TOP / "meson/components.py"),
            "--dest",
            str(root / "components"),
            "--soc",
            "o22n2",
            "--release",
            "spacebuns",
            "--layer",
            "webos=" + str(layer),
            "--stamp",
            str(root / "published.stamp"),
        ],
        check=True,
        capture_output=True,
    )
    release = root / "components/release/spacebuns"
    selections = json.loads((release / "selections.json").read_text())
    assert list(selections) == ["o22n2-spacebuns"]
    variant = release / "variants" / selections["o22n2-spacebuns"]
    assert {str(p.relative_to(variant)) for p in variant.rglob("*") if p.is_file()} == {
        HELPER,
        DROPIN,
    }
    for relative in (HELPER, DROPIN):
        assert (variant / relative).read_bytes() == (ROOTFS / relative).read_bytes()
