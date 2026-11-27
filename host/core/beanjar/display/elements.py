# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Pixel elements from the optional, externally installed go-textshot CLI, or from the recognition
server built on the same go-textshot (host/tools/textshot-serve), which keeps the models loaded."""

import json
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile

from ..base import Error

INSTALL_HINT = "Install it with: go install github.com/devcell-sh/go-textshot/cmd/textshot@latest"


def executable():
    """Resolve the user's textshot before capturing a picture or contacting a host."""
    name = os.environ.get("BEANJAR_TEXTSHOT") or "textshot"
    binary = shutil.which(name)
    if binary is None:
        raise Error("go-textshot is not installed\n" + INSTALL_HINT)
    return os.path.abspath(binary)


def read(image, binary=None, out=None):
    """Read one PNG on this machine: through the recognition server (serve_executable) when there
    is one, which keeps the models loaded, else with one textshot process for this image. Both
    give the CLI's -json elements. Upstream owns its cache, runtime and model downloads."""
    binary = binary or executable()
    image = os.path.abspath(image)
    try:
        with open(image, "rb") as f:
            header = f.read(24)
    except OSError as e:
        raise Error(f"go-textshot: {e}") from None
    if len(header) != 24 or header[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR":
        raise Error(f"{image}: not a PNG")
    width, height = struct.unpack(">II", header[16:24])
    if not width or not height:
        raise Error(f"{image}: invalid PNG size")
    env = textshot_env()
    data = served(image, env, out)
    if data is None:
        data = run_cli(binary, image, env, out)
    try:
        return frame(data, width, height)
    except (ValueError, TypeError, KeyError) as e:
        raise Error(f"go-textshot returned invalid element JSON: {e}") from None


THREADS = 4  # inference threads, the CLI's and the server's


def textshot_env():
    """The environment of textshot and the server. Explicit relative cache and library paths are
    resolved (both run in a scratch directory). With TEXTSHOT_ONNXRUNTIME_LIB in a pip
    onnxruntime-gpu wheel (site-packages/onnxruntime/capi), the wheel's CUDA and cuDNN libraries
    (site-packages/nvidia/*/lib) go on the loader path, where its CUDA provider looks for them."""
    env = dict(os.environ)
    for key in ("TEXTSHOT_CACHE_DIR", "TEXTSHOT_ONNXRUNTIME_LIB"):
        if env.get(key):
            env[key] = os.path.abspath(env[key])
    # an empty entry is the working directory, for the loader
    loader = env.get("LD_LIBRARY_PATH")
    paths = [os.path.abspath(p or ".") for p in loader.split(":")] if loader else []
    lib = env.get("TEXTSHOT_ONNXRUNTIME_LIB")
    if lib:
        capi = lib if os.path.isdir(lib) else os.path.dirname(lib)
        site = os.path.dirname(os.path.dirname(capi))
        if (
            os.path.basename(capi) == "capi"
            and os.path.basename(os.path.dirname(capi)) == "onnxruntime"
        ):
            import glob

            for d in [capi] + sorted(glob.glob(os.path.join(site, "nvidia", "*", "lib"))):
                if d not in paths:
                    paths.append(d)
    if paths:
        env["LD_LIBRARY_PATH"] = ":".join(paths)
    return env


def run_cli(binary, image, env, out=None):
    """The CLI's -json elements of one image, in a process of its own (models loaded for it)."""
    try:
        # ORT can leave a :memory:.ses scratch file in its working directory.
        # Keep inference scratch away from the user's image and the checkout.
        with tempfile.TemporaryDirectory(prefix="beanjar-elements-") as tmp:
            result = subprocess.run(
                [binary, "-json", "-threads", str(THREADS), image],
                cwd=tmp,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
    except subprocess.TimeoutExpired:
        raise Error("go-textshot timed out after 180 s") from None
    except OSError as e:
        raise Error(f"go-textshot: {e}") from None
    if result.returncode:
        raise Error(result.stderr.strip() or f"go-textshot exited with status {result.returncode}")
    if out and result.stderr.strip():
        out(result.stderr.rstrip())
    try:
        return json.loads(result.stdout)
    except ValueError as e:
        raise Error(f"go-textshot returned invalid element JSON: {e}") from None


# The recognition server: host/tools/textshot-serve, built from the checkout (its README), the
# same engine and output as the CLI with the models kept loaded between frames. One per
# configuration (its binary, runtime, cache), shared by every `beanjar elements` and viewer of
# this user; it exits after SERVE_IDLE without a request.
SERVE_IDLE = "5m"
SERVE_START_S = 60.0  # a first start may download and load the models
SERVE_READ_S = 180.0


def serve_executable():
    """BEANJAR_TEXTSHOT_SERVE (a path or name; "none": never use a server), else textshot-serve
    on PATH, else the checkout's build/textshot-serve; None when there is none."""
    name = os.environ.get("BEANJAR_TEXTSHOT_SERVE")
    if name == "none":
        return None
    if name:
        found = shutil.which(name)
        return os.path.abspath(found) if found else None
    found = shutil.which("textshot-serve")
    if found:
        return os.path.abspath(found)
    from ..paths import TREE

    built = TREE.build("textshot-serve")
    return built if os.path.isfile(built) and os.access(built, os.X_OK) else None


def serve_socket(server, env):
    """The server's socket for this configuration, in the user's runtime directory."""
    import hashlib

    try:
        mtime = os.stat(server).st_mtime_ns
    except OSError:
        mtime = 0
    key = "\0".join(
        [server, str(mtime), str(THREADS)]
        + [
            env.get(k, "")
            for k in ("TEXTSHOT_ONNXRUNTIME_LIB", "TEXTSHOT_CACHE_DIR", "LD_LIBRARY_PATH")
        ]
    )
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    folder = os.path.join(base, f"beanjar-textshot-{os.getuid()}")
    if len(folder.encode()) > 80:  # a socket's path is at most 107 bytes
        folder = os.path.join(tempfile.gettempdir(), f"beanjar-textshot-{os.getuid()}")
    os.makedirs(folder, mode=0o700, exist_ok=True)
    return os.path.join(folder, hashlib.sha256(key.encode()).hexdigest()[:16] + ".sock")


def served(image, env, out=None):
    """The CLI's -json elements of image from the recognition server, started when it is not
    running; None when there is no server or it cannot start (the caller runs the CLI)."""
    import socket as socketlib
    import threading
    import time

    server = serve_executable()
    if server is None:
        return None
    path = serve_socket(server, env)

    def ask():
        conn = socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_STREAM)
        try:
            conn.settimeout(SERVE_READ_S)
            conn.connect(path)
            conn.sendall(json.dumps({"image": image}).encode() + b"\n")
            reply = b""
            while not reply.endswith(b"\n"):
                chunk = conn.recv(1 << 16)
                if not chunk:
                    raise ConnectionResetError("the server closed the connection")
                reply += chunk
        finally:
            conn.close()
        return json.loads(reply)

    try:
        reply = ask()
    except (FileNotFoundError, ConnectionRefusedError):
        if out:
            out("starting the recognition server (textshot-serve)")
        try:
            log = open(path + ".log", "ab")
            proc = subprocess.Popen(
                [server, "-socket", path, "-idle", SERVE_IDLE, "-threads", str(THREADS)],
                cwd=os.path.dirname(path),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            log.close()
        except OSError as e:
            if out:
                out(f"textshot-serve: {e}; reading with one textshot process")
            return None
        threading.Thread(target=proc.wait, daemon=True).start()  # reaped when it exits
        deadline = time.monotonic() + SERVE_START_S
        reply = None
        while time.monotonic() < deadline:
            if os.path.exists(path):
                try:
                    reply = ask()
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    pass
            if proc.poll() not in (None, 0):
                break
            time.sleep(0.1)
        if reply is None:
            if out:
                out(
                    f"textshot-serve did not start (see {path}.log); reading with one textshot process"
                )
            return None
    except (OSError, ValueError) as e:
        if out:
            out(f"textshot-serve: {e}; reading with one textshot process")
        return None
    if not isinstance(reply, dict):
        return None
    if "error" in reply:
        raise Error(str(reply["error"]))
    return reply.get("elements")


def read_screen(image, binary=None, out=None):
    """read() plus where the TV picture sits in the frame (framing.frame_geometry, from the
    display size a beanjar screenshot records in its PNG): `beanjar elements --json` and the
    viewer's Elements layer, one reading of a frame."""
    from . import framing

    data = read(image, binary, out=out)
    with open(image, "rb") as f:
        source = framing.png_source(f.read())
    return data | framing.frame_geometry((data["width"], data["height"]), source)


def frame(data, width, height):
    """Preserve upstream fields and reading order; add convenient geometry and a unit alias."""
    if not isinstance(data, list):
        raise ValueError("expected an array")
    elements = []
    for e in data:
        if not isinstance(e, dict):
            raise ValueError("expected an element object")
        if type(e["id"]) is not int or e["kind"] not in ("button", "icon", "text", "progress"):
            raise ValueError("invalid id or kind")
        box = [e[k] for k in ("x0", "y0", "x1", "y1")]
        if not all(type(v) is int for v in box):
            raise ValueError("expected integer box coordinates")
        if not isinstance(e["style"], dict) or not isinstance(e.get("text", ""), str):
            raise ValueError("invalid style or text")
        if e.get("value") is not None and (
            type(e["value"]) not in (float, int) or not math.isfinite(e["value"])
        ):
            raise ValueError("invalid value")
        elements.append(
            {
                **e,
                "box": box,
                "centre": [(box[0] + box[2]) / 2, (box[1] + box[3]) / 2],
                "text": e.get("text", ""),
                "value": e.get("value"),
                "unit": e.get("uom", ""),
            }
        )
    # The CLI exposes neither theme nor backdrop. Do not infer them from styles or logs.
    return {"width": width, "height": height, "elements": elements}


def quote(text):
    """JSON-style double quotes, with YAML line breaks and controls also escaped."""
    return re.sub(
        r"[\x7f-\x9f\u2028\u2029]",
        lambda m: f"\\u{ord(m[0]):04x}",
        json.dumps(text, ensure_ascii=False),
    )


def yaml_key(text):
    """Quote the whole scalar when label punctuation would create YAML syntax.

    Playwright's ariaSnapshotRenderer also applies yamlEscapeKeyIfNeeded after
    quoting a name. Our keys always start with a role, so only interior hazards apply.
    """
    if re.search(r"[:](?:\s|$)|\s#|[{}`]", text):
        return "'" + text.replace("'", "''") + "'"
    return text


def snapshot(data):
    """Flat, aria-snapshot-like lines; refs are upstream IDs for this frame only."""
    lines = [f"- screen [size={data['width']}x{data['height']}]"]
    for e in data["elements"]:
        line = e["kind"]
        if e["text"]:
            line += " " + quote(e["text"])
        line += f" [ref=e{e['id']}] [box={','.join(str(v) for v in e['box'])}]"
        if e["value"] is not None:
            line += f" [value={json.dumps(e['value'])}]"
        if e["unit"]:
            line += f" [unit={quote(e['unit'])}]"
        # Upstream "disabled" is low contrast, not state: webOS draws unfocused buttons grey.
        for flag, name in (
            ("disabled", "dimmed"),
            ("highlighted", "highlighted"),
            ("focused", "focused"),
        ):
            if e["style"].get(flag) is True:
                line += f" [{name}]"
        lines.append("- " + yaml_key(line))
    return "\n".join(lines)
