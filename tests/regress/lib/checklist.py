# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The bring-up checklist of scripts/regress (docs/dev/guides/build.md, "Bring-up checklist"): the recurring
pitfalls of a new target, judged on the host from the facts tests/regress/guest.sh's checklist step
gathers in the guest. Each item is PASS, WARN or FAIL with a one-line reason. FAIL is a broken
bring-up (the set misbehaves for it); WARN is worth a look but seen on working targets.

  first-app       bootd launched a first app, and the EEPROM's Last_Input_info DB is not blank
  region          tvservice's broadcast system, the country, the country groups, the continent and
                  the smart service country agree with the firmware's broadcast standard
  mount-readwrite its flag file made, the job not failed, pm_freeze_timeout as it writes it,
                  preload/ where it reads it
  tvservice       no restart of tvservice, no crash report or core file
  sysprops        luna-prefs' getAllSysProperties answers, with storageCapacity (the eMMC's size)
                  (WARN where the firmware's nyx has no name for the board: NYX_BOARD_GAPS)
  eeprom          tvservice's model name read (not empty) and the one the bootloader passes
  modules         lgemu.ko and the SoC's modules loaded, built for the running kernel, no sign
                  of a stale build in the kernel log
"""

import json
import os
import re
import sys

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        ),
        "host",
        "core",
    ),
)
from beanjar import targets  # noqa: E402

ITEMS = ("first-app", "region", "mount-readwrite", "tvservice", "sysprops", "eeprom", "modules")
ORDER = {"PASS": 0, "WARN": 1, "FAIL": 2}

# The broadcast standards (targets.BROADCASTS) and what belongs to them. A firmware is built for one;
# its model_id.json says which (targets.firmware_broadcast), a firmware before it its paks
# (targets.broadcast_of_paks). countries: ISO 3166 alpha-3 as settingsservice's
# option.country; groups: the two-letter group and area codes (option.countryGroup,
# contiLangCountry, tvconfig's rmmgroup and continentname, the smart service country's prefix:
# "US7" -> US). A DVB firmware takes any region not of another standard.
BROADCASTS = targets.BROADCASTS
REGIONS = {
    "atsc": {"countries": {"USA", "CAN", "MEX", "KOR"}, "groups": {"US", "KR", "CA", "MX"}},
    "arib": {"countries": {"JPN"}, "groups": {"JP", "JA"}},
}
# kernel taint bit 1 (F): a module force-loaded past its vermagic
TAINT_FORCED = 1 << 1


def broadcast_of_firmware(fw_dir, pak_files=()):
    """The broadcast standard of the firmware directory (targets.firmware_broadcast: its
    model_id.json); for a firmware without one (before jhericurl), the paks it has, and for a
    directory that is not here the target's (pak_files: targets.pak_files)."""
    return (
        targets.firmware_broadcast(fw_dir)
        or targets.broadcast_of_paks(targets.firmware_paks(fw_dir))
        or targets.broadcast_of_paks(pak_files)
    )


def region_broadcast(value, kind):
    """The broadcast standard a country (kind "countries") or a group code ("groups") belongs to:
    atsc or arib where listed, else dvb."""
    for broadcast, r in REGIONS.items():
        if value.upper() in r[kind]:
            return broadcast
    return "dvb"


def parse(text):
    """{name: [values]} of the guest's "name=value" lines (other lines ignored)."""
    facts = {}
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep and re.fullmatch(r"[a-z_]+", name):
            facts.setdefault(name, []).append(value.strip())
    return facts


def _one(facts, name):
    v = facts.get(name)
    return v[0] if v else None


def _json(facts, name):
    """A luna-send answer, {} if missing or not JSON."""
    try:
        v = json.loads(_one(facts, name) or "{}")
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def _dmesg(facts, pattern):
    return [l for l in facts.get("dmesg", []) if re.search(pattern, l)]


def first_app(facts):
    status = _json(facts, "bootstatus")
    if not status:
        return "FAIL", "bootd's getBootStatus did not answer"
    # webOS 5's bootd answers firstAppId "" (it keeps the template): the id its log launched
    app = status.get("firstAppId") or _one(facts, "firstapp_launch") or ""
    if status.get("firstAppLaunched") is not True or not app:
        return (
            "FAIL",
            f"no first app (firstAppLaunched {status.get('firstAppLaunched')}, firstAppId {app!r})",
        )
    li = _one(facts, "lastinput")
    if li:
        off, _, mark = li.partition(" ")
        if mark in ("", "unread"):
            return "FAIL", f"the EEPROM's Last_Input_info DB at {off} could not be read"
        if set(mark) <= {"f"} or set(mark) <= {"0"}:
            return "FAIL", f"blank Last_Input_info DB at {off} ({mark}): no last input"
        return "PASS", f"first app {app}; Last_Input_info DB at {off} marked {mark}"
    return "PASS", f"first app {app}"


def region(facts, broadcast):
    if broadcast not in BROADCASTS:
        return "WARN", "the firmware's broadcast standard is unknown (no -dvb/-atsc/-arib pak)"
    info, settings = _json(facts, "sysinfo"), _json(facts, "settings").get("settings") or {}
    seen, bad = [], []
    system = info.get("tvSystemName")
    if system:
        seen.append(f"system {system}")
        if system.lower() != broadcast:
            bad.append(f"tvservice's system {system}")
    country = settings.get("country")
    if country:
        seen.append(f"country {country}")
        if region_broadcast(country, "countries") != broadcast:
            bad.append(f"country {country}")
    for label, value in (
        ("group", settings.get("countryGroup")),
        ("contiLangCountry", info.get("contiLangCountry")),
        ("rmmgroup", _one(facts, "tvconfig_rmmgroup")),
        ("continent", _one(facts, "tvconfig_continentname")),
    ):
        if value:
            seen.append(f"{label} {value}")
            if region_broadcast(value, "groups") != broadcast:
                bad.append(f"{label} {value}")
    rmm = _one(facts, "tvconfig_rmmcountry")
    if rmm and region_broadcast(rmm, "countries") != broadcast:
        bad.append(f"rmmcountry {rmm}")
    services = [
        settings.get(k)
        for k in ("smartServiceCountryCode2", "smartServiceCountryCode3")
        if settings.get(k)
    ]
    for s in services:
        if s.lower() == "other":
            bad.append(f"service country still {s}")
        # a group with a number (JP7), or the set's own country, as FirstUse's completeSetup
        # writes smartServiceCountryCode2/3 (JP, JPN)
        elif region_broadcast(re.sub(r"\d+$", "", s), "groups") != broadcast and s != country:
            bad.append(f"service country {s}")
    if services:
        seen.append("service " + "/".join(dict.fromkeys(services)))
    if not country:
        return "FAIL", "no country from settingsservice"
    if bad:
        return "FAIL", f"{broadcast} firmware with " + ", ".join(bad)
    return "PASS", f"{broadcast}: " + ", ".join(seen)


def mount_readwrite(facts):
    failed = _dmesg(facts, r"init: mount-readwrite ")
    if failed:
        return "FAIL", failed[0]
    flag = _one(facts, "mrw_flag")
    if not flag:
        return "FAIL", "no word on mount-readwrite"
    path, _, made = flag.rpartition(" ")
    if made != "yes":
        return "FAIL", f"mount-readwrite did not finish: no {path}"
    want, _, have = (_one(facts, "pm_freeze_timeout") or "none missing").partition(" ")
    if want != "none" and want != have:
        return "FAIL", f"pm_freeze_timeout {have}, mount-readwrite writes {want}"
    need, _, there = (_one(facts, "preload") or "unused no").partition(" ")
    if need == "needed" and there != "yes":
        return "WARN", "no /mnt/lg/appstore/preload, which mount-readwrite reads"
    status = _one(facts, "mrw_status") or ""
    if "stop/waiting" not in status and status != "systemd active/success":
        return "WARN", f"mount-readwrite {status or 'unknown'}"
    extra = f", pm_freeze_timeout {have}" if want != "none" else ""
    return "PASS", f"{path} made{extra}" + (", preload/ there" if need == "needed" else "")


def _crash_name(path):
    """The process a crash report is about: rdx's "..._<name>__Linux Crash: ..." or the file name."""
    base = path.rsplit("/", 1)[-1]
    m = re.search(r"Linux Crash: (\S+) crashes", base)
    return m.group(1) if m else base


def tvservice(facts):
    own = _dmesg(facts, r"init: tvservice ")
    crashes = [_crash_name(c) for c in facts.get("crash", [])]
    cores = facts.get("core", [])
    if own:
        return "FAIL", f"tvservice restarted: {own[0]}" + (
            f" (+{len(own) - 1} more)" if len(own) > 1 else ""
        )
    mine = [c for c in crashes if c in ("tvservice", "RELEASE")]
    if mine:
        return "FAIL", f"crash report of tvservice ({len(mine)})"
    killed = [
        re.sub(r"^init: | main process.*$|\(\d+\)", "", l).strip()
        for l in _dmesg(facts, r"killed by [A-Z]+ signal")
    ]
    notes = []
    if crashes:
        notes.append(f"{len(crashes)} crash report(s): " + ", ".join(dict.fromkeys(crashes)))
    if killed:
        notes.append("jobs killed by a signal: " + ", ".join(dict.fromkeys(killed)))
    if cores:
        notes.append(f"core files: {', '.join(cores)}")
    if notes:
        return "WARN", "tvservice not restarted; " + "; ".join(notes)
    return "PASS", "tvservice not restarted, no crash report or core file"


# Boards whose own nyx DeviceInfo module cannot name the board, so `boardType` fails and
# getAllSysProperties answers "required system resource is missing" on the stock firmware too.
# Measured on W15L (mtka5lr 03.31.00) in a running guest: RELEASE.cfg's eMainBoardType is
# BOARD_A5LR_DVB_EU, DILE_BOARDINFO_GetMainBoardType returns 0x11 (init 0), and
# nyxDeviceInfoMain.module's board_type looks 0x11 up in tables whose keys are 0..4 and 2, 4, 7
# (H13 and M14 boards only, ended by 0xff), finds nothing and answers error 8.
NYX_BOARD_GAPS = {"BOARD_A5LR_DVB_EU": "0x11"}


def sysprops(facts):
    """luna-prefs' getAllSysProperties: an array of one-property objects, or an error object when
    any property fails (storageCapacity without an mmcblk0 line in /proc/partitions)."""
    raw = _one(facts, "sysprops")
    try:
        answer = json.loads(raw or "")
    except ValueError:
        return "FAIL", f"getAllSysProperties did not answer: {(raw or '').strip()[:80]!r}"
    if isinstance(answer, dict):
        why = answer.get("errorText") or answer.get("errorCode") or "no properties"
        board = _one(facts, "main_board") or ""
        if board in NYX_BOARD_GAPS and "Error 8 " in (_one(facts, "nyx_board_type") or ""):
            return "WARN", (
                f"getAllSysProperties: {why}; the firmware's nyx has no board name for {board} "
                f"({NYX_BOARD_GAPS[board]}): board_type fails with error 8 on the stock set too"
            )
        return "FAIL", f"getAllSysProperties: {why}"
    props = {}
    for p in answer if isinstance(answer, list) else []:
        if isinstance(p, dict):
            props.update(p)
    cap = props.get("com.palm.properties.storageCapacity")
    if not cap:
        return "FAIL", "getAllSysProperties has no storageCapacity"
    return "PASS", f"{len(props)} properties, storageCapacity {cap}"


def eeprom(facts):
    info = _json(facts, "sysinfo")
    if not info:
        return "FAIL", "tv.systemproperty getSystemInfo did not answer"
    model = info.get("modelName") or ""
    board = _one(facts, "cmdline_model") or ""
    if not model.strip():
        return "FAIL", "tvservice's model name is empty: the EEPROM's ModelInfoDB was not read"
    if board and model != board:
        return "FAIL", f"tvservice's model name {model!r}, the bootloader's {board!r}"
    return "PASS", f"model name {model}"


def modules(facts):
    kernel = _one(facts, "kernel") or "?"
    mods = [m.split() for m in facts.get("module", [])]
    if not mods:
        return "FAIL", "no word on the kernel modules"
    bad = []
    for name, state, vermagic in ((m + ["?", "?"])[:3] for m in mods):
        if state != "loaded":
            bad.append(f"{name} not loaded")
        if vermagic != kernel:
            bad.append(f"{name} built for {vermagic}, running {kernel}")
    stale = _dmesg(
        facts,
        r"disagrees about version|Unknown symbol|version magic|i2c adapter has no name|"
        r"i2c_adapters: cannot register",
    )
    if stale:
        bad.append(f"kernel log: {stale[0]}")
    try:
        if int(_one(facts, "tainted") or "0") & TAINT_FORCED:
            bad.append("a module was force-loaded (taint F)")
    except ValueError:
        pass
    if bad:
        return "FAIL", "; ".join(bad)
    return "PASS", f"{', '.join(m[0] for m in mods)} loaded, built for {kernel}"


def judge(text, broadcast):
    """[(item, status, reason)] in ITEMS order for the guest's checklist output and the firmware's
    broadcast standard (broadcast_of_firmware). A firmware whose paks name none (W21P's global
    build) has its board's: nyx-option-gen's tvBroadcastSystem (nyx_system)."""
    facts = parse(text)
    if "CHECKLIST FACTS END" not in text:
        return [(i, "FAIL", "the guest's facts are incomplete") for i in ITEMS]
    board_broadcast = (_one(facts, "nyx_system") or "").lower()
    if broadcast is None and board_broadcast in BROADCASTS:
        broadcast = board_broadcast
    return [
        ("first-app", *first_app(facts)),
        ("region", *region(facts, broadcast)),
        ("mount-readwrite", *mount_readwrite(facts)),
        ("tvservice", *tvservice(facts)),
        ("sysprops", *sysprops(facts)),
        ("eeprom", *eeprom(facts)),
        ("modules", *modules(facts)),
    ]


def verdict(items):
    """The worst status of the items (PASS with none)."""
    return max((s for _, s, _ in items), key=ORDER.get, default="PASS")


def short(items):
    """The table's cell: the verdict, and the items that are not PASS."""
    v = verdict(items)
    bad = [i for i, s, _ in items if s != "PASS"]
    return v if not bad else f"{v}({','.join(bad)})"
