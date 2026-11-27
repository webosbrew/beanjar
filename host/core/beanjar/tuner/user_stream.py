# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""A user's transport stream for a physical channel (`beanjar io tuner map NAME RF --stream FILE`):
the checks it gets before it is copied into the VM's state, and what the tuner needs to loop it.

Errors (inspect): not a whole number of 188-byte packets, a packet without its sync byte,
scrambled packets in its first seconds (a CA-protected recording: Beanjar never descrambles), no
PAT, no PCR (its bit rate is the PCR's). Warnings, the user's tables never rewritten: a NIT or PSIP
of another standard than the board's, a delivery descriptor whose frequency is not the RF's, no
clock tables (ATSC STT; ARIB and DVB TDT and TOT).

What the tuner keeps of it (the record): the bit rate from its first and last PCR, its programs,
its PCR PIDs, and per PID the first and the last packet that starts a PES or section (cuts): the
loop plays each PID from its first start to before its last, so no PES or section is cut by the
wrap (tuner.RecordedStream)."""

import mmap
import os

from ..base import Error

PACKET = 188
NULL_PID = 0x1FFF
PCR_HZ = 27_000_000
PCR_WRAP = (1 << 33) * 300
PCR_STEP_MAX = PCR_HZ * 7 // 10  # ISO/IEC 13818-1: PCRs at most 100 ms apart; past this, a jump
SCRAMBLE_SECONDS = 5  # the first seconds in which a scrambled packet is an error
TOLERANCE_HZ = 1_000_000

# the tables looked for, by PID: PAT; NIT; TDT and TOT; ATSC PSIP (MGT, TVCT, CVCT, STT)
SI_PIDS = (0x0000, 0x0010, 0x0014, 0x1FFB)
TAG_ISDBT_DELIVERY, TAG_DVBT_DELIVERY, TAG_DVBC_DELIVERY = 0xFA, 0x5A, 0x44


def _pcr(mm, i):
    """The PCR of packet i (27 MHz), or None."""
    at = i * PACKET
    if not mm[at + 3] & 0x20 or mm[at + 4] == 0 or not mm[at + 5] & 0x10:
        return None
    b = mm[at + 6 : at + 12]
    base = (b[0] << 25) | (b[1] << 17) | (b[2] << 9) | (b[3] << 1) | (b[4] >> 7)
    ext = ((b[4] & 1) << 8) | b[5]
    return base * 300 + ext


class _Sections:
    """The whole sections of a PID, reassembled from its packets' payloads."""

    def __init__(self):
        self.buf = None
        self.out = []

    def add(self, pkt):
        afc = (pkt[3] >> 4) & 3
        if not afc & 1:
            return
        pl = pkt[4 + (1 + pkt[4] if afc & 2 else 0) :]
        if pkt[1] & 0x40:
            if not pl:
                return
            ptr = pl[0]
            if self.buf is not None:
                self.buf += pl[1 : 1 + ptr]
                self._flush()
            self.buf = bytearray(pl[1 + ptr :])
        elif self.buf is not None:
            self.buf += pl
        else:
            return
        self._flush()

    def _flush(self):
        b = self.buf
        while len(b) >= 3 and b[0] != 0xFF:
            n = 3 + (((b[1] & 0xF) << 8) | b[2])
            if len(b) < n:
                break
            self.out.append(bytes(b[:n]))
            del b[:n]
        if b and b[0] == 0xFF:
            self.buf = None


def _descriptors(data):
    at = 0
    while at + 2 <= len(data):
        tag, n = data[at], data[at + 1]
        yield tag, data[at + 2 : at + 2 + n]
        at += 2 + n


def _nit_delivery(sec):
    """(kinds, frequencies in Hz) of a NIT section's delivery descriptors."""
    kinds, freqs = set(), []
    if len(sec) < 16:
        return kinds, freqs
    at = 10 + (((sec[8] & 0xF) << 8) | sec[9])
    end = len(sec) - 4
    at += 2  # transport_stream_loop_length
    while at + 6 <= end:
        n = ((sec[at + 4] & 0xF) << 8) | sec[at + 5]
        for tag, d in _descriptors(sec[at + 6 : at + 6 + n]):
            if tag == TAG_ISDBT_DELIVERY and len(d) >= 4:
                kinds.add("arib")
                freqs += [
                    int.from_bytes(d[k : k + 2], "big") * 1_000_000 // 7
                    for k in range(2, len(d) - 1, 2)
                ]
            elif tag == TAG_DVBT_DELIVERY and len(d) >= 4:
                kinds.add("dvb")
                freqs.append(int.from_bytes(d[0:4], "big") * 10)
            elif tag == TAG_DVBC_DELIVERY and len(d) >= 4:  # 8 BCD digits, 100 Hz units
                kinds.add("dvb")
                freqs.append(int(d[0:4].hex()) * 100 if d[0:4].hex().isdigit() else 0)
        at += 6 + n
    return kinds, freqs


def _pat_programs(sec):
    out = []
    for at in range(8, len(sec) - 4 - 3, 4):
        number = int.from_bytes(sec[at : at + 2], "big")
        if number:  # 0: the NIT's PID
            out.append(number)
    return out


def inspect(path, broadcast, center_hz=None, channel=None):
    """The record of the stream at path for an RF of a board of the broadcast standard (center_hz:
    the RF's centre frequency), with "warnings"; Error for a stream the tuner cannot play."""
    name = os.path.basename(path)
    try:
        size = os.path.getsize(path)
    except OSError as e:
        raise Error(f"{path}: {e.strerror}") from None
    if size == 0 or size % PACKET:
        hint = (
            " (a 192-byte M2TS or BDAV recording: remux it to TS with ffmpeg -c copy)"
            if (size and size % 192 == 0)
            else ""
        )
        raise Error(f"{name}: {size} bytes is not a whole number of 188-byte packets{hint}")
    n = size // PACKET
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        sync = mm[0::PACKET]
        if sync.count(0x47) != n:
            i = next(k for k, b in enumerate(sync) if b != 0x47)
            raise Error(
                f"{name}: packet {i} (byte {i * PACKET}) has no sync byte 0x47: not a transport "
                "stream, or a damaged one"
            )
        h1, h2, h3 = mm[1::PACKET], mm[2::PACKET], mm[3::PACKET]
        firsts, lasts = {}, {}
        scrambled = None
        pcrs = {}  # PID: [(packet, first PCR), (packet, last PCR), count]
        tables = {pid: _Sections() for pid in SI_PIDS}
        for i in range(n):
            b1 = h1[i]
            pid = ((b1 & 0x1F) << 8) | h2[i]
            if pid == NULL_PID:
                continue
            b3 = h3[i]
            if b3 & 0xC0 and scrambled is None:
                scrambled = (i, pid, b3 >> 6)
            if b1 & 0x40:
                firsts.setdefault(pid, i)
                lasts[pid] = i
            if b3 & 0x20:
                pcr = _pcr(mm, i)
                if pcr is not None:
                    # [last packet, last PCR, packets and ticks between PCRs on one time base]
                    got = pcrs.get(pid)
                    if got is None:
                        pcrs[pid] = [i, pcr, 0, 0]
                    else:
                        ticks = (pcr - got[1]) % PCR_WRAP
                        if 0 < ticks < PCR_STEP_MAX:  # else a discontinuity: not counted
                            got[2] += i - got[0]
                            got[3] += ticks
                        got[0], got[1] = i, pcr
            t = tables.get(pid)
            if t is not None and len(t.out) < 64:
                t.add(mm[i * PACKET : (i + 1) * PACKET])
    if not pcrs or not max(t for _i, _p, _n, t in pcrs.values()):
        raise Error(f"{name}: no PCR: the tuner sends a stream at its PCR's rate, and the set's "
                    "decoders time it on the PCR")  # fmt: skip
    pcr_pid = max(pcrs, key=lambda p: pcrs[p][3])
    _i, _p, packets, ticks = pcrs[pcr_pid]
    rate = packets * PACKET * 8 * PCR_HZ // ticks
    if not 100_000 <= rate <= 100_000_000:
        raise Error(f"{name}: its PCR gives {rate} bit/s: not a stream the tuner can pace")
    if scrambled is not None:
        i, pid, tsc = scrambled
        at_s = i * PACKET * 8 / rate
        if at_s < SCRAMBLE_SECONDS:
            raise Error(
                f"{name}: scrambled (transport_scrambling_control {tsc} on PID 0x{pid:04X} at "
                f"{at_s:.1f} s): a recording protected by B-CAS, A-CAS or another conditional "
                "access system; Beanjar never descrambles, give it a clear (free-to-air) stream"
            )
    secs = {pid: t.out for pid, t in tables.items()}
    pats = [s for s in secs[0x0000] if s[0] == 0x00]
    if not pats:
        raise Error(f"{name}: no PAT: the set finds no programme in it")
    warnings = []
    psip = [s for s in secs[0x1FFB] if s[0] in (0xC7, 0xC8, 0xC9, 0xCD)]
    nits = [s for s in secs[0x0010] if s[0] == 0x40]
    found, freqs = set(), []
    if psip:
        found.add("atsc")
    for s in nits:
        kinds, f = _nit_delivery(s)
        found |= kinds or {"dvb"}
        freqs += f
    what = {"atsc": "ATSC (PSIP)", "arib": "ISDB-T (NIT)", "dvb": "DVB (NIT)"}
    if not found:
        warnings.append(
            f"{name} has neither a NIT nor ATSC PSIP: the set may find no channel in it"
        )
    elif broadcast not in found:
        warnings.append(
            f"{name}'s tables are {', '.join(what[k] for k in sorted(found))} and the board's "
            f"broadcast standard is {broadcast}: the set may find no channel in it"
        )
    if freqs and center_hz and not any(abs(f - center_hz) <= TOLERANCE_HZ for f in freqs):
        warnings.append(
            f"{name}'s delivery descriptor gives "
            f"{', '.join(f'{f / 1e6:.3f}' for f in sorted(set(freqs)))} MHz "
            f"and RF {channel} is {center_hz / 1e6:.3f} MHz: the set may skip the channel or look "
            "for it elsewhere"
        )
    if broadcast in ("arib", "dvb"):
        if not any(s[0] in (0x70, 0x73) for s in secs[0x0014]):
            warnings.append(f"{name} has no TDT or TOT: the set's clock does not come from it")
    elif not any(s[0] == 0xCD for s in secs[0x1FFB]):
        warnings.append(f"{name} has no STT: the set's clock does not come from it")
    if scrambled is not None:
        i, pid, _tsc = scrambled
        warnings.append(
            f"{name} has scrambled packets from {i * PACKET * 8 / rate:.0f} s on (PID 0x{pid:04X})"
        )
    programs = sorted({p for s in pats for p in _pat_programs(s)})
    cuts = {}
    for pid, first in firsts.items():
        last = lasts[pid]
        cuts[str(pid)] = [first, last if last > first else n]
    return {
        "name": name,
        "bytes": size,
        "packets": n,
        "rate": rate,
        "seconds": round(n * PACKET * 8 / rate, 3),
        "programs": programs,
        "pcr_pids": sorted(pcrs),
        "cuts": cuts,
        "standard": sorted(found),
        "warnings": warnings,
    }
