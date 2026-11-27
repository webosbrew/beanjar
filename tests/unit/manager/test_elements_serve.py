#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""beanjar.display.elements through the recognition server (host/tools/textshot-serve), against a stand-in
server: started once and shared, its errors passed on, the CLI when it cannot start or is turned
off, and the CUDA wheel's libraries on the loader path. No models, VM or network."""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from unittest import mock

import pytest

from beanjar.display import elements

TOP = Path(__file__).resolve().parents[3]
# A 640 x 240 PNG header: the stand-in server never decodes the picture, only elements.read() checks
# the signature and size
FIXTURE = Path()
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + (640).to_bytes(4, "big") + (240).to_bytes(4, "big")


@pytest.fixture(autouse=True)
def _fixture_png(tmp_path_factory):
    global FIXTURE
    FIXTURE = tmp_path_factory.mktemp("png") / "continue.png"
    FIXTURE.write_bytes(PNG)


DATA = json.loads((Path(__file__).parent / "data" / "elements.json").read_text())

# A stand-in textshot-serve: the real one's flags and protocol; each start appends a line to
# $FAKE_STARTS; an image named "*bad*" is an error; FAKE_FAIL=1 exits at once.
FAKE = textwrap.dedent(
    f"""\
    #!{sys.executable}
    import json, os, socket, sys
    if os.environ.get("FAKE_FAIL"):
        sys.exit(1)
    a = sys.argv[1:]
    path = a[a.index("-socket") + 1]
    with open(os.environ["FAKE_STARTS"], "a") as f:
        f.write(" ".join(a) + "\\n")
    data = json.loads(os.environ["FAKE_DATA"])
    s = socket.socket(socket.AF_UNIX)
    s.bind(path + ".part"); s.listen(4); os.rename(path + ".part", path)
    s.settimeout(10)
    try:
        while True:
            c, _ = s.accept()
            f = c.makefile("rw")
            for line in f:
                req = json.loads(line)
                if "bad" in req["image"]:
                    rep = {{"error": req["image"] + ": decode: broken"}}
                else:
                    rep = {{"elements": [dict(e, file=req["image"]) for e in data]}}
                f.write(json.dumps(rep) + "\\n"); f.flush()
            c.close()
    except TimeoutError:
        os.unlink(path)
    """
)


@pytest.fixture
def server(tmp_path, monkeypatch):
    fake = tmp_path / "textshot-serve"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    starts = tmp_path / "starts"
    monkeypatch.setenv("BEANJAR_TEXTSHOT_SERVE", str(fake))
    run = tempfile.mkdtemp(prefix="bj-")  # short: a socket's path is at most 107 bytes
    monkeypatch.setenv("XDG_RUNTIME_DIR", run)
    monkeypatch.setenv("FAKE_STARTS", str(starts))
    monkeypatch.setenv("FAKE_DATA", json.dumps(DATA))
    yield starts
    # stop what the tests started
    for pid in subprocess.run(
        ["pgrep", "-f", "-x", f"{sys.executable} {fake} .*"], capture_output=True, text=True
    ).stdout.split():
        os.kill(int(pid), signal.SIGTERM)
    shutil.rmtree(run, ignore_errors=True)


def no_cli(*_a, **_k):
    raise AssertionError("the CLI ran")


def test_server_started_once_and_shared(server):
    notes = []
    with mock.patch.object(elements, "run_cli", no_cli):
        first = elements.read(FIXTURE, "/bin/true", out=notes.append)
        second = elements.read(FIXTURE, "/bin/true")
    want = elements.frame([dict(e, file=str(FIXTURE)) for e in DATA], 640, 240)
    assert first == second == want
    starts = server.read_text().splitlines()
    assert len(starts) == 1 and "-threads 4" in starts[0] and "-idle 5m" in starts[0]
    assert any("starting the recognition server" in n for n in notes)


def test_server_error_is_the_error(server, tmp_path):
    bad = tmp_path / "bad.png"
    bad.write_bytes(FIXTURE.read_bytes())
    with (
        mock.patch.object(elements, "run_cli", no_cli),
        pytest.raises(elements.Error, match="decode: broken"),
    ):
        elements.read(bad, "/bin/true")


def test_cli_when_the_server_cannot_start(server, monkeypatch):
    monkeypatch.setenv("FAKE_FAIL", "1")
    notes = []
    with mock.patch.object(elements, "run_cli", return_value=[]) as cli:
        data = elements.read(FIXTURE, "/bin/true", out=notes.append)
    assert cli.called and data["elements"] == []
    assert any("one textshot process" in n for n in notes)


def test_server_off_or_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("BEANJAR_TEXTSHOT_SERVE", "none")
    assert elements.serve_executable() is None
    monkeypatch.setenv("BEANJAR_TEXTSHOT_SERVE", str(tmp_path / "missing"))
    assert elements.serve_executable() is None
    with mock.patch.object(elements, "run_cli", return_value=[]) as cli:
        elements.read(FIXTURE, "/bin/true")
    assert cli.called


def test_socket_per_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    cpu = elements.serve_socket("/x/textshot-serve", {})
    gpu = elements.serve_socket("/x/textshot-serve", {"TEXTSHOT_ONNXRUNTIME_LIB": "/gpu.so"})
    assert cpu != gpu and os.path.dirname(cpu) == os.path.dirname(gpu)
    assert os.stat(os.path.dirname(cpu)).st_mode & 0o777 == 0o700


def test_cuda_wheel_libraries_on_the_loader_path(monkeypatch, tmp_path):
    site = tmp_path / "venv/lib/python3/site-packages"
    capi = site / "onnxruntime/capi"
    for d in (capi, site / "nvidia/cudnn/lib", site / "nvidia/cu13/lib"):
        d.mkdir(parents=True)
    lib = capi / "libonnxruntime.so.1.30.0"
    lib.write_bytes(b"")
    monkeypatch.setenv("TEXTSHOT_ONNXRUNTIME_LIB", str(lib))
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/x")
    env = elements.textshot_env()
    assert env["LD_LIBRARY_PATH"].split(":") == [
        "/opt/x",
        str(capi),
        str(site / "nvidia/cu13/lib"),
        str(site / "nvidia/cudnn/lib"),
    ]
    monkeypatch.setenv("TEXTSHOT_ONNXRUNTIME_LIB", "/usr/lib/libonnxruntime.so")
    assert elements.textshot_env()["LD_LIBRARY_PATH"] == "/opt/x"


def test_ready_server_answers_fast(server):
    elements.read(FIXTURE, "/bin/true")  # started
    t = time.monotonic()
    with mock.patch.object(elements, "run_cli", no_cli):
        elements.read(FIXTURE, "/bin/true")
    assert time.monotonic() - t < 2
