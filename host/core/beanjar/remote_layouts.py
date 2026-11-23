# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The LG remotes' layouts as data, without GTK (host/viewer/README.md, "TV remote"): the keys with
their boxes in CSS pixels, colours, labels and LG IR data codes, for the GTK remote
(beanjar_ui/viewer/remote.py, which draws them) and the web viewer (beanjar remote layout,
docs/dev/architecture/web-viewer.md).

Firmware layouts and the shared service remote:
- "normal": build/remote-reference.html's layout, 48 buttons in CSS pixels on a 188x652 panel;
- "netcast": the selected NetCast firmware's dashboard and legacy keys;
- "simplesmart": the existing SimpleSmart IR mapping, with HOME rather than NetCast;
- "japan": the normal remote with a Japanese remote's keys, for a VM whose board is ARIB: the
  broadcast types, one-touch 1-12, d (data broadcasting), display and 3-digit input;
- "service": LG's factory service remote, "Factory SVC Remocon" MKJ39170828, from a photo of it,
  on a 188x764 panel.
Every key is a button of the remote's infrared: its LG IR data code (NEC custom code 0x04) goes to
the guest's micom (include/webos-ir.h). The Japanese remote's broadcast-type keys and one-touch 11
and 12 send extended codes, 0xDB00 | N (EXT_PREFIX, webos-ir.h WEBOS_IR_EXT). Keys with no code
(None) are dimmed and send nothing.
"""

from collections import namedtuple

REMOTE_W = 188  # every layout's panel width, CSS px
# a Japanese remote's extended code: EXT_PREFIX << 8 | N (webos-ir.h WEBOS_IR_EXT)
EXT_PREFIX = 0xDB


def code_valid(code):
    """Whether code is one a remote button sends: an LG IR data code (0 to 255) or an extended code
    (0xDB00 to 0xDBFF)."""
    return 0 <= code <= 0xFF or code >> 8 == EXT_PREFIX


# The remote's window beside the TV picture (window_scale): 1.125x the picture's height, at 1.125x
# to 2.25x the layout (the normal remote 212x734 to 423x1467): taller than the picture beside it.
WINDOW_SHARE, WINDOW_MIN, WINDOW_MAX = 1.125, 1.125, 2.25
# The palette, sampled from photos of LG remotes and rounded to a few clean swatches
# (host/viewer/README.md, "TV remote"). Every colour on the bodies and keys is one of these.
BODY_FACE, BODY_RIM = 0x222222, 0x444444  # near-black body; its lighter glossy edge
KEY_DARK, KEY_LIGHT = 0x111111, 0xCCCCCC  # black keys; the light grey groups
LABEL_ON_DARK, LABEL_ON_LIGHT = 0xEEEEEE, 0x333333
PRINT, CHIN_PRINT = 0x888888, 0x666666  # names printed on the body; the chin's "LG"
POWER_RED = 0xEE3344
RED, GREEN, YELLOW, BLUE = 0xEE3333, 0x11AA66, 0xEEAA77, 0x3388DD
NETFLIX_RED, AMAZON_BLUE = 0xCC1111, 0x1166AA  # brand text on the light streaming keys
SVC_FACE, SVC_RIM = 0x3A3A3A, 0x555555  # the service remote's charcoal body and its edge
SVC_PRINT = 0xBBBBBB  # names printed on the service remote's body
POWER_TEAL = 0x117799  # the service remote's power key
SVC_GREEN, SVC_YELLOW, SVC_BLUE = 0x99DDAA, 0xEEDD66, 0x44BBEE  # its paler colour keys
SMALL, LARGE = 4.5, 9.0  # text cap heights, CSS px

# glyph: an icon name (symbolic, recoloured with the key's ink) and a text fallback
GLYPHS = {
    "POWER": ("beanjar-power-symbolic", "⏻"),
    "INPUT": ("go-next-symbolic", "IN"),
    "SEARCH": ("system-search-symbolic", "SRCH"),
    "MUTE": ("audio-volume-muted-symbolic", "MUTE"),
    "HOME": ("go-home-symbolic", "HOME"),
    "GEAR": ("applications-system-symbolic", "SET"),
    "PLUS": ("list-add-symbolic", "+"),
    "MINUS": ("list-remove-symbolic", "-"),
    "CH_UP": ("pan-up-symbolic", "^"),
    "CH_DOWN": ("pan-down-symbolic", "v"),
    "UP": ("pan-up-symbolic", "^"),
    "DOWN": ("pan-down-symbolic", "v"),
    "LEFT": ("pan-start-symbolic", "<"),
    "RIGHT": ("pan-end-symbolic", ">"),
    "STOP": ("media-playback-stop-symbolic", "STOP"),
    "REWIND": ("media-seek-backward-symbolic", "<<"),
    "PLAY": ("media-playback-start-symbolic", "PLAY"),
    "PAUSE": ("media-playback-pause-symbolic", "||"),
    "FORWARD": ("media-seek-forward-symbolic", ">>"),
    "PREV": ("media-skip-backward-symbolic", "|<<"),
    "NEXT": ("media-skip-forward-symbolic", ">>|"),
}

# A key: its name, box (CSS px), face colour, corner radii (tl, tr, br, bl), glyph or None, text,
# cap height, ink colour, LG IR data code (None: dimmed, sends nothing) and ring (a thin outline
# colour, or None).
Key = namedtuple("Key", "name x y w h face radii glyph text cap ink code ring", defaults=(None,))
# A remote: its panel height; the body (x, y, w, h, top radius, bottom radius), CSS px, the rest of
# the panel is margin; body colours (face, rim, printed names); the keys; names printed on the
# body (x, y centre, text; "\n" for two lines); decorations under the keys (x, y, w, h, radius,
# face or None, border or None); the chin's own controls (name, x, y, size, icon, text fallback,
# tooltip: "menu" opens the window's menu, "close" closes the window); what LG prints on the chin
# (x, y centre, text, cap height).
Layout = namedtuple(
    "Layout", "name title height body face rim print keys labels decor controls chin"
)


def _r(r):
    return (r, r, r, r)


def _light(colour):
    return ((colour >> 16) & 255) + ((colour >> 8) & 255) + (colour & 255) > 384


def _controls(y):
    return [
        ("menu", 71, y, 18, "open-menu-symbolic", "=", "Remote options"),
        (
            "close",
            99,
            y,
            18,
            "window-close-symbolic",
            "x",
            "Close the remote (the Menu key or the Remote toggle opens it again)",
        ),
    ]


# ---- the normal remote ----------------------------------------------------------------------
# LG's IR data codes, the NEC custom code being 0x04 (openlgtv OPENRELEASE include/keys.h; WT1H's
# tvservice _gKeyTable @0xb74530 maps each to the Linux key in the comment).
_D, _L = KEY_DARK, KEY_LIGHT
NORMAL = Layout(
    "normal",
    "webOS Remote",
    652,
    (4, 4, 180, 644, 16, 10),
    BODY_FACE,
    BODY_RIM,
    PRINT,
    [
        Key(
            "POWER", 20, 28, 28, 28, POWER_RED, _r(14), "POWER", None, 0, LABEL_ON_DARK, 0x08
        ),  # KEY_POWER
        Key(
            "INPUT_HUB", 144, 36, 20, 20, _D, _r(10), "INPUT", None, 0, LABEL_ON_DARK, 0x0B
        ),  # KEY_VIDEO_NEXT
        Key(
            "CC", 20, 72, 28, 20, _L, _r(6), None, "CAPTION", SMALL, LABEL_ON_LIGHT, 0x39
        ),  # 0x31f subtitle
        Key(
            "GUIDE", 60, 72, 28, 20, _L, _r(6), None, "?", LARGE, LABEL_ON_LIGHT, 0xAB
        ),  # KEY_PROGRAM
        Key("SEARCH", 100, 72, 28, 20, _L, _r(6), "SEARCH", None, 0, LABEL_ON_LIGHT, None),
        Key(
            "LIVETV", 140, 72, 28, 20, _L, _r(6), None, "TV", SMALL, LABEL_ON_LIGHT, 0x0F
        ),  # KEY_TV
        *[
            Key(
                str(d),
                20 + 54 * ((d - 1) % 3),
                104 + 32 * ((d - 1) // 3),
                40,
                24,
                _D,
                _r(6),
                None,
                str(d),
                LARGE,
                LABEL_ON_DARK,
                0x10 + d,
            )
            for d in range(1, 10)
        ],  # KEY_1..9
        Key("LIST", 20, 200, 40, 24, _D, _r(6), None, "LIST", SMALL, LABEL_ON_DARK, 0x53),  # 0x313
        Key("0", 74, 200, 40, 24, _D, _r(6), None, "0", LARGE, LABEL_ON_DARK, 0x10),  # KEY_0
        Key(
            "FLASHBACK", 128, 200, 40, 24, _D, _r(6), None, "FLASHBK", SMALL, LABEL_ON_DARK, 0x1A
        ),  # 0x304
        Key("VOLUMEUP", 20, 236, 40, 38, _L, (6, 6, 0, 0), "PLUS", None, 0, LABEL_ON_LIGHT, 0x02),
        Key(
            "VOLUMEDOWN", 20, 274, 40, 38, _L, (0, 0, 6, 6), "MINUS", None, 0, LABEL_ON_LIGHT, 0x03
        ),
        Key(
            "CHANNELUP", 128, 236, 40, 38, _L, (6, 6, 0, 0), "CH_UP", None, 0, LABEL_ON_LIGHT, 0x00
        ),
        Key(
            "CHANNELDOWN",
            128,
            274,
            40,
            38,
            _L,
            (0, 0, 6, 6),
            "CH_DOWN",
            None,
            0,
            LABEL_ON_LIGHT,
            0x01,
        ),
        Key("FAVORITES", 74, 236, 40, 18, _L, _r(6), None, "FAV", SMALL, LABEL_ON_LIGHT, 0x1E),
        Key("INFO", 74, 266, 40, 18, _L, _r(6), None, "INFO", SMALL, LABEL_ON_LIGHT, 0xAA),
        Key("MUTE", 74, 294, 40, 18, _L, _r(6), "MUTE", None, 0, LABEL_ON_LIGHT, 0x09),
        Key("NETFLIX", 20, 324, 40, 20, _L, _r(6), None, "NETFLIX", SMALL, NETFLIX_RED, None),
        Key("HOME", 74, 324, 40, 20, _L, _r(6), "HOME", None, 0, LABEL_ON_LIGHT, 0x7C),  # 0x305
        Key("AMAZON", 128, 324, 40, 20, _L, _r(6), None, "prime video", SMALL, AMAZON_BLUE, None),
        Key("MENU", 20, 356, 40, 20, _L, _r(6), "GEAR", None, 0, LABEL_ON_LIGHT, 0x43),  # KEY_MENU
        Key("UP", 74, 356, 40, 28, _L, _r(6), "UP", None, 0, LABEL_ON_LIGHT, 0x40),
        Key(
            "LIVE_ZOOM",
            128,
            356,
            40,
            20,
            _L,
            _r(6),
            None,
            "LIVE\nZOOM",
            SMALL,
            LABEL_ON_LIGHT,
            None,
        ),
        Key("LEFT", 20, 396, 40, 28, _L, _r(6), "LEFT", None, 0, LABEL_ON_LIGHT, 0x07),
        Key("ENTER", 76, 396, 36, 28, _L, _r(6), None, "OK", LARGE, LABEL_ON_LIGHT, 0x44),
        Key("RIGHT", 128, 396, 40, 28, _L, _r(6), "RIGHT", None, 0, LABEL_ON_LIGHT, 0x06),
        Key(
            "BACK", 20, 444, 40, 20, _L, _r(6), None, "BACK", SMALL, LABEL_ON_LIGHT, 0x28
        ),  # KEY_PREVIOUS
        Key("DOWN", 74, 436, 40, 28, _L, _r(6), "DOWN", None, 0, LABEL_ON_LIGHT, 0x41),
        Key("EXIT", 128, 444, 40, 20, _L, _r(6), None, "EXIT", SMALL, LABEL_ON_LIGHT, 0x5B),
        Key("SAP", 20, 480, 40, 20, _D, _r(6), None, "SAP *", SMALL, LABEL_ON_DARK, 0x0A),  # 0x31e
        Key(
            "TIMER", 74, 480, 40, 20, _D, _r(6), None, "SLEEP", SMALL, LABEL_ON_DARK, 0x0E
        ),  # 0x31b
        Key("STOP", 128, 480, 40, 20, _D, _r(6), "STOP", None, 0, LABEL_ON_DARK, 0xB1),
        Key("REWIND", 20, 516, 28, 20, _L, _r(6), "REWIND", None, 0, LABEL_ON_LIGHT, 0x8F),
        Key("PLAY", 60, 516, 28, 20, _L, _r(6), "PLAY", None, 0, LABEL_ON_LIGHT, 0xB0),
        Key("PAUSE", 100, 516, 28, 20, _L, _r(6), "PAUSE", None, 0, LABEL_ON_LIGHT, 0xBA),
        Key("FASTFORWARD", 140, 516, 28, 20, _L, _r(6), "FORWARD", None, 0, LABEL_ON_LIGHT, 0x8E),
        Key("RED", 20, 552, 28, 20, RED, _r(6), None, None, 0, 0, 0x72),
        Key("GREEN", 60, 552, 28, 20, GREEN, _r(6), None, None, 0, 0, 0x71),
        Key("YELLOW", 100, 552, 28, 20, YELLOW, _r(6), None, None, 0, 0, 0x63),
        Key("BLUE", 140, 552, 28, 20, BLUE, _r(6), None, None, 0, 0, 0x61),
    ],
    [(40, 274, "VOL"), (148, 274, "CH"), (94, 230, "QUICK ACCESS"), (74, 508, "MAGIC LINK")],
    [],
    _controls(597),
    [],
)

# ---- the service remote ---------------------------------------------------------------------
# LG's "Factory SVC Remocon" MKJ39170828, measured from a photo (body 220x924 px there, 180x756
# CSS px here). IR codes as above; the factory keys' Linux codes are LG's 0x38x/0x3bx, which WT1H's
# compositor maps to Qt::Key_webOS_Factory* keys (libqstarfishegl's table). Dimmed: ETC, B-TOOTH and
# X-STUDIO (no code known for WT1H); SIMPLINK (0x7E -> 0x30a) and AV3 (0xD1 -> 0x3b8), which
# tvservice maps but WT1H's compositor has no Qt key for; RGB (0xD5), COMP2 (0xD4) and HDMI4
# (0xDA), direct-input keys tvservice drops because this model has no such input
# (_MICOM_CheckInvalidDiscreteKeyValue, from the tool options: one component input, three HDMI,
# no RGB-PC).
_AUTO = object()


def _svc(
    name,
    x,
    y,
    w,
    h,
    text,
    code,
    cap=SMALL,
    face=KEY_DARK,
    radii=None,
    glyph=None,
    ink=None,
    ring=_AUTO,
):
    """A service remote key: round unless radii are given; the black keys have a thin light ring."""
    light = _light(face)
    return Key(
        name,
        x,
        y,
        w,
        h,
        face,
        radii or _r(min(w, h) / 2),
        glyph,
        text,
        cap,
        ink if ink is not None else (LABEL_ON_LIGHT if light else LABEL_ON_DARK),
        code,
        (None if face != KEY_DARK else PRINT) if ring is _AUTO else ring,
    )


def _round(name, x, y, text, code, face=KEY_DARK, size=18, **kw):
    return _svc(name, x, y, size, size, text, code, face=face, **kw)


SERVICE = Layout(
    "service",
    "Service Remote",
    764,
    (4, 4, 180, 756, 5, 5),
    SVC_FACE,
    SVC_RIM,
    SVC_PRINT,
    [
        _svc("P_ONLY", 21, 27, 31, 14, "P-ONLY", 0xFE, face=KEY_LIGHT),  # 0x387 power only
        _svc(
            "POWER",
            76,
            41,
            34,
            23,
            None,
            0x08,
            face=POWER_TEAL,
            radii=_r(5),
            glyph="POWER",
            ring=None,
        ),
        _svc("ETC", 133, 27, 31, 14, "ETC", None, face=KEY_LIGHT, ink=RED),
        _svc("INPUT", 21, 53, 31, 15, "INPUT", 0x0B),  # KEY_VIDEO_NEXT
        _svc("ARC", 133, 53, 31, 15, "ARC", 0x79),  # 0x31d aspect ratio
        _svc("PSM", 21, 81, 31, 15, "PSM", 0x4D),  # 0x320 picture mode
        _svc("P_CHECK", 65, 81, 30, 16, "P", 0xFC, face=KEY_LIGHT, radii=(8, 0, 0, 8)),  # 0x384
        _svc("S_CHECK", 95, 81, 29, 16, "S", 0xFD, face=KEY_LIGHT, radii=(0, 8, 8, 0)),  # 0x386
        _svc("SSM", 133, 81, 31, 15, "SSM", 0x52),  # 0x321 sound mode
        _svc("PIP", 24, 109, 26, 15, "PIP", 0x60),  # 0x31a
        _svc("TEXT", 61, 109, 26, 15, "TEXT", 0x20),  # 0x30b teletext
        _svc("CAP", 99, 109, 26, 15, "CAP", 0x39),  # 0x31f subtitle
        _svc("MPX", 137, 109, 26, 15, "MPX", 0x0A),  # 0x31e multi sound
        _round("RED", 23, 142, None, 0x72, face=POWER_RED, ring=None),
        _round("GREEN", 65, 142, None, 0x71, face=SVC_GREEN, ring=None),
        _round("YELLOW", 105, 142, None, 0x63, face=SVC_YELLOW, ring=None),
        _round("BLUE", 147, 142, None, 0x61, face=SVC_BLUE, ring=None),
        _round("SIMPLINK", 23, 176, None, None, face=KEY_LIGHT),
        _svc("PLAY", 52, 177, 36, 15, None, 0xB0, face=KEY_LIGHT, radii=_r(3), glyph="PLAY"),
        _svc("PAUSE", 100, 177, 36, 15, None, 0xBA, face=KEY_LIGHT, radii=_r(3), glyph="PAUSE"),
        _round("TILT", 148, 176, "TILT", 0xF9, size=17, face=KEY_LIGHT, ring=KEY_DARK),  # 0x393
        _round("EYE", 23, 205, None, 0x95, face=KEY_LIGHT),  # 0x381 eye Q
        _svc("REWIND", 52, 207, 36, 15, None, 0x8F, face=KEY_LIGHT, radii=_r(3), glyph="PREV"),
        _svc(
            "FASTFORWARD", 100, 207, 36, 15, None, 0x8E, face=KEY_LIGHT, radii=_r(3), glyph="NEXT"
        ),
        _round("B_TOOTH", 147, 205, None, None, face=KEY_LIGHT),
        _round("ADJ", 46, 246, None, 0xFF),  # 0x388 adjust
        _svc("X_STUDIO", 74, 237, 39, 16, "X STUDIO", None, face=KEY_LIGHT),
        _round("MENU", 124, 246, None, 0x43),  # KEY_MENU
        _round("IN_START", 26, 267, None, 0xFB),  # 0x383
        _round("EXIT", 145, 267, None, 0x5B),  # KEY_EXIT
        _svc("UP", 71, 275, 46, 21, None, 0x40, face=KEY_LIGHT, radii=_r(6), glyph="UP"),
        _svc("DOWN", 71, 342, 46, 21, None, 0x41, face=KEY_LIGHT, radii=_r(6), glyph="DOWN"),
        _svc("LEFT", 50, 296, 21, 46, None, 0x07, face=KEY_LIGHT, radii=_r(6), glyph="LEFT"),
        _svc("RIGHT", 117, 296, 21, 46, None, 0x06, face=KEY_LIGHT, radii=_r(6), glyph="RIGHT"),
        _svc("ENTER", 76, 301, 36, 36, "OK", 0x44, cap=LARGE * 0.8, face=KEY_LIGHT),
        _svc(
            "VOLUMEUP",
            34,
            373,
            29,
            36,
            None,
            0x02,
            face=KEY_LIGHT,
            radii=(14, 14, 0, 0),
            glyph="PLUS",
        ),
        _svc(
            "VOLUMEDOWN",
            34,
            409,
            29,
            36,
            None,
            0x03,
            face=KEY_LIGHT,
            radii=(0, 0, 14, 14),
            glyph="MINUS",
        ),
        _round("MUTE", 83, 386, None, 0x09, size=21, glyph="MUTE"),
        _svc(
            "CHANNELUP",
            124,
            373,
            29,
            36,
            None,
            0x00,
            face=KEY_LIGHT,
            radii=(14, 14, 0, 0),
            glyph="PLUS",
        ),
        _svc(
            "CHANNELDOWN",
            124,
            409,
            29,
            36,
            None,
            0x01,
            face=KEY_LIGHT,
            radii=(0, 0, 14, 14),
            glyph="MINUS",
        ),
        _round("IN_STOP", 83, 422, None, 0xFA, size=21),  # 0x382
        *[
            _round(
                str(d),
                34 + 49 * ((d - 1) % 3),
                464 + 31 * ((d - 1) // 3),
                str(d),
                0x10 + d,
                size=21,
                cap=LARGE * 0.8,
            )
            for d in range(1, 10)
        ],
        _round("RGB", 34, 557, "RGB", None, size=21),
        _round("0", 83, 557, "0", 0x10, size=21, cap=LARGE * 0.8),
        _round("TV", 132, 557, "TV", 0xD6, size=21),  # 0x3b3 factory TV
        _svc("AV1", 29, 605, 36, 13, "AV1", 0x5A),  # 0x3b6
        _svc("AV2", 76, 605, 36, 13, "AV2", 0xD0),  # 0x3b7
        _svc("AV3", 124, 605, 36, 13, "AV3", None),
        _svc("COMP1", 29, 632, 36, 13, "COMP1", 0xBF),  # 0x3b9
        _svc("COMP2", 76, 632, 36, 13, "COMP2", None),
        _svc("HDMI1", 124, 632, 36, 13, "HDMI1", 0xCE),  # 0x3bf
        _svc("HDMI2", 29, 658, 36, 13, "HDMI2", 0xCC),  # 0x3c0
        _svc("HDMI3", 76, 658, 36, 13, "HDMI3", 0xE9),  # 0x3c1
        _svc("HDMI4", 124, 658, 36, 13, "HDMI4", None),
    ],
    [
        (94, 33, "POWER"),
        (94, 74, "P-CHECK-S"),
        (32, 135, "INFO"),
        (74, 135, "STILL"),
        (114, 135, "HDMI HOT"),
        (155, 135, "USB HOT"),
        (32, 168, "PIP PR-"),
        (74, 168, "PIP PR+"),
        (114, 168, "SWAP"),
        (155, 168, "PIP INPUT"),
        (32, 200, "SIMPLINK"),
        (32, 229, "EYE"),
        (156, 229, "B-TOOTH"),
        (55, 240, "ADJ"),
        (133, 240, "MENU"),
        (26, 262, "IN START"),
        (158, 262, "EXIT"),
        (24, 319, "TIME\nSHIFT"),
        (164, 319, "TIME\nSHIFT"),
        (94, 380, "MUTE"),
        (94, 449, "IN STOP"),
        (48, 409, "VOL"),
        (138, 409, "CH"),
        (170, 409, "PAGE"),
        (142, 678, "DVI"),
    ],
    [
        (44, 269, 100, 100, 50, KEY_LIGHT, None),
        (72, 297, 44, 44, 22, SVC_FACE, None),
        (18, 594, 154, 90, 4, None, SVC_RIM),
    ],
    _controls(688),
    [(94, 718, "Factory SVC", 6.0), (94, 731, "Remocon", 6.0), (94, 744, "MKJ39170828", 2.5)],
)

# NetCast's dashboard key is MyApps/NetCast (0x42), distinct from HOME (0x7c).
# Codes are from the selected LM14 US 04.11.18 RELEASE.
# Keep the familiar numeric/D-pad geometry, with the NetCast keys in place of the
# streaming shortcuts. Unresolved labels send nothing rather than guessed codes.
_netcast_keys = {
    "SEARCH": dict(name="ENERGY_SAVING", glyph=None, text="ENERGY\nSAVING", code=None),
    "FLASHBACK": dict(text="Q.VIEW"),
    "NETFLIX": dict(name="QMENU", text="Q.MENU", ink=LABEL_ON_LIGHT, code=0x45),
    "HOME": dict(glyph=None, text="NetCast", cap=SMALL, code=0x42),
    "AMAZON": dict(name="AV_MODE", text="AV MODE", ink=LABEL_ON_LIGHT, code=0x30),
    "LIVE_ZOOM": dict(name="RATIO", text="RATIO", code=0x79),
    "TIMER": dict(name="SIMPLINK", text="SIMPLINK", code=0x7E),
}
NETCAST = Layout(
    "netcast",
    "NetCast Remote",
    724,
    (4, 4, 180, 716, 16, 10),
    BODY_FACE,
    BODY_RIM,
    PRINT,
    [key._replace(**_netcast_keys.get(key.name, {})) for key in NORMAL.keys]
    + [
        Key("TEXT", 20, 584, 40, 20, _L, _r(6), None, "TEXT", SMALL, LABEL_ON_LIGHT, 0x20),
        Key("TOPT", 74, 584, 40, 20, _L, _r(6), None, "T.OPT", SMALL, LABEL_ON_LIGHT, 0x21),
        Key("AD", 128, 584, 40, 20, _L, _r(6), None, "AD", SMALL, LABEL_ON_LIGHT, 0x91),
        Key("APP", 74, 616, 40, 20, _L, _r(6), None, "APP/*", SMALL, LABEL_ON_LIGHT, None),
    ],
    [(40, 274, "VOL"), (148, 274, "CH")],
    [],
    _controls(669),
    [],
)

# SimpleSmart has the existing normal IR mapping, including HOME (0x7c), rather
# than NetCast's dashboard key. See docs/dev/architecture/simplesmart.md.
SIMPLESMART = NORMAL._replace(name="simplesmart", title="SimpleSmart Remote")


# ---- the Japanese remote --------------------------------------------------------------------
# The normal remote with a Japanese (ISDB) remote's keys, for an ARIB board. Codes from two JP
# builds' key tables (docs/research/arib-data-broadcasting.md, "JP remote infrared codes"): d 0x9F
# (885 Key_webOS_BMLData, which the firmware keeps for country JPN only), display 0xAC (886
# Key_webOS_JapanDisplay), 3-digit input 0x23 (884 Key_webOS_3DigitInput); the broadcast types are
# extended codes, 0xDB N: BS 00, CS 10, terrestrial 30, BS4K 40. 1-10 are the ordinary 0x11-0x19 and
# 0x10; 11 and 12 exist only as extended codes, N = type + 0x0B, 0x0C: the keys carry terrestrial's,
# and the viewer core sends them for the type selected last, as the remote does
# (host/viewer/core/ir.h). LG's physical JP remote layout is not known: the keys are placed on the
# normal remote, two rows (the types; 10-12) taller, FLASHBACK and LIST giving way to 10 and 12.
def _ext(n):
    return (EXT_PREFIX << 8) | n


_JP_SHIFT = 48  # the normal remote's keys from the volume row down
_JP_TOP = {  # the keys above the number pad, one row lower
    "CC",
    "GUIDE",
    "SEARCH",
    "LIVETV",
}


def _jp_key(key):
    if key.name in ("LIST", "0", "FLASHBACK") or key.name.isdigit():
        return None
    if key.name in _JP_TOP:
        return key._replace(y=key.y + 24)
    if key.y >= 236:
        return key._replace(y=key.y + _JP_SHIFT)
    return key


def _jp_type(name, x, text, n):
    return Key(name, x, 64, 28, 18, _L, _r(6), None, text, SMALL, LABEL_ON_LIGHT, _ext(n))


def _jp_digit(d):
    # 1-9, then 10 (Key_0's 0x10) under 7, 11 and 12 under 8 and 9
    code = 0x10 + d if d < 10 else 0x10 if d == 10 else _ext(0x30 + d)
    row, col = (d - 1) // 3, (d - 1) % 3
    return Key(str(d), 20 + 54 * col, 128 + 30 * row, 40, 22, _D, _r(6), None, str(d), LARGE,
               LABEL_ON_DARK, code)  # fmt: skip


JAPAN = Layout(
    "japan",
    "Japanese Remote",
    NORMAL.height + _JP_SHIFT,
    NORMAL.body[:3] + (NORMAL.body[3] + _JP_SHIFT,) + NORMAL.body[4:],
    BODY_FACE,
    BODY_RIM,
    PRINT,
    [k for k in map(_jp_key, NORMAL.keys) if k is not None]
    + [
        _jp_type("TERRESTRIAL", 20, "TER", 0x30),
        _jp_type("BS", 60, "BS", 0x00),
        _jp_type("CS", 100, "CS", 0x10),
        _jp_type("BS4K", 140, "BS4K", 0x40),
        *[_jp_digit(d) for d in range(1, 13)],
        Key("DATA", 20, 252, 40, 20, _D, _r(6), None, "d", LARGE, LABEL_ON_DARK, 0x9F),
        Key("DISPLAY", 74, 252, 40, 20, _D, _r(6), None, "DISPLAY", SMALL, LABEL_ON_DARK, 0xAC),
        Key(
            "THREE_DIGIT", 128, 252, 40, 20, _D, _r(6), None, "3-DIGIT", SMALL, LABEL_ON_DARK, 0x23
        ),  # fmt: skip
    ],
    [(x, y + _JP_SHIFT, t) for x, y, t in NORMAL.labels],
    [],
    _controls(597 + _JP_SHIFT),
    [],
)
LAYOUTS = {layout.name: layout for layout in (NORMAL, NETCAST, SIMPLESMART, JAPAN, SERVICE)}
# the normal layout's tables, as before there were two
BUTTONS, BODY, REMOTE_H = NORMAL.keys, NORMAL.body, NORMAL.height


def find(name, layout=NORMAL):
    for i, b in enumerate(layout.keys):
        if b.name == name:
            return i
    return -1


def remote_layout(requested, info):
    """The layout's name for a VM whose launch wrote info (vm-info.toml: platform, release,
    broadcast): the selected target's remote by default, the Japanese remote on a webOS board whose
    broadcast is ARIB; an explicit layout takes precedence, if it is the firmware's own or the
    service remote."""
    if requested != "auto":
        if info and requested not in (remote_layout("auto", info), "service"):
            raise ValueError(f"remote layout {requested!r} does not match this firmware")
        return requested
    if info.get("platform") == "SimpleSmart" or info.get("release") == "simplesmart":
        return "simplesmart"
    if info.get("platform") == "NetCast" or info.get("release") == "netcast45":
        return "netcast"
    if info.get("broadcast") == "arib":
        return "japan"
    return "normal"


def _colour(c):
    return None if c is None else f"#{c:06x}"


def as_json(layout):
    """A layout as plain JSON data (the web viewer's layout.json): CSS pixels on a REMOTE_W x height
    panel, colours as "#rrggbb", glyphs by name (GLYPHS: name -> its text fallback)."""
    bx, by, bw, bh, top, bottom = layout.body
    return {
        "name": layout.name,
        "title": layout.title,
        "width": REMOTE_W,
        "height": layout.height,
        "body": {"x": bx, "y": by, "w": bw, "h": bh, "top_radius": top, "bottom_radius": bottom},
        "face": _colour(layout.face),
        "rim": _colour(layout.rim),
        "print": _colour(layout.print),
        "keys": [
            {
                "name": k.name,
                "x": k.x,
                "y": k.y,
                "w": k.w,
                "h": k.h,
                "face": _colour(k.face),
                "radii": list(k.radii),
                "glyph": k.glyph,
                "text": k.text,
                "cap": k.cap,
                "ink": _colour(k.ink),
                "code": k.code,
                "ring": _colour(k.ring),
            }
            for k in layout.keys
        ],
        "labels": [{"x": x, "y": y, "text": t} for x, y, t in layout.labels],
        "decor": [
            {"x": x, "y": y, "w": w, "h": h, "radius": r, "face": _colour(f), "border": _colour(b)}
            for x, y, w, h, r, f, b in layout.decor
        ],
        "chin": [{"x": x, "y": y, "text": t, "cap": cap} for x, y, t, cap in layout.chin],
        "glyphs": {name: text for name, (_icon, text) in GLYPHS.items()},
    }
