# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The set's inputs and ports: io tuner and io usb."""

import argparse
import json
import os

from .. import remote
from ..core import Library
from .common import Usage, emit, progress, table, warn


def tuner_words(a):
    """io tuner's verb and channels: ("list", []), ("map", [RF...]) or ("unmap", [RF...] or None
    for all)."""
    verb, rest = a.verb, getattr(a, "channels", [])
    if verb == "list":
        return verb, []
    if verb == "unmap" and rest == ["all"]:
        return verb, None
    try:
        return verb, [int(w) for w in rest]
    except ValueError:
        raise Usage(f"io tuner {verb}: {' '.join(rest)}: physical channel numbers") from None


def tuner_text(name, data):
    if not data["tuner"]:
        return f"{name}: no tuner of ours on its board (no dtv_soc)"
    lines = [f"{data.get('broadcast', 'atsc').upper()} lineup"] + [
        f"RF{c['channel']:<3} {c['center_hz'] / 1e6:7.1f} MHz  {'mapped  ' if c['mapped'] else 'unmapped'}  "
        + ", ".join(c["services"])
        for c in data["channels"]
    ]
    tuned = data["tuned_hz"]
    lines.append(
        "tuned: "
        + (f"{tuned / 1e6:.3f} MHz" if tuned else "nothing yet")
        + f", {data['sent_packets']} packets sent"
        + (", fed from another machine" if data.get("fed") else "")
        + ("" if data["connected"] else " (the tuner is not connected: VM off, or starting)")
    )
    feed = data.get("feed")
    if feed is not None:
        lines.append(
            f"feed from this machine: pid {feed['pid']}" if feed else "feed from this machine: none"
        )
    return "\n".join(lines)


def cmd_tuner(lib, a):
    from .. import paths, tuner

    vm = lib.vm(a.name)
    verb, channels = tuner_words(a)
    if verb == "list":
        data = tuner.status(vm)
    else:
        tree = paths.Tree(vm.library.repo)
        if verb == "map":
            data = tuner.change(vm, tree, add=channels, fed=a.fed)
        else:
            data = tuner.change(vm, tree, remove=channels or (), clear=channels is None, fed=a.fed)
    return emit(a, data, tuner_text(a.name, data))


def remote_tuner(a, host, name):
    """tuner for VM name on host: map and unmap change the map there (--fed: no streams made
    there) and keep a feed from this machine running while anything is mapped and the VM runs
    (tuner.start_feed, stop_feed)."""
    from .. import paths, tuner

    verb, channels = tuner_words(a)
    uri = remote.hosts()[host]
    if verb == "list":
        data = remote.call_json(host, uri, ["io", "tuner", "list", remote.own(name)])
    else:
        # the host's board's lineup: its broadcast standard
        listed = remote.call_json(host, uri, ["io", "tuner", "list", remote.own(name)])
        lu = tuner.lineup(listed.get("broadcast") if isinstance(listed, dict) else None)
        tuner.check_channels(channels or [], lu)
        cache = tuner.cache_dir(paths.Tree(Library(a.library, a.repo).repo))
        if verb == "map":
            tuner.make(cache, channels, lu=lu)  # here, where the feed makes them: its errors now
        words = [*(map(str, channels) if channels is not None else ["all"]), "--fed"]
        data = remote.call_json(host, uri, ["io", "tuner", verb, remote.own(name), *words])
        if not isinstance(data, dict) or "feed_socket" not in data:
            raise remote.HostError(host, "its beanjar io tuner has no feeds: update it")
        if not any(c["mapped"] for c in data["channels"]):
            tuner.stop_feed(host, name)
        elif verb == "map" and data["connected"]:
            tuner.start_feed(host, uri, name, data["feed_socket"], cache)
        elif verb == "map":
            warn(f"{host}/{name} is not running: map again once it runs to feed it from here")
    if isinstance(data, dict) and data.get("tuner"):
        info = tuner.find_feed(host, name)
        data["feed"] = {"pid": info["pid"]} if info else {}
    return emit(a, data, tuner_text(f"{host}/{name}", data))


def usb_attach_usage(a, remote_host=None):
    """io usb attach's argument checks, here or before it goes to remote_host."""
    if sum(map(bool, (a.image, a.device, a.camera))) > 1:
        raise Usage("io usb attach: one of --image, --device or --camera")
    if a.readonly and not a.image:
        raise Usage("io usb attach: --readonly goes with --image")
    if a.sudo and not a.device:
        raise Usage("io usb attach: --sudo goes with --device")
    if a.pattern and not a.camera:
        raise Usage("io usb attach: --pattern goes with --camera")
    if remote_host and a.image and not os.path.isabs(a.image):
        raise Usage(
            f"io usb attach {remote_host}/...: --image is a path on {remote_host}, the image is not "
            f"copied; give it absolute ({a.image!r})"
        )


def cmd_usb_attach(lib, a):
    from .. import usb

    usb_attach_usage(a)
    if a.device or a.camera:
        return plug_usb(lib, a)
    if a.image:
        data = usb.attach_image(lib.vm(a.name), a.image, a.readonly, a.id)
        ro = " (read-only)" if data["readonly"] else ""
        text = f"{a.name}: {data['image']}{ro} attached as USB device {data['id']}"
        return emit(a, {"name": a.name} | data, text)
    data = usb.attach(lib.vm(a.name), a.id)
    return emit(a, {"name": a.name} | data, data["socket"])


def cmd_usb_mkimage(lib, a):
    from .. import usb

    size = usb.parse_size(a.size)
    data = usb.make_image(a.file, a.fs, size, a.label, a.overwrite)
    text = f"{data['image']}: {a.fs}, {size // usb.MIB} MiB, label {data['label']}"
    return emit(a, data, text)


def cmd_usb_detach(lib, a):
    from .. import usb

    data = usb.detach(lib.vm(a.name), a.id)
    return emit(a, {"name": a.name} | data, f"{a.name}: USB device {a.id} detached")


def usb_source(d):
    """usb list's SOURCE: a usb-redir device's socket, a stick's image."""
    if d.get("kind") == "storage":
        return (d.get("image") or "-") + (" (read-only)" if d.get("readonly") else "")
    return d.get("socket") or "-"


def usb_rows(devices):
    return table(
        devices,
        [
            ("ID", lambda d: d["id"], False),
            ("KIND", lambda d: d.get("kind", "redir"), False),
            ("CLIENT", lambda d: "connected" if d.get("connected") else "-", False),
            ("GUEST", lambda d: "attached" if d["attached"] else "-", False),
            ("SOURCE", usb_source, False),
        ],
    )


def cmd_usb_list(lib, a):
    from .. import usb

    st = usb.state(lib.vm(a.name))
    devices = st["devices"]
    if st["restarting"]:
        text = f"{a.name}: restarting; its USB devices come back with the new QEMU"
    else:
        text = usb_rows(devices) if devices else f"{a.name}: no USB devices attached"
    return emit(a, {"name": a.name, **st}, text)


def plug_usb(lib, a, host=None):
    """io usb attach --device or --camera: a device of this machine, or the virtual camera, fed
    into lib's VM a.name, or with host, that host's VM a.name, until this command ends."""
    from .. import usb

    usb_attach_usage(a)
    if a.camera:
        client = usb.Camera(a.pattern or "smpte")
    else:
        client = usb.Usbredirect(usb.find_device(a.device), a.sudo)
    if a.id is not None:
        usb.check_id(a.id)

    def ready(data):
        if a.json:
            print(json.dumps(data), flush=True)
        else:
            what = data["device"] + (f" ({data['product']})" if data.get("product") else "")
            print(
                f"{data['name']}: {what} plugged in as USB device {data['id']}; Ctrl-C unplugs it",
                flush=True,
            )

    out = None if a.json else progress
    if host:
        return usb.plug_remote(host, a.name, client, a.id, ready, out)
    return usb.plug_local(lib.vm(a.name), client, a.id, ready, out)


def register(cli):
    command, group, vm_name = cli.command, cli.group, cli.vm_name
    io = group(
        "io",
        "the hardware a set talks to, from outside: USB devices and the tuner's antenna",
        dest="area",
        metavar="DEVICE",
    )
    tuner = group(
        "tuner",
        "the antenna of a set with our tuner: list the lineup of its board's broadcast standard "
        "(US ATSC channels, or a Japanese ISDB-T one on an ARIB board), its physical channels "
        "and what the set is tuned to, map RF... to put a signal on them (their transport "
        "streams, with the channel tables and guide, are made with TSDuck and ffmpeg; for HOST/NAME on this machine, fed to the host over ssh while the VM runs), or "
        "unmap RF... or all; kept for the VM's next starts",
        under=io,
        dest="verb",
    )
    s = command(
        tuner,
        "list",
        "the lineup's physical channels, which are mapped, and what the set is tuned to",
        cmd_tuner,
    )
    vm_name(s, "tuner")
    for verb, text, metavar in (
        ("map", "put a signal on physical channels RF...", "RF"),
        ("unmap", "take the signal off physical channels RF..., or all", "RF|all"),
    ):
        s = command(
            tuner,
            verb,
            text,
            cmd_tuner,
            usage=f"beanjar io tuner {verb} NAME {metavar}... [--json]",
        )
        vm_name(s, "tuner")
        s.add_argument("channels", nargs="+", metavar=metavar)
        # from another machine's map and unmap (remote_tuner): its feed makes the streams
        s.add_argument("--fed", action="store_true", help=argparse.SUPPRESS)

    g = group(
        "usb",
        "USB devices plugged into a running VM: a device of a machine over usbredir, or a disk "
        "image as a USB stick; on its xHCI, gone when the VM stops (not a setting)",
        under=io,
    )
    s = command(
        g,
        "attach",
        "attach a USB device to the running VM. Without a source: a usb-redir device, its unix "
        "socket in the VM's run directory (run/usb-ID.sock), printed; the guest sees the device "
        "once a usbredir client (usbredirect, through a TCP relay or an ssh -L forward to the "
        "socket) connects. With --image: a raw disk image as a USB stick (usb-storage). With "
        "--device or --camera: a USB device of this machine, or the virtual camera, fed to it "
        "from here (HOST/NAME: over the host's ssh connection) until Ctrl-C, SIGTERM or SIGHUP, "
        "or until the device is unplugged, then detached; it prints one line (with --json a "
        "JSON object) once the guest has the device",
        cmd_usb_attach,
        usage="beanjar io usb attach NAME [--image FILE [--readonly] | --device "
        "VENDOR:PRODUCT|BUS-DEVICE [--sudo] | --camera [--pattern NAME]] [--id ID] [--json]",
    )
    vm_name(s, "usb-attach")
    s.add_argument("--id", metavar="ID", help="its id (default the lowest free number, 1, 2, ...)")
    s.add_argument(
        "--image",
        metavar="FILE",
        help="a raw disk image on the VM's host, the stick's medium (for HOST/NAME an absolute "
        "path there: it is not copied); the guest writes to it unless --readonly",
    )
    s.add_argument("--readonly", action="store_true", help="with --image: open the image read-only")
    s.add_argument(
        "--device",
        metavar="VENDOR:PRODUCT|BUS-DEVICE",
        help="a device of this machine, as lsusb shows it: 046d:082d (hex), or 1-4 (bus and "
        "device number), fed through usbredirect",
    )
    s.add_argument(
        "--sudo",
        action="store_true",
        help="with --device: run usbredirect through sudo, for a device node that is not "
        "writable for you",
    )
    s.add_argument(
        "--camera",
        action="store_true",
        help="a virtual webcam: a Logitech C920 (046d:082d), as LG's camera service accepts, "
        "showing GStreamer's test pattern; made on this machine (beanjar-virtcam, "
        "gst-launch-1.0), so a remote VM's host needs nothing of it",
    )
    s.add_argument(
        "--pattern",
        metavar="NAME",
        help="with --camera: the test pattern, a videotestsrc pattern (default smpte; ball, "
        "snow, colors, ...)",
    )
    s = command(
        g,
        "detach",
        "remove the VM's USB device ID (a usb-redir device or a stick): the guest sees it "
        "unplugged; a stick's image is released",
        cmd_usb_detach,
    )
    vm_name(s)
    s.add_argument("id", metavar="ID")
    s = command(
        g,
        "list",
        "the VM's USB devices: id, kind (redir or storage), whether a client is connected to a "
        "usb-redir device's socket, whether the guest has the device, the socket or the image",
        cmd_usb_list,
    )
    vm_name(s)
    s = command(
        g,
        "mkimage",
        "make a disk image for io usb attach --image on this machine: an MBR with one partition "
        "from 1 MiB to the end, a FAT32, exFAT or NTFS filesystem in it (sfdisk, mkfs.vfat, "
        "mkfs.exfat, mkfs.ntfs)",
        cmd_usb_mkimage,
        usage="beanjar io usb mkimage FILE --fs fat32|exfat|ntfs [--size 64M] [--label LABEL] "
        "[--overwrite] [--json]",
    )
    s.add_argument("file", metavar="FILE")
    s.add_argument("--fs", required=True, choices=["fat32", "exfat", "ntfs"])
    s.add_argument("--size", default="64M", help="the disk's size, whole MiB (default 64M)")
    s.add_argument("--label", default="BEANJAR", help="the filesystem's label (default BEANJAR)")
    s.add_argument("--overwrite", action="store_true", help="replace FILE if it exists")
