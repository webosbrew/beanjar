# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""A command for a VM of another host (HOST/NAME): the remote machine's own `beanjar ... --json`
over ssh (remote.py), its result shown here as a local command's.

Before it, the host's `beanjar --version --json` (remote.identity) is compared with this beanjar's
(compat.drift): a host that runs another commit's code, or whose guest payload is another commit's
than its beanjar, is a warning on stderr, once per command, and with --json a "drift" field in the
command's object (its result, or its error). It never stops the command."""

import json
import os
import struct
import sys
import tempfile

from .. import framing, remote
from ..core import Error
from .common import Usage, emit, kv_lines, parse_size, read_new_password, version_info, warn
from .devices import gamepad_controls, plug_usb, remote_tuner, usb_attach_usage
from .mitm import mitm_cleared
from .shell import remote_shell


def stream_to(host):
    """Prefixes a remote stderr line with the host's name."""
    return lambda line: print(f"{host}: {line}", file=sys.stderr, flush=True)


def host_drift(host):
    """compat.drift of this beanjar and host's, each item warned about on stderr; [] when host
    cannot say its version."""
    from .. import compat

    items = compat.drift(version_info(), remote.identity(host, remote.hosts()[host]), host)
    for item in items:
        warn(compat.drift_text(item))
    return items


def with_drift(data, drift):
    """data, a JSON object, with the drift items (unchanged when there are none)."""
    return dict(data, drift=drift) if drift and isinstance(data, dict) else data


def remote_failure(rc, err, drift=()):
    """A remote beanjar's failure (status 1, or 2 for a usage error) as our own Error, with the
    drift for --json's error object."""
    message = remote.remote_error(err) or f"beanjar exited with status {rc}"
    e = (Usage if rc == 2 else Error)(message)
    e.drift = list(drift)
    raise e


def json_with_drift(text, drift):
    """A remote beanjar's JSON text (its result on stdout, its error on stderr) with the drift
    items added to its object; anything else as it is."""
    if not drift:
        return text
    try:
        data = json.loads(text)
    except ValueError:
        return text
    if not isinstance(data, dict):
        return text
    return json.dumps(with_drift(data, drift), indent=2 if "\n " in text else None) + "\n"


def run_remote(a, host, words, stdin=None, timeout=None):
    """Runs words on host and relays the result: stdout as it is, stderr (a prefixed line each in
    text mode, as it comes; with --json the remote's own JSON error, after it ends), its status.
    With --json the drift (a.drift, host_drift's) goes into the object it prints."""
    as_json = a.json
    drift = getattr(a, "drift", None) or []
    rc, out, err = remote.call(
        host,
        remote.hosts()[host],
        words,
        stdin=stdin,
        on_stderr=None if as_json else stream_to(host),
        timeout=timeout,
    )
    if as_json and err:
        sys.stderr.write(json_with_drift(err, drift) if rc else err)
        sys.stderr.flush()
    if as_json and rc == 0 and drift:
        out = json_with_drift(out.decode(errors="replace"), drift).encode()
    sys.stdout.flush()
    sys.stdout.buffer.write(out)
    sys.stdout.buffer.flush()
    return rc


def remote_info(a, host, name):
    from .. import gpu

    rc, out, err = remote.call(host, remote.hosts()[host], ["show", remote.own(name), "--json"])
    if rc:
        remote_failure(rc, err, getattr(a, "drift", ()))
    try:
        data = json.loads(out)
        gpu_label = gpu.label(data["gpu"])
    except (ValueError, KeyError, TypeError):
        raise remote.HostError(
            host, "its beanjar show is not what this one reads: update it"
        ) from None
    data = {"host": host, **data}
    text = "\n".join(kv_lines(dict(data, gpu=gpu_label)))
    return emit(a, with_drift(data, getattr(a, "drift", None)), text)


def png_size(png):
    return struct.unpack(">II", png[16:24])


def remote_picture(a, host, name):
    """The screenshot transport, shared by screenshot and local element inference."""
    frame = getattr(a, "frame", None)
    parse_size(frame) if frame else None
    words = ["screenshot", remote.own(name), "-"] + (["--frame", frame] if frame else [])
    words += ["--last"] if getattr(a, "last", False) else []
    rc, png, err = remote.call(
        host,
        remote.hosts()[host],
        words,
        on_stderr=None if a.json else stream_to(host),
        timeout=120,
    )
    if rc:
        if not a.json:
            return rc, None  # its message was passed on
        remote_failure(rc, err, getattr(a, "drift", ()))
    if not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png) < 24:
        raise remote.HostError(
            host,
            "its beanjar sent no picture: it predates `screenshot NAME -`, update beanjar on the host "
            "(an older one wrote a file named - in its working directory)",
        )
    return 0, png


def remote_screenshot(a, host, name):
    if a.json and a.file == "-":
        raise Usage("screenshot NAME - prints the PNG on stdout: it takes no --json")
    rc, png = remote_picture(a, host, name)
    if rc:
        return rc
    if a.file == "-":
        sys.stdout.buffer.write(png)
        sys.stdout.buffer.flush()
        return 0
    path = os.path.abspath(a.file)
    folder = os.path.dirname(path)
    if not os.path.isdir(folder):
        raise Error(f"{folder}: no such directory")
    with tempfile.TemporaryDirectory(prefix="beanjar-shot-", dir=folder) as tmp:
        part = os.path.join(tmp, "shot.png")
        with open(part, "wb") as f:
            f.write(png)
        os.replace(part, path)
    w, h = png_size(png)
    data = {"name": name, "host": host, "file": path, "width": w, "height": h, "taken_by": None}
    data |= framing.frame_geometry((w, h), framing.png_source(png))
    return emit(a, with_drift(data, getattr(a, "drift", None)), path)


def remote_password(a, host, name):
    """vnc password set NAME: the new password is read here (FILE, stdin, a prompt) and goes to the
    remote beanjar on its stdin, never in an argument."""
    password = read_new_password(a.file)
    words = ["vnc", "password", "set", remote.own(name)] + (["--json"] if a.json else [])
    return run_remote(a, host, words, stdin=(password + "\n").encode())


def bare_new_name(a):
    """rename's NEW: a bare name, the VM's new name on its own host (a VM never moves)."""
    if "/" in a.new:
        raise Usage(
            f"rename NAME NEW: NEW is a bare name, the VM keeps its host ({a.new!r}; to copy a VM to "
            "another host: beanjar clone)"
        )


def remote_command(a, words, host, name):
    """The command a on the VM called name on host, run there by that host's beanjar; words is
    the command line without its global options (parser.parse)."""
    kind = a.remote
    if kind == "viewer":
        raise Error(
            f"the viewer is a window on the machine that runs the VM, and {name} runs on {host}: "
            f"beanjar vnc start {host}/{name} serves its picture over VNC (its lan address is in "
            f"beanjar show {host}/{name}; the password: beanjar vnc password show {host}/{name})"
        )
    if kind == "authkeys-add":
        raise Error(
            f"add --mine adds this machine's ~/.ssh/*.pub: run it on {host} for {name}, or add "
            f"the keys to {name}'s identity/authorized_keys there"
        )
    if kind == "mic-play":
        raise Error(
            f"io mic play plays a file on this machine, and {name} runs on {host}: run it on {host}"
        )
    if kind == "keymodel-set":
        raise Error(f"key model set reads a file on this machine: run it on {host} for {name}")
    # what this machine can refuse is refused before the host is asked anything, its version included
    if kind == "usb-attach":
        usb_attach_usage(a, host)
    if kind == "gamepad-press":
        gamepad_controls(a)
    if kind not in ("pipe", "mitm-connect", "mitm-clear"):
        # not for a byte stream, often a loop's step, nor the inspector, which runs here
        a.drift = host_drift(host)
    if kind == "show":
        return remote_info(a, host, name)
    if kind == "rename":
        bare_new_name(a)
    nwords = len(a.path)
    if kind == "pipe":
        # a byte stream both ways (vnc pipe, remote pipe): this process's stdio, unbuffered
        return remote.attach(
            host, remote.hosts()[host], remote.rewrite(words, nwords, a.name, name)
        )
    if kind == "mitm-connect":
        from .. import inspector

        return inspector.connect_remote(host, name)
    if kind == "mitm-clear":
        from .. import inspector

        return mitm_cleared(a, inspector.clear_remote(host, name))
    if kind == "screenshot":
        return remote_screenshot(a, host, name)
    if kind == "usb-attach":
        if a.device or a.camera:
            a.name = name
            return plug_usb(None, a, host)
    if kind == "tuner":
        return remote_tuner(a, host, name)
    if kind == "shell":
        return remote_shell(a, host, name)
    if kind == "password-set":
        return remote_password(a, host, name)
    words = remote.rewrite(words, nwords, a.name, name)
    if a.json:
        # at the end, but before a `--`: what follows it is the command's own (start's kernel
        # words, beanjar-vnc's options)
        words.insert(words.index("--") if "--" in words else len(words), "--json")
    return run_remote(a, host, words)
