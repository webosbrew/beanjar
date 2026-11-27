# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Native screensaver control, verified on webOS 10.3.1 (no viewer or root SSH).

Completion comes from tvpowerd and SAM's real foreground window types. The builtin
fallback recovers the Ready state left by a failed video ad; it does not simulate
power notifications or change the ad/privacy configuration.
"""

import base64
import binascii
import fcntl
import json
import math
import os
import re
import secrets
import shlex
import time

from .. import base, paths
from ..runtime import poweroff
from ..access import console
from ..base import Error

POWER = "luna://com.webos.service.tvpower/power2/"
SAM = "luna://com.webos.applicationManager/"
ACTOR = "com.webos.applicationManager"
BUILTIN = "com.webos.app.screensaver"
VIDEOADS = "com.webos.app.videoads"
WINDOW = "_WEBOS_WINDOW_TYPE_SCREENSAVER"
READY = "screen saver ready"
FALLBACK_DELAY = 5.0
DEFAULT_TIMEOUT = 60


def timeout_value(value):
    """A finite CLI/API timeout, in seconds."""
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        raise ValueError("timeout must be 5 to 300 seconds") from None
    if not math.isfinite(seconds) or not 5 <= seconds <= 300:
        raise ValueError("timeout must be 5 to 300 seconds")
    return seconds


def parse_frame(output, nonce, label="screensaver"):
    """Only our complete, nonce-framed response counts; never parse serial log JSON."""
    begin, end = f"BJSS-{nonce}|", f"|END-{nonce}"
    frames = [line for line in output.splitlines() if line.startswith(begin)]
    if len(frames) != 1 or not frames[0].endswith(end):
        raise Error(f"{label}: missing, truncated or duplicate console response frame")
    body = frames[0][len(begin) : -len(end)]
    try:
        rc, encoded = body.split("|", 1)
        if not re.fullmatch(r"[0-9]+", rc):
            raise ValueError("invalid exit status")
        raw = base64.b64decode(encoded, validate=True).decode("utf-8")
        if int(rc):
            raise Error(f"{label}: native call exited with status {rc}: {raw.strip()}")
        data = json.loads(raw)
    except (ValueError, UnicodeError, binascii.Error) as error:
        raise Error(f"{label}: invalid console response: {error}") from None
    if not isinstance(data, dict) or data.get("returnValue") is not True:
        raise Error(f"{label}: native API failed: {data!r}")
    return data


def require_running(vm):
    """Unlike power_off_refusal, a pending power-off is never ready for this helper."""
    state, detail = vm.state()
    pid = vm.pid()
    if state != base.RUNNING or detail or not pid:
        raise Error(f"VM {vm.name}: screensaver requires a running webOS VM ({state} {detail})")
    if base.read_pid(vm.power_off_file) == pid:
        raise Error(f"VM {vm.name}: power-off is pending")
    why = vm.power_off_refusal()
    if why:
        raise Error(f"VM {vm.name}: cannot control screensaver: {why}")
    # power_off_refusal permits a busy/unavailable QMP connection; fail closed here.
    status = poweroff.qmp_status(vm.qmp_sock)
    if status != "running":
        raise Error(f"VM {vm.name}: QEMU is {status or 'unavailable'}")


class Native:
    """Luna calls inside the running guest over its console (luna-send as actor), each answer
    framed and checked (parse_frame); label names the caller in errors. Also rotation.py's."""

    def __init__(self, vm, deadline, actor=ACTOR, label="screensaver"):
        self.vm = vm
        self.deadline = deadline
        self.actor = actor
        self.label = label

    def call(self, uri, payload):
        remaining = self.deadline - time.monotonic()
        # Reserve one second for console bridge setup and one for framing/draining.
        seconds = min(5, int(remaining - 2))
        if seconds < 1:
            raise Error(f"{self.label}: timed out waiting for native completion")
        nonce = secrets.token_hex(16)
        argv = [
            "luna-send",
            "-n",
            "1",
            *(("-a", self.actor) if self.actor else ()),
            uri,
            json.dumps(payload, separators=(",", ":")),
        ]
        # Encode inside webOS: initramfs busybox has no base64. Split marker pieces
        # in the typed command so its terminal echo cannot look like a response.
        # busybox's timeout takes -t before busybox 1.30 (webOS 6 and older), the plain number later
        script = (
            'if timeout -t 1 true 2>/dev/null; then t="timeout -s KILL -t"; '
            'else t="timeout -s KILL"; fi; '
            f"reply=$($t {seconds} {shlex.join(argv)} </dev/null 2>&1); rc=$?; "
            "encoded=$(printf '%s' \"$reply\" | /bin/base64 | tr -d '\\n'); "
            "printf '\\n%s%s|%s|%s|%s%s\\n' "
            f'BJSS- {shlex.quote(nonce)} "$rc" "$encoded" END- {shlex.quote(nonce)}'
        )
        line = f"webos-enter sh -c {shlex.quote(script)} </dev/null"
        try:
            output = console.send(
                line,
                wait=seconds + 1,
                sock=os.path.join(self.vm.run_dir, "console.sock"),
                log=self.vm.console_log,
                tree=paths.Tree(self.vm.library.repo),
            )
        except OSError as error:
            raise Error(f"{self.label}: console transport failed: {error}") from None
        return parse_frame(output, nonce, self.label)


def snapshot(native, waking=False):
    power = native.call(POWER + "getPowerState", {})
    state, sub, processing = power.get("state"), power.get("sub state"), power.get("sub processing")
    if state != "ACTIVE" or sub not in ("active", "screen saver"):
        raise Error(f"screensaver: unsupported native power state: {power!r}")
    # fallback-1-dismiss records this exact report during our accepted wake.
    # MachineOff is an FSM event in completion-contract, not a reported state.
    own_screen_on = waking and sub == "screen saver" and processing == "screen on"
    if processing not in (None, "", READY) and not own_screen_on:
        raise Error(f"screensaver: unrelated native power transition: {power!r}")
    foreground = native.call(SAM + "getForegroundApps", {"extraInfo": True}).get("foregroundApps")
    if not isinstance(foreground, list) or any(
        not isinstance(app, dict)
        or not isinstance(app.get("appId"), str)
        or not isinstance(app.get("windowType"), str)
        for app in foreground
    ):
        raise Error("screensaver: unsupported SAM foreground window schema")
    windows = [app["appId"] for app in foreground if app["windowType"] == WINDOW]
    return power, sub, processing, windows


def control(vm, action, timeout=DEFAULT_TIMEOUT):
    """Start/stop and verify native completion within one shared deadline."""
    if action not in ("start", "stop"):
        raise ValueError(action)
    timeout = timeout_value(timeout)
    deadline = time.monotonic() + timeout
    require_running(vm)
    # Serialize our helpers, including the gap between a launch reply and its
    # actual window. Leave the lock inode in run/ so another opener cannot bypass it.
    try:
        lock = open(os.path.join(vm.run_dir, "screensaver.lock"), "a")
    except OSError as error:
        raise Error(f"screensaver: cannot open operation lock: {error}") from None
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Error(f"VM {vm.name}: another screensaver command is in progress") from None
        except OSError as error:
            raise Error(f"screensaver: cannot lock operation: {error}") from None
        return _control(vm, action, deadline)


def _control(vm, action, deadline):
    native = Native(vm, deadline)
    changed = fallback = requested = waking = closing = False
    withdrawn = False
    ready_since = None
    while True:
        power, sub, processing, windows = snapshot(native, waking=waking)
        active = sub == "active" and not processing
        saver = sub == "screen saver" and not processing
        other_saver = any(app != BUILTIN for app in windows)
        # Another actor can produce a real window after the last pre-launch
        # query. Do not report success while our own fallback is still in flight.
        # Once visible, normally close only our builtin and retain the real saver.
        if (
            action == "start"
            and fallback
            and saver
            and other_saver
            and BUILTIN in windows
            and not withdrawn
        ):
            native.call(SAM + "closeByAppId", {"id": BUILTIN})
            withdrawn = True
        fallback_complete = True
        if fallback:
            fallback_complete = (
                BUILTIN not in windows if withdrawn else BUILTIN in windows and not other_saver
            )
        start_done = action == "start" and saver and windows and fallback_complete
        stop_done = action == "stop" and active and not windows
        if start_done or stop_done:
            return {
                "action": action,
                "changed": changed,
                "fallback": fallback,
                "power": power,
                "screensaver_windows": windows,
            }

        if action == "start" and not requested and active and not windows:
            native.call(POWER + "turnOnScreenSaver", {})
            changed = requested = True
        elif action == "stop" and not waking and (saver or (active and windows)):
            native.call(POWER + "turnOnScreen", {"reason": "remoteKey"})
            changed = waking = True
        elif action == "stop" and waking and active and BUILTIN in windows and not closing:
            native.call(SAM + "closeByAppId", {"id": BUILTIN})
            changed = closing = True

        if processing == READY:
            if ready_since is None:
                ready_since = time.monotonic()
            if not windows and not fallback and time.monotonic() - ready_since >= FALLBACK_DELAY:
                running = native.call(SAM + "running", {}).get("running")
                if not isinstance(running, list) or any(
                    not isinstance(app, dict) or not isinstance(app.get("id"), str)
                    for app in running
                ):
                    raise Error("screensaver: unsupported SAM running-app schema")
                if not any(app["id"] in (VIDEOADS, BUILTIN) for app in running):
                    # running() is another round trip: native power or a real
                    # foreground window may have changed since the first query.
                    _, fresh_sub, fresh_processing, fresh_windows = snapshot(native)
                    if fresh_sub == "active" and fresh_processing == READY and not fresh_windows:
                        native.call(SAM + "launch", {"id": BUILTIN})
                        changed = fallback = True
        else:
            ready_since = None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Error("screensaver: timed out waiting for native completion")
        time.sleep(min(0.25, remaining))
