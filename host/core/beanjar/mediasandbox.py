# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The media helper's bubblewrap sandbox (command): what a decoder bug reached by a guest's
bitstream can touch.

The helper (host/helpers/media-helper) decodes what the guest sends with FFmpeg. In bwrap it has
its own user, mount, pid, network, IPC, UTS and cgroup namespaces, a new session and no
capabilities. It sees:
- read-only: /usr, /etc, the root's library links, its own binary and the FFmpeg beside it (a
  checkout's build/ffmpeg-helper, the helper's own build), and what LD_LIBRARY_PATH, LIBVA_DRIVERS_PATH and the Vulkan loader's
  variables name outside /usr;
- read-write: its own directories only (launch.start_media_helper: RUN_DIR/media and the pool's
  RUNTIME_DIRECTORY/media), never RUN_DIR itself, which holds QMP sockets;
- a private /dev, /proc, /tmp and /dev/shm, and with hardware decoding (MEDIA_HWDEC other than
  none) the decoders' device nodes: /dev/dri's render nodes, /dev/dxg (WSL; its drivers are in
  /usr/lib/wsl), /dev/nvidia*, /dev/video*, /dev/media*, /dev/dma_heap, with /sys and /run/udev
  read-only (V4L2 request finds its decoder through them). Without it, only /sys's CPU and NUMA
  tables (thread counts).
Not $HOME, not the network, not the user's session bus, agents or other VMs' sockets. Its sockets
to the viewers and QEMU's pool socket are the launcher's, passed as open descriptors, and so is
the pidfd it watches QEMU with (pid namespaces make a pid meaningless). HOME is /tmp inside (a
driver's shader or compute cache is lost at the end), XDG_*_HOME are dropped.

Inside, the helper confines itself too (host/media/sandbox.h: no_new_privs, Landlock and seccomp),
also where bwrap is missing or cannot make a user namespace; there the launcher says so on
media-helper.out and starts it without bwrap. MEDIA_SANDBOX=off turns both off, for debugging.

bubblewrap exists on Linux only; elsewhere (macOS) command() is never reached.
"""

import glob
import os
import subprocess
import sys

ENV = "MEDIA_SANDBOX"  # on (the default) or off
BWRAP = "bwrap"
ROOT_DIRS = ("bin", "sbin", "lib", "lib32", "lib64", "libx32")  # links into /usr if merged
# the hardware decoders' device nodes (globs), bound when MEDIA_HWDEC is not none
DEVICES = (
    "/dev/dri/renderD*",
    "/dev/dxg",
    "/dev/nvidia*",
    "/dev/video*",
    "/dev/media*",
    "/dev/dma_heap",
)
# variables naming files a decoder's driver loads: directories (:-separated), or files
LIBRARY_DIRS = ("LD_LIBRARY_PATH", "LIBVA_DRIVERS_PATH")
LIBRARY_FILES = ("VK_ICD_FILENAMES", "VK_DRIVER_FILES", "VK_ADD_DRIVER_FILES")
HOME_VARS = ("XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME")


class Error(Exception):
    pass


def enabled(env):
    """MEDIA_SANDBOX: True (on, the default) or False (off); Error for anything else."""
    value = env.get(ENV) or "on"
    if value not in ("on", "off"):
        raise Error(f"{ENV}={value}: on or off")
    return value == "on"


def find(env):
    """bwrap's path on a Linux host (on PATH, else /usr/bin or /bin), or None."""
    if not sys.platform.startswith("linux"):
        return None
    from .network import mitmtool

    return mitmtool.sandbox(env)


def problem(bwrap):
    """Why bwrap cannot run the helper here (it cannot make the namespaces: an unprivileged user
    namespace refused, as by AppArmor on Ubuntu 24.04), or "" when it can."""
    try:
        r = subprocess.run(
            [bwrap, *namespaces(), "--ro-bind", "/", "/", "--", "/bin/true"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)
    return "" if r.returncode == 0 else (r.stderr.decode(errors="replace").strip() or "failed")


def namespaces():
    return [
        "--unshare-user-try",
        "--unshare-ipc",
        "--unshare-pid",
        "--unshare-net",
        "--unshare-uts",
        "--unshare-cgroup-try",
    ]


def _under(path, top):
    return path == top or path.startswith(top.rstrip("/") + "/")


def _system(path):
    return any(_under(path, top) for top in ("/usr", "/etc"))


def _ro(path):
    """--ro-bind-try of path's target at path, unless /usr or /etc has it already."""
    if not os.path.isabs(path):
        return []
    real = os.path.realpath(path)
    if _system(real) and _system(os.path.abspath(path)):
        return []
    return ["--ro-bind-try", real, os.path.abspath(path)]


def command(bwrap, helper, dirs, env, hwdec):
    """bwrap's command line up to the helper's own: helper (its path) in the sandbox described
    above, writing dirs only; env is the helper's (environment()), hwdec its --hwdec."""
    cmd = [bwrap, *namespaces(), "--new-session", "--cap-drop", "ALL"]
    cmd += ["--ro-bind", "/usr", "/usr"]
    for name in ROOT_DIRS:
        path = "/" + name
        if os.path.islink(path):
            cmd += ["--symlink", os.readlink(path), path]
        elif os.path.isdir(path):
            cmd += ["--ro-bind", path, path]
    cmd += ["--ro-bind", "/etc", "/etc"]
    cmd += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/dev/shm", "--tmpfs", "/tmp"]
    # the binary and the FFmpeg its RUNPATH names ($ORIGIN/../ffmpeg-helper/lib in a checkout)
    cmd += _ro(helper)
    cmd += _ro(os.path.join(os.path.dirname(os.path.abspath(helper)), "..", "ffmpeg-helper"))
    for key in LIBRARY_DIRS:
        for path in (env.get(key) or "").split(":"):
            cmd += _ro(path)
    for key in LIBRARY_FILES:
        for path in (env.get(key) or "").split(":"):
            cmd += _ro(path)
    if hwdec != "none":
        for pattern in DEVICES:
            for node in sorted(glob.glob(pattern)):
                cmd += ["--dev-bind", node, node]
        cmd += ["--ro-bind", "/sys", "/sys", "--ro-bind-try", "/run/udev", "/run/udev"]
    else:
        for path in ("/sys/devices/system/cpu", "/sys/devices/system/node"):
            cmd += ["--ro-bind-try", path, path]
    for path in dirs:
        cmd += ["--bind", os.path.realpath(path), os.path.abspath(path)]
    cmd += ["--chdir", "/", "--"]
    return cmd


def environment(env):
    """The helper's environment (launch.media_helper_environment's) as the sandbox has it: HOME a
    private /tmp, no XDG_*_HOME (both the user's home)."""
    inside = {k: v for k, v in env.items() if k not in HOME_VARS}
    inside["HOME"] = "/tmp"
    return inside
