# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The sound a running VM's built-in microphone hears: `beanjar io mic play NAME FILE`.

A model with LG's far-field microphone (targets.Soc.mic: lgemu's mic=1, its LGSIC capture card)
always has it: the webos.mic virtio-serial port (qemu.py), whose host side is the socket SOCK in
the VM's run directory. What arrives there is the sound around the set, 48 kHz stereo S16_LE
(guest/kernel/modules/lgemu/mic.c, "the air"): the guest's pump (guest/initramfs/src/virtio-mic.c)
writes it to lgemu, which plays it at real time into every capture of the card's microphone PCM,
hw:1,0, and holds the writer back while it is more than half a second ahead. With nothing sent the
microphone hears silence. No host microphone is ever captured.

`play` decodes a sound file (or stdin) with ffmpeg to that format, a mono file on both channels at
its own level, and sends it to the socket; the port's flow control paces it. It returns when all
of it has been sent: the last few seconds are still on their way (the socket's and the virtio
queue's buffers, about 4 s at most) and are heard after it returns."""

import os
import socket
import subprocess
import time

from .base import Error

SOCK = "mic.sock"
RATE = 48000
CHANNELS = 2
FRAME = 2 * CHANNELS
CHUNK = 64 * 1024
# a mono file on both channels at its own level (ffmpeg's own upmix lowers it by 3 dB), a stereo
# one as it is
PAN = "pan=stereo|FL=FL+FC|FR=FR+FC"


def sock_path(run_dir):
    return os.path.join(run_dir, SOCK)


def decode_command(path):
    """ffmpeg: path ("-": stdin), decoded to the air's samples on stdout."""
    stdin = path == "-"
    return ["ffmpeg", *([] if stdin else ["-nostdin"]), "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0" if stdin else path, "-map", "0:a:0", "-af", PAN, "-ar", str(RATE),
            "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"]  # fmt: skip


def need_mic(vm):
    """Error unless vm runs and its model has the microphone; the socket's path."""
    from . import targets

    if not targets.soc(vm.target()).mic:
        raise Error(
            f"VM {vm.name}: its TV model has no built-in microphone (LG's far-field voice), so "
            "there is no microphone to play a sound to"
        )
    if not vm.pid():
        raise Error(f"VM {vm.name} is not running: beanjar start {vm.name}")
    sock = sock_path(vm.run_dir)
    if not os.path.exists(sock):
        raise Error(
            f"VM {vm.name} runs without the microphone's port ({sock}): restart it with this "
            "version of beanjar"
        )
    return sock


def send(stream, sock):
    """All of stream's samples to the socket sock, in whole frames: the frames sent."""
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect(sock)
    except OSError as e:
        raise Error(
            f"the microphone's port {sock} does not take a connection: {e.strerror or e} "
            "(another play still running?)"
        ) from None
    sent = 0
    carry = b""
    with s:
        while True:
            data = stream.read(CHUNK)
            if not data:
                break
            data = carry + data
            whole = len(data) - len(data) % FRAME
            carry = data[whole:]
            try:
                s.sendall(data[:whole])
            except OSError as e:
                raise Error(f"the microphone's port closed: {e.strerror or e}") from None
            sent += whole // FRAME
    return sent


def play(vm, path, run=subprocess.Popen):
    """Plays the sound file path ("-": stdin) once to vm's microphone; returns when it has been
    sent: {name, file, seconds}."""
    sock = need_mic(vm)
    if path != "-":
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            raise Error(f"{path}: no such file")
    start = time.monotonic()
    proc = run(
        decode_command(path),
        stdin=None if path == "-" else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        frames = send(proc.stdout, sock)
    finally:
        proc.stdout.close()
        if proc.poll() is None:
            proc.terminate()
        err = proc.stderr.read().decode(errors="replace").strip()
        proc.wait()
    if proc.returncode:
        raise Error(
            f"ffmpeg could not decode {path}"
            + (f": {err.splitlines()[-1]}" if err else f" (status {proc.returncode})")
        )
    return {
        "name": vm.name,
        "file": path,
        "seconds": round(frames / RATE, 3),
        "sent_in": round(time.monotonic() - start, 3),
    }
