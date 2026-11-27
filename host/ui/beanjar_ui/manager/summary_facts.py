# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""What the VM Summary (summary_ui.py) shows, worked out without GTK: the stat tiles' texts, when a
VM last ran, which hardware rows are worth a line, and the one problem its banner names."""

import datetime
import os
import re
from collections import namedtuple

# A problem for the Summary's banner: its title, its one button's label and action (win.<name>),
# or None for none, and the tooltip (the full text when the title is shortened).
Problem = namedtuple("Problem", "kind title button action tooltip", defaults=(None, None, None))

# The hardware rows shown only when they differ from these (config.py fills audio from the
# display, settings.py: free page reporting is off by default).
FREE_PAGE_REPORTING_DEFAULT = "off"


def audio_default(display):
    """The audio backend a VM gets without audio= in its vm.toml (config.py)."""
    return "dbus" if display == "dbus" else "none"


def shown_hardware(config, display):
    """{"audio": bool, "free_page_reporting": bool}: whether the Hardware card shows each, i.e.
    whether vm.toml sets it to something other than its default ("off" and "none" are the same
    audio: no sound)."""
    audio = config.get("audio") or audio_default(display)
    default = audio_default(display)
    norm = {"off": "none"}
    return {
        "audio": norm.get(audio, audio) != norm.get(default, default),
        "free_page_reporting": (config.get("free_page_reporting") or FREE_PAGE_REPORTING_DEFAULT)
        != FREE_PAGE_REPORTING_DEFAULT,
    }


def relative_time(when, now=None):
    """when (a POSIX time) as "just now", "5 min ago", "2 h ago", "yesterday", "3 days ago", or a
    date ("2026-10-05") past four weeks; "Never" for None."""
    if when is None:
        return "Never"
    now = datetime.datetime.now().timestamp() if now is None else now
    age = max(0, now - when)
    if age < 60:
        return "just now"
    if age < 3600:
        return f"{int(age // 60)} min ago"
    if age < 86400:
        return f"{int(age // 3600)} h ago"
    days = int(age // 86400)
    if days == 1:
        return "yesterday"
    if days < 28:
        return f"{days} days ago"
    return datetime.datetime.fromtimestamp(when).strftime("%Y-%m-%d")


# The logs a run writes to while it runs (library.py VM.console_log; the launcher's qemu.out): the
# newer's time is when the VM last did something, which for a VM that is off is when it stopped.
RUN_LOGS = ("console.log", "qemu.out")


def last_run(logs_dir):
    """When the VM whose logs are in logs_dir last ran: the newest modification time of its
    console log and QEMU's output (the guest writes to its console until it stops), None if it
    never ran."""
    times = []
    for name in RUN_LOGS:
        try:
            times.append(os.stat(os.path.join(logs_dir, name)).st_mtime)
        except OSError:
            pass
    return max(times) if times else None


def fmt_gib(mib):
    """Memory as the tiles show it: "1.9 GiB", "512 MiB"."""
    if mib >= 1024:
        gib = mib / 1024
        return f"{gib:.0f} GiB" if abs(gib - round(gib)) < 0.05 else f"{gib:.1f} GiB"
    return f"{mib} MiB"


def first_sentence(text):
    """text's first sentence, capitalised, for a title: up to its first ". ", without a closing
    parenthesis of advice ("... (stop another VM, or ...)")."""
    head = re.split(r"\.\s", text, maxsplit=1)[0].rstrip(".")
    head = re.sub(r"\s+\((?:stop|raise|run|see)\b[^()]*\)$", "", head)
    return head[:1].upper() + head[1:]


FAILED_KINDS = {"start": "Start", "shut down": "Shut down", "unplug": "Unplug"}


def failure_title(kind, rc):
    """ "Start failed (exit 1)": an operation's failure as the banner says it."""
    return f"{FAILED_KINDS.get(kind, kind[:1].upper() + kind[1:])} failed (exit {rc})"


def problem(broken=None, qmp_detail="", failed=None, firmware_missing=None, blocked=None):
    """The one problem the Summary's banner shows, the most urgent first, or None:
    broken: why vm.toml does not read; qmp_detail: QEMU's run state while it runs ("paused",
    "shutdown"; "standby" is Quick Start+'s and normal); failed: (kind, exit status) of the last
    start or stop that failed; firmware_missing: the firmware's directory, when it is not there;
    blocked: why it cannot start (capacity, max_running)."""
    if broken:
        return Problem("broken", broken, "Open VM Folder", "folder")
    if qmp_detail in ("paused", "shutdown", "internal-error", "guest-panicked", "io-error"):
        title = {
            "paused": "QEMU is paused",
            "shutdown": "QEMU has shut the guest down but still runs",
            "internal-error": "QEMU stopped on an internal error",
            "guest-panicked": "The guest kernel panicked",
            "io-error": "QEMU is paused on a disk error",
        }[qmp_detail]
        return Problem("qemu", title)
    if failed:
        return Problem("failed", failure_title(*failed), "Show Output", "show-output")
    if firmware_missing:
        return Problem(
            "firmware",
            "Firmware missing: not found at " + firmware_missing,
            "Settings…",
            "settings",
        )
    if blocked:
        return Problem("blocked", first_sentence(blocked), tooltip=blocked)
    return None
