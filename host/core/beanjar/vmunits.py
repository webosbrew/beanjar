# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The VMs' systemd user units: systemctl, the unit and slice names of a VM, the units
that run (vm_units), and the reason a VM runs (reason.json in its unit's runtime directory)."""

import hashlib
import json
import os
import re
import subprocess
import sys
import time

from .base import (
    Error,
)


UNIT_PROPS = ("ActiveState", "SubState", "MainPID", "Result", "ExecMainStatus", "Description")


UNIT_UP = ("active", "activating", "deactivating", "reloading")


def systemctl(*args, timeout=5):
    """`systemctl --user ARGS...`: (exit status, stdout); (None, "") without systemctl."""
    try:
        p = subprocess.run(
            ["systemctl", "--user", *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, ""
    return p.returncode, p.stdout


def linger():
    """Have logind linger the user, so that the user manager, and the VMs and services it runs,
    stay after the user's last session ends (logind lets a user set their own linger without
    authentication). False, with a warning, if it cannot; BEANJAR_LINGER=0 (the tests): leave it."""
    if os.environ.get("BEANJAR_LINGER", "1") == "0":
        return True
    uid = str(os.getuid())
    try:
        p = subprocess.run(
            ["loginctl", "show-user", uid, "--property=Linger", "--value"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5,
        )  # fmt: skip
        if p.stdout.strip() == "yes":
            return True
        p = subprocess.run(
            ["loginctl", "enable-linger", uid],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
        )  # fmt: skip
        if p.returncode == 0:
            return True
        why = p.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        why = str(e)
    print(
        f"beanjar: cannot enable lingering ({why}): the VM stops when your last session ends",
        file=sys.stderr,
    )
    return False


def unit_show(unit):
    """The unit's UNIT_PROPS as a dict ({} if systemd cannot be asked). A unit that is gone reads
    as ActiveState=inactive (the VMs' units are --collect'ed once they stop)."""
    rc, out = systemctl("show", *(f"--property={p}" for p in UNIT_PROPS), f"{unit}.service")
    if rc != 0:
        return {}
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


# Every VM's unit runs in the slice tree under ROOT_SLICE: the VM vm_dir of a library runs as
# beanjar-vm-<tag>-<name>.service in beanjar-<tag>.slice, its library's slice (library_tag); the
# regress harness's as beanjar-vm-regress-<target> (beanjar-regress.slice); a UNIT of another name in
# ROOT_SLICE itself (unit_slice). A tag has no '-': systemd reads each '-' of a slice's name as a
# level of the tree.
UNIT_PREFIX = "beanjar-vm-"


ROOT_SLICE = "beanjar.slice"


def library_tag(root):
    """The library's tag in its VMs' unit names: its directory's name (letters and digits, at most
    12, '_' between them) and 6 hex digits of its real path, so two libraries of one name differ."""
    root = os.path.realpath(root)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", os.path.basename(root))[:12].strip("_") or "library"
    return f"{slug}_{hashlib.sha256(root.encode()).hexdigest()[:6]}"


def vm_unit_name(vm_dir):
    """The unit the VM in vm_dir (<library>/vms/<name>) runs as: beanjar-vm-<library tag>-<name>."""
    vm_dir = os.path.abspath(vm_dir)
    name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(vm_dir))
    return f"{UNIT_PREFIX}{library_tag(os.path.dirname(os.path.dirname(vm_dir)))}-{name}"


def unit_tag(unit):
    """(tag, name) of a beanjar-vm-<tag>-<name> unit, else (None, unit)."""
    m = re.fullmatch(r"beanjar-vm-([A-Za-z0-9_]+)-(.+?)(?:\.service)?", unit)
    return (m[1], m[2]) if m else (None, unit.removesuffix(".service"))


def unit_slice(unit):
    """The slice a VM's unit runs in: its tag's, beanjar-<tag>.slice (in ROOT_SLICE), else ROOT_SLICE."""
    tag, _ = unit_tag(unit)
    return f"beanjar-{tag}.slice" if tag else ROOT_SLICE


# Why a VM runs (beanjar start --reason, beanjar status set): REASON_FILE in its unit's RuntimeDirectory
# (unit.start), which systemd makes when the unit starts and removes when it stops, whether by a
# stop, a crash or the host's reboot (/run is a tmpfs). So the reason lives exactly as long as the
# unit, with no code to clear it; a VM without a unit (the setsid fallback) cannot have one.
REASON_FILE = "reason.json"


REASON_MAX = 300  # characters of the text


REASON_BY_MAX = 64


def unit_runtime_dir(unit):
    """The user unit's RuntimeDirectory=UNIT (unit.start), in the user manager's runtime directory."""
    run = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.path.join(run, unit.removesuffix(".service"))


def reason_text(text, what="reason", limit=REASON_MAX):
    """text as a reason keeps it: one line, its runs of white space as one space; Error if longer
    than limit. "" for none."""
    text = " ".join(str(text or "").split())
    if len(text) > limit:
        raise Error(f"the {what} is {len(text)} characters long: at most {limit}", what)
    return text


def unit_reason(unit):
    """The reason the unit runs for ({"text", "set", "user", "by"}), None if it has none or is
    gone."""
    if not unit:
        return None
    try:
        with open(os.path.join(unit_runtime_dir(unit), REASON_FILE)) as f:
            r = json.load(f)
    except (OSError, ValueError):
        return None
    return r if isinstance(r, dict) and isinstance(r.get("text"), str) and r["text"] else None


def set_unit_reason(unit, text, by=None):
    """Set (text) or clear ("") the reason the unit runs for; the new reason, or None. The caller's
    user and the optional label by are recorded with the time. Error if the unit is not up (its
    runtime directory is gone)."""
    import pwd

    text = reason_text(text)
    by = reason_text(by, "label", REASON_BY_MAX) or None
    d = unit_runtime_dir(unit)
    if not os.path.isdir(d):
        raise Error(f"unit {unit} is not running: a reason lasts as long as the VM's unit")
    path = os.path.join(d, REASON_FILE)
    if not text:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return None
    try:
        user = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        user = str(os.getuid())
    reason = {"text": text, "set": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "user": user, "by": by}
    tmp = f"{path}.tmp{os.getpid()}"
    try:
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump(reason, f)
        os.replace(tmp, path)
    except FileNotFoundError:  # the unit stopped meanwhile, its directory with it
        raise Error(f"unit {unit} stopped: a reason lasts as long as the VM's unit") from None
    return reason


def reason_line(reason):
    """A reason as one line: its text, then who set it and when."""
    who = ", ".join(filter(None, (reason.get("user"), reason.get("by"))))
    return f"{reason['text']} (set {reason.get('set') or '?'}{' by ' + who if who else ''})"


VM_UNIT_PROPS = ("Id", "Slice", "Description", "ActiveState", "SubState", "MainPID")


def vm_units():
    """{unit name: its VM_UNIT_PROPS} of every service up in ROOT_SLICE's tree, whatever library
    (or none: the regress harness's, a UNIT of another name) it is of. BEANJAR_UNITS, a regular
    expression, keeps only the units whose name it matches: the tests' own, not the host's other VMs."""
    rc, out = systemctl("list-units", "--plain", "--no-legend", "--all", "--type=service")
    only = re.compile(os.environ["BEANJAR_UNITS"]) if os.environ.get("BEANJAR_UNITS") else None
    up = []
    for line in out.splitlines() if rc == 0 else []:
        f = line.split(None, 4)  # UNIT LOAD ACTIVE SUB DESCRIPTION
        if len(f) >= 4 and f[2] in UNIT_UP and (only is None or only.match(f[0])):
            up.append(f[0])
    if not up:
        return {}
    rc, out = systemctl("show", f"--property={','.join(VM_UNIT_PROPS)}", *up, timeout=10)
    units = {}
    for block in out.split("\n\n") if rc == 0 else []:
        props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        sl = props.get("Slice", "")
        if (
            props.get("Id")
            and props.get("ActiveState") in UNIT_UP
            and (sl == ROOT_SLICE or sl.startswith("beanjar-"))
        ):
            units[props["Id"].removesuffix(".service")] = props
    return units


def unit_run_dir(props):
    """The RUN_DIR a VM's unit runs (its description, "webOS VM <RUN_DIR>": unit.py), or None."""
    desc = props.get("Description", "")
    return desc.removeprefix("webOS VM ").strip() if desc.startswith("webOS VM ") else None
