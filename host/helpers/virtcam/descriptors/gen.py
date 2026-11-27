#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Parse a `lsusb -v` dump of a UVC camera into byte-exact USB descriptors.

usage: python3 -I gen.py <lsusb.txt> <outdir> [--vc-intr-maxtransfer N]
                         [--compressed-alt WMAXPACKETSIZE:BINTERVAL]... [--verbose]

The virtual camera's build runs it (host/helpers/virtcam/meson.build) on the C920's dump beside it
(c920-046d-082d-lsusb.txt, from libuvc's cameras/logitech_hd_pro_920.txt) for descriptors.h.
The bytes were checked against the dump (wTotalLength 3452, the 5-byte
class-specific interrupt endpoint descriptor that lsusb does not print). Emits device.bin,
qualifier.bin, config.bin, string0..2.bin, descriptors.h, layout.txt (every descriptor with offset
and length) and prints the checks (all with --verbose, else only on a failure). Standard library
only.

Each --compressed-alt adds one VideoStreaming alternate setting after the dump's last one (in the
order given), with the same endpoint at the wMaxPacketSize and bInterval given: the virtual
camera's own settings for compressed formats, which a real C920 does not have. The total length
check then expects the dump's wTotalLength plus 16 bytes for each.
"""

import os
import re
import struct
import sys

SERIAL = "00000000"  # fixed, non-identifying (real unit had a hex serial)
LANGID = 0x0409


def bcd(tok):
    a, b = tok.split(".")
    return (int(a, 16) << 8) | int(b, 16)


def num(tok):
    return int(tok, 0)


def parse_blocks(text):
    """Return (device_fields, [blocks]) for the first configuration, in file order."""
    blocks = []  # dicts: kind, f (fields), arr (indexed fields), line
    cur = None
    section = "dev"
    hdr = re.compile(
        r"^(\s*)((?:\w+ )?(?:Interface Descriptor|Endpoint Descriptor|Interface Association|Configuration Descriptor|Device Descriptor|Device Qualifier)[^:]*):\s*$"
    )
    kv = re.compile(r"^\s*(\w+)\s*(?:[\(\[]\s*(\d+)\s*[\)\]])?\s+(\S.*?)\s*$")
    for ln, raw in enumerate(text.splitlines(), 1):
        m = hdr.match(raw)
        if m:
            name = m.group(2).strip()
            if name.startswith("Device Qualifier"):
                break
            cur = {"kind": name, "f": {}, "arr": {}, "line": ln}
            blocks.append(cur)
            continue
        if cur is None:
            continue
        m = kv.match(raw)
        if not m:
            continue
        k, idx, v = m.group(1), m.group(2), m.group(3)
        if idx is not None:
            cur["arr"].setdefault(k, {})[int(idx)] = v
        else:
            cur["f"].setdefault(k, v)  # first occurrence wins
    dev = blocks[0]
    assert dev["kind"] == "Device Descriptor"
    return dev, blocks[1:]


def tok(b, k):
    return b["f"][k].split()[0]


def I(b, k):
    return num(tok(b, k))


def arr(b, k, n, conv=num):
    d = b["arr"][k]
    return [conv(d[i].split()[0]) for i in range(n)]


def guid(s):
    h = re.sub(r"[^0-9a-fA-F]", "", s.strip("{}").split()[0])
    assert len(h) == 32
    return bytes.fromhex(h)  # lsusb prints the GUID bytes in wire order


def ctl_bytes(val, n):
    return val.to_bytes(n, "little")


def hdr3(length, dtype, sub):
    return bytes([length, dtype, sub])


def pack(b, vc_intr_max):
    """Return descriptor bytes for one parsed block (kind-specific)."""
    k = b["kind"]
    if k == "Configuration Descriptor":
        return None  # built last, needs total length
    if k == "Interface Association":
        return bytes(
            [
                8,
                11,
                I(b, "bFirstInterface"),
                I(b, "bInterfaceCount"),
                I(b, "bFunctionClass"),
                I(b, "bFunctionSubClass"),
                I(b, "bFunctionProtocol"),
                I(b, "iFunction"),
            ]
        )
    if k == "Interface Descriptor":
        return bytes(
            [
                9,
                4,
                I(b, "bInterfaceNumber"),
                I(b, "bAlternateSetting"),
                I(b, "bNumEndpoints"),
                I(b, "bInterfaceClass"),
                I(b, "bInterfaceSubClass"),
                I(b, "bInterfaceProtocol"),
                I(b, "iInterface"),
            ]
        )
    if k == "Endpoint Descriptor":
        out = struct.pack(
            "<BBBBHB",
            7,
            5,
            I(b, "bEndpointAddress"),
            I(b, "bmAttributes"),
            I(b, "wMaxPacketSize"),
            I(b, "bInterval"),
        )
        if "bRefresh" in b["f"]:  # audio endpoint, 9 bytes
            out = bytes([9]) + out[1:] + bytes([I(b, "bRefresh"), I(b, "bSynchAddress")])
        return out
    sub = I(b, "bDescriptorSubtype")
    if k == "VideoControl Interface Descriptor":
        if sub == 1:  # HEADER
            n = I(b, "bInCollection")
            clk = int(round(float(re.match(r"[\d.]+", tok(b, "dwClockFrequency")).group()) * 1e6))
            return (
                hdr3(12 + n, 36, 1)
                + struct.pack("<HHIB", bcd(tok(b, "bcdUVC")), I(b, "wTotalLength"), clk, n)
                + bytes(arr(b, "baInterfaceNr", n))
            )
        if sub == 2:  # INPUT_TERMINAL (camera)
            n = I(b, "bControlSize")
            return (
                hdr3(15 + n, 36, 2)
                + struct.pack(
                    "<BHBB",
                    I(b, "bTerminalID"),
                    I(b, "wTerminalType"),
                    I(b, "bAssocTerminal"),
                    I(b, "iTerminal"),
                )
                + struct.pack(
                    "<HHH",
                    I(b, "wObjectiveFocalLengthMin"),
                    I(b, "wObjectiveFocalLengthMax"),
                    I(b, "wOcularFocalLength"),
                )
                + bytes([n])
                + ctl_bytes(I(b, "bmControls"), n)
            )
        if sub == 3:  # OUTPUT_TERMINAL
            return hdr3(9, 36, 3) + struct.pack(
                "<BHBBB",
                I(b, "bTerminalID"),
                I(b, "wTerminalType"),
                I(b, "bAssocTerminal"),
                I(b, "bSourceID"),
                I(b, "iTerminal"),
            )
        if sub == 5:  # PROCESSING_UNIT, UVC 1.0 layout: no bmVideoStandards
            n = I(b, "bControlSize")  # (lsusb prints it past the end: "too short")
            return (
                hdr3(8 + n + 1, 36, 5)
                + struct.pack(
                    "<BBHB", I(b, "bUnitID"), I(b, "bSourceID"), I(b, "wMaxMultiplier"), n
                )
                + ctl_bytes(I(b, "bmControls"), n)
                + bytes([I(b, "iProcessing")])
            )
        if sub == 6:  # EXTENSION_UNIT
            pins = I(b, "bNrPins")
            n = I(b, "bControlSize")
            return (
                hdr3(24 + pins + n, 36, 6)
                + bytes([I(b, "bUnitID")])
                + guid(b["f"]["guidExtensionCode"])
                + bytes([I(b, "bNumControl"), pins])
                + bytes(arr(b, "baSourceID", pins))
                + bytes([n])
                + bytes(arr(b, "bmControls", n))
                + bytes([I(b, "iExtension")])
            )
    if k == "VideoStreaming Interface Descriptor":
        if sub == 1:  # INPUT_HEADER
            nf = I(b, "bNumFormats")
            n = I(b, "bControlSize")
            return (
                hdr3(13 + nf * n, 36, 1)
                + struct.pack(
                    "<BHBBBBBBB",
                    nf,
                    I(b, "wTotalLength"),
                    I(b, "bEndPointAddress"),
                    I(b, "bmInfo"),
                    I(b, "bTerminalLink"),
                    I(b, "bStillCaptureMethod"),
                    I(b, "bTriggerSupport"),
                    I(b, "bTriggerUsage"),
                    n,
                )
                + bytes(arr(b, "bmaControls", nf * n))
            )
        il = lambda: bytes([I(b, "bmInterlaceFlags")])
        common = lambda: bytes(
            [
                I(b, "bDefaultFrameIndex"),
                I(b, "bAspectRatioX"),
                I(b, "bAspectRatioY"),
                I(b, "bmInterlaceFlags"),
                I(b, "bCopyProtect"),
            ]
        )
        if sub == 4:  # FORMAT_UNCOMPRESSED
            return (
                hdr3(27, 36, 4)
                + bytes([I(b, "bFormatIndex"), I(b, "bNumFrameDescriptors")])
                + guid(b["f"]["guidFormat"])
                + bytes([I(b, "bBitsPerPixel")])
                + common()
            )
        if sub == 16:  # FORMAT_FRAME_BASED
            return (
                hdr3(28, 36, 16)
                + bytes([I(b, "bFormatIndex"), I(b, "bNumFrameDescriptors")])
                + guid(b["f"]["guidFormat"])
                + bytes([I(b, "bBitsPerPixel")])
                + common()
                + bytes([I(b, "bVariableSize")])
            )
        if sub == 6:  # FORMAT_MJPEG
            return (
                hdr3(11, 36, 6)
                + bytes([I(b, "bFormatIndex"), I(b, "bNumFrameDescriptors"), I(b, "bFlags")])
                + common()
            )
        if sub in (5, 7, 17):  # FRAME_UNCOMPRESSED / MJPEG / FRAME_BASED
            nt = I(b, "bFrameIntervalType")
            ivals = arr(b, "dwFrameInterval", nt)
            head = struct.pack(
                "<BBHHIIB",
                I(b, "bFrameIndex"),
                I(b, "bmCapabilities"),
                I(b, "wWidth"),
                I(b, "wHeight"),
                I(b, "dwMinBitRate"),
                I(b, "dwMaxBitRate"),
                0,
            )[:-1]
            if sub == 17:
                body = (
                    head
                    + struct.pack("<IB", I(b, "dwDefaultFrameInterval"), nt)
                    + struct.pack("<I", I(b, "dwBytesPerLine"))
                )
            else:
                body = head + struct.pack(
                    "<IIB", I(b, "dwMaxVideoFrameBufferSize"), I(b, "dwDefaultFrameInterval"), nt
                )
            body += b"".join(struct.pack("<I", v) for v in ivals)
            return hdr3(3 + len(body), 36, sub) + body
        if sub == 13:  # COLORFORMAT
            return hdr3(6, 36, 13) + bytes(
                [
                    I(b, "bColorPrimaries"),
                    I(b, "bTransferCharacteristics"),
                    I(b, "bMatrixCoefficients"),
                ]
            )
    if k == "AudioControl Interface Descriptor":
        if sub == 1:
            n = I(b, "bInCollection")
            return (
                hdr3(8 + n, 36, 1)
                + struct.pack("<HHB", bcd(tok(b, "bcdADC")), I(b, "wTotalLength"), n)
                + bytes(arr(b, "baInterfaceNr", n))
            )
        if sub == 2:
            return hdr3(12, 36, 2) + struct.pack(
                "<BHBBHBB",
                I(b, "bTerminalID"),
                I(b, "wTerminalType"),
                I(b, "bAssocTerminal"),
                I(b, "bNrChannels"),
                I(b, "wChannelConfig"),
                I(b, "iChannelNames"),
                I(b, "iTerminal"),
            )
        if sub == 3:
            return hdr3(9, 36, 3) + struct.pack(
                "<BHBBB",
                I(b, "bTerminalID"),
                I(b, "wTerminalType"),
                I(b, "bAssocTerminal"),
                I(b, "bSourceID"),
                I(b, "iTerminal"),
            )
        if sub == 6:
            n = I(b, "bControlSize")
            return (
                hdr3(7 + n, 36, 6)
                + bytes([I(b, "bUnitID"), I(b, "bSourceID"), n])
                + bytes(arr(b, "bmaControls", 1))
                + bytes([I(b, "iFeature")])
            )
    if k == "AudioStreaming Interface Descriptor":
        if sub == 1:
            return hdr3(7, 36, 1) + struct.pack(
                "<BBH", I(b, "bTerminalLink"), I(b, "bDelay"), I(b, "wFormatTag")
            )
        if sub == 2:
            nf = I(b, "bSamFreqType")
            return (
                hdr3(8 + 3 * nf, 36, 2)
                + bytes(
                    [
                        I(b, "bFormatType"),
                        I(b, "bNrChannels"),
                        I(b, "bSubframeSize"),
                        I(b, "bBitResolution"),
                        nf,
                    ]
                )
                + b"".join(arr(b, "tSamFreq", nf)[i].to_bytes(3, "little") for i in range(nf))
            )
    if k == "AudioControl Endpoint Descriptor":
        return struct.pack(
            "<BBBBBH", 7, 37, 1, I(b, "bmAttributes"), I(b, "bLockDelayUnits"), I(b, "wLockDelay")
        )
    raise SystemExit(
        "unhandled block %s sub %s line %d" % (k, b["f"].get("bDescriptorSubtype"), b["line"])
    )


def fmt_is_vs(cfg, off):
    """True when the class descriptor at off belongs to a VideoStreaming interface
    (bInterfaceClass 14, subclass 2)."""
    o = 9
    cur = None
    while o < off:
        if cfg[o + 1] == 4:
            cur = (cfg[o + 5], cfg[o + 6])
        o += cfg[o]
    return cur == (14, 2)


def u16str(s):
    d = s.encode("utf-16-le")
    return bytes([2 + len(d), 3]) + d


def main():
    src, outdir = sys.argv[1], sys.argv[2]
    report = []
    say = lambda *a: report.append(" ".join(str(x) for x in a))
    vc_intr_max = 16
    if "--vc-intr-maxtransfer" in sys.argv:
        vc_intr_max = int(sys.argv[sys.argv.index("--vc-intr-maxtransfer") + 1])
    extra_alts = []
    for i, arg in enumerate(sys.argv):
        if arg == "--compressed-alt":
            mps, ivl = sys.argv[i + 1].split(":")
            extra_alts.append((num(mps), num(ivl)))
    text = open(src, encoding="utf-8").read()
    dev, blocks = parse_blocks(text)
    os.makedirs(outdir, exist_ok=True)

    # device descriptor
    device = struct.pack(
        "<BBHBBBBHHHBBBB",
        18,
        1,
        bcd(tok(dev, "bcdUSB")),
        I(dev, "bDeviceClass"),
        I(dev, "bDeviceSubClass"),
        I(dev, "bDeviceProtocol"),
        I(dev, "bMaxPacketSize0"),
        I(dev, "idVendor"),
        I(dev, "idProduct"),
        bcd(tok(dev, "bcdDevice")),
        I(dev, "iManufacturer"),
        I(dev, "iProduct"),
        I(dev, "iSerial"),
        I(dev, "bNumConfigurations"),
    )
    qual = struct.pack(
        "<BBHBBBBBB",
        10,
        6,
        bcd(tok(dev, "bcdUSB")),
        I(dev, "bDeviceClass"),
        I(dev, "bDeviceSubClass"),
        I(dev, "bDeviceProtocol"),
        I(dev, "bMaxPacketSize0"),
        I(dev, "bNumConfigurations"),
        0,
    )

    cfgblk = blocks[0]
    assert cfgblk["kind"] == "Configuration Descriptor"
    items = []  # (label, bytes, lsusb bLength or None)
    gap_inserted = False
    vs = None  # the current interface descriptor's (number, alt) when it is VideoStreaming
    vs_last = None  # (items index, interface number, alt, endpoint block) of its last endpoint
    for b in blocks[1:]:
        if b["kind"] == "Interface Descriptor":
            is_vs = (I(b, "bInterfaceClass"), I(b, "bInterfaceSubClass")) == (14, 2)
            vs = (I(b, "bInterfaceNumber"), I(b, "bAlternateSetting")) if is_vs else None
        d = pack(b, vc_intr_max)
        lsl = int(b["f"]["bLength"].split()[0]) if "bLength" in b["f"] else None
        label = "%s sub=%s line %d" % (
            b["kind"],
            b["f"].get("bDescriptorSubtype", "-").split()[0],
            b["line"],
        )
        items.append((label, d, lsl))
        if b["kind"] == "Endpoint Descriptor" and vs:
            vs_last = (len(items), vs[0], vs[1], b)
        # class-specific VC interrupt endpoint right after its standard endpoint
        if (
            b["kind"] == "Endpoint Descriptor"
            and I(b, "bmAttributes") == 3
            and I(b, "bEndpointAddress") == 0x83
        ):
            cs = bytes([5, 0x25, 3]) + struct.pack("<H", vc_intr_max)
            items.append(
                (
                    "CS_ENDPOINT EP_INTERRUPT (not in lsusb text) wMaxTransferSize=%d"
                    % vc_intr_max,
                    cs,
                    None,
                )
            )
            gap_inserted = True
    if extra_alts:
        at, number, alt, ep = vs_last
        added = []
        for n, extra_alt in enumerate(extra_alts, alt + 1):
            intf = bytes([9, 4, number, n, 1, 14, 2, 0, 0])
            endp = struct.pack(
                "<BBBBHB", 7, 5, I(ep, "bEndpointAddress"), I(ep, "bmAttributes"), *extra_alt
            )
            added += [
                ("Interface Descriptor alt %d (added: --compressed-alt)" % n, intf, None),
                ("Endpoint Descriptor (added: --compressed-alt)", endp, None),
            ]
        items[at:at] = added
    body = b"".join(d for _, d, _ in items)
    total = 9 + len(body)
    cfg = (
        struct.pack(
            "<BBHBBBBB",
            9,
            2,
            total,
            I(cfgblk, "bNumInterfaces"),
            I(cfgblk, "bConfigurationValue"),
            I(cfgblk, "iConfiguration"),
            I(cfgblk, "bmAttributes"),
            int(re.match(r"(\d+)", cfgblk["f"]["MaxPower"]).group(1)) // 2,
        )
        + body
    )

    # checks
    ok = True
    lsusb_total = I(cfgblk, "wTotalLength") + 16 * len(extra_alts)
    lsusb_sum = 9 + sum(l for _, _, l in items if l is not None)
    say("lsusb bLength sum (text, incl. config 9): %d" % lsusb_sum)
    say("lsusb wTotalLength                      : %d" % lsusb_total)
    say("emitted config length                   : %d" % len(cfg))
    say(
        "gap descriptor inserted                 : %s (%d bytes)"
        % (gap_inserted, len(cfg) - lsusb_sum - 16 * len(extra_alts))
    )
    if len(cfg) != lsusb_total:
        ok = False
        say("FAIL total length")
    for label, d, l in items:
        if d[0] != len(d):
            ok = False
            say("FAIL self bLength", label)
        if l is not None and l != len(d):
            ok = False
            say("FAIL lsusb bLength", label, l, len(d))
    # walk the emitted configuration like a USB stack
    off, n = 0, 0
    while off < len(cfg):
        ln = cfg[off]
        assert ln >= 2 and off + ln <= len(cfg), "walk failure at %d" % off
        off += ln
        n += 1
    assert off == len(cfg)
    say("walk: %d descriptors, ends exactly at %d" % (n, off))
    # structural checks on the emitted bytes
    off = 9
    fmt = None
    fr = 0
    alt = None
    eps = 0
    alts = 0

    def close_fmt():
        global_ok = True
        if fmt is not None and fr != fmt:
            say("FAIL frame count", fmt, fr)
            return False
        return True

    while off < len(cfg):
        ln, ty = cfg[off], cfg[off + 1]
        sub = cfg[off + 2] if ln > 2 else 0
        if ty == 4:
            if alt is not None and eps != alt:
                ok = False
                say("FAIL bNumEndpoints", alt, eps)
            alt, eps, alts = cfg[off + 4], 0, alts + 1
        elif ty == 5:
            eps += 1
        elif (
            ty == 0x24
            and cfg[off + 3 : off + 4] is not None
            and cfg[off + 2] in (4, 6, 16)
            and alt is not None
            and fmt_is_vs(cfg, off)
        ):
            if not close_fmt():
                ok = False
            fmt, fr = cfg[off + 4], 0
        elif ty == 0x24 and cfg[off + 2] in (5, 7, 17) and fmt_is_vs(cfg, off):
            fr += 1
        off += ln
    if not close_fmt():
        ok = False
    if alt is not None and eps != alt:
        ok = False
        say("FAIL bNumEndpoints last", alt, eps)
    say(
        "structure: %d interface/alt descriptors, formats/frames/endpoint counts consistent" % alts
        if ok
        else "structure FAIL"
    )

    # class-descriptor sub-totals
    def sum_between(pred):
        return sum(len(d) for lab, d, _ in items if pred(lab, d))

    vc = sum(len(d) for lab, d, _ in items if lab.startswith("VideoControl"))
    vs = sum(len(d) for lab, d, _ in items if lab.startswith("VideoStreaming"))
    ac = sum(len(d) for lab, d, _ in items if lab.startswith("AudioControl Interface"))
    say(
        "VC class bytes %d (header wTotalLength %d)"
        % (
            vc,
            struct.unpack_from(
                "<H", [d for l, d, _ in items if l.startswith("VideoControl")][0], 5
            )[0],
        )
    )
    say(
        "VS class bytes %d (header wTotalLength %d)"
        % (
            vs,
            struct.unpack_from(
                "<H", [d for l, d, _ in items if l.startswith("VideoStreaming")][0], 4
            )[0],
        )
    )
    say(
        "AC class bytes %d (header wTotalLength %d)"
        % (
            ac,
            struct.unpack_from(
                "<H", [d for l, d, _ in items if l.startswith("AudioControl Interface")][0], 5
            )[0],
        )
    )
    # a few intra-descriptor consistency checks
    say("RESULT:", "OK" if ok else "FAIL")

    strings = {
        0: struct.pack("<BBH", 4, 3, LANGID),
        1: u16str(SERIAL),
        2: u16str(dev["f"]["iProduct"].split(None, 1)[1]),
    }

    def w(name, data):
        open(os.path.join(outdir, name), "wb").write(data)

    w("device.bin", device)
    w("qualifier.bin", qual)
    w("config.bin", cfg)
    for i, s in strings.items():
        w("string%d.bin" % i, s)
    with open(os.path.join(outdir, "layout.txt"), "w") as f:
        off = 9
        f.write("%5d %3d configuration header\n" % (0, 9))
        for label, d, _ in items:
            f.write("%5d %3d %s\n" % (off, len(d), label))
            off += len(d)

    def carr(name, data):
        lines = []
        for i in range(0, len(data), 16):
            lines.append("    " + ", ".join("0x%02x" % x for x in data[i : i + 16]) + ",")
        return "static const unsigned char %s[%d] = {\n%s\n};\n" % (
            name,
            len(data),
            "\n".join(lines),
        )

    h = [
        "/* Generated by gen.py from a published C920 (046d:082d) lsusb -v dump. Do not edit. */",
        "#ifndef VIRTCAM_C920_DESCRIPTORS_H",
        "#define VIRTCAM_C920_DESCRIPTORS_H",
        "",
    ]
    h += [
        carr("virtcam_device_desc", device),
        carr("virtcam_qualifier_desc", qual),
        carr("virtcam_config_desc", cfg),
    ]
    for i, s in strings.items():
        h.append(carr("virtcam_string%d_desc" % i, s))
    h += [
        "#define VIRTCAM_VID 0x%04x" % I(dev, "idVendor"),
        "#define VIRTCAM_PID 0x%04x" % I(dev, "idProduct"),
        "#define VIRTCAM_CONFIG_LEN %d" % len(cfg),
        "",
        "#endif",
        "",
    ]
    open(os.path.join(outdir, "descriptors.h"), "w").write("\n".join(h))
    if not ok or "--verbose" in sys.argv:
        print("\n".join(report), file=sys.stderr if not ok else sys.stdout)
    sys.exit(0 if ok else 1)


main()
