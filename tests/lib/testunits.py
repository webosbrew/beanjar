# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The transient user units the manager tests start (fake-vm, and the real launcher in
tests/integration/launch/test_launcher.py), named after the test process: beanjar-vm-test<pid>-<vm> (fake-vm, in
beanjar-test<pid>.slice), or beanjar-vm-test<pid>_<hash>-<vm> (the real launcher's name for a VM of a
library directory named test<pid>, core.vm_unit_name). They are stopped and reset however
a test ends - its tearDown, the process's exit, or a SIGTERM/SIGHUP/SIGINT (meson's timeout, a
Ctrl-C), which skip tearDown - and those of an earlier test process that is gone swept at the start,
so no test run leaves one behind."""

import atexit
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time

PREFIX = f"beanjar-vm-test{os.getpid()}-"
OURS = re.compile(rf"beanjar-vm-test{os.getpid()}[-_]")
OWNER = re.compile(r"beanjar-vm-test(\d+)[-_]")
_cleanups = []


def _systemctl(*args, timeout=60):
    try:
        return subprocess.run(
            ["systemctl", "--user", *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def units():
    """{name: description} of every beanjar-vm-test* unit systemd knows, whatever its state."""
    p = _systemctl(
        "list-units", "--all", "--plain", "--no-legend", "beanjar-vm-test*.service", timeout=10
    )
    out = {}
    for line in p.stdout.splitlines() if p and p.returncode == 0 else []:
        f = line.split(None, 4)  # UNIT LOAD ACTIVE SUB DESCRIPTION
        if f:
            out[f[0].removesuffix(".service")] = f[4] if len(f) > 4 else ""
    return out


def slices():
    """The beanjar-test* slices systemd has (a test's units' slices outlive them: unit.py)."""
    p = _systemctl(
        "list-units", "--all", "--plain", "--no-legend", "beanjar-test*.slice", timeout=10
    )
    return [
        line.split()[0]
        for line in (p.stdout.splitlines() if p and p.returncode == 0 else [])
        if line.split()
    ]


def _gone(pid):
    try:
        os.kill(int(pid), 0)
        return False
    except ProcessLookupError:
        return True
    except PermissionError:
        return False


def _running(pid):
    """pid exists and is not a zombie."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


def end(pid, group=False, timeout=5):
    """Stop a process a test left (group: the session it leads): SIGTERM, and SIGKILL only if it
    is still there after timeout seconds, so that it exits by itself where it can (a coverage
    build's counters, gcov's and coverage.py's, are written at exit)."""
    send = os.killpg if group else os.kill
    try:
        send(pid, signal.SIGTERM)
    except OSError:
        return
    end_at = time.monotonic() + timeout
    while _running(pid) and time.monotonic() < end_at:
        time.sleep(0.05)
    if _running(pid):
        try:
            send(pid, signal.SIGKILL)
        except OSError:
            pass


def stop_slices(ours_only=True):
    """Stop this process's slices, or (ours_only False) also those of test processes that are gone."""
    for s in slices():
        m = re.match(r"beanjar-test(\d+)[._]", s)
        if m and (int(m[1]) == os.getpid() or not ours_only and _gone(m[1])):
            _systemctl("stop", s, timeout=10)


def stop(names):
    for n in names:
        _systemctl("stop", f"{n}.service")
        _systemctl("reset-failed", f"{n}.service", timeout=10)


def stop_ours():
    stop([n for n in units() if OURS.match(n)])
    stop_slices()


def _stale(name, desc):
    """A unit of a test (its run dir, "webOS VM <run dir>", in a beanjar-* temporary directory) whose
    test process has exited, or whose directory is gone."""
    run_dir = desc.removeprefix("webOS VM ").strip()
    tmp = tempfile.gettempdir().rstrip("/") + "/beanjar-"
    if not desc.startswith("webOS VM ") or not run_dir.startswith(tmp):
        return False
    if not os.path.isdir(run_dir):
        return True
    m = OWNER.match(name)
    return bool(m) and _gone(m.group(1))


# The host's firmware collections (its config.toml's firmware_dirs), read before isolate() hides
# them: where tests that need a real firmware look for it.
HOST_FIRMWARE_DIRS = []


def host_firmware(name):
    """The directory of the host's firmware called name, or None."""
    return next(
        (os.path.join(d, name) for d in HOST_FIRMWARE_DIRS if os.path.isdir(os.path.join(d, name))),
        None,
    )


def isolate():
    """Keep the host out of the test: the VMs running on it (core.vm_units sees only this process's
    units: BEANJAR_UNITS), the user's linger (BEANJAR_LINGER=0: core.linger leaves it) and the
    machine's settings, max_running (XDG_CONFIG_HOME: an empty directory of the test's own, removed
    at exit), its firmware collections kept in HOST_FIRMWARE_DIRS first. Subprocesses get these
    from os.environ."""
    from beanjar.runtime import hostconfig

    if not HOST_FIRMWARE_DIRS:
        try:
            HOST_FIRMWARE_DIRS[:] = hostconfig.firmware_dirs()
        except ValueError:
            pass
    os.environ["BEANJAR_UNITS"] = OURS.pattern
    os.environ["BEANJAR_LINGER"] = "0"
    config = tempfile.mkdtemp(prefix="beanjar-config-")
    os.environ["XDG_CONFIG_HOME"] = config
    atexit.register(shutil.rmtree, config, ignore_errors=True)


def install(cleanup=None):
    """Isolate the test from the host (isolate), sweep the stale units, and stop ours (after
    cleanup(), if given) at exit and on a signal."""
    isolate()
    stop([n for n, d in units().items() if _stale(n, d)])
    stop_slices(ours_only=False)
    if cleanup:
        _cleanups.append(cleanup)

    def at_exit():
        for c in _cleanups:
            try:
                c()
            except Exception:
                pass
        stop_ours()

    atexit.register(at_exit)

    def on_signal(sig, _frame):
        for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(s, signal.SIG_IGN)
        at_exit()
        os._exit(128 + sig)

    for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(s, on_signal)
