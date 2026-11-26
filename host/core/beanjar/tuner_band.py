# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The physical channels (RFs) a set of each broadcast standard tunes, by name: their centre
frequencies and delivery systems (enum fe_delivery_system).

An RF's name is its channel number, or C and the number for a cable channel: "7" is US air channel
7, "C78" US cable channel 78. The plans are the firmware's own tables (legacy-broadcast-atsc and
-dvb of W21P 03.70.25):

- atsc: air 2 to 36 (8VSB, SYS_ATSC); cable C1 to C135, the US cable plan's standard (STD)
  centres, TBL_STD_NADC_F (QAM annex B, SYS_DVBC_ANNEX_B). The set retries a cable channel at its
  IRC and HRC offsets itself; the host's signal is on the STD centre.
- arib: UHF 13 to 62 (ISDB-T).
- dvb: VHF 6 to 12 on the 8 MHz raster and UHF 21 to 69 (FE_TBL_STD_DVBT_V8: DVB-T, SYS_DVBT, or
  DVB-T2 where the lineup says so); cable C1 to C106 from 50 MHz on 8 MHz steps (FE_TBL_STD_DVBC:
  DVB-C annex A, SYS_DVBC_ANNEX_A)."""

import re

from .base import Error as BaseError


class Error(BaseError):
    pass


# enum fe_delivery_system
SYS_DVBC_ANNEX_A, SYS_DVBC_ANNEX_B, SYS_DVBT, SYS_ISDBT, SYS_ATSC, SYS_DVBT2 = 1, 2, 3, 8, 11, 16

CABLE = "C"
_NAME = re.compile(r"^(C?)0*([1-9][0-9]{0,2})$")


def parse(text):
    """(cable, number) of an RF name: "7" (False, 7), "C78" (True, 78); Error for another form."""
    m = _NAME.match(str(text))
    if not m:
        raise Error(
            f"{text!r}: an RF is a channel number (7) or C and a cable channel number (C78)"
        )
    return bool(m.group(1)), int(m.group(2))


def name(number, cable=False):
    return f"{CABLE if cable else ''}{number}"


def canonical(text):
    """The RF name of text as a user wrote it ("07" is "7"); Error for another form."""
    cable, n = parse(text)
    return name(n, cable)


def us_air_hz(ch):
    """The US (ATSC) air channel's centre frequency in Hz."""
    if 2 <= ch <= 4:
        return (57 + 6 * (ch - 2)) * 1000000
    if 5 <= ch <= 6:
        return (79 + 6 * (ch - 5)) * 1000000
    if 7 <= ch <= 13:
        return (177 + 6 * (ch - 7)) * 1000000
    if 14 <= ch <= 36:
        return (473 + 6 * (ch - 14)) * 1000000
    raise Error(f"no US air channel {ch} (2 to 36)")


# TBL_STD_NADC_F: (first channel, last, the first one's centre in MHz), 6 MHz apart
_US_CABLE = ((1, 1, 75), (2, 4, 57), (5, 6, 79), (7, 13, 177), (14, 22, 123), (23, 94, 219),
             (95, 99, 93), (100, 135, 651))  # fmt: skip


def us_cable_hz(ch):
    """The US cable channel's standard (STD) centre frequency in Hz."""
    for first, last, mhz in _US_CABLE:
        if first <= ch <= last:
            return (mhz + 6 * (ch - first)) * 1000000
    raise Error(f"no US cable channel C{ch} (C1 to C135)")


def jp_uhf_hz(ch):
    """The Japanese UHF channel's centre frequency (13 to 62: 473 + 6 (ch - 13) + 1/7 MHz)."""
    if not 13 <= ch <= 62:
        raise Error(f"no Japanese UHF channel {ch} (13 to 62)")
    return (473 + 6 * (ch - 13)) * 1000000 + 142857


def dvbt_hz(ch):
    """The DVB-T channel's centre frequency: VHF 6 to 12 from 178 MHz and UHF 21 to 69 from 474 MHz,
    8 MHz apart."""
    if 6 <= ch <= 12:
        return (178 + 8 * (ch - 6)) * 1000000
    if 21 <= ch <= 69:
        return (474 + 8 * (ch - 21)) * 1000000
    raise Error(f"no DVB-T channel {ch} (VHF 6 to 12, UHF 21 to 69)")


def dvbc_hz(ch):
    """The DVB-C channel's centre frequency: C1 at 50 MHz to C106 at 890 MHz, 8 MHz apart."""
    if not 1 <= ch <= 106:
        raise Error(f"no DVB-C channel C{ch} (C1 to C106)")
    return (50 + 8 * (ch - 1)) * 1000000


def center_hz(broadcast, rf):
    """The centre frequency in Hz of the RF named rf of the broadcast standard; Error if it has
    none."""
    cable, n = parse(rf)
    if broadcast == "arib":
        if cable:
            raise Error(f"{rf}: a Japanese set's RFs are UHF channels, 13 to 62")
        return jp_uhf_hz(n)
    if broadcast == "dvb":
        return dvbc_hz(n) if cable else dvbt_hz(n)
    return us_cable_hz(n) if cable else us_air_hz(n)


def delsys(broadcast, rf, system=None):
    """The delivery system of the RF named rf of the broadcast standard (system: a DVB lineup's
    "t2" for DVB-T2)."""
    cable, _n = parse(rf)
    if broadcast == "arib":
        return SYS_ISDBT
    if broadcast == "dvb":
        return SYS_DVBC_ANNEX_A if cable else (SYS_DVBT2 if system == "t2" else SYS_DVBT)
    return SYS_DVBC_ANNEX_B if cable else SYS_ATSC


def sort_key(rf):
    """Air before cable, each by number."""
    cable, n = parse(rf)
    return (cable, n)
