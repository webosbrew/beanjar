# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""USB devices plugged into a running VM over usbredir.

On the VM's host, `io usb attach` gives the VM's qemu-xhci a `usb-redir` device whose chardev is a
unix socket in the VM's run directory (`run/usb-ID.sock`), `io usb detach` removes it, and `io usb list`
shows them. The guest sees the USB device only while a usbredir client (usbredirect) is connected
to the socket and has sent its handshake. A unix socket because QEMU may run in the VM's network
namespace (mode = "mitm"), where a TCP chardev would not be reachable from the host; the run
directory is on the shared filesystem.

On the machine with the device, `io usb attach --device` does the lot: attach, a way from 127.0.0.1:PORT to the
socket (a relay in this process for a local VM, a forward of the host's shared ssh connection to
the socket for a remote one: ares.Forward), `usbredirect --device BUS-DEV --to 127.0.0.1:PORT`,
and at the end (Ctrl-C, SIGTERM, SIGHUP, the device unplugged, usbredirect gone) the detach.
`io usb attach --camera` runs the virtual camera (host/helpers/virtcam, beanjar-virtcam: a Logitech
C920 showing GStreamer's test pattern) in usbredirect's place, here, where GStreamer is: a remote
VM's host needs nothing of it.

`io usb gamepad attach` is a virtual Xbox 360 pad served from the VM's host (gamepad.py,
host/helpers/virtpad), which stays after the command; `io usb list` shows its device as kind
`gamepad`, and `io usb detach` ends its program too.

`io usb attach --image FILE` is a USB stick instead: QMP `blockdev-add` of the raw image (read-only
with --readonly) and `device_add usb-storage` with it on the same bus; `io usb detach` removes the
device and then the block node. `io usb mkimage` makes such an image: an MBR with one partition and a
FAT32, exFAT or NTFS filesystem in it, made with the system's sfdisk and mkfs tools.

Attached devices are the running VM's, not its settings: they are recorded (lifecycle.USB_RECORD,
in the unit's RuntimeDirectory) and put back on the new QEMU when the guest restarts (a restart ends
QEMU and the unit starts it again: lifecycle.py), as a stick stays in a TV's port across a reboot;
`io usb attach --device` starts its client again for the new QEMU. A new start of the VM has none.
"""

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

from ..base import Error

BUS = "usb-bus.0"  # the bus of the launcher's qemu-xhci, which has no id (qemu.py)
DRIVER = "usb-redir"
STORAGE = "usb-storage"  # a disk image as a USB stick
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,15}")
SUN_PATH_MAX = 107  # sun_path's 108 bytes, its NUL included
QMP_WAIT = 15.0  # seconds to wait for QMP while another client holds it (one at a time)
DELETE_WAIT = 5.0  # seconds device_del may take to finish
ATTACH_WAIT = 30.0  # seconds plug waits for the guest to see the device
POLL = 0.5
RESTART_WAIT = 180.0  # seconds plug waits for a restarting VM's new QEMU
SYSFS = "/sys/bus/usb/devices"
DEVFS = "/dev/bus/usb"
TOOL = "usbredirect"
RECORD_PREFIX = "beanjar-usb-"  # plug's record directory of a remote forward (ares.sweep_homes)


# ------------------------------------------------------------------ names
def check_id(text):
    if not ID_RE.fullmatch(text or ""):
        raise Error(
            f"invalid USB id {text!r}: 1 to 16 letters, digits, '_' or '-', a letter or digit first"
        )
    return text


def device_id(id_):
    """The QEMU device id of attachment id_ (device_add, device_del)."""
    return f"usb-{id_}"


def chardev_id(id_):
    return f"usb-{id_}-chr"


def socket_path(vm, id_):
    return os.path.join(vm.run_dir, f"usb-{id_}.sock")


# ------------------------------------------------------------------ QMP payloads
def chardev_add(id_, path):
    """A listening unix socket that does not wait for its client."""
    return {
        "execute": "chardev-add",
        "arguments": {
            "id": chardev_id(id_),
            "backend": {
                "type": "socket",
                "data": {
                    "addr": {"type": "unix", "data": {"path": path}},
                    "server": True,
                    "wait": False,
                },
            },
        },
    }


def device_add(id_):
    return {
        "execute": "device_add",
        "arguments": {
            "driver": DRIVER,
            "id": device_id(id_),
            "bus": BUS,
            "chardev": chardev_id(id_),
        },
    }


def device_del(id_):
    return {"execute": "device_del", "arguments": {"id": device_id(id_)}}


def chardev_remove(id_):
    return {"execute": "chardev-remove", "arguments": {"id": chardev_id(id_)}}


def node_name(id_):
    """The block node of attachment id_'s image (blockdev-add, blockdev-del)."""
    return f"usb-{id_}-disk"


def blockdev_add(id_, image, readonly):
    """image, a raw disk image, as block node node_name(id_); read-only all the way down when
    readonly (QEMU then takes only a shared lock on the file)."""
    return {
        "execute": "blockdev-add",
        "arguments": {
            "driver": "raw",
            "node-name": node_name(id_),
            "read-only": readonly,
            "file": {"driver": "file", "filename": image, "read-only": readonly},
        },
    }


def storage_add(id_):
    """A USB stick with node_name(id_) as its medium; removable, as a stick reports itself."""
    return {
        "execute": "device_add",
        "arguments": {
            "driver": STORAGE,
            "id": device_id(id_),
            "bus": BUS,
            "drive": node_name(id_),
            "removable": True,
        },
    }


def blockdev_del(id_):
    return {"execute": "blockdev-del", "arguments": {"node-name": node_name(id_)}}


# ------------------------------------------------------------------ QMP client
class QmpError(Error):
    def __init__(self, cmd, error):
        self.cls = error.get("class", "")
        self.desc = error.get("desc", str(error))
        super().__init__(f"QMP {cmd}: {self.desc}")


class Qmp:
    """`with Qmp(path) as q`: one QMP session, q.run(cmd) per command. QEMU serves one client at a
    time; a connection made while another client holds it is served when that one leaves, so the
    greeting is waited for up to wait seconds (beanjar list's and info's are short)."""

    def __init__(self, path, wait=QMP_WAIT):
        self.path, self.wait = path, wait
        self.sock = self.file = None
        self.events = []

    def __enter__(self):
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(self.wait)
        try:
            s.connect(self.path)
            self.sock, self.file = s, s.makefile("rw")
            self._line()  # the greeting
        except TimeoutError:
            self.close()
            raise Error(f"QMP is busy: another client has held it for {self.wait:g} s") from None
        except OSError as e:
            self.close()
            raise Error(f"QMP {self.path}: {e.strerror or e}") from None
        self.sock.settimeout(10)
        self.run({"execute": "qmp_capabilities"})
        return self

    def __exit__(self, *_exc):
        self.close()

    def close(self):
        for f in (self.file, self.sock):
            if f:
                try:
                    f.close()
                except OSError:
                    pass
        self.file = self.sock = None

    def _line(self):
        line = self.file.readline()
        if not line:
            raise Error("QEMU closed the QMP connection")
        return json.loads(line)

    def run(self, cmd):
        """cmd's return value; its error a QmpError. Events met meanwhile are kept in events."""
        self.file.write(json.dumps(cmd) + "\n")
        self.file.flush()
        while True:
            r = self._line()
            if "event" in r:
                self.events.append(r)
            elif "error" in r:
                raise QmpError(cmd["execute"], r["error"])
            elif "return" in r:
                return r["return"]

    def wait_event(self, name, device, timeout):
        """Whether event name for QEMU device id device came within timeout seconds."""

        def match(e):
            return e.get("event") == name and (e.get("data") or {}).get("device") == device

        if any(match(e) for e in self.events):
            return True
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            self.sock.settimeout(left)
            try:
                r = self._line()
            except TimeoutError:
                return False
            if "event" in r:
                self.events.append(r)
                if match(r):
                    return True
        return False


def _running(vm):
    if vm.pid() is None:
        raise Error(f"VM {vm.name} is not running")


def record_dir(vm):
    """Where vm's attached devices are recorded: its unit's RuntimeDirectory, else run/."""
    from .. import lifecycle, vmunits

    unit_name = vm.unit_name()
    return lifecycle.state_dir(
        vm.run_dir, vmunits.unit_runtime_dir(unit_name) if unit_name else None
    )


def _record(vm, add=None, remove=None):
    """Adds the device add to vm's record, or removes the id remove from it, under a lock."""
    import fcntl

    from .. import lifecycle

    d = record_dir(vm)
    with open(os.path.join(d, lifecycle.USB_RECORD + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        devices = [
            x for x in lifecycle.read_usb(d) if x["id"] not in (remove, (add or {}).get("id"))
        ]
        if add:
            devices.append(add)
        lifecycle.write_usb(d, devices)


def state(vm):
    """{restarts, restarting, devices}: how many times vm's unit started QEMU again, whether it is
    doing so now (then no devices: the new QEMU is not up), and its devices (query). Error if the VM
    is not running and not restarting."""
    from .. import lifecycle

    restarting = lifecycle.restarting(vm.run_dir)
    out = {"restarts": lifecycle.restarts(vm.run_dir), "restarting": restarting, "devices": []}
    if restarting:
        return out
    _running(vm)
    with Qmp(vm.qmp_sock) as q:
        out["devices"] = _kinds(vm, query(q))
    return out


def _kinds(vm, devices):
    """devices with a usb-redir device that the virtual gamepad serves (gamepad.py) as kind
    "gamepad"."""
    from . import gamepad

    pads = gamepad.records(vm)
    for d in devices:
        if d["kind"] == "redir" and d["id"] in pads:
            d["kind"] = "gamepad"
    return devices


def query(q):
    """The attached USB devices of the session q's QEMU, by kind: a usb-redir device is
    {id, kind: "redir", socket, connected, attached} (connected: a client on the socket), a disk
    image {id, kind: "storage", image, readonly, attached}; attached: the guest's bus has it."""
    peripherals = q.run({"execute": "qom-list", "arguments": {"path": "/machine/peripheral"}})
    kinds = {f"child<{DRIVER}>": "redir", f"child<{STORAGE}>": "storage"}
    ours = [
        (p["name"], kinds[p.get("type")])
        for p in sorted(peripherals, key=lambda p: p["name"])
        if p.get("type") in kinds and p["name"].startswith("usb-")
    ]
    chardevs = nodes = None
    out = []
    for name, kind in ours:
        id_ = name.removeprefix("usb-")
        path = f"/machine/peripheral/{name}"
        attached = q.run(
            {"execute": "qom-get", "arguments": {"path": path, "property": "attached"}}
        )
        if kind == "storage":
            if nodes is None:
                nodes = {
                    n.get("node-name"): n
                    for n in q.run(
                        {"execute": "query-named-block-nodes", "arguments": {"flat": True}}
                    )
                }
            node = nodes.get(node_name(id_)) or {}
            out.append(
                {
                    "id": id_,
                    "kind": kind,
                    "image": node.get("file"),
                    "readonly": bool(node.get("ro")),
                    "attached": bool(attached),
                }
            )
            continue
        if chardevs is None:
            chardevs = {c["label"]: c for c in q.run({"execute": "query-chardev"})}
        label = q.run({"execute": "qom-get", "arguments": {"path": path, "property": "chardev"}})
        filename = (chardevs.get(label) or {}).get("filename", "")
        sock = re.match(r"(?:disconnected:)?unix:([^,]*)", filename)
        out.append(
            {
                "id": id_,
                "kind": kind,
                "socket": sock.group(1) if sock else None,
                "connected": bool(filename) and not filename.startswith("disconnected:"),
                "attached": bool(attached),
            }
        )
    return out


def free_id(taken):
    n = 1
    while str(n) in taken:
        n += 1
    return str(n)


def attach(vm, id_=None):
    """A usb-redir device on vm's xHCI with its socket in the run directory: {id, socket}."""
    _running(vm)
    if id_ is not None:
        check_id(id_)
    with Qmp(vm.qmp_sock) as q:
        taken = {d["id"] for d in query(q)}
        if id_ is None:
            id_ = free_id(taken)
        elif id_ in taken:
            raise Error(f"VM {vm.name} has a USB device {id_} already (beanjar io usb list)")
        path = socket_path(vm, id_)
        if len(os.fsencode(path)) > SUN_PATH_MAX:
            raise Error(f"{path}: longer than a unix socket path may be ({SUN_PATH_MAX} bytes)")
        q.run(chardev_add(id_, path))
        try:
            q.run(device_add(id_))
        except Error:
            try:
                q.run(chardev_remove(id_))
            except QmpError:
                pass
            raise
    _record(vm, {"id": id_, "kind": "redir", "socket": path})
    return {"id": id_, "socket": path}


def check_image(image, readonly):
    """image as an absolute path, a regular file this user may read (and write unless readonly):
    QEMU opens it as this user."""
    path = os.path.abspath(image)
    if not os.path.isfile(path):
        raise Error(
            f"{path}: no such disk image (a regular file; beanjar io usb mkimage makes one)"
        )
    if not os.access(path, os.R_OK):
        raise Error(f"{path}: not readable for you")
    if not readonly and not os.access(path, os.W_OK):
        raise Error(f"{path}: not writable for you: attach it with --readonly")
    return path


def attach_image(vm, image, readonly=False, id_=None):
    """The raw disk image image as a USB stick on vm's xHCI: {id, image, readonly}."""
    _running(vm)
    if id_ is not None:
        check_id(id_)
    path = check_image(image, readonly)
    with Qmp(vm.qmp_sock) as q:
        taken = {d["id"] for d in query(q)}
        if id_ is None:
            id_ = free_id(taken)
        elif id_ in taken:
            raise Error(f"VM {vm.name} has a USB device {id_} already (beanjar io usb list)")
        q.run(blockdev_add(id_, path, readonly))
        try:
            q.run(storage_add(id_))
        except Error:
            try:
                q.run(blockdev_del(id_))
            except QmpError:
                pass
            raise
    _record(vm, {"id": id_, "kind": "storage", "image": path, "readonly": readonly})
    return {"id": id_, "image": path, "readonly": readonly}


def detach(vm, id_):
    """Removes vm's USB device id_: the guest sees it unplugged. A usb-redir device's chardev, and
    so its socket, goes with it (QEMU destroys it); a disk image's block node is deleted after it,
    which releases the image."""
    from . import gamepad

    _running(vm)
    check_id(id_)
    gamepad.stop(vm, id_)  # a virtual gamepad's program unplugs it first
    with Qmp(vm.qmp_sock) as q:
        kind = {d["id"]: d["kind"] for d in query(q)}.get(id_)
        if kind is None:
            _record(vm, remove=id_)
            raise Error(f"VM {vm.name} has no USB device {id_} (beanjar io usb list)")
        q.run(device_del(id_))
        _record(vm, remove=id_)  # not put back at a restart, whatever comes of the removal
        if not q.wait_event("DEVICE_DELETED", device_id(id_), DELETE_WAIT):
            raise Error(f"VM {vm.name}: USB device {id_} was not removed in {DELETE_WAIT:g} s")
        if kind == "storage":
            q.run(blockdev_del(id_))
            return {"id": id_}
        try:  # gone with the device (usb-redir's unrealize); one left over is removed here
            q.run(chardev_remove(id_))
        except QmpError:
            pass
    try:
        os.unlink(socket_path(vm, id_))
    except FileNotFoundError:
        pass
    return {"id": id_}


def devices(vm):
    _running(vm)
    with Qmp(vm.qmp_sock) as q:
        return _kinds(vm, query(q))


# ------------------------------------------------------------------ the device here (plug)
def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def find_device(spec, sysfs=SYSFS):
    """This machine's USB device spec (VENDOR:PRODUCT, hex, or BUS-DEVICE, decimal, as usbredirect
    takes them): {sysfs, bus, dev, vid, pid, product}. Several devices of one VENDOR:PRODUCT are an
    error asking for BUS-DEVICE."""
    m = re.fullmatch(r"([0-9a-fA-F]{4}):([0-9a-fA-F]{4})|(\d+)-(\d+)", spec or "")
    if not m:
        raise Error(
            f"invalid --device {spec!r}: VENDOR:PRODUCT (hex, 046d:082d) or BUS-DEVICE (1-4)"
        )
    found = []
    for entry in sorted(os.listdir(sysfs)) if os.path.isdir(sysfs) else []:
        if ":" in entry:
            continue  # an interface
        d = os.path.join(sysfs, entry)
        bus, dev = _read(os.path.join(d, "busnum")), _read(os.path.join(d, "devnum"))
        vid, pid = _read(os.path.join(d, "idVendor")), _read(os.path.join(d, "idProduct"))
        if not (bus and dev and vid and pid):
            continue
        if m.group(1):
            hit = (vid.lower(), pid.lower()) == (m.group(1).lower(), m.group(2).lower())
        else:
            hit = (int(bus), int(dev)) == (int(m.group(3)), int(m.group(4)))
        if hit:
            found.append(
                {
                    "sysfs": d,
                    "bus": int(bus),
                    "dev": int(dev),
                    "vid": vid.lower(),
                    "pid": pid.lower(),
                    "product": _read(os.path.join(d, "product")),
                }
            )
    if not found:
        raise Error(f"no USB device {spec} on this machine (lsusb)")
    if len(found) > 1:
        which = ", ".join(f"{d['bus']}-{d['dev']}" for d in found)
        raise Error(
            f"several USB devices {spec} on this machine: say which, --device BUS-DEVICE ({which})"
        )
    return found[0]


def present(device):
    """Whether device (find_device's) is still plugged in: the same sysfs entry, the same number."""
    return _read(os.path.join(device["sysfs"], "devnum")) == str(device["dev"])


def node(device):
    return os.path.join(DEVFS, f"{device['bus']:03d}", f"{device['dev']:03d}")


def tool_argv(device, port, sudo=False):
    """usbredirect for device, to 127.0.0.1:port; with sudo through sudo."""
    argv = [TOOL, "--device", f"{device['bus']}-{device['dev']}", "--to", f"127.0.0.1:{port}", "-k"]
    return ["sudo", "--", *argv] if sudo else argv


class Usbredirect:
    """plug's client for a USB device of this machine (find_device's): usbredirect, through sudo
    with sudo."""

    def __init__(self, device, sudo=False):
        self.device, self.sudo = device, sudo
        self.tool = TOOL

    def check(self):
        if shutil.which(TOOL) is None:
            raise Error(f"no {TOOL} on this machine: install it (Debian: usbredirect)")
        if not self.sudo and not os.access(node(self.device), os.W_OK):
            raise Error(
                f"{node(self.device)} is not writable for you: run with --sudo (usbredirect "
                "through sudo), or give your user the device (a udev rule)"
            )

    def argv(self, port):
        return tool_argv(self.device, port, self.sudo)

    def present(self):
        return present(self.device)

    def gone(self):
        d = self.device
        return f"the device {d['bus']}-{d['dev']} was unplugged"

    def describe(self):
        d = self.device
        return {
            "device": f"{d['vid']}:{d['pid']}",
            "bus_device": f"{d['bus']}-{d['dev']}",
            "product": d.get("product"),
        }


# videotestsrc's patterns (gst-inspect-1.0 videotestsrc)
PATTERNS = (
    "smpte", "snow", "black", "white", "red", "green", "blue", "checkers-1", "checkers-2",
    "checkers-4", "checkers-8", "circular", "blink", "smpte75", "zone-plate", "gamut",
    "chroma-zone-plate", "solid-color", "ball", "smpte100", "bar", "pinwheel", "spokes",
    "gradient", "colors", "smpte-rp-219",
)  # fmt: skip
CAMERA = "beanjar-virtcam"
CAMERA_ID = "046d:082d"  # a Logitech C920 (host/helpers/virtcam/camera.h)
CAMERA_STATS = 10  # seconds between the camera's counters on stderr


class Camera:
    """plug's client for the virtual camera (host/helpers/virtcam): a Logitech C920 showing
    GStreamer's test pattern, made on this machine."""

    tool = CAMERA
    sudo = False

    def __init__(self, pattern="smpte", program=None):
        if pattern not in PATTERNS:
            raise Error(f"invalid --pattern {pattern!r}: " + ", ".join(PATTERNS))
        self.pattern = pattern
        if program is None:
            from .. import paths

            program = paths.TREE.virtcam()
        self.program = program

    def check(self):
        if not os.access(self.program, os.X_OK):
            raise Error(
                f"no {CAMERA} at {self.program}: build it (meson compile -C build/host; it needs "
                "cmake, libspdlog-dev, libasio-dev, libusbredirparser-dev) or install beanjar"
            )
        if shutil.which("gst-launch-1.0") is None:
            raise Error(
                "no gst-launch-1.0 on this machine: install it (Debian: gstreamer1.0-tools, "
                "gstreamer1.0-plugins-base, gstreamer1.0-plugins-good)"
            )

    def argv(self, port):
        return [
            self.program, "--to", f"127.0.0.1:{port}", "--pattern", self.pattern,
            "--stats", str(CAMERA_STATS),
        ]  # fmt: skip

    def present(self):
        return True

    def gone(self):
        return ""

    def describe(self):
        return {"device": CAMERA_ID, "product": "HD Pro Webcam C920 (virtual)",
                "pattern": self.pattern}  # fmt: skip


class Relay:
    """`with Relay(path) as port`: 127.0.0.1:port here reaches the unix socket path, one
    connection to it per connection here, until the block ends (a local VM's plug: usbredirect
    takes host:port alone)."""

    def __init__(self, path):
        self.path = path
        self.listener = None
        self.socks = []
        self.lock = threading.Lock()

    def __enter__(self):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        threading.Thread(target=self._accept, daemon=True).start()
        return self.listener.getsockname()[1]

    def __exit__(self, *_exc):
        with self.lock:
            socks, self.socks = self.socks, None
        for s in [self.listener, *(socks or [])]:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()

    def _accept(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            out = socket.socket(socket.AF_UNIX)
            try:
                out.connect(self.path)
            except OSError:
                conn.close()
                out.close()
                continue
            with self.lock:
                if self.socks is None:
                    conn.close()
                    out.close()
                    return
                self.socks += [conn, out]
            for a, b in ((conn, out), (out, conn)):
                threading.Thread(target=self._pump, args=(a, b), daemon=True).start()

    @staticmethod
    def _pump(a, b):
        try:
            while data := a.recv(65536):
                b.sendall(data)
        except OSError:
            pass
        try:
            b.shutdown(socket.SHUT_WR)
        except OSError:
            pass


class Local:
    """plug's VM on this machine."""

    def __init__(self, vm):
        self.vm = vm

    def attach(self, id_):
        return attach(self.vm, id_)

    def detach(self, id_):
        return detach(self.vm, id_)

    def devices(self):
        return devices(self.vm)

    def state(self):
        return state(self.vm)

    def way(self, path):
        return Relay(path)


class Remote:
    """plug's VM name on host (uri): its beanjar's usb commands over the shared ssh connection,
    and a forward of it to the socket there."""

    def __init__(self, host, uri, name, record=None):
        self.host, self.uri, self.name, self.record = host, uri, name, record

    def _call(self, *words):
        from ..access import remote

        return remote.call_json(
            self.host, self.uri, ["io", "usb", words[0], remote.own(self.name), *words[1:]]
        )

    def attach(self, id_):
        data = self._call("attach", *(["--id", id_] if id_ else []))
        if not isinstance(data, dict) or not isinstance(data.get("socket"), str):
            raise Error(
                f"host {self.host}: its beanjar io usb attach is not what this one reads: update it"
            )
        return data

    def detach(self, id_):
        return self._call("detach", id_)

    def devices(self):
        return self.state()["devices"]

    def state(self):
        """usb list's answer there; restarts 0 from a beanjar that does not say (no re-plug)."""
        data = self._call("list")
        data = data if isinstance(data, dict) else {}
        return {
            "restarts": data.get("restarts", 0),
            "restarting": bool(data.get("restarting")),
            "devices": data.get("devices", []),
        }

    def way(self, path):
        from ..access import ares

        return ares.Forward(self.host, self.uri, path, None, self.record)


def _held():
    return signal.pthread_sigmask(signal.SIG_BLOCK, (signal.SIGINT, signal.SIGTERM, signal.SIGHUP))


def stop_tool(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def restarted(target, id_, seen, wait=RESTART_WAIT, sleep=time.sleep):
    """After plug's client ended: the VM's restart count if it restarted since seen (a restart ends
    QEMU, and with it the client's connection) and its new QEMU has device id_ back (lifecycle.py
    puts it back); None if it did not (the client ended on its own), or the VM stopped."""
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            st = target.state()
        except Error:
            return None  # not running, not restarting: stopped
        if not st["restarting"]:
            if st["restarts"] <= seen:
                return None
            if any(d["id"] == id_ for d in st["devices"]):
                return st["restarts"]
            return None
        sleep(POLL)
    return None


def plug(target, name, client, id_=None, ready=None, out=None, spawn=subprocess.Popen):
    """Plugs client's device (Usbredirect: a device of this machine; Camera: the virtual camera)
    into VM name (target: Local or Remote) until Ctrl-C, SIGTERM or SIGHUP (KeyboardInterrupt, a
    Terminated), the device's unplugging or the client's end: attach, the way to the socket, the
    client (usbredirect, beanjar-virtcam); ready(data) once the guest has the device; then,
    whatever ends it, the detach. When the guest restarts, the client ends with the old QEMU and is
    started again for the new one, which has the device back (restarted). Its exit status: 0 when
    the device was unplugged, 1 when the client ended on its own (a signal is the caller's: 130,
    128+N)."""
    say = out or (lambda line: None)
    client.check()
    data = target.attach(id_)
    id_ = data["id"]
    proc = None
    status = 0
    try:
        seen = target.state()["restarts"]
        with target.way(data["socket"]) as port:
            while True:
                # without sudo in a session of its own: Ctrl-C is this process's, which stops it
                proc = spawn(
                    client.argv(port),
                    stdin=None if client.sudo else subprocess.DEVNULL,
                    start_new_session=not client.sudo,
                )
                deadline = time.monotonic() + ATTACH_WAIT
                while True:
                    if proc.poll() is not None:
                        raise Error(
                            f"{client.tool} exited (status {proc.returncode}) "
                            f"before {name} had the device"
                        )
                    if any(d["id"] == id_ and d["attached"] for d in target.devices()):
                        break
                    if time.monotonic() > deadline:
                        raise Error(f"{name} did not get the device in {ATTACH_WAIT:g} s")
                    time.sleep(POLL)
                if ready:
                    ready({"name": name, "id": id_, "socket": data["socket"], **client.describe()})
                    ready = None
                ended = False
                while True:
                    if proc.poll() is not None:
                        ended = True
                        break
                    if not client.present():
                        say(client.gone())
                        break
                    time.sleep(POLL)
                if not ended:
                    break
                n = restarted(target, id_, seen)
                if n is None:
                    say(f"{client.tool} exited (status {proc.returncode})")
                    status = 1
                    break
                seen = n
                say(f"{name} restarted: {client.tool} again for its new QEMU")
    finally:
        held = _held()
        try:
            if proc:
                stop_tool(proc)
            try:
                target.detach(id_)
                say(f"{name}: USB device {id_} detached")
            except Error as e:
                print(f"beanjar: warning: {name}: detach {id_}: {e}", file=sys.stderr)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, held)
    return status


def plug_remote(host, name, client, id_=None, ready=None, out=None):
    """plug for VM name on host, its forward recorded for ares.sweep_homes should this process be
    killed outright."""
    from ..access import ares, remote

    uri = remote.hosts()[host]
    old = {s: signal.signal(s, ares.raise_terminated) for s in (signal.SIGTERM, signal.SIGHUP)}
    home = None
    try:
        ares.sweep_homes(ares.home_parent())
        home = tempfile.mkdtemp(prefix=f"{RECORD_PREFIX}{os.getpid()}-", dir=ares.home_parent())
        target = Remote(host, uri, name, os.path.join(home, ares.FORWARD_RECORD))
        return plug(target, f"{host}/{name}", client, id_, ready, out)
    except ares.Terminated as e:
        return e.status
    finally:
        if home:
            shutil.rmtree(home, ignore_errors=True)
        for s, handler in old.items():
            signal.signal(s, handler)


def plug_local(vm, client, id_=None, ready=None, out=None):
    from ..access import ares

    old = {s: signal.signal(s, ares.raise_terminated) for s in (signal.SIGTERM, signal.SIGHUP)}
    try:
        return plug(Local(vm), vm.name, client, id_, ready, out)
    except ares.Terminated as e:
        return e.status
    finally:
        for s, handler in old.items():
            signal.signal(s, handler)


# ------------------------------------------------------------------ disk images (mkimage)
MIB = 1 << 20
PART_START = 2048  # the partition's first sector: 1 MiB in, as fdisk and sticks have it
SECTOR = 512
MKIMAGE_SIZE = 64 * MIB
MKIMAGE_LABEL = "BEANJAR"
# fs: (MBR partition type, its mkfs argv for label and the partition's file)
FILESYSTEMS = {
    "fat32": (
        0x0C,
        lambda label, f: ["mkfs.vfat", "-F", "32", "-h", str(PART_START), "-n", label, f],
    ),
    "exfat": (0x07, lambda label, f: ["mkfs.exfat", "-L", label, f]),
    "ntfs": (
        0x07,
        lambda label, f: ["mkfs.ntfs", "-F", "-Q", "-p", str(PART_START), "-L", label, f],
    ),
}
SBIN = ("/usr/sbin", "/sbin")  # where Debian keeps sfdisk and mkfs, often not on a user's PATH


def parse_size(text):
    """A size in bytes: N with K, M or G (binary), or plain bytes; whole MiB, 2 MiB or more."""
    m = re.fullmatch(r"(\d+)([KMG]?)i?B?", (text or "").strip(), re.IGNORECASE)
    if not m:
        raise Error(f"invalid size {text!r}: N, NK, NM or NG (64M)")
    size = int(m.group(1)) << {"": 0, "K": 10, "M": 20, "G": 30}[m.group(2).upper()]
    if size % MIB or size < 2 * MIB:
        raise Error(f"invalid size {text!r}: a whole number of MiB, 2M or more")
    return size


def which(tool):
    found = shutil.which(tool) or shutil.which(tool, path=os.pathsep.join(SBIN))
    if not found:
        raise Error(f"no {tool} on this machine: install it (Debian: util-linux, dosfstools, "
                    "exfatprogs, ntfs-3g)")  # fmt: skip
    return found


def _run(argv, run, stdin=None):
    r = run(argv, input=stdin, capture_output=True, text=True)
    if r.returncode:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
        raise Error(
            f"{os.path.basename(argv[0])} failed (status {r.returncode}): " + " / ".join(tail)
        )


def sfdisk_script(ptype):
    """One MBR partition from PART_START to the end of the disk."""
    return f"label: dos\nstart={PART_START}, type={ptype:x}\n"


def make_image(path, fs, size=MKIMAGE_SIZE, label=MKIMAGE_LABEL, force=False, run=subprocess.run):
    """A disk image at path as PDM takes a stick: an MBR, one partition from 1 MiB to the end,
    and in it a filesystem fs (FILESYSTEMS) labelled label. The filesystem is made in a file of
    its own beside path (mkfs.exfat cannot write at an offset) and copied in, its holes kept;
    path is replaced only once all went well. {image, fs, size, label}."""
    if fs not in FILESYSTEMS:
        raise Error(f"invalid filesystem {fs!r}: " + ", ".join(FILESYSTEMS))
    path = os.path.abspath(path)
    if os.path.exists(path) and not force:
        raise Error(f"{path} exists: --force replaces it")
    ptype, mkfs = FILESYSTEMS[fs]
    sfdisk = which("sfdisk")
    argv = mkfs(label, "PART")
    argv[0] = which(argv[0])
    directory = os.path.dirname(path)
    fd, part = tempfile.mkstemp(prefix=".beanjar-mkimage-", dir=directory)
    image = None
    try:
        os.ftruncate(fd, size - PART_START * SECTOR)
        os.close(fd)
        fd = None
        _run([part if a == "PART" else a for a in argv], run)
        fd, image = tempfile.mkstemp(prefix=".beanjar-mkimage-", dir=directory)
        os.ftruncate(fd, size)
        os.close(fd)
        fd = None
        _run(
            [sfdisk, "--quiet", "--no-reread", "--no-tell-kernel", image], run, sfdisk_script(ptype)
        )
        with open(part, "rb") as src, open(image, "r+b") as dst:
            dst.seek(PART_START * SECTOR)
            while chunk := src.read(MIB):
                if chunk.count(0) == len(chunk):
                    dst.seek(len(chunk), os.SEEK_CUR)  # a hole stays one
                else:
                    dst.write(chunk)
        os.chmod(image, 0o644)
        os.replace(image, path)
        image = None
    finally:
        if fd is not None:
            os.close(fd)
        for f in (part, image):
            if f:
                try:
                    os.unlink(f)
                except FileNotFoundError:
                    pass
    return {"image": path, "fs": fs, "size": size, "label": label}
