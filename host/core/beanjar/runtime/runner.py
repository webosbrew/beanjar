# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""How a VM's processes run on this host: the runner (launch.Bg drives it).

  systemd   a transient systemd user service (unit.start; RUN_DIR/unit names it), the default
            where a user systemd runs
  session   a session of its own (unit.spawn; RUN_DIR/launcher.pid), without one

Either runs the same main process, `python3 -m beanjar.runtime.unit run`, with the same environment
(unit.env_for); only who holds the process tree, and how it is found and stopped, differs. Each
runner is an object with start (the VM's processes started), running (they are up) and stop (they
are ended); the stop of every runner is tried, since a VM is stopped by the runner it was started
with whatever the choice is now.

The machine's setting runner (beanjar host config set runner=session; hostconfig.py) or
BEANJAR_RUNNER, which wins, names one; auto or unset chooses systemd where a user systemd runs
(unit.available, which VM_SYSTEMD=0 turns off), else a session. A runner that is named but not
usable here is an Error at start, never another runner instead.
"""

import os
import time

from .. import base
from . import unit, vmunits
from ..base import Error

SYSTEMD, SESSION = "systemd", "session"
AUTO = "auto"
NAMES = (SYSTEMD, SESSION)
ENV = "BEANJAR_RUNNER"


def parse(value, where="runner"):
    """A runner's name as a setting gives it: one of NAMES or auto; None for empty or auto."""
    s = str(value or "").strip().lower()
    if s in ("", AUTO):
        return None
    if s not in NAMES:
        raise Error(f"{where}: {s} is not a runner (auto, {', '.join(NAMES)})", "runner")
    return s


def configured(env=None):
    """The runner BEANJAR_RUNNER, else the machine's setting, names; None for auto."""
    env = os.environ if env is None else env
    if env.get(ENV):
        return parse(env[ENV], ENV)
    from . import hostconfig

    return hostconfig.global_config().get("runner")


def choose(env=None):
    """The name of the runner a VM started now runs in: the named one (configured), checked; else
    systemd where unit.available, else a session."""
    env = os.environ if env is None else env
    named = configured(env)
    if named == SYSTEMD:
        if not unit.available(dict(env, VM_SYSTEMD="1")):
            raise Error("runner systemd: there is no usable user systemd here")
        return SYSTEMD
    if named == SESSION:
        return SESSION
    return SYSTEMD if unit.available(env) else SESSION


class Systemd:
    """The VM as the transient user service bg.unit (unit.py)."""

    name = SYSTEMD

    def start(self, bg, env, words, mem=None):
        if not unit.start(bg.unit, env, words, mem=mem, keep=bg.keep):
            raise Error(f"could not start {bg.unit}.service")
        with open(os.path.join(bg.run_dir, "unit"), "w") as f:
            f.write(bg.unit + "\n")

    def running(self, bg):
        return any(bg.unit_state(u) in ("active", "activating", "reloading") for u in bg.units())

    def stop(self, bg):
        for name in bg.units():
            st = bg.unit_state(name)
            if st in unit.UP:
                # ExecStop syncs the guest (unit.py sync), then SIGTERM, SIGKILL after 20 s.
                vmunits.systemctl("stop", f"{name}.service", timeout=120)
            elif st == "failed":
                vmunits.systemctl("reset-failed", f"{name}.service")

    def where(self, bg):
        """Where its output goes, for a start that failed."""
        return f"journalctl --user -u {bg.unit}"


class Session:
    """The VM in a session of its own (unit.spawn), its leader's pid in RUN_DIR/launcher.pid."""

    name = SESSION

    @staticmethod
    def pidfile(bg):
        return os.path.join(bg.run_dir, "launcher.pid")

    def start(self, bg, env, words, mem=None):
        with open(self.pidfile(bg), "w") as f:
            f.write(f"{unit.spawn(env, words, keep=bg.keep)}\n")

    def running(self, bg):
        return unit.alive(base.read_pid(self.pidfile(bg)))

    def stop(self, bg):
        """Sync, then the launcher's session (QEMU, the mode=mitm helpers, the bus), QEMU, the
        console bridge: SIGTERM, SIGKILL 5 s later."""
        launcher_pid = self.pidfile(bg)
        if os.path.exists(launcher_pid) or os.path.exists(bg.pidfile):
            unit.sync_guest(bg.paths)
        for f in (bg.sock + ".in.pid", launcher_pid, bg.pidfile):
            pid = base.read_pid(f)
            if pid is None:
                if os.path.exists(f):
                    os.unlink(f)
                continue
            if f == bg.pidfile and not unit.comm(pid).startswith("qemu-system"):
                os.unlink(f)
                continue
            unit.kill_session(pid)
            for _ in range(5):
                if not unit.alive(pid):
                    break
                time.sleep(1)
            unit.kill_session(pid, 9)
            try:
                os.unlink(f)
            except FileNotFoundError:
                pass

    def where(self, bg):
        return f"{bg.log_dir}/qemu.out"


RUNNERS = {SYSTEMD: Systemd(), SESSION: Session()}


def get(name):
    return RUNNERS[name]


def every():
    """Every runner, the order a stop tries them in."""
    return list(RUNNERS.values())
