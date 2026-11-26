# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The set's inputs and ports: io tuner, io usb, io soundbar and io mic."""

import argparse
import json
import os

from .. import remote
from ..core import Library
from .common import DURATION_HELP, Usage, duration, emit, progress, table, warn


def tuner_words(a):
    """io tuner's verb and channels: ("list", []), ("map", [RF...]) or ("unmap", [RF...] or None
    for all)."""
    verb, rest = a.verb, getattr(a, "channels", [])
    if verb == "list":
        return verb, []
    if verb == "unmap" and rest == ["all"]:
        return verb, None
    try:
        channels = [int(w) for w in rest]
    except ValueError:
        raise Usage(f"io tuner {verb}: {' '.join(rest)}: physical channel numbers") from None
    if verb == "map" and getattr(a, "stream", None) is not None and len(channels) != 1:
        raise Usage("io tuner map --stream FILE: one physical channel, RF, the stream's")
    return verb, channels


def tuner_text(name, data):
    if not data["tuner"]:
        return f"{name}: no tuner of ours on its board (no dtv_soc)"
    own = " (the VM's own)" if data.get("lineup") == "own" else ""
    lines = [f"{data.get('broadcast', 'atsc').upper()} lineup{own}"]
    for c in data["channels"]:
        what = ", ".join(c["services"])
        if c.get("stream"):
            what = f"stream {c['stream']}" + (f": {what}" if what else "")
        lines.append(
            f"RF{c['channel']:<3} {c['center_hz'] / 1e6:7.1f} MHz  "
            f"{'mapped  ' if c['mapped'] else 'unmapped'}  {what}"
        )
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


def lineup_text(name, data):
    """lineup set's and reset's text: what the VM's antenna carries now."""
    values = data["values"]
    rfs = ", ".join(
        f"RF{rf['channel']} ({len(rf['services'])} service{'s' if len(rf['services']) > 1 else ''})"
        for rf in values["rf"]
    )
    which = "its own" if data["lineup"] == "own" else "the built-in"
    return f"{name}: {which} {data['broadcast'].upper()} lineup: {rfs}"


def lineup_done(a, name, data):
    for w in data.pop("warnings", []):
        warn(w)
    if data.get("unmapped"):
        warn(
            f"{name}: RF {', '.join(map(str, data['unmapped']))} unmapped: the lineup has no such "
            "channel"
        )
    return emit(a, data, lineup_text(name, data))


def read_lineup_file(path):
    """(name, TOML text or None) of lineup set's FILE: "-" reads stdin."""
    import sys

    if path == "-":
        return "-", sys.stdin.read()
    return path, None


def cmd_tuner(lib, a):
    from .. import paths, tuner

    vm = lib.vm(a.name)
    if a.verb == "lineup":
        if a.lineup_verb == "show":
            data = tuner.lineup_show(vm)
            return emit(a, data, data["text"].rstrip("\n"))
        if a.lineup_verb == "set":
            path, text = read_lineup_file(a.file)
            if a.fed and text is None:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
            data = tuner.lineup_set(vm, paths.Tree(vm.library.repo), path, text, a.fed)
        else:
            data = tuner.lineup_reset(vm, paths.Tree(vm.library.repo))
        return lineup_done(a, a.name, data)
    verb, channels = tuner_words(a)
    if verb == "list":
        data = tuner.status(vm)
    else:
        tree = paths.Tree(vm.library.repo)
        if verb == "map":
            data = tuner.change(vm, tree, add=channels, fed=a.fed, stream=a.stream)
        else:
            data = tuner.change(vm, tree, remove=channels or (), clear=channels is None, fed=a.fed)
        for w in data.pop("warnings", []):
            warn(w)
    return emit(a, data, tuner_text(a.name, data))


def remote_lineup(a, host, name, uri, mirror, listed):
    """io tuner lineup for VM name on host: the host keeps the file (--fed), this machine, which
    feeds the VM, the file and the files it names (mirror) and makes the streams."""
    from .. import paths, tuner, tuner_lineup

    words = ["io", "tuner", "lineup", a.lineup_verb, remote.own(name)]
    if a.lineup_verb == "show":
        data = remote.call_json(host, uri, words)
        return emit(a, data, data["text"].rstrip("\n"))
    dest = os.path.join(mirror, tuner.LINEUP_DIR)
    if a.lineup_verb == "reset":
        data = remote.call_json(host, uri, words)
        import shutil

        shutil.rmtree(dest, ignore_errors=True)
        library = Library(a.library, a.repo)
        tuner.prune_cache(paths.Tree(library.repo), library)
        return lineup_done(a, f"{host}/{name}", data)
    path, text = read_lineup_file(a.file)
    broadcast = listed.get("broadcast", "atsc")
    spec = (
        tuner_lineup.load(path, broadcast)
        if text is None
        else tuner_lineup.parse(text, path, os.getcwd(), broadcast)
    )
    # the mapped channels' streams, made here, where the feed makes them: their errors now
    recorded = tuner.streams(mirror)
    new = tuner.from_spec(spec)
    wanted = [
        c["channel"]
        for c in listed["channels"]
        if c["mapped"] and c["channel"] in new.channels and c["channel"] not in recorded
    ]
    cache = tuner.cache_dir(paths.Tree(Library(a.library, a.repo).repo))
    tuner.make(cache, wanted, lu=new)
    os.makedirs(mirror, exist_ok=True)
    tuner_lineup.install(path, dest, broadcast, text)
    rc, out, err = remote.call(
        host, uri, [*words, "-", "--fed", "--json"], stdin=spec["text"].encode()
    )
    if rc:
        raise remote.HostError(host, err.strip() or f"io tuner lineup set: status {rc}")
    library = Library(a.library, a.repo)
    tuner.prune_cache(paths.Tree(library.repo), library)
    return lineup_done(a, f"{host}/{name}", json.loads(out))


def remote_tuner(a, host, name):
    """tuner for VM name on host: map and unmap change the map there (--fed: no streams made
    there) and keep a feed from this machine running while anything is mapped and the VM runs
    (tuner.start_feed, stop_feed). The VM's own lineup and the user's streams are kept here too,
    in this machine's directory for it (tuner.remote_dir), where the feed reads them."""
    from .. import paths, tuner

    uri = remote.hosts()[host]
    mirror = tuner.remote_dir(Library(a.library, a.repo).root, host, name)
    if a.verb == "lineup":
        listed = {}
        if a.lineup_verb == "set":
            listed = remote.call_json(host, uri, ["io", "tuner", "list", remote.own(name)])
        return remote_lineup(a, host, name, uri, mirror, listed)
    verb, channels = tuner_words(a)
    warnings = []
    if verb == "list":
        data = remote.call_json(host, uri, ["io", "tuner", "list", remote.own(name)])
    else:
        # the host's board's lineup: its broadcast standard, and the VM's own lineup, kept here
        listed = remote.call_json(host, uri, ["io", "tuner", "list", remote.own(name)])
        broadcast = listed.get("broadcast") if isinstance(listed, dict) else None
        lu = tuner.vm_lineup(mirror, broadcast)
        cache = tuner.cache_dir(paths.Tree(Library(a.library, a.repo).repo))
        recorded = tuner.streams(mirror)
        stream = getattr(a, "stream", None)
        words = [*(map(str, channels) if channels is not None else ["all"]), "--fed"]
        if verb == "map" and stream is not None:
            (ch,) = channels
            rec = tuner.inspect_stream(stream, lu, ch)
            warnings = rec["warnings"]
            tuner.drop_streams(mirror, recorded, [ch])
            recorded[ch] = tuner.put_stream(mirror, ch, stream, rec)
            words += ["--stream", os.path.basename(stream)]
        elif verb == "map":
            tuner.check_channels(channels, lu)
            tuner.make(cache, channels, lu=lu)  # here, where the feed makes them: its errors now
            tuner.drop_streams(mirror, recorded, channels)
        else:
            tuner.check_channels(channels or [], lu, recorded)
            tuner.drop_streams(mirror, recorded, list(recorded) if channels is None else channels)
        tuner.save_map(mirror, [], recorded)
        data = remote.call_json(host, uri, ["io", "tuner", verb, remote.own(name), *words])
        if not isinstance(data, dict) or "feed_socket" not in data:
            raise remote.HostError(host, "its beanjar io tuner has no feeds: update it")
        data.pop("warnings", None)
        if not any(c["mapped"] for c in data["channels"]):
            tuner.stop_feed(host, name)
        elif verb == "map" and data["connected"]:
            tuner.start_feed(host, uri, name, data["feed_socket"], cache, content=mirror)
        elif verb == "map":
            warnings.append(
                f"{host}/{name} is not running: map again once it runs to feed it from here"
            )
    for w in warnings:
        warn(w)
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


def gamepad_controls(a):
    """io usb gamepad press's controls, checked here, also before they go to another host."""
    from .. import gamepad

    try:
        return gamepad.check_controls(a.controls)
    except gamepad.Error as e:
        raise Usage(f"io usb gamepad press: {e}") from None


def cmd_gamepad(lib, a):
    from .. import gamepad

    controls = gamepad_controls(a) if a.gamepad_verb == "press" else None
    vm = lib.vm(a.name)
    if a.gamepad_verb == "attach":
        data = gamepad.attach(vm, a.id)
        text = (
            f"{a.name}: an Xbox 360 controller ({data['device']}) plugged in as USB device "
            f"{data['id']}"
        )
        return emit(a, data, text)
    if a.gamepad_verb == "press":
        data = gamepad.press(vm, controls, a.hold if a.hold is not None else gamepad.HOLD, a.id)
        if not data["polled"]:
            warn(
                f"{a.name}: the guest does not poll the pad (nothing has its input device open), "
                "so the presses went nowhere"
            )
        text = f"{a.name}: pressed {' '.join(data['controls'])} ({data['reports']} reports)"
        return emit(a, data, text)
    data = gamepad.detach(vm, a.id)
    return emit(a, data, f"{a.name}: gamepad {data['id']} unplugged")


def soundbar_text(name, data):
    if not data["cec"] and not data["earc"]:
        return f"{name}: no sound bar connection of ours on its board (no CEC bus, no eARC)"
    lines = []
    if data["attached"]:
        st = data["status"] or {}
        lines.append(
            f"{name}: {'eARC' if data['link'] == 'earc' else 'ARC'} sound bar on HDMI "
            f"{data['port']}"
            + (f": {', '.join(data['formats'])}" if data.get("formats") else "")
            + (f"; ARC {'on' if st.get('arc') else 'off'}" if st else "")
            + (f", system audio {'on' if st.get('system_audio') else 'off'}" if st else "")
            + (f", volume {st['volume']}{' (muted)' if st.get('mute') else ''}" if st else "")
        )
    else:
        lines.append(f"{name}: no sound bar")
    link = data.get("earc_status")
    if link:
        lines.append(
            f"eARC: {'enabled' if link['enabled'] else 'not enabled'} by the TV, "
            f"connection {link['status']}"
        )
    tv = data.get("tv") or {}
    if "arcActivation" in tv:
        lines.append(
            f"TV CEC: ARC {'active' if tv.get('arcActivation') else 'not active'}"
            + (f", SADs {tv['arcSADs']}" if tv.get("arcSADs") else ", no SADs")
            + (f", profile {tv['arcProfile']}" if tv.get("arcProfile") else "")
        )
    arc = tv.get("arccontroller")
    if arc:
        lines.append(
            f"TV: {arc.get('arcActivation') or 'not known'}"
            + (
                f", capability {', '.join(arc['earcCapability'])}"
                if arc.get("earcCapability")
                else ""
            )
            + (", Atmos" if arc.get("earcATMOS") else "")
            + (f", SADs {' '.join(arc['earcSADs'])}" if arc.get("earcSADs") else "")
        )
    return "\n".join(lines)


def cmd_soundbar(lib, a):
    from .. import soundbar

    vm = lib.vm(a.name)
    if a.verb == "attach":
        try:
            names = soundbar.parse_formats(a.formats or ",".join(soundbar.DEFAULT_FORMATS))
        except ValueError as e:
            raise Usage(f"io soundbar attach: --formats: {e}") from None
        data = soundbar.attach(vm, names, a.arc_only)
    elif a.verb == "detach":
        data = soundbar.detach(vm)
    else:
        data = soundbar.show(vm)
    return emit(a, data, soundbar_text(a.name, data))


def cmd_mic(lib, a):
    from .. import mic

    vm = lib.vm(a.name)
    data = mic.play(vm, a.file)
    return emit(a, data, f"{a.name}: sent {data['seconds']:g} s of {a.file} to the microphone")


def register(cli):
    command, group, vm_name = cli.command, cli.group, cli.vm_name
    io = group(
        "io",
        "the hardware a set talks to, from outside: USB devices, the tuner's antenna, a sound bar "
        "and the sound the built-in microphone hears",
        dest="area",
        metavar="DEVICE",
    )
    tuner = group(
        "tuner",
        "the antenna of a set with our tuner: its lineup (the built-in one of its board's broadcast "
        "standard, US ATSC channels or a Japanese ISDB-T one on an ARIB board, or the VM's own: "
        "lineup set), its physical channels and what the set is tuned to (list); map RF... to put "
        "a signal on them (their transport streams, with the channel tables and guide, made with "
        "TSDuck and ffmpeg; or a recording of yours, --stream, looped), unmap RF... or all; kept "
        "for the VM's next starts. For HOST/NAME the streams are made on this machine and fed to "
        "the host over ssh while the VM runs",
        under=io,
        dest="verb",
    )
    s = command(
        tuner,
        "list",
        "the lineup's physical channels and those of your streams, which are mapped, and what the "
        "set is tuned to",
        cmd_tuner,
    )
    vm_name(s, "tuner")
    s = command(
        tuner,
        "map",
        "put a signal on physical channels RF...: the lineup's streams, or with --stream one RF "
        "that plays your transport stream",
        cmd_tuner,
        usage="beanjar io tuner map NAME RF... [--stream FILE] [--json]",
    )
    vm_name(s, "tuner")
    s.add_argument("channels", nargs="+", metavar="RF")
    s.add_argument(
        "--stream",
        metavar="FILE",
        help="a transport stream (.ts, 188-byte packets, not scrambled, with a PAT and a PCR) for "
        "the one RF, copied into the VM's state and looped at its own rate with the time tables set "
        "to now (ATSC STT, ARIB TDT and TOT); its own tables are kept as they are, so they should "
        "be of the board's standard and name the RF's frequency (warnings otherwise). Map the RF "
        "again without it to go back to the lineup's stream",
    )
    s.add_argument("--fed", action="store_true", help=argparse.SUPPRESS)
    s = command(
        tuner,
        "unmap",
        "take the signal off physical channels RF..., or all; a stream of yours on one is removed",
        cmd_tuner,
        usage="beanjar io tuner unmap NAME RF|all... [--json]",
    )
    vm_name(s, "tuner")
    s.add_argument("channels", nargs="+", metavar="RF|all")
    # from another machine's map and unmap (remote_tuner): its feed makes the streams
    s.add_argument("--fed", action="store_true", help=argparse.SUPPRESS)

    lineup = group(
        "lineup",
        "the VM's lineup: the physical channels, services, programme events, pictures and sounds "
        "(and, on ARIB, BML data broadcasts) its antenna carries, as a TOML file "
        "(docs/user/tuner.md); the built-in one of its board's broadcast standard unless set",
        under=tuner,
        dest="lineup_verb",
    )
    s = command(
        lineup,
        "show",
        "the lineup in effect, the VM's own or the built-in one, as TOML (with --json, its values "
        "as JSON)",
        cmd_tuner,
    )
    vm_name(s, "tuner")
    s = command(
        lineup,
        "set",
        "give the VM its own lineup: FILE is checked (its broadcast is the board's standard, the "
        "files it names are there), the streams of the mapped channels are made, then FILE and the "
        "files it names are copied into the VM's state, for its next starts; mapped channels the "
        "lineup lacks are unmapped. FILE - reads the TOML from stdin, its paths relative to the "
        "current folder",
        cmd_tuner,
        usage="beanjar io tuner lineup set NAME FILE [--json]",
    )
    vm_name(s, "tuner")
    s.add_argument("file", metavar="FILE")
    s.add_argument("--fed", action="store_true", help=argparse.SUPPRESS)
    s = command(
        lineup,
        "reset",
        "back to the built-in lineup of the board's broadcast standard",
        cmd_tuner,
    )
    vm_name(s, "tuner")

    g = group(
        "usb",
        "USB devices plugged into a running VM: a device of a machine over usbredir, a virtual "
        "camera or gamepad, or a disk image as a USB stick; on its xHCI, gone when the VM stops "
        "(not a setting)",
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
        "the VM's USB devices: id, kind (redir, gamepad or storage), whether a client is connected to a "
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

    from ..gamepad import CONTROLS, DEVICE

    pad = group(
        "gamepad",
        f"a virtual gamepad: a wired Xbox 360 controller ({DEVICE}) with the real one's "
        "descriptors, which the guest's own xpad driver binds, made on the VM's host by "
        "beanjar-virtpad and fed into the running VM over usbredir (a usb-redir device, as io usb "
        "attach); it stays plugged in after attach, across a restart of the guest, until detach "
        "or the VM stops",
        under=g,
        dest="gamepad_verb",
    )
    s = command(
        pad,
        "attach",
        "plug a gamepad into the running VM; returns once the guest has it",
        cmd_gamepad,
        usage="beanjar io usb gamepad attach NAME [--id ID] [--json]",
    )
    vm_name(s)
    s.add_argument("--id", metavar="ID", help="its USB id (default the lowest free number)")
    s = command(
        pad,
        "press",
        "press and release each CONTROL in turn, each held --hold and released as long, as "
        "input reports to the guest; they arrive only while the guest polls the pad (an app or "
        "a reader has its input device open; otherwise a warning)",
        cmd_gamepad,
        usage="beanjar io usb gamepad press NAME CONTROL... [--hold DURATION] [--id ID] [--json]",
    )
    vm_name(s, "gamepad-press")
    s.add_argument(
        "controls",
        nargs="+",
        metavar="CONTROL",
        help="a button, a trigger (pulled all the way) or a direction of the d-pad: "
        + ", ".join(CONTROLS),
    )
    s.add_argument(
        "--hold",
        type=duration,
        metavar="DURATION",
        help=f"how long each control is held, and released after it (default 100ms); "
        f"{DURATION_HELP}",
    )
    s.add_argument("--id", metavar="ID", help="the gamepad, when the VM has more than one")
    s = command(
        pad,
        "detach",
        "unplug the VM's gamepad: the guest sees it go, and its program on the VM's host ends",
        cmd_gamepad,
        usage="beanjar io usb gamepad detach NAME [--id ID] [--json]",
    )
    vm_name(s)
    s.add_argument("--id", metavar="ID", help="the gamepad, when the VM has more than one")

    g = group(
        "soundbar",
        "a sound bar on the set's HDMI ARC input: on its CEC bus an audio system that answers the "
        "ARC handshake, System Audio Mode and the TV's request for its audio formats (short audio "
        "descriptors), and on its eARC transmitter an eARC receiver with the same formats, as a "
        "bar plugged in and switched on; on a running VM whose board has either of ours (CEC: the "
        "O22 family, the lm21 boards; eARC: the K8 family and O22's), gone when the VM or its "
        "guest restarts (not a setting). The TV uses CEC with its HDMI-CEC (SIMPLINK) setting on "
        "and eARC with eARC Support on; with that off, or with an ARC-only bar, it turns ARC on "
        "over CEC",
        under=io,
        dest="verb",
    )
    from ..soundbar import DEFAULT_FORMATS, FORMATS

    s = command(
        g,
        "attach",
        "plug a sound bar into the TV's ARC input (its firmware's system.arcPort or arc.port) and "
        "switch it on: it reports itself over CEC, and the TV asks it for ARC and its formats; on "
        "an eARC board it is an eARC bar, whose formats the TV reads from its eARC capability",
        cmd_soundbar,
        usage="beanjar io soundbar attach NAME [--formats FORMAT,...] [--arc-only] [--json]",
    )
    vm_name(s)
    s.add_argument(
        "--formats",
        metavar="FORMAT,...",
        help="the audio formats the bar decodes, listed in that order: "
        + "; ".join(f"{k} ({v[0]})" for k, v in FORMATS.items())
        + f". eac3 and eac3-atmos exclude each other. Default {','.join(DEFAULT_FORMATS)}",
    )
    s.add_argument(
        "--arc-only",
        action="store_true",
        help="a bar without eARC: the TV's eARC discovery fails (IDLE2) and it turns ARC on over "
        "CEC even with eARC Support on; needs the board's CEC bus",
    )
    s = command(
        g,
        "detach",
        "switch the sound bar off and unplug it: it turns System Audio Mode and ARC off, as a bar "
        "going to standby does, and leaves the bus a second later; its eARC connection ends",
        cmd_soundbar,
    )
    vm_name(s)
    s = command(
        g,
        "show",
        "the VM's sound bar (its ARC input, formats, and what it agreed with the TV: ARC, System "
        "Audio Mode, the volume; the eARC connection) and the TV's view (eim's getCECState: "
        "arcActivation, arcSADs, arcProfile; arccontroller's getARCState: arcActivation ARC or "
        "eARC, the eARC capability; where the firmware has them)",
        cmd_soundbar,
    )
    vm_name(s)
    g = group(
        "mic",
        'the sound around a set with LG\'s built-in far-field microphone (the one "Hey LG" '
        "wakes), which such a model always has: it hears silence but for the sound files played "
        "to it, at real time, as its capture card records them",
        under=io,
        dest="verb",
    )
    s = command(
        g,
        "play",
        "play the sound file FILE (any format ffmpeg reads; - for stdin) once to the microphone; "
        "returns when it has been sent, with up to about 4 s of it still to be heard",
        cmd_mic,
        usage="beanjar io mic play NAME FILE [--json]",
    )
    vm_name(s, "mic-play")
    s.add_argument("file", metavar="FILE", help="the sound file on this machine, or - for stdin")
