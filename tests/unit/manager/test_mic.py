#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""io mic play: a model without the microphone refused, the file decoded by ffmpeg to the air's
format (48 kHz stereo S16_LE, a mono file on both channels at its own level) and sent to the
VM's mic.sock in whole frames."""

import io
import math
import os
import shutil
import socket
import struct
import threading
import types
import wave

import pytest
from beanjar.base import Error
from beanjar.devices import mic


def fake_vm(tmp_path, target, pid=1234):
    return types.SimpleNamespace(
        name="tv1", run_dir=str(tmp_path), target=lambda: target, pid=lambda: pid
    )


def listen(path):
    """A server on path that collects what one client sends: (thread, bytes holder)."""
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(path)
    srv.listen(1)
    got = bytearray()

    def run():
        conn, _ = srv.accept()
        with conn:
            while data := conn.recv(65536):
                got.extend(data)
        srv.close()

    t = threading.Thread(target=run)
    t.start()
    return t, got


def test_model_without_mic(tmp_path):
    with pytest.raises(Error, match="no built-in microphone"):
        mic.play(fake_vm(tmp_path, ("k8hp", "ombre")), "x.wav")


def test_not_running(tmp_path):
    with pytest.raises(Error, match="not running"):
        mic.play(fake_vm(tmp_path, ("o22", "queue"), pid=None), "x.wav")


def test_no_port(tmp_path):
    with pytest.raises(Error, match="without the microphone's port"):
        mic.play(fake_vm(tmp_path, ("o22", "queue")), "x.wav")


def test_send_whole_frames(tmp_path):
    """A stream that ends inside a frame: the frames before it are sent, the rest dropped."""
    sock = str(tmp_path / mic.SOCK)
    t, got = listen(sock)
    frames = mic.send(io.BytesIO(bytes(range(10)) * 3), sock)  # 30 bytes: 7 frames and 2 bytes
    t.join()
    assert frames == 7 and bytes(got) == (bytes(range(10)) * 3)[:28]


def test_decode_command():
    cmd = mic.decode_command("/a/b.flac")
    assert cmd[cmd.index("-i") + 1] == "/a/b.flac" and "-nostdin" in cmd
    assert cmd[cmd.index("-ar") + 1] == "48000" and cmd[-3:] == ["-f", "s16le", "pipe:1"]
    stdin = mic.decode_command("-")
    assert stdin[stdin.index("-i") + 1] == "pipe:0" and "-nostdin" not in stdin


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_play_mono_file(tmp_path):
    """A 16 kHz mono tone, 0.5 s: 24000 frames at 48 kHz, both channels the tone at its level."""
    src = tmp_path / "tone.wav"
    with wave.open(str(src), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(
            b"".join(
                struct.pack("<h", round(16000 * math.sin(2 * math.pi * 440 * i / 16000)))
                for i in range(8000)
            )
        )
    t, got = listen(str(tmp_path / mic.SOCK))
    data = mic.play(fake_vm(tmp_path, ("o22", "queue"), pid=os.getpid()), str(src))
    t.join()
    assert data["name"] == "tv1" and data["file"] == str(src)
    n = len(got) // 4
    assert abs(n - 24000) <= 64 and data["seconds"] == round(n / 48000, 3)
    samples = struct.unpack(f"<{2 * n}h", bytes(got[: 4 * n]))
    left, right = samples[0::2], samples[1::2]
    assert left == right
    assert 15500 <= max(left) <= 16500  # its own level, not 3 dB down


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_play_not_audio(tmp_path):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not a sound file")
    t, _ = listen(str(tmp_path / mic.SOCK))
    with pytest.raises(Error, match="ffmpeg could not decode"):
        mic.play(fake_vm(tmp_path, ("o22", "queue")), str(bad))
    t.join()
