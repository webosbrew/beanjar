# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The TV feature suite of scripts/regress --tv (docs/dev/guides/build.md, "TV feature suite"): the checks, and
their judging on the host from the facts tests/regress/tv.sh gathers in the guest.

Five steps (draw in graphics only), each a list of checks; every check is PASS or FAIL, or SKIP where the target's row of the
expected table, tests/regress/tv-expect/<target>.toml (made by tests/regress/tv-expect-gen.py from
the firmware; load_expect), says the API, method or key is absent from that firmware and cites the file that shows
it. There is no WARN: a check that fails needs treatment.

  luna-info  the public Luna APIs LG documents for apps (CATALOGUE: TV Device Information, Settings
             Service, Connection Manager, Device Unique ID, System Service, Application Manager,
             Magic Remote, Camera, BLE GATT), each method and documented key called on its own and
             its answer's documented fields checked, the values against the board and firmware the
             target runs (VALUE checks: model name, firmware version, SDK version, board type, region);
             tvservice's own identity keys; nyx-cmd OSInfo and DeviceInfo, every key
  network    eth0's DHCP lease (10.0.2.15, router .2, DNS .254) and the connection manager's word on
             it, DNS through the guest's resolver against the VM host's own answer, an HTTP fetch
             from a server in the VM's own network namespace (tests/regress/http-token.py at
             10.0.2.254, the guest's request seen from 10.0.2.15), and the
             connection manager's online state against what the VM host itself reaches of the
             firmware's online check (connman's Ipv4StatusUrl)
  apps       each stock app of the target's table (APP_ROLES) launched, in front, the remote's keys,
             still in front in the same process, its screenshot not black, closed (judge_app); our
             test web app (tvapp) installed with dev/install, the same, removed at the end
  tee        where the firmware has a TEE client (the expected table's tee: an ELF naming /dev/tee; SKIP
             with that evidence elsewhere): /dev/tee0, and through the firmware's own libteec
             (tests/regress/tee/tee-probe in webOS) a session of rtk.ko's secure-store TA and of each TA the
             firmware ships (ITEM_NOT_FOUND by design), each secure-store command its clients use
             with its round trip, the answers checked against tests/regress/lib/teeref.py from the VM's
             own identity/tee-huk (only generated keys), and no crash report of a TEE client from
             the boot on (judge_tee)
  draw       the test app's pattern (tvapp/pattern.js) with canvas 2D and WebGL, on the screenshot
             where the pattern puts it (image_facts on the VM host, judge_draw here)
  audio      tones through the firmware's sound path, recorded by QEMU (judge_audio)
  decode     graphics: the test app plays each of DECODE_CASES (clips made at test time,
             tests/regress/video/make-clips.sh --decode) with <video> and with its Media Source player,
             while a viewer on the VM host decodes what the guest's sinks send (media_facts there,
             judge_decode here); SKIP where the expected table's decode row has no evidence that the
             firmware's stack takes the codec on that path; headless SKIP (no web engine)
  smp        graphics: the SMP check app (tests/regress/smp: a StarfishMediaAPIs client as ss4s's player,
             a developer-mode native app) feeds an 8 s H.264 clip at 30 fps; its FRAMEREADY
             callbacks (the shown frame's PTS) must come while it plays, advance and reach the
             clip's end, and the viewer must show the clip (judge_smp); SKIP where the expected
             table's smp row says the firmware's libplayerAPIs.so.1 exports none of the names of
             one of the app's lookups, or it has no such library; headless SKIP (no viewer of the step's own)
"""

import json
import math
import os
import re
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REGRESS = os.path.dirname(HERE)  # tests/regress: the suite's data, the app, the guest scripts
TOP = os.path.dirname(os.path.dirname(REGRESS))
EXPECT_DIR = os.path.join(REGRESS, "tv-expect")  # the expected table: a <target>.toml each
STEPS = ("luna-info", "network", "apps", "tee", "draw", "audio", "decode", "smp")
# The guest's network, link=nat's namespace (host/core/beanjar/netns.py, which a unit test holds these
# to): the lease (GUEST), slirp4netns's router (ROUTER), the name server DHCP hands out (NET_DNS:
# lg_mock.py at the TAP's own address, MOCK_DNS, with LG's services mocked, the default), and the
# suite's test server (TEST_SERVER: tests/regress/http-token.py in the VM's own namespace at the
# TAP's address, DHCP_SERVER, the way the guest reaches the mock; never the host's loopback)
NET_GUEST, NET_ROUTER, NET_DNS = "10.0.2.15", "10.0.2.2", "10.0.2.254"
TEST_SERVER = "10.0.2.254"
DNS_NAME = "example.com"
# what the VM host itself resolves and fetches: the reference for the guest's DNS and online state
HOST_PROBE = (
    "echo \"dns=$(getent ahostsv4 {name} | awk '{{print $1}}' | sort -u | tr '\\n' ' ')\"; "
    "echo \"online_dns=$(getent ahostsv4 {host} | awk '{{print $1}}' | sort -u | tr '\\n' ' ')\"; "
    "echo \"online_http=$(curl -s -m 10 -o /dev/null -w '%{{http_code}}' {url} 2>&1)\""
)
BROADCASTS = ("dvb", "atsc", "arib")
REGION_GROUPS = {"atsc": {"US", "KR", "CA", "MX"}, "arib": {"JP", "JA"}}
REGION_COUNTRIES = {"atsc": {"USA", "CAN", "MEX", "KOR"}, "arib": {"JPN"}}

# LG's webOS TV developer site (webostv.developer.lge.com, "API reference", the Introduction's
# compatibility matrix, webOS TV device row): each API's support on webOS TV 1.x to 6.x (Y yes,
# P partially, N no, D deprecated)
REFERENCE = {
    "TV Device Information": "PYYYYY",
    "Settings Service": "YYYYYY",
    "Connection Manager": "YYYYYY",
    "Application Manager": "YYYYYY",
    "Device Unique ID": "NNYYYY",
    "Magic Remote": "PPPPPP",
    "System Service": "YYYYYY",
    "BLE GATT": "NNNNNN",
    "Camera": "YYYDDD",
}
# method-level notes of the same pages
REFERENCE_NOTES = {
    "sam.getAppLoadStatus": "available on webOS TV 4.0 or higher",
    "mrcu.getAPIVersion": "sensor2 and getAPIVersion: webOS TV 24 or higher",
    "mrcu.getSensorState": "sensor2 and getAPIVersion: webOS TV 24 or higher",
    "blegatt.getState": "webOS TV 24 or higher",
    "sm.getIDs": "webOS TV 3.0 or later",
}

SERVICES = {
    "tvsp": "com.webos.service.tv.systemproperty",
    "settings": "com.webos.settingsservice",
    "connman.palm": "com.palm.connectionmanager",
    "connman.webos": "com.webos.service.connectionmanager",
    "sm": "com.webos.service.sm",
    "systemservice": "com.palm.systemservice",
    "sam": "com.webos.applicationManager",
    "mrcu": "com.webos.service.mrcu",
    "camera": "com.webos.service.camera",
    "blegatt": "com.webos.service.blegatt",
}
SYSINFO_KEYS = ("boardType", "firmwareVersion", "modelName", "sdkVersion", "UHD", "_3d")
TVSP_KEYS = (
    "otaId",
    "hardwareVersion",
    "serialNumber",
    "tvSystemName",
    "contiLangCountry",
    "platformName",
)
OPTION_KEYS = ("country", "smartServiceCountryCode2", "audioGuidance")
CAPTION_KEYS = (
    "captionEnable",
    "captionAnalog",
    "captionDigital",
    "captionStyle",
    "captionSize",
    "captionFont",
    "captionTextColor",
    "captionTextOpacity",
    "captionBgColor",
    "captionBgOpacity",
    "captionEdgeType",
    "captionEdgeColor",
    "captionWindowColor",
    "captionWindowOpacity",
)
LOCALES = ("UI", "TV", "FMT", "NLP", "STT", "AUD", "AUD2")
# nyx's query types (nyx-lib's nyx_device_info_common.h, NYX_DEVICE_INFO_*, in order): the index a
# module's device_info_query switches on
NYX_DEVICE_INFO = (
    "batt_ch",
    "batt_rsp",
    "board_type",
    "bt_addr",
    "device_name",
    "hardware_revision",
    "installer",
    "keyboard_type",
    "last_reset_type",
    "modem_present",
    "nduid",
    "product_id",
    "radio_type",
    "ram_size",
    "serial_number",
    "storage_free",
    "storage_size",
    "wifi_addr",
    "hardware_id",
    "manufacture_date",
    "msn",
    "uptime",
    "wired_addr",
)
NYX_OS_INFO = (
    "core_os_kernel_config",
    "core_os_kernel_version",
    "core_os_name",
    "core_os_release",
    "core_os_release_codename",
    "encryption_key_type",
    "webos_api_version",
    "webos_build_datetime",
    "webos_build_id",
    "webos_imagename",
    "webos_manufacturing_version",
    "webos_name",
    "webos_prerelease",
    "webos_release",
    "webos_release_codename",
    "webos_release_platformcode",
)


# Wi-Fi and Bluetooth are absent by design (no adapter; out of scope): these checks expect the
# guest to report them not available, and fail if they ever answer a value
DESIGN_ABSENT = {"nyx.DeviceInfo.wifi_addr": "Wi-Fi", "nyx.DeviceInfo.bt_addr": "Bluetooth"}
DESIGN_REASON = "absent by design: no Wi-Fi/BT hardware (out of scope)"
# nyx queries whose value is many lines (the kernel's config)
MULTILINE = ("core_os_kernel_config",)


def catalogue():
    """{check id: dict(api, service, method, key, category)} of luna-info's Luna checks, in order;
    tv.sh calls each one with the fact name of its id. key: the documented key the call asks for
    (getSystemInfo's, a settings category's); category: the settings category ("" for none)."""
    c = {}
    for k in SYSINFO_KEYS:
        c[f"sysinfo.{k}"] = dict(
            api="TV Device Information", service="tvsp", method="getSystemInfo", key=k
        )
    for k in TVSP_KEYS:
        c[f"tvsp.getSystemInfo.{k}"] = dict(api=None, service="tvsp", method="getSystemInfo", key=k)
    c["tvsp.getCurrentCountryGroup"] = dict(
        api=None, service="tvsp", method="getCurrentCountryGroup"
    )
    for k in OPTION_KEYS:
        c[f"settings.option.{k}"] = dict(
            api="Settings Service",
            service="settings",
            method="getSystemSettings",
            key=k,
            category="option",
        )
    for k in CAPTION_KEYS:
        c[f"settings.caption.{k}"] = dict(
            api="Settings Service",
            service="settings",
            method="getSystemSettings",
            key=k,
            category="caption",
        )
    c["settings.localeInfo"] = dict(
        api="Settings Service",
        service="settings",
        method="getSystemSettings",
        key="localeInfo",
        category="",
    )
    c["connman.palm"] = dict(api="Connection Manager", service="connman.palm", method="getStatus")
    c["connman.webos"] = dict(api="Connection Manager", service="connman.webos", method="getStatus")
    c["sm.getIDs"] = dict(api="Device Unique ID", service="sm", method="deviceid/getIDs")
    c["systime"] = dict(api="System Service", service="systemservice", method="time/getSystemTime")
    c["sam.getAppLoadStatus"] = dict(
        api="Application Manager", service="sam", method="getAppLoadStatus"
    )
    c["mrcu.getAPIVersion"] = dict(api="Magic Remote", service="mrcu", method="getAPIVersion")
    c["mrcu.getSensorState"] = dict(
        api="Magic Remote", service="mrcu", method="sensor2/getSensorState"
    )
    c["camera.getList"] = dict(api="Camera", service="camera", method="getList")
    c["blegatt.getState"] = dict(api="BLE GATT", service="blegatt", method="getState")
    return c


def webos_major(release):
    """webOS's major version from etc/starfish-release's version (1.4.0-2341 -> 1; webOS TV 22's
    mullet is 7)."""
    return int(release.split(".")[0])


def reference(api, major):
    """The developer site's word on api for webOS TV <major>.x, or None (not a public API, or a
    release past the matrix's 1.x to 6.x columns: from webOS 7 on, the rows cite the firmware
    alone)."""
    if api is None or not 1 <= major <= len(REFERENCE[api]):
        return None
    word = {"Y": "Yes", "P": "Partially", "N": "No", "D": "Deprecated"}[REFERENCE[api][major - 1]]
    return f"LG API reference: {api} on webOS TV {major}.x: {word}"


# --- the facts ---


def parse(text, name_re=r"[A-Za-z0-9_.]+"):
    """{name: value} of tv.sh's "name=value" lines (the first of a name)."""
    facts = {}
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep and re.fullmatch(name_re, name) and name not in facts:
            facts[name] = value
    return facts


def answer(value):
    """A luna-send answer as a dict ({} when not JSON)."""
    if not value:
        return {}
    try:
        v = json.loads(value)
    except ValueError:
        m = re.search(r"\{.*\}", value)
        try:
            v = json.loads(m.group()) if m else {}
        except ValueError:
            return {}
    return v if isinstance(v, dict) else {}


def nyx(value):
    """(exit status, first line) of a nyx-cmd fact "rc=N value"."""
    m = re.match(r"rc=(\d+) ?(.*)", value or "")
    return (int(m.group(1)), m.group(2).strip()) if m else (None, "")


def error_of(a, raw):
    if not a:
        return f"no answer: {raw[:160]!r}" if raw else "no answer"
    return (
        f"{a.get('errorCode', '')} {a.get('errorText', '')}".strip()
        or f"returnValue {a.get('returnValue')}"
    )


# --- the context: what the target is ---


def expect_targets():
    """The targets of the expected table (tests/regress/tv-expect), by name."""
    return sorted(f[:-5] for f in os.listdir(EXPECT_DIR) if f.endswith(".toml"))


def load_expect(target):
    """A target's row of the expected table (tests/regress/tv-expect/<target>.toml), the evidence of
    its checks resolved from the file's [sources]; {} where the target has no file."""
    import tomllib

    path = os.path.join(EXPECT_DIR, f"{target}.toml")
    try:
        with open(path, "rb") as f:
            row = tomllib.load(f)
    except FileNotFoundError:
        return {}
    sources = row.pop("sources", {})
    for cid, spec in row.get("checks", {}).items():
        if "source" in spec:
            if spec["source"] not in sources:
                raise ValueError(f"{path}: {cid}: no [sources] entry {spec['source']}")
            spec["source"] = sources[spec["source"]]
    return row


def context(target, fw, env, expect=None, tree=None):
    """What the checks compare with: the target's board (its board.cmdline: model name, serial, MAC,
    chip, RAM), its firmware (the name: version; the OTA id is the guest's, tv.sh's ota_id), its broadcast standard, and its row of
    the expected table (expect, else load_expect)."""
    import sys

    sys.path.insert(0, os.path.join(TOP, "host", "core"))
    from beanjar import nvm, targets, paths  # noqa: E402
    import checklist  # noqa: E402

    p = targets.by_name(target)
    row = load_expect(target) if expect is None else expect
    fw_dir = paths.find_firmware(fw)  # in the host's collections (firmware_dirs)
    board_broadcast = targets.board_broadcast(p, fw_dir)
    board = nvm.board_params(p, broadcast=board_broadcast)
    broadcast = checklist.broadcast_of_firmware(fw_dir, targets.pak_files(p, fw_dir)) or row.get(
        "broadcast"
    )
    return dict(
        target=target,
        fw=fw,
        model=board.get("modelName", ""),
        serial=env.get("SERIAL") or board.get("serialNum", ""),
        mac=(env.get("MAC") or board.get("ethaddr", "")).lower(),
        chip=board.get("chip", ""),
        ram=nvm.board_ram(p, broadcast=board_broadcast),
        broadcast=broadcast,
        expect=row,
    )


def fw_version(fw):
    """The firmware's version as tvservice gives it: 05.06.10.01-HE_DTV_WT1H_AFAAABAA -> 05.06.10."""
    return ".".join(fw.split("-")[0].split(".")[:3])


def want_ota_id(ctx):
    """(OTA id, what it is) the firmware's own files give (tv.sh's ota_id); never the firmware
    collection's name, which may be another build's."""
    ota = ctx.get("ota_id")
    if ota:
        return ota, f"the firmware's {ota} (its model_id.json, model_id or RELEASE.cfg)"
    return (
        None,
        "the firmware's OTA id, which tv.sh did not report (no model_id.json, model_id or RELEASE.cfg)",
    )


def region_broadcast(value, kind):
    table = REGION_GROUPS if kind == "groups" else REGION_COUNTRIES
    for broadcast, values in table.items():
        if value.upper() in values:
            return broadcast
    return "dvb"


def chip_name(chip):
    """The board's chip without its revision: H13B0 -> H13, K5LPA0 -> K5LP."""
    return re.sub(r"[A-Z]\d$", "", chip)


def uhd_of_model(model):
    """LG's model names: inch digits, then the series letter, U for an UHD set (49UB850V, 43UP77009LB)."""
    m = re.match(r"\d+([A-Z])", model)
    return "true" if m and m.group(1) == "U" else "false"


# --- luna-info ---


def _value_sysinfo(key, v, ctx):
    rel = ctx["expect"].get("release", {})
    if key == "modelName":
        return v == ctx["model"], f"board's {ctx['model']}"
    if key == "firmwareVersion":
        return v == fw_version(ctx["fw"]), f"the firmware's {fw_version(ctx['fw'])}"
    if key == "sdkVersion":
        want = ".".join(rel.get("version", "?").split(".")[:2])
        return ".".join(v.split(".")[:2]) == want, f"release {rel.get('version')} ({want}.x)"
    if key == "boardType":
        # webOS 5+: the SoC name of the firmware's tv.hw.json (tv.sh's soc_chip); before it, tvservice
        # takes the board's chip= without its revision
        soc = ctx.get("soc_chip") or chip_name(ctx["chip"])
        parts = v.split("_")
        ok = (
            len(parts) == 3
            and parts[0] == soc
            and parts[1].lower() == ctx["broadcast"]
            and region_broadcast(parts[2], "groups") == ctx["broadcast"]
        )
        return (
            ok,
            f"{soc}_{(ctx['broadcast'] or '?').upper()}_<a {ctx['broadcast']} area>",
        )
    if key == "UHD":
        # tvservicemock's releases: the board's panel, as the firmware decodes it (tv-expect-gen.py
        # uhd_of); tvservice's: the model name
        spec = ctx["expect"].get("checks", {}).get("sysinfo.UHD", {})
        if "value" in spec:
            want = spec["value"]
            return want is not None and v == want, f"{want} ({spec['value_source']})"
        return v == uhd_of_model(ctx["model"]), f"{uhd_of_model(ctx['model'])} for a {ctx['model']}"
    if key == "_3d":
        return v in ("true", "false"), "true or false"
    if key == "otaId":
        want, why = want_ota_id(ctx)
        return want is not None and v == want, why
    if key == "serialNumber":
        return v == ctx["serial"], f"the VM's {ctx['serial']}"
    if key == "tvSystemName":
        return v == ctx["broadcast"], f"the firmware's broadcast {ctx['broadcast']}"
    if key == "contiLangCountry":
        return region_broadcast(v, "groups") == ctx["broadcast"], f"a {ctx['broadcast']} group"
    return bool(v), "not empty"


def _check_luna(cid, spec, raw, ctx):
    """(ok, reason) of one Luna check's answer."""
    a = answer(raw)
    # webOS 1 and 2's LunaSysService answers getSystemTime without returnValue
    bare = cid == "systime" and "returnValue" not in a and "errorCode" not in a and "utc" in a
    if a.get("returnValue") is not True and not bare:
        return False, error_of(a, raw)
    key = spec.get("key")
    if spec["method"] == "getSystemInfo":
        if key not in a:
            return False, f"no {key} in {raw[:160]}"
        v = a[key]
        ok, want = _value_sysinfo(key, v, ctx)
        return ok, f"{key} {v!r}" + ("" if ok else f", expected {want}")
    if spec["method"] == "getSystemSettings":
        s = a.get("settings") or {}
        if key not in s:
            return False, f"no settings.{key} in {raw[:160]}"
        v = s[key]
        if key == "localeInfo":
            loc = v.get("locales") if isinstance(v, dict) else None
            miss = [f"locales.{x}" for x in LOCALES if not (loc or {}).get(x)] + [
                x
                for x in ("clock", "keyboards", "timezone")
                if not isinstance(v, dict) or x not in v
            ]
            return not miss, (
                f"missing {', '.join(miss)}"
                if miss
                else f"UI {loc['UI']}, keyboards {v['keyboards']}"
            )
        if not isinstance(v, str) or not v:
            return False, f"{key} {v!r}"
        if key == "country":
            ok = region_broadcast(v, "countries") == ctx["broadcast"]
            return ok, f"country {v}" + ("" if ok else f", not a {ctx['broadcast']} country")
        if key == "smartServiceCountryCode2":
            ok = (
                v.lower() != "other"
                and region_broadcast(re.sub(r"\d+$", "", v), "groups") == ctx["broadcast"]
            )
            return ok, f"{key} {v}" + ("" if ok else f", not of the {ctx['broadcast']} standard")
        if key == "audioGuidance":
            return v in ("on", "off"), f"{key} {v}"
        return True, f"{key} {v}"
    if cid == "tvsp.getCurrentCountryGroup":
        g = a.get("shortName", "")
        ok = (
            bool(g)
            and region_broadcast(g, "groups") == ctx["broadcast"]
            and isinstance(a.get("groupCode"), int)
        )
        return ok, f"group {a.get('groupCode')} {g}" + (
            "" if ok else f", expected a {ctx['broadcast']} group"
        )
    if cid.startswith("connman."):
        miss = [
            k
            for k in ("isInternetConnectionAvailable", "wired", "wifi", "wifiDirect")
            if k not in a
        ]
        miss += [
            f"{k}.state"
            for k in ("wired", "wifi", "wifiDirect")
            if isinstance(a.get(k), dict) and "state" not in a[k]
        ]
        if miss:
            return False, f"missing {', '.join(miss)}"
        return (
            True,
            f"wired {a['wired'].get('state')}, internet {a['isInternetConnectionAvailable']}",
        )
    if cid == "sm.getIDs":
        ids = a.get("idList") or []
        ok = (
            bool(ids)
            and ids[0].get("idType") == "LGUDID"
            and re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                ids[0].get("idValue", ""),
            )
        )
        return bool(ok), "LGUDID, a UUID" if ok else f"idList {ids!r}"
    if cid == "systime":
        miss = [
            k
            for k in ("utc", "localtime", "offset", "timezone", "TZ", "timeZoneFile")
            if k not in a
        ]
        if miss:
            return False, f"missing {', '.join(miss)}"
        skew = abs(a["utc"] - ctx.get("now", time.time()))
        return skew < 900, (
            f"utc {skew:.0f} s from the VM host's clock, {a['timezone']} {a['TZ']}"
            + (" (no returnValue, as LunaSysService of webOS 1 and 2 answers)" if bare else "")
        )
    if cid == "sam.getAppLoadStatus":
        return a.get("exist") is True, f"exist {a.get('exist')} for com.webos.app.livetv"
    if cid == "mrcu.getAPIVersion":
        return "version" in a, f"version {a.get('version')}"
    if cid == "mrcu.getSensorState":
        return "isAlive" in a, f"isAlive {a.get('isAlive')}"
    if cid == "camera.getList":
        return isinstance(a.get("uriList"), list), f"uriList {a.get('uriList')}"
    if cid == "blegatt.getState":
        return "isScan" in a, f"isScan {a.get('isScan')}"
    return True, "returnValue true"


def _check_nyx(module, key, value, err, ctx):
    rc, v = nyx(value)
    if rc:
        v = " ".join(x for x in (v, err) if x)
    rel = ctx["expect"].get("release", {})
    if rc is None:
        return False, "no answer"
    if rc != 0:
        return False, v.strip() or f"exit {rc}"
    want = None
    if module == "OSInfo":
        version = rel.get("version", "")
        want = {
            "core_os_name": rel.get("name"),
            "core_os_release": version,
            "core_os_release_codename": rel.get("codename"),
            "webos_release_codename": rel.get("codename"),
            "webos_release": version.split("-")[0],
            "webos_build_id": version.partition("-")[2],
            "webos_manufacturing_version": fw_version(ctx["fw"]),
            "core_os_kernel_version": ctx.get("uname"),
        }.get(key)
        if key == "webos_prerelease":
            return True, f"{v!r} (empty for a release build)"
    else:
        if key == "product_id":
            want = ctx["model"]
        elif key == "serial_number":
            want = ctx["serial"]
        elif key == "hardware_id":
            want, why = want_ota_id(ctx)
            if want is None:
                return False, why
        elif key == "wired_addr":
            want = ctx["mac"]
            v = v.lower()
        elif key == "board_type":
            ok = f"_{(ctx['broadcast'] or '?').upper()}" in v
            return ok, f"{v}" + ("" if ok else f", not of the {ctx['broadcast']} standard")
        elif key == "ram_size":
            m = re.fullmatch(r"([\d.]+) GB", v)
            ok = bool(m) and float(m.group(1)) * 1024 == ctx["ram"]
            return ok, f"{v}" + ("" if ok else f", the board has {ctx['ram']} MiB")
        elif key == "nduid":
            return bool(re.fullmatch(r"[0-9a-f]{40}", v)), v
    if want is not None:
        return v == want, f"{v!r}" + ("" if v == want else f", expected {want!r}")
    return bool(v), f"{v!r}"


def judge_luna(text, ctx):
    """[(check, PASS/FAIL/SKIP, reason)] of luna-info's facts."""
    facts = parse(text)
    if "TV FACTS END" not in text:
        return [("luna-info", "FAIL", "the guest's facts are incomplete")]
    ctx = dict(
        ctx, uname=facts.get("uname"), soc_chip=facts.get("soc_chip"), ota_id=facts.get("ota_id")
    )
    if facts.get("bootdone_wait") == "none":
        out_first = [("boot-done", "FAIL", "no boot-done from bootd before the calls")]
    else:
        out_first = []
    if not ctx.get("broadcast") and (facts.get("nyx_system") or "").lower() in BROADCASTS:
        ctx["broadcast"] = facts["nyx_system"].lower()  # a global firmware: the board's broadcast
    checks = ctx["expect"].get("checks", {})
    out = out_first

    def skip_or(cid, judge):
        exp = checks.get(cid)
        if exp is not None and exp.get("design"):
            rc, v = nyx(facts.get(cid))
            v = " ".join(x for x in (v, facts.get(cid + ".err", "")) if x)
            ok = rc not in (None, 0) and "not available" in v
            out.append(
                (
                    cid,
                    "PASS" if ok else "FAIL",
                    f"not available, as expected ({exp['source']})"
                    if ok
                    else f"{exp['source']}, but it answered: rc={rc} {v!r}",
                )
            )
        elif exp is None:
            out.append(
                (
                    cid,
                    "FAIL",
                    "not in the target's tests/regress/tv-expect file (run tv-expect-gen.py)",
                )
            )
        elif not exp["present"]:
            out.append((cid, "SKIP", f"absent: {exp['source']}"))
        else:
            ok, why = judge()
            out.append((cid, "PASS" if ok else "FAIL", why))

    def either_bus(cid, spec):
        # two hubs (webOS 1 and 2): the public hub's answer, else the private one's
        ok, why = _check_luna(cid, spec, facts.get(cid, ""), ctx)
        if ok or f"{cid}.prv" not in facts:
            return ok, why + (" (public hub)" if f"{cid}.prv" in facts and ok else "")
        ok2, why2 = _check_luna(cid, spec, facts[f"{cid}.prv"], ctx)
        return ok2, (
            f"{why2} (private hub; the public answered: {why})"
            if ok2
            else f"public hub: {why}; private hub: {why2}"
        )

    for cid, spec in catalogue().items():
        skip_or(cid, lambda cid=cid, spec=spec: either_bus(cid, spec))
    for module, keys in (("OSInfo", NYX_OS_INFO), ("DeviceInfo", NYX_DEVICE_INFO)):
        listed = (facts.get(f"nyx.{module}.keys") or "").split()
        for key in keys:
            cid = f"nyx.{module}.{key}"
            exp = checks.get(cid)
            if exp is None:
                continue  # not a key of any firmware's nyx-cmd
            if exp["present"] and key not in listed:
                out.append((cid, "FAIL", f"nyx-cmd -h {module} does not list it"))
                continue
            skip_or(
                cid,
                lambda m=module, k=key, c=cid: _check_nyx(
                    m, k, facts.get(c), facts.get(c + ".err", ""), ctx
                ),
            )
        # nyx-cmd prints nothing but the value: anything else (a library's messages on stdout after
        # it, or on stderr) is the firmware's libraries meeting something they lack; a design-absent
        # key's own messages (the module's hcitool) aside
        noisy = [
            (k, " ".join(facts.get(f"nyx.{module}.{k}.{w}", "") for w in ("more", "err")).strip())
            for k in listed
            if f"nyx.{module}.{k}" not in DESIGN_ABSENT
            and nyx(facts.get(f"nyx.{module}.{k}"))[0] == 0
            and k not in MULTILINE
        ]
        # the firmware's own lines the table names (W16R's KADP teardown), exactly, and nothing else
        exp = checks.get(f"nyx.{module}.output") or {}
        accepted = 0
        for line in exp.get("accept", ()):
            accepted += sum(n.count(line) for _, n in noisy)
            noisy = [(k, n.replace(line, " ")) for k, n in noisy]
        noisy = [(k, n) for k, n in noisy if n.strip()]
        out.append(
            (
                f"nyx.{module}.output",
                "FAIL" if noisy else "PASS",
                f"{len(noisy)} of {len(listed)} queries printed more than their value, e.g. "
                f"{noisy[0][0]}: {noisy[0][1].strip()[:160]}"
                if noisy
                else f"{len(listed)} queries, values only"
                + (
                    f" but the firmware's own {accepted} lines ({exp['source']})"
                    if accepted
                    else ""
                ),
            )
        )
    return out


# --- network ---


def _hex_ip(h):
    """/proc/net/route's little-endian hex address."""
    return ".".join(str(int(h[i : i + 2], 16)) for i in (6, 4, 2, 0))


def judge_network(text, ctx, token="", served="", host=""):
    """[(check, PASS/FAIL, reason)] of the network step's facts; served: the VM host's server's
    log, host: HOST_PROBE's output there."""
    facts = parse(text)
    if "TV FACTS END" not in text:
        return [("network", "FAIL", "the guest's facts are incomplete")]
    hostf = parse(host)
    out = []
    addr = facts.get("addr", "")
    m = re.search(r"inet (?:addr:)?([\d.]+)(?:/(\d+))?", addr)
    ip = m.group(1) if m else None
    routes = re.findall(r"eth0\s+([0-9A-F]{8})\s+([0-9A-F]{8})\s+\d+", facts.get("route", ""))
    gw = next((_hex_ip(g) for d, g in routes if d == "00000000"), None)
    ok = ip == NET_GUEST and gw == NET_ROUTER
    out.append(
        (
            "dhcp-lease",
            "PASS" if ok else "FAIL",
            f"eth0 {ip or 'has no IPv4 address'}, default route via {gw}"
            + ("" if ok else f" (the namespace leases {NET_GUEST}, router {NET_ROUTER})"),
        )
    )
    # the connection manager under the name the firmware has (webOS 1 and 2: com.palm's getstatus)
    cm, which = {}, None
    for name in ("connman.webos.getStatus", "connman.palm.getStatus", "connman.palm.getstatus"):
        a = answer(facts.get(name, ""))
        if a.get("returnValue") is True:
            cm, which = a, name
            break
    wired = cm.get("wired") if isinstance(cm.get("wired"), dict) else {}
    if not cm:
        out.append(("connman-wired", "FAIL", "no connection manager answered getStatus/getstatus"))
    else:
        want = {
            "state": "connected",
            "interfaceName": "eth0",
            "ipAddress": ip,
            "method": "dhcp",
            "gateway": NET_ROUTER,
            "dns1": NET_DNS,
        }
        bad = [f"{k} {wired.get(k)!r}" for k, v in want.items() if wired.get(k) != v]
        out.append(
            (
                "connman-wired",
                "FAIL" if bad else "PASS",
                f"{which}: "
                + (
                    "; ".join(bad) + f" (expected {want})"
                    if bad
                    else f"wired connected on eth0 {ip} by DHCP, gateway {NET_ROUTER}, DNS {NET_DNS}"
                ),
            )
        )
    # DNS: the guest's resolver (connman's proxy on the loopback) against the VM host's answer
    dns = facts.get("dns", "")
    host_ips = set(hostf.get("dns", "").split())
    got = set(re.findall(r"\b(\d+\.\d+\.\d+\.\d+)\b", re.sub(r"Server:.*?Name:", "", dns)))
    got.discard("127.0.0.1")
    if not host_ips:
        out.append(("dns", "FAIL", f"the VM host cannot resolve {DNS_NAME} itself: no reference"))
    else:
        ok = bool(got & host_ips)
        out.append(
            (
                "dns",
                "PASS" if ok else "FAIL",
                f"{DNS_NAME}: guest {sorted(got) or dns[:120]}, VM host {sorted(host_ips)}",
            )
        )
    # HTTP from the server in the VM's namespace, which sees the guest's own address
    http = facts.get("http", "")
    seen = re.search(
        rf"REQUEST {re.escape(NET_GUEST)} \"GET /{token} HTTP/1\.[01]\" 200", served or ""
    )
    ok = bool(token) and http.split(" ", 1)[-1].strip() == token and bool(seen)
    out.append(
        (
            "http",
            "PASS" if ok else "FAIL",
            f"GET http://{TEST_SERVER}:<port>/<token>: {http[:120]!r}"
            + ("" if seen else f"; the server saw no such request from {NET_GUEST}"),
        )
    )
    # online: what connman's own check (the firmware's Ipv4StatusUrl; TakeDnsIpResolutionAsOnline:
    # its host resolving is enough) gets from here, through the same NAT
    online = ctx["expect"].get("online", {})
    if not cm:
        out.append(("online", "FAIL", "no connection manager answer"))
    else:
        http_ok = hostf.get("online_http", "").strip()[:1] in ("2", "3")
        dns_ok = bool(hostf.get("online_dns", "").split())
        want = http_ok or (online.get("dns_as_online") and dns_ok)
        have_w = wired.get("onInternet") == "yes"
        have_i = cm.get("isInternetConnectionAvailable") is True
        ok = have_w == bool(want) and have_i == bool(want)
        out.append(
            (
                "online",
                "PASS" if ok else "FAIL",
                f"onInternet {wired.get('onInternet')!r}, isInternetConnectionAvailable "
                f"{cm.get('isInternetConnectionAvailable')}; the VM host gets HTTP "
                f"{hostf.get('online_http', '?').strip()} from {online.get('url')}"
                f"{', resolves it' if dns_ok else ''}: expected {'online' if want else 'offline'}",
            )
        )
    return out


# --- apps and draw ---

# The stock apps the apps step launches: (role, the app ids that play it, the first the firmware
# has, what it is). tv-expect-gen.py records each role's app from the firmware's appinfo.json (its
# type and main), or the evidence that the firmware has none. webOS 1 and 2's Home is no app: the
# compositor's StarfishLauncher (LAUNCHER_QML), which HOME opens ("kind": "key").
APP_ROLES = (
    ("settings", ("com.palm.app.settings",), "Settings"),
    ("browser", ("com.webos.app.browser",), "Web Browser"),
    ("livetv", ("com.webos.app.livetv",), "Live TV"),
    (
        "inputpicker",
        ("com.webos.app.inputpicker", "com.webos.app.inputmgr"),
        "the input picker (Input Hub)",
    ),
    ("hdmi1", ("com.webos.app.hdmi1",), "HDMI1"),
    ("home", ("com.webos.app.home", "com.webos.app.mystarter"), "Home (the launcher)"),
    ("dashboard", ("com.webos.app.homeconnect",), "Home Dashboard"),
    ("userguide", ("com.webos.app.tvuserguide",), "User Guide"),
)
LAUNCHER_QML = "usr/lib/qt5/qml/StarfishLauncher/LauncherView.qml"
# LG's terms screen: sam opens it in place of an app whose appinfo.json has requiredEULA when the
# set's user has not agreed to those terms (K8HP's W22H: the browser's generalTerms; emulator-firstuse
# agrees to none, as FirstUse without Internet shows no EULA). It returns to the app once agreed.
TERMS_SCREEN = "com.webos.app.firstuse-overlay"
# headless has no compositor and no WebAppMgr (guest/layers/headless), and its stand-in for the input
# apps' binary ends at once: no app can stay up there. Each is launched all the same, Settings
# first, and must not stay running with sam still answering ("absent by design", as Wi-Fi in
# luna-info), its reason in the expected table (headless_of)
HEADLESS_MAINS = ("file:///usr/bin/com.webos.app.inputcommon",)
HEADLESS_REASON = {
    "web": "a web app: no WebAppMgr headless (guest/layers/headless: WebAppMgr.override manual, "
    "webapp-mgr.service.d ConditionPathExists=!/)",
    "qml": "a QML app: no compositor headless (guest/layers/headless: surface-manager is a stand-in without Wayland)",
}
HEADLESS_DEFAULT = "a Wayland client: no compositor headless (guest/layers/headless: surface-manager is a stand-in)"


def headless_of(row):
    """{"runs": bool, "source": why} of an app row in the headless lane."""
    if row.get("main") in HEADLESS_MAINS:
        return dict(
            runs=False,
            source="the headless layer's stand-in for its binary sends bootd's banner-ready and "
            "ends (guest/layers/headless/rootfs/usr/bin/com.webos.app.inputcommon)",
        )
    return dict(
        runs=False,
        source=HEADLESS_REASON.get(row.get("type"), HEADLESS_DEFAULT) + f" ({row.get('type')})",
    )


# our test web app (tests/regress/tvapp, packed with ares-package, installed with appinstalld's
# dev/install: the graphics lane's devmode layer)
TEST_APP = "com.example.tvsuite"
TEST_APP_DIR = os.path.join(REGRESS, "tvapp")
# LG IR codes (custom 0x04, tests/regress/ir-key.py): the remote's DOWN DOWN RIGHT LEFT UP, the
# apps' navigation; HOME
NAV_KEYS = (0x41, 0x41, 0x06, 0x07, 0x40)
HOME_KEY = 0x7C
NONBLACK_LEVEL, NONBLACK_MIN = (
    24,
    0.005,
)  # a screenshot is black below 0.5 % of pixels over 24 (W20L's
# Live TV: a no-signal dialog over black, 1.4 %)
DRAW_TOL, DRAW_MIN = 48, 0.9  # a patch: 90 % of its samples within 48 of each channel


def pattern(path=None):
    """The test app's draw pattern (tvapp/pattern.js: "var PATTERN = {...};")."""
    with open(path or os.path.join(TEST_APP_DIR, "pattern.js")) as f:
        text = f.read()
    return json.loads(re.search(r"var PATTERN = (\{.*\});", text, re.S).group(1))


def app_plan(expect, lane):
    """[(role, row)] the apps step runs in lane, Settings first (headless: every app with an id;
    those that cannot run there are expected not to come up), and [(check, SKIP, evidence)] of the
    roles the firmware has no app for."""
    run, skips = [], []
    for role, _, what in APP_ROLES:
        row = (expect.get("apps") or {}).get(role)
        if not row:
            run.append((role, dict(present=True, id=None, what=what)))  # not in the table: fails
        elif not row["present"]:
            skips.append((f"app.{role}", "SKIP", row["source"]))
        elif lane == "graphics" or row.get("id"):
            run.append((role, row))
    return run, skips


def terms_first(row, launch):
    """The terms screen's id where the launch's facts (tv.sh app-launch) show it in front in the
    app's place: the app needs terms (the expected table's eula) the set has not agreed to; else None."""
    screen = (row.get("eula") or {}).get("screen")
    if screen and parse(launch).get("front_id") == screen:
        return screen
    return None


def running_entry(value, app_id):
    """sam's running list's entry for app_id, or None."""
    for e in answer(value).get("running") or []:
        if isinstance(e, dict) and e.get("id") == app_id:
            return e
    return None


def in_front(facts, app_id, kind="app"):
    """Whether facts (app-state's) show app_id in front: sam's foreground app, or a compositor
    window of its; kind "key" (webOS 1 and 2's launcher): the compositor's home state."""
    if kind == "key":
        st = answer(facts.get("lsm_state", "")).get("currentState")
        return st == "home" or "_WEBOS_WINDOW_TYPE_HOME" in facts.get("lsm_fg", "")
    return answer(facts.get("sam_fg", "")).get("appId") == app_id or f'"{app_id}"' in facts.get(
        "lsm_fg", ""
    )


def crashes(facts):
    """{process.pid} of the crash reports app-state found (crashd's librdx)."""
    return set(facts.get("crashes", "").split())


def crash_sig(facts, cid):
    """(signal, [frame...]) of crash report cid (process.pid) from app-state's crashsig.<pid> line:
    the report's signal and its crashing thread's frames as symbol+offset; None if not there."""
    v = facts.get(f"crashsig.{cid.rsplit('.', 1)[-1]}", "").split()
    if len(v) < 2 or v[0] != cid.rsplit(".", 1)[0]:
        return None
    return v[1], v[2:]


# LG's own crashes of a stock app, each confirmed on the target and matched narrowly: the targets, the
# role, the lane, the signal and frames of the report (each of "frames" among its crashing
# thread's, as symbol+offset; "top" its first).
# Any other crash report of the app's process fails as before.
EXPECTED_CRASHES = (
    dict(
        # Confirmed on k6hp-jhericurl: its browser (the same libcbe frames, Rockhopper 5.6.2-21)
        # aborted at the close with the first-use dialog open, as in the suite's run; with both
        # first-use dialogs answered (the error-report consent, Block Ads) the close left no report.
        targets=("k6lp-jhericurl", "k6hp-jhericurl"),
        role="browser",
        lane="graphics",
        signal="6",
        frames=(
            "__cxa_pure_virtual+0x12",
            "_ZN7Browser12CloseBrowserEb+0x1be",
            "_ZN7Browser33OnFirstUseErrorReportDialogClosedEbRKNSt7__cxx1112basic_stringItN4base18"
            "string16_internals20string16_char_traitsESaItEEE+0x6e",
        ),
        why="W20L's browser calls a pure virtual when closed with its first-use dialog open "
        "(Browser::CloseBrowser -> OnFirstUseErrorReportDialogClosed; LG's, not on K5 or K7)",
    ),
    dict(
        targets=("k5lp-goldilocks2",),
        role="browser",
        lane="headless",
        signal="11",
        top="_ZN7display7DisplayC2ERKS0_+0x3",
        frames=("_ZN5views26DesktopFactoryOzoneWayland19CreateDesktopScreenEv+0x82",),
        why='no compositor: Ozone\'s FATAL "failed to initialize display hardware" does not end the '
        "release build, which copies a display that is not there",
    ),
    dict(
        targets=("k6lp-jhericurl", "k6hp-jhericurl"),
        role="browser",
        lane="headless",
        signal="11",
        top="_ZNSt6vectorISt4pairIN2ui7DomCodeEjESaIS3_EE13_M_insert_auxIS3_EEvN9__gnu_cxx17"
        "__normal_iteratorIPS3_S5_EEOT_+0x11a4d",
        why='no compositor: Ozone\'s FATAL "failed to initialize display hardware" does not end the '
        "release build, which copies a display that is not there (libcbe+0x1f876b6, from NULL)",
    ),
    dict(
        targets=("k7lp-kisscurl",),
        role="browser",
        lane="headless",
        signal="11",
        top="_ZNSt6vectorIN2gl16GLImplementationESaIS1_EE17_M_realloc_insertIJS1_EEEvN9__gnu_cxx17"
        "__normal_iteratorIPS1_S3_EEDpOT_+0xf19",
        why='no compositor: Ozone\'s FATAL "failed to initialize display hardware" does not end the '
        "release build, which copies a display that is not there (libcbe+0x2896aba, from NULL)",
    ),
)


def expected_crash(target, role, lane, sig):
    """The EXPECTED_CRASHES entry a report's signature (crash_sig) matches, or None."""
    if not sig:
        return None
    signal, frames = sig
    for e in EXPECTED_CRASHES:
        if target not in e["targets"]:
            continue
        if (e["role"], e["lane"], e["signal"]) != (role, lane, signal):
            continue
        if "top" in e and (not frames or frames[0] != e["top"]):
            continue
        if all(f in frames for f in e.get("frames", ())):
            return e
    return None


def app_processes(row):
    """The process names a crash report of the app would carry: its executable's (sam's main, or
    the binary its launcher script execs: the expected table's process), the runtime's for web
    (WebAppMgr) and QML (qml-runner) apps, and the compositor's."""
    names = {"surface-manager"}
    t, main = row.get("type"), row.get("main") or ""
    if t == "web":
        names.add("WebAppMgr")
    elif t == "qml":
        names.add("qml-runner")
    elif row.get("process"):
        names.add(row["process"]["name"])
    elif main:
        names.add(os.path.basename(main.replace("file://", "")))
    return names


def crashed(row, new):
    """The crash reports of new that are the app's (or the compositor's): process names compared
    on their first 15 characters, as the kernel keeps them."""
    mine = {n[:15] for n in app_processes(row)}
    return sorted(c for c in new if c.rsplit(".", 1)[0][:15] in mine)


def app_crashes(row, role, lane, target, facts, before=None):
    """([report...], {report: EXPECTED_CRASHES entry}) of the app's crash reports (crashed) that
    came after before (default: the launch's, facts[0]): those not expected, and the expected ones."""
    seen = set().union(*(crashes(x) for x in facts))
    new = seen - (crashes(facts[0]) if before is None else set(before))
    mine, known = [], {}
    for c in crashed(row, new):
        sig = next((s for s in (crash_sig(x, c) for x in reversed(facts)) if s), None)
        e = expected_crash(target, role, lane, sig)
        if e:
            known[c] = e
        else:
            mine.append(c)
    return mine, known


def known_note(known):
    """The PASS reason's note of expected crash reports."""
    return "".join(f"; expected crash report {c}: {e['why']}" for c, e in sorted(known.items()))


def shot_ok(img):
    """(ok, why) of a screenshot's image_facts: not black."""
    if not img or "error" in img:
        return False, f"no screenshot ({(img or {}).get('error', 'none')})"
    if img["nonblack"] < NONBLACK_MIN:
        return (
            False,
            f"the screenshot is black ({img['nonblack'] * 100:.1f} % of pixels over {NONBLACK_LEVEL})",
        )
    return (
        True,
        f"screenshot {img['size'][0]}x{img['size'][1]}, {img['nonblack'] * 100:.0f} % not black",
    )


def judge_app(role, row, launch, after, close, img=None, lane="graphics", before=None, target=None):
    """(app.ROLE, PASS/FAIL, why) of one app: known to sam before the launch (getAppInfo; else
    "app not present", a FAIL: the table has it in the firmware), launched (in front, graphics; running, headless),
    still so after the remote's keys (graphics) or a pause (headless) in the same process (no crash
    or respawn: its pid, WebAppMgr's for a web app, the compositor's), its screenshot not black
    (graphics), and gone after closeByAppId (the launcher: HOME again hides it); no crash report
    of its process from before the launch (before: the crash reports then; default the launch's)
    to the close, but one of LG's EXPECTED_CRASHES of the target (headless too, where the app cannot
    run)."""
    terms = terms_first(row, launch)
    if terms:
        # the terms screen came up in the app's place: it is what is in front, gets the keys and
        # is closed (tv_app follows it), a web app of WebAppMgr's
        row = dict(
            row,
            id=terms,
            type="web",
            main=None,
            process=None,
            eula=dict(row["eula"], app=row.get("id")),
        )
    check, app_id, kind = f"app.{role}", row.get("id"), row.get("kind", "app")
    if kind == "app" and not app_id:
        return (
            check,
            "FAIL",
            "not in the target's tests/regress/tv-expect file: regenerate it (tv-expect-gen.py)",
        )
    name = app_id or "StarfishLauncher"
    L, A, C = parse(launch), parse(after), parse(close)
    bad = []
    if "TV FACTS END" not in launch:
        return check, "FAIL", f"{name}: the launch's facts are incomplete"
    if kind == "app" and L.get("present") == "no":
        # the table has it in the firmware (an absent role is a SKIP and never launched), but sam
        # does not know it at run time (not installed, or filtered by region or model)
        return (
            check,
            "FAIL",
            (
                f"{name}: app not present: sam's getAppInfo {L.get('appinfo', '')[:120] or 'no answer'}, "
                f"where the expected table has it in the firmware ({row.get('source', '')})"
            ),
        )
    if lane == "headless" and not (row.get("headless") or {}).get("runs", True):
        # it cannot stay up headless: after the look it must be gone (a process that started and
        # ended, or none), and sam still answering
        why, last = row["headless"]["source"], A if "TV FACTS END" in after else L
        if answer(last.get("running", "")).get("returnValue") is not True:
            return (
                check,
                "FAIL",
                f"{name}: sam's running list did not answer after the launch: {last.get('running', '')[:120]}",
            )
        e = running_entry(last.get("running"), app_id)
        mine, known = app_crashes(row, role, lane, target, (L, A, C), before)
        if mine:
            return (
                check,
                "FAIL",
                f"{name}: crash reports {', '.join(mine)} (not LG's expected ones of the target)",
            )
        if not e:
            return (
                check,
                "PASS",
                (
                    f"{name}: launch {L.get('launch', '')[:60] or 'no answer'}; not running headless, "
                    f"absent by design: {why}" + known_note(known)
                ),
            )
        return (
            check,
            "FAIL",
            (
                f"{name}: still running headless (pid {e.get('processid')}), which the expected table "
                f"says it cannot ({why}): correct the table"
            ),
        )
    if kind == "app" and answer(L.get("launch", "")).get("returnValue") is not True:
        return (
            check,
            "FAIL",
            f"{name}: launch refused: {error_of(answer(L.get('launch', '')), L.get('launch', ''))}",
        )
    if lane == "headless":
        e1, e2 = running_entry(L.get("running"), app_id), running_entry(A.get("running"), app_id)
        if not e1:
            return check, "FAIL", f"{name}: not running {L.get('front_wait')} s after the launch"
        if not e2:
            bad.append("gone from sam's running list after the pause (crashed)")
        elif str(e1.get("processid")) != str(e2.get("processid")):
            bad.append(f"respawned (pid {e1.get('processid')} -> {e2.get('processid')})")
    else:
        if L.get("front_wait") == "none" or not in_front(L, app_id, kind):
            return (
                check,
                "FAIL",
                (
                    f"{name}: not in front after the launch: sam {L.get('sam_fg', '')[:120]}, "
                    f"compositor {L.get('lsm_fg', '')[:120] or L.get('lsm_state', '')[:120]}"
                ),
            )
        if "TV FACTS END" not in after:
            bad.append("no facts after the keys")
        elif not in_front(A, app_id, kind):
            bad.append(f"not in front after the keys: sam {A.get('sam_fg', '')[:120]}")
        if kind == "app" and "TV FACTS END" in after:
            e1, e2 = (
                running_entry(L.get("running"), app_id),
                running_entry(A.get("running"), app_id),
            )
            if not e2:
                bad.append("gone from sam's running list after the keys (crashed)")
            elif e1 and str(e1.get("processid")) != str(e2.get("processid")):
                bad.append(f"respawned (pid {e1.get('processid')} -> {e2.get('processid')})")
            if row.get("type") == "web" and L.get("pid.wam") != A.get("pid.wam"):
                bad.append(f"WebAppMgr restarted ({L.get('pid.wam')} -> {A.get('pid.wam')})")
        if L.get("pid.lsm") != A.get("pid.lsm"):
            bad.append(f"the compositor restarted ({L.get('pid.lsm')} -> {A.get('pid.lsm')})")
        ok, why = shot_ok(img)
        if not ok:
            bad.append(why)
    if kind == "key":
        if "TV FACTS END" not in close or in_front(C, None, kind):
            bad.append("still shown after HOME again")
    elif row.get("stays"):
        # the Home of webOS 4.5 on answers closeByAppId and stays up (the expected table's "stays")
        e0, e1 = answer(C.get("close_before", "")), running_entry(C.get("running"), app_id)
        if "TV FACTS END" not in close or answer(C.get("close", "")).get("returnValue") is not True:
            bad.append(f"closeByAppId failed: {C.get('close', '')[:120]}")
        elif not e1:
            bad.append(
                "gone after closeByAppId, where the firmware keeps it up ("
                + row["stays"]["source"]
                + ")"
            )
        elif str(e0.get("processid")) != str(e1.get("processid")):
            bad.append(
                f"respawned at closeByAppId (pid {e0.get('processid')} -> {e1.get('processid')})"
            )
    elif "TV FACTS END" not in close or C.get("gone_wait") == "none":
        bad.append(
            f"still running in the same process after closeByAppId: {C.get('close', '')[:120]}"
        )
    seen = set().union(*(crashes(x) for x in (L, A, C)))
    new = seen - (crashes(L) if before is None else set(before))
    mine, known = app_crashes(row, role, lane, target, (L, A, C), before)
    if mine:
        bad.append(f"crash reports {', '.join(mine)}")
    others = sorted(new - set(mine) - set(known))
    if bad:
        return (
            check,
            "FAIL",
            f"{name}: "
            + "; ".join(bad)
            + (f" (other crash reports meanwhile: {', '.join(others)})" if others else ""),
        )
    if terms:
        return (
            check,
            "PASS",
            f"{row['eula'].get('app', name)}: the terms first, {name} ({row['eula']['source']}, "
            "not agreed on this set): "
            f"in front after {L.get('front_wait')} s, still after the keys, same process; "
            f"{shot_ok(img)[1]}; closed in {C.get('gone_wait')} s",
        )
    back = running_entry(C.get("running"), app_id) if kind == "app" else None
    how = (
        f"in front after {L.get('front_wait')} s, still after the keys, same process; "
        f"{shot_ok(img)[1]}; "
        if lane == "graphics"
        else f"running after {L.get('front_wait')} s, still after the pause, same pid; "
    )
    if row.get("stays"):
        return (
            check,
            "PASS",
            f"{name}: {how}stays up after closeByAppId, same pid ({row['stays']['source']})",
        )
    return (
        check,
        "PASS",
        f"{name}: {how}"
        + (
            "hidden by HOME"
            if kind == "key"
            else f"closed in {C.get('gone_wait')} s"
            + (f", back as pid {back.get('processid')} (the base app)" if back else "")
        )
        + (f" (other crash reports meanwhile: {', '.join(others)})" if others else "")
        + known_note(known),
    )


def judge_install(text, app_id=TEST_APP):
    """(app.install, PASS/FAIL, why): dev/install reached "installed" and sam knows the app."""
    f = parse(text)
    st = re.findall(r'"state": *"([^"]*)"', f.get("install", ""))
    info = answer(f.get("appinfo", ""))
    ok = "installed" in st and info.get("returnValue") is True
    return (
        "app.install",
        "PASS" if ok else "FAIL",
        f"dev/install {' -> '.join(dict.fromkeys(st)) or f.get('install', '')[:160]!s}"
        f" (developer mode {f.get('devmode_enabled', '?')}); getAppInfo "
        + ("has it" if info.get("returnValue") is True else error_of(info, f.get("appinfo", ""))),
    )


def judge_remove(text, app_id=TEST_APP):
    f = parse(text)
    st = re.findall(r'"state": *"([^"]*)"', f.get("remove", ""))
    info = answer(f.get("appinfo", ""))
    ok = "removed" in st and info.get("returnValue") is not True
    return (
        "app.remove",
        "PASS" if ok else "FAIL",
        f"dev/remove {' -> '.join(dict.fromkeys(st)) or f.get('remove', '')[:160]!s}; getAppInfo "
        + ("still has it" if info.get("returnValue") is True else "no longer has it"),
    )


def image_facts(path, pat=None):
    """What a screenshot shows, for the judges: its size, the share of pixels not black, and for
    each patch of the test app's pattern and for its background (the gaps between patches) the
    median colour and the share of samples within DRAW_TOL of each mode's colour."""
    from PIL import Image

    try:
        im = Image.open(path).convert("RGB")
    except OSError as e:
        return dict(error=str(e))
    pat = pat or pattern()
    W, H = im.size
    px = im.load()
    small = im.resize((160, 90))
    sp = list(small.getdata())
    nonblack = sum(1 for p in sp if max(p) > NONBLACK_LEVEL) / len(sp)

    def samples(rect, n=12):
        x, y, w, h = rect
        return [
            px[
                min(W - 1, int((x + w * (0.2 + 0.6 * i / (n - 1))) * W)),
                min(H - 1, int((y + h * (0.2 + 0.6 * j / (n - 1))) * H)),
            ]
            for i in range(n)
            for j in range(n)
        ]

    def near(p, c):
        return all(abs(p[k] - c[k]) <= DRAW_TOL for k in range(3))

    def median(ps):
        return [sorted(p[k] for p in ps)[len(ps) // 2] for k in range(3)]

    out = dict(size=[W, H], nonblack=round(nonblack, 4), patches={}, bg={})
    for p in pat["patches"]:
        ss = samples(p["rect"])
        out["patches"][p["name"]] = dict(
            median=median(ss), match=round(sum(near(q, p["rgb"]) for q in ss) / len(ss), 3)
        )
    # the background: the bands above, between and below the patch rows, and the columns between
    gaps = [
        (0.0, 0.0, 1.0, 0.1),
        (0.0, 0.43, 1.0, 0.14),
        (0.0, 0.9, 1.0, 0.1),
        (0.31, 0.12, 0.07, 0.76),
        (0.62, 0.12, 0.07, 0.76),
    ]
    bs = [q for g in gaps for q in samples(g, 8)]
    for mode, c in pat["modes"].items():
        out["bg"][mode] = round(sum(near(q, c) for q in bs) / len(bs), 3)
    out["bg_median"] = median(bs)
    every = [q for g in ((0, 0, 1, 1),) for q in samples(g, 16)]
    out["nowebgl"] = round(sum(near(q, pat["nowebgl"]) for q in every) / len(every), 3)
    return out


def judge_draw(mode, img, pat=None):
    """(draw.MODE, PASS/FAIL, why) of the test app's screenshot in mode (canvas, webgl): every
    patch and the mode's background where the pattern puts them."""
    check = f"draw.{mode}"
    if not img or "error" in img:
        return check, "FAIL", f"no screenshot ({(img or {}).get('error', 'none')})"
    pat = pat or pattern()
    if mode == "webgl" and img.get("nowebgl", 0) >= DRAW_MIN:
        return (
            check,
            "FAIL",
            "the page found no WebGL context (it filled the window with PATTERN.nowebgl)",
        )
    if img["nonblack"] < NONBLACK_MIN:
        return (
            check,
            "FAIL",
            f"the screen is black ({img['nonblack'] * 100:.1f} % of pixels over {NONBLACK_LEVEL})",
        )
    bad = [
        f"{n} median {v['median']} ({v['match'] * 100:.0f} % match)"
        for n, v in img["patches"].items()
        if v["match"] < DRAW_MIN
    ]
    bg = img["bg"].get(mode, 0)
    if bg < DRAW_MIN:
        bad.append(
            f"background median {img['bg_median']} ({bg * 100:.0f} % match {pat['modes'][mode]})"
        )
    if bad:
        return check, "FAIL", f"{img['size'][0]}x{img['size'][1]}: " + "; ".join(bad)
    return (
        check,
        "PASS",
        (
            f"{img['size'][0]}x{img['size'][1]}: {len(img['patches'])} patches and the background "
            f"within {DRAW_TOL} (worst patch {min(v['match'] for v in img['patches'].values()) * 100:.0f} %"
            f", background {bg * 100:.0f} %)"
        ),
    )


# --- audio ---

# The audio step's tones (tests/regress/audio/check-audio.py --make-tone, played in the guest): each phase its
# own frequency, so the recording tells them apart however the guest's timing falls; level -12 dBFS
AUDIO_PHASES = (("native", 1000), ("down", 750), ("muted", 1250), ("restored", 500))
AUDIO_SECONDS = 2.0
AUDIO_LEVEL = -12
WEBAUDIO_HZ = 660  # tvapp's {"mode":"audio"}: its WebAudio oscillator
# at the native phase's volume, through ALSA: aplay to its default (LG's asound.conf: the pulse
# plugin) and aplay to pcm_output's own PCM (hw:0,6, PulseAudio bypassed)
ALSA_TONES = (("aplay", 875), ("hw", 1125))
AUDIO_STEPS = 5  # volumeDown presses, then as many volumeUp
AUDIO_WINDOW = 2048  # frames per analysis window (46 ms at 44.1 kHz)
AUDIO_FLOOR = -90.0  # dBFS: a quieter window is silence
AUDIO_PURE = 0.8  # share of a window's energy in its tone, to count as that tone
AUDIO_MIN_DROP = 1.0  # dB: AUDIO_STEPS presses of volumeDown must lower the level by
AUDIO_RESTORE_TOL = 0.5  # dB: restored to the first level within


def _goertzel(x, k_hz, rate):
    w = 2 * math.pi * k_hz / rate
    c = 2 * math.cos(w)
    s1 = s2 = 0.0
    for v in x:
        s1, s2 = v + c * s1 - s2, s1
    return (s1 * s1 + s2 * s2 - c * s1 * s2) * 2 / len(x) ** 2


def tone_facts(path, freqs):
    """What a recording (QEMU's wavcapture, 44100 Hz s16 stereo) holds of each frequency: per
    AUDIO_WINDOW window, the loudest channel's level and which of freqs carries AUDIO_PURE of its
    energy; per frequency the seconds of windows it held and their median level (dBFS)."""
    import wave
    import struct

    try:
        with wave.open(path, "rb") as w:
            rate, ch, width, n = (
                w.getframerate(),
                w.getnchannels(),
                w.getsampwidth(),
                w.getnframes(),
            )
            data = w.readframes(n)
    except (OSError, EOFError, wave.Error) as e:
        return dict(error=f"{path}: {e}")
    if width != 2:
        return dict(error=f"{path}: {8 * width}-bit, not s16")
    n = len(data) // (2 * ch)
    s = struct.unpack(f"<{n * ch}h", data[: n * ch * 2])
    out = dict(
        rate=rate, channels=ch, seconds=round(n / rate, 3), tones={}, other=0.0, peak_dbfs=None
    )
    levels = {f: [] for f in freqs}
    other, peak = 0, -math.inf
    for i in range(0, n - AUDIO_WINDOW + 1, AUDIO_WINDOW):
        chans = [s[(i * ch) + c : (i + AUDIO_WINDOW) * ch : ch] for c in range(ch)]
        x = max(chans, key=lambda c: sum(v * v for v in c))
        ms = sum(v * v for v in x) / len(x)
        db = 10 * math.log10(ms) - 20 * math.log10(32768) if ms > 0 else -math.inf
        if db < AUDIO_FLOOR:
            continue
        peak = max(peak, db)
        powers = {f: _goertzel(x, f, rate) for f in freqs}
        f = max(powers, key=powers.get)
        if powers[f] >= AUDIO_PURE * ms:
            levels[f].append(db)
        else:
            other += 1
    for f, ls in levels.items():
        ls.sort()
        out["tones"][str(f)] = dict(
            seconds=round(len(ls) * AUDIO_WINDOW / rate, 3),
            dbfs=round(ls[len(ls) // 2], 2) if ls else None,
        )
    out["other"] = round(other * AUDIO_WINDOW / rate, 3)
    out["peak_dbfs"] = round(peak, 2) if peak > -math.inf else None
    return out


AUDIO_SERVICE = "luna://com.webos.audio"  # LG's Audio API (setMuted, volumeUp, volumeDown)
# webOS 5 on: audiod's master volume, which its root getVolume does not answer (the scenario's)
AUDIO_MASTER = "luna://com.webos.service.audio/master/getVolume"


def volume_of(answer_):
    """(volume, muted) of a getVolume answer: the root one's volume and muted, or master/getVolume's
    volumeStatus (volume, muteStatus)."""
    s = answer_.get("volumeStatus")
    if isinstance(s, dict):
        return s.get("volume"), s.get("muteStatus")
    return answer_.get("volume"), answer_.get("muted")


def _tone(tf, hz):
    t = (tf or {}).get("tones", {}).get(str(hz)) or {}
    return t.get("seconds") or 0.0, t.get("dbfs")


def _heard(tf, hz):
    """Whether a phase's tone is in the recording: at least half its length."""
    sec, db = _tone(tf, hz)
    return sec >= AUDIO_SECONDS / 2 and db is not None


def judge_audio(facts, web, row, lane="graphics"):
    """The audio step's checks. facts: (tv.sh audio's output, tone_facts of its recording), None
    where the firmware has no Audio API (row's api); web: (the test app's launch facts, tone_facts
    of the WebAudio recording), None headless. row: the expected table's audio."""
    items, phases = [], dict(AUDIO_PHASES)
    api = row.get("api") or {}
    checks = (
        "audio.native",
        "audio.readback",
        "audio.volumeDown",
        "audio.muted",
        "audio.restored",
        "audio.aplay",
        "audio.alsa-hw",
    )
    if facts is None:
        why = f"SKIP: {api.get('source', 'no Audio API')}"
        return [(c, "SKIP", why) for c in checks] + (
            [] if web is None else judge_webaudio(web, row)
        )
    text, tf = facts
    f = parse(text)
    if not tf or "error" in tf:
        err = (tf or {}).get("error", "no recording")
        return [(c, "FAIL", f"no recording: {err}") for c in checks] + (
            [] if web is None else judge_webaudio(web, row)
        )
    vols = {k: answer(f.get(f"vol.{k}")) for k in phases}

    def level(name):
        sec, db = _tone(tf, phases[name])
        return f"{db:.2f} dBFS for {sec:.2f} s" if db is not None else "not heard"

    def played(name):
        v = f.get(f"play.{name}", "")
        return v.endswith("rc=0"), v

    ok, how = played("native")
    if not ok:
        items.append(
            (
                "audio.native",
                "FAIL",
                f"paplay: {how[:160] or 'no answer'} (pulse {f.get('pulse') or 'not running'}, "
                f"sinks {f.get('sinks', '')[:80]})",
            )
        )
    elif not _heard(tf, phases["native"]):
        items.append(
            (
                "audio.native",
                "FAIL",
                f"{phases['native']} Hz {level('native')} in the recording "
                f"(pulse {f.get('pulse') or 'not running'}, default sink {f.get('default_sink')})",
            )
        )
    else:
        items.append(
            (
                "audio.native",
                "PASS",
                f"paplay's {phases['native']} Hz at {level('native')}, volume "
                f"{volume_of(vols['native'])[0]}",
            )
        )
    v = {k: volume_of(vols[k])[0] for k in phases}
    m = {k: volume_of(vols[k])[1] for k in phases}
    want = (
        isinstance(v["native"], int)
        and v["down"] == v["native"] - AUDIO_STEPS
        and m["muted"] is True
        and v["restored"] == v["native"]
        and m["restored"] is False
        and m["native"] is False
    )
    seq = ", ".join(f"{k} {v[k]}{' muted' if m[k] else ''}" for k in phases)
    if not all(vols.values()):
        bad = [k for k in phases if not vols[k]]
        items.append(
            (
                "audio.readback",
                "FAIL",
                f"getVolume did not answer ({', '.join(bad)}): {f.get('vol.' + bad[0], '')[:160]}",
            )
        )
    else:
        how = (
            "master/getVolume"
            if "/master/" in (row.get("readback") or {}).get("uri", "")
            else "getVolume"
        )
        items.append(
            (
                "audio.readback",
                "PASS" if want else "FAIL",
                f"{how}: {seq} (expected {AUDIO_STEPS} lower, then muted, then back unmuted)",
            )
        )
    setters = [k for k in f if re.fullmatch(r"(down|up)\.\d+|warm\.(down|up)|mute|unmute", k)]
    refused = [f"{k} {f[k][:80]}" for k in setters if answer(f[k]).get("returnValue") is not True]
    if refused:
        items.append(("audio.calls", "FAIL", "; ".join(refused)[:240]))
    n0, n1 = _tone(tf, phases["native"])[1], _tone(tf, phases["down"])[1]
    if n0 is None or not _heard(tf, phases["down"]):
        items.append(
            (
                "audio.volumeDown",
                "FAIL",
                f"native {level('native')}, after {AUDIO_STEPS} volumeDown {level('down')}",
            )
        )
    else:
        drop = n0 - n1
        items.append(
            (
                "audio.volumeDown",
                "PASS" if drop >= AUDIO_MIN_DROP else "FAIL",
                f"{AUDIO_STEPS} volumeDown ({v['native']} -> {v['down']}): {drop:.2f} dB lower "
                f"(at least {AUDIO_MIN_DROP})",
            )
        )
    sec, db = _tone(tf, phases["muted"])
    items.append(
        (
            "audio.muted",
            "PASS" if sec < 0.1 else "FAIL",
            f"muted: silence (no {phases['muted']} Hz above {AUDIO_FLOOR:g} dBFS)"
            if sec < 0.1
            else f"muted, the tone still plays: {db} dBFS for {sec:.2f} s",
        )
    )
    n3 = _tone(tf, phases["restored"])[1]
    if n0 is None or not _heard(tf, phases["restored"]):
        items.append(
            ("audio.restored", "FAIL", f"unmuted and {AUDIO_STEPS} volumeUp: {level('restored')}")
        )
    else:
        d = n3 - n0
        items.append(
            (
                "audio.restored",
                "PASS" if abs(d) <= AUDIO_RESTORE_TOL else "FAIL",
                f"unmuted and {AUDIO_STEPS} volumeUp: {d:+.2f} dB from the first tone (within "
                f"{AUDIO_RESTORE_TOL})",
            )
        )
    items += judge_alsa(f, tf, row, n0)
    if web is not None:
        items += judge_webaudio(web, row, n0)
    return items


def judge_alsa(f, tf, row, native):
    """audio.aplay: aplay's tone to ALSA's default (the pulse plugin, the route a browser's ALSA
    output takes) at the native tone's level (the same volume, within 2 * AUDIO_RESTORE_TOL);
    audio.alsa-hw: aplay's tone straight to pcm_output's hw PCM, heard (PulseAudio's volume model
    bypassed, so not compared). SKIP where row's aplay / alsa_hw has the route absent, citing it;
    FAIL where the row is missing."""
    hz = dict(ALSA_TONES)
    items = []
    for check, name, key in (("audio.aplay", "aplay", "aplay"), ("audio.alsa-hw", "hw", "alsa_hw")):
        exp = row.get(key)
        if not exp:
            items.append((check, "FAIL", f"the expected table's audio has no {key} row"))
            continue
        if exp.get("present") is not True:
            items.append((check, "SKIP", exp.get("source", f"no {key}")))
            continue
        v = f.get(f"play.{name}", "")
        sec, db = _tone(tf, hz[name])
        dev = "default" if name == "aplay" else exp.get("device")
        if not v.endswith("rc=0"):
            wait = (
                f", after {f['hw.suspend_wait']} s waiting for pcm_output's suspend"
                if name == "hw"
                else ""
            )
            items.append((check, "FAIL", f"aplay -D {dev}: {v[:160] or 'not run'}{wait}"))
        elif not _heard(tf, hz[name]):
            items.append(
                (
                    check,
                    "FAIL",
                    f"aplay -D {dev} returned 0, its {hz[name]} Hz "
                    f"{'not heard' if db is None else f'only {sec:.2f} s'} in the recording",
                )
            )
        elif name == "hw" or native is None:
            items.append(
                (check, "PASS", f"aplay -D {dev}: {hz[name]} Hz at {db:.2f} dBFS for {sec:.2f} s")
            )
        else:
            ok = abs(db - native) <= 2 * AUDIO_RESTORE_TOL
            items.append(
                (
                    check,
                    "PASS" if ok else "FAIL",
                    f"aplay -D {dev}: {hz[name]} Hz at {db:.2f} dBFS for {sec:.2f} s, {db - native:+.2f} dB "
                    f"from paplay's tone (within {2 * AUDIO_RESTORE_TOL:g})",
                )
            )
    return items


def judge_webaudio(web, row, native=None):
    """audio.webaudio: the test app's WebAudio oscillator in the recording, at the native tone's
    level plus the row's level_db (the same -12 dBFS peak at the same master volume, on a sink whose
    policy volume the firmware sets: W21P's pfeedback; within AUDIO_RESTORE_TOL * 2) where that was
    heard; or SKIP where the firmware's web engine has no WebAudio (row's webaudio, with
    its evidence)."""
    wa = row.get("webaudio") or {}
    if wa.get("present") is False:
        return [("audio.webaudio", "SKIP", wa.get("source", "no WebAudio"))]
    launch, tf = web
    if not tf or "error" in tf:
        return [("audio.webaudio", "FAIL", f"no recording: {(tf or {}).get('error', 'none')}")]
    sec, db = _tone(tf, WEBAUDIO_HZ)
    if launch and ("front_wait=none" in launch or "TV FACTS END" not in launch):
        return [
            ("audio.webaudio", "FAIL", f"the test app is not in front: {launch.strip()[-160:]}")
        ]
    if _heard(tf, WEBAUDIO_HZ):
        how = f"the test app's WebAudio {WEBAUDIO_HZ} Hz at {db:.2f} dBFS for {sec:.2f} s"
        if native is None:
            return [("audio.webaudio", "PASS", how)]
        # the stream's own PulseAudio volume where the firmware gives its stream type one (row's
        # webaudio level_db, with its evidence)
        want = wa.get("level_db", 0.0)
        ok = abs(db - native - want) <= 2 * AUDIO_RESTORE_TOL
        return [
            (
                "audio.webaudio",
                "PASS" if ok else "FAIL",
                f"{how}, {db - native:+.2f} dB from paplay's tone (expected {want:+.2f}, within "
                f"{2 * AUDIO_RESTORE_TOL:g})",
            )
        ]
    log = parse(launch or "").get("console", "")
    return [
        (
            "audio.webaudio",
            "FAIL",
            f"{WEBAUDIO_HZ} Hz for {sec:.2f} s in the recording (peak "
            f"{tf.get('peak_dbfs')} dBFS); the page logged: {log[:160] or 'nothing'}",
        )
    ]


# --- verdicts ---

# rtk.ko's stand-in secure-store TA (guest/kernel/modules/rtk/tee-ta.c): LG's sstore UUID, the same in
# W19P's, W20L's and W21P's libkadaptor
SSTORE_UUID = "a1bc890f-21dc-f54b-9745-d6f6a0e768f6"
TEEC_SUCCESS, ITEM_NOT_FOUND, NOT_SUPPORTED, MAC_INVALID = 0, 0xFFFF0008, 0xFFFF000A, 0xFFFF3071
TEEC_ORIGIN_TEE = 3
TEE_LIB = re.compile(r"/lib/lib[^/]*\.so[.0-9]*$")
# what tests/regress/tee/tee-probe passes (its constants)
PROBE = dict(
    clearkey=b"tee-probe clear key",
    key=b"tee-probe protected key",
    inputs=b"tee-probe inputs",
    header=(1, 12),
    hmac=b"tee-probe hmac data",
    hmac_p2=b"\x5a" * 32,
    hmac_sub=4,
    newdata=b"tee-probe new",
    newdata_value=7,
    image=4096,
)


def tee_plan(row, target):
    """(the guest path of the firmware's libteec, the TAs' UUIDs) from the target's expected row's tee
    (tv-expect-gen.py tee_of), or None where the firmware has no TEE client (the step SKIPs)."""
    import sys

    sys.path.insert(0, os.path.join(TOP, "host", "core"))
    from beanjar import targets  # noqa: E402

    tee = row.get("tee") or {}
    libs = [c for c in tee.get("clients", []) if TEE_LIB.search(c)]
    if not libs:
        return None
    pak, _, path = libs[0].partition(":")
    p = targets.by_name(target)
    pk = next((x for x in targets.paks(p) if x.file + ".pak" == pak), None)
    if pk is None:
        return None
    for low in targets.release(p).pak_lowers:
        if low.serial == pk.serial and path.startswith(f"/{low.subdir}/"):
            path = path[len(low.subdir) + 1 :]
    if pk.mount not in ("/", None):
        path = pk.mount.rstrip("/") + path
    return path, tee.get("tas", [])


def _words(v):
    """The numbers of a probe answer ("0x0 75", "0 75"), or [] if it is not one."""
    try:
        return [int(w, 0) for w in v.split()]
    except ValueError:
        return []


def _hex(v):
    try:
        return bytes.fromhex(v)
    except ValueError:
        return None


def judge_tee(text, row, huk=None):
    """The tee step's checks of tv.sh tee's facts (tee_plan's row; huk: the VM's identity/tee-huk,
    hex). Each answer is the one rtk.ko's stand-in gives by design, its outputs those
    tests/regress/lib/teeref.py makes from the HUK (so made from nothing but the VM's own key)."""
    import teeref

    tee = row.get("tee") or {}
    if not [c for c in tee.get("clients", []) if TEE_LIB.search(c)]:
        return [
            (
                "tee",
                "SKIP",
                tee.get("source") or "no tee row in the expected table (run tv-expect-gen.py)",
            )
        ]
    f = parse(text, r"[A-Za-z0-9_.-]+")  # open.UUID
    if tee.get("node") is False:
        return judge_tee_no_node(f, text, tee)
    items = []

    def check(name, ok, why_ok, why_bad):
        items.append((f"tee.{name}", "PASS" if ok else "FAIL", why_ok if ok else why_bad))

    def code(name, want, what):
        v = f.get(f"sstore.{name}")
        got = _words(v or "")
        check(
            name,
            bool(got) and got[0] == want,
            f"{what}: {want:#x}",
            f"{what}: wanted {want:#x}, got {v if v is not None else 'nothing'}",
        )
        return got

    check(
        "device",
        f.get("tee_dev") == "char",
        "/dev/tee0 is a character device (rtk.ko tee=1)",
        f"/dev/tee0: {f.get('tee_dev', 'not looked at')}",
    )
    if "lib" not in f or not f["lib"].startswith("/"):
        check(
            "client",
            False,
            "",
            f"tee-probe could not use the firmware's libteec: {f.get('lib', text.strip()[-200:])}",
        )
        return items
    if f.get("tee_dev") != "char":
        # the firmware has a TEE client and the VM no TEE: every answer after is the same failure
        # (the client's own TEEC_ERROR_GENERIC), so the device's FAIL stands for them
        return items
    v = _words(f.get(f"open.{SSTORE_UUID}", ""))
    check(
        "open.sstore",
        v[:1] == [TEEC_SUCCESS],
        "the secure-store TA's session opens",
        f"open of {SSTORE_UUID}: {f.get(f'open.{SSTORE_UUID}', 'no answer')}",
    )
    for ta in tee.get("tas", []):
        v = _words(f.get(f"open.{ta}", ""))
        check(
            f"open.{ta}",
            v == [ITEM_NOT_FOUND, TEEC_ORIGIN_TEE],
            "the firmware's TA has no stand-in: TEEC_ERROR_ITEM_NOT_FOUND from the TEE, by design (its "
            "clients take it as they took no TEE)",
            f"open: {f.get(f'open.{ta}', 'no answer')}",
        )
    key = bytes.fromhex(huk) if huk else None

    def derived(name, ok_fn, what):
        if key is None:
            check(name, False, "", "the VM's identity/tee-huk could not be read")
            return
        try:
            ok = ok_fn()
        except KeyError as e:
            check(name, False, "", f"{what}: tee-probe printed no {e.args[0]}")
            return
        except (TypeError, ValueError):
            ok = False
        check(name, ok, f"{what}, from the VM's own HUK", f"{what}: not what the VM's HUK gives")

    code("checkvalid", TEEC_SUCCESS, "CheckValid (0x200)")
    v = code("reencrypt", TEEC_SUCCESS, "ReEncrypt at init (0x4)")
    check(
        "reencrypt.roundtrip",
        f.get("sstore.reencrypt.roundtrip") == "ok",
        "ReEncrypt keeps the store",
        "ReEncrypt changed the store",
    )
    v = code("newdata", TEEC_SUCCESS, "MakeNewSecureData (0x800)")
    derived(
        "newdata.out",
        lambda: (
            _hex(f["sstore.newdata.out"])
            == teeref.new_data(key, PROBE["newdata_value"], PROBE["newdata"])
        ),
        "MakeNewSecureData's container",
    )
    for cmd in (0x1619, 0x400, 0x401, 0x10000):
        code(
            f"unsupported.{cmd:#x}",
            NOT_SUPPORTED,
            f"command {cmd:#x}, not modelled (RPMB, DRM key, MFi)",
        )
    for name, data, cmds in (
        ("clearkey", PROBE["clearkey"], "MakeClearKey 0x203, 0x206, 0x204"),
        ("key", PROBE["key"], "MakeKey 0x201, 0x205, 0x202"),
    ):
        if name == "key":
            v = code("key.probe", TEEC_SUCCESS, "MakeKey's length probe")
            check(
                "key.probe.size",
                v[1:] == [len(data) + teeref.BLOB_EXTRA],
                "the length probe gets the blob's size",
                f"length probe: {f.get('sstore.key.probe')}",
            )
        v = code(name, TEEC_SUCCESS, cmds.split(",")[0])
        check(
            f"{name}.size",
            v[1:] == [len(data) + teeref.BLOB_EXTRA],
            "the blob is the data + 176 bytes, as LG's",
            f"size: {f.get('sstore.' + name)}",
        )
        derived(
            f"{name}.blob",
            lambda: teeref.unwrap(key, _hex(f[f"sstore.{name}.blob"])) == data,
            "the blob opens to the data",
        )
        v = code(f"{name}.len", TEEC_SUCCESS, "the length command")
        check(
            f"{name}.len.value",
            v[1:] == [len(data)],
            "the data's length",
            f"length: {f.get(f'sstore.{name}.len')}",
        )
        code(f"{name}.get", TEEC_SUCCESS, "the get command")
        check(
            f"{name}.roundtrip",
            f.get(f"sstore.{name}.roundtrip") == "ok",
            f"{cmds}: the data back",
            "not the data back",
        )
        code(f"{name}.altered", MAC_INVALID, "an altered blob")
    header = struct_header(PROBE["header"], len(PROBE["inputs"]))
    code("cipher.encrypt", TEEC_SUCCESS, "Encryption (0x2)")
    derived(
        "cipher.enc",
        lambda: (
            _hex(f["sstore.cipher.enc"])
            == teeref.cipher(key, header, PROBE["inputs"], _hex(f["sstore.cipher.data"]))
        ),
        "the ciphertext",
    )
    code("cipher.decrypt", TEEC_SUCCESS, "Decryption (0x1)")
    check(
        "cipher.roundtrip",
        f.get("sstore.cipher.roundtrip") == "ok",
        "0x2 then 0x1: the data back",
        "not the data back",
    )
    code("box.encrypt", TEEC_SUCCESS, "Encryption to a container (0x20)")
    derived(
        "box.enc",
        lambda: (
            teeref.box_open(key, header, PROBE["inputs"], _hex(f["sstore.box.enc"]))[1]
            == _hex(f["sstore.box.data"])[teeref.BOX_HEAD :]
        ),
        "the container",
    )
    code("box.decrypt", TEEC_SUCCESS, "Decryption of a container (0x40)")
    check(
        "box.roundtrip",
        f.get("sstore.box.roundtrip") == "ok",
        "0x20 then 0x40: the data back",
        "not the data back",
    )
    code("box.altered", MAC_INVALID, "an altered container")
    image = bytearray((i * 7 + 3) & 0xFF for i in range(PROBE["image"]))
    image[:2] = b"SE"
    code("storemac", TEEC_SUCCESS, "the store's MAC (0x8)")
    derived(
        "storemac.mac",
        lambda: _hex(f["sstore.storemac.mac"]) == teeref.store_mac(key, bytes(image)),
        "the store's MAC",
    )
    code("storecheck", TEEC_SUCCESS, "the store's MAC checked (0x10)")
    code("storecheck.altered", MAC_INVALID, "an altered store")
    code("hmac", TEEC_SUCCESS, "GetHMAC (0x80)")
    derived(
        "hmac.tag",
        lambda: (
            _hex(f["sstore.hmac.tag"])
            == teeref.hmac_of(key, PROBE["hmac_sub"], PROBE["hmac"], PROBE["hmac_p2"])
        ),
        "the HMAC",
    )
    code("hmaccheck", TEEC_SUCCESS, "VerifyHMAC (0x100)")
    code("hmaccheck.altered", MAC_INVALID, "VerifyHMAC of another tag")
    check(
        "probe",
        f.get("sstore.fails") == "0" and f.get("probe_sstore_rc") == "0",
        "tee-probe's round trips all held",
        f"tee-probe: fails {f.get('sstore.fails')}, exit {f.get('probe_sstore_rc')}",
    )
    crashed = f.get("tee_crashes", "").split()
    check(
        "crashes",
        "tee_crashes" in f and not crashed,
        "no crash report of a TEE client (TEEC_*, sstore) since the boot",
        f"crash reports of TEE clients: {' '.join(crashed) or 'not looked at'}",
    )
    return items


def judge_tee_no_node(f, text, tee):
    """The tee step where the VM is expected to have no TEE node (the expected table's tee.node is
    false: the chip has no module with the TEE, as MStar's): the firmware's TEE client has nothing to
    open, and must fail cleanly. PASS when /dev/tee0 is absent, the client's init answers
    TEEC_ERROR_ITEM_NOT_FOUND (what libkadaptor's MstarCustomerTEEInit logs) and no crash report names a
    TEE client; the secure-store TA's answers are Realtek's stand-in's and are not judged."""
    items = []

    def check(name, ok, why_ok, why_bad):
        items.append((f"tee.{name}", "PASS" if ok else "FAIL", why_ok if ok else why_bad))

    check(
        "device",
        f.get("tee_dev") not in (None, "char"),
        "no /dev/tee0, as expected: " + tee.get("node_source", "the VM has no TEE"),
        f"/dev/tee0: {f.get('tee_dev', 'not looked at')} (the expected table says the VM has none: "
        "regenerate it if a module now makes one)",
    )
    if "lib" not in f or not f["lib"].startswith("/"):
        check(
            "client",
            False,
            "",
            f"tee-probe could not use the firmware's libteec: {f.get('lib', text.strip()[-200:])}",
        )
        return items
    v = f.get(f"open.{SSTORE_UUID}", "")
    stage, _, code = v.partition(":")
    got = _words(code)
    check(
        "init",
        stage == "init" and got[:1] == [ITEM_NOT_FOUND],
        "the firmware's TEE init finds no device and fails cleanly: TEEC_ERROR_ITEM_NOT_FOUND",
        f"the firmware's TEE init: {v or 'no answer'} (wanted init:{ITEM_NOT_FOUND:#x})",
    )
    crashed = f.get("tee_crashes", "").split()
    check(
        "crashes",
        "tee_crashes" in f and not crashed,
        "no crash report of a TEE client (TEEC_*, sstore) since the boot",
        f"crash reports of TEE clients: {' '.join(crashed) or 'not looked at'}",
    )
    return items


# --- decode ---

# (case, path, the viewer's libavcodec decoder for its video, for its audio or None); the files and
# MIME types are tests/regress/video/make-clips.sh --decode's (media/cases.js). VP9 and AV1 with Opus in
# WebM are what YouTube's video on demand sends; libavcodec decodes AV1 with libdav1d.
DECODE_CASES = (
    ("file.h264", "file", "h264", "aac"),
    ("file.hevc", "file", "hevc", "aac"),
    ("file.vp9", "file", "vp9", None),
    ("file.mpeg2", "file", "mpeg2video", "aac"),
    ("file.vp9-opus", "file", "vp9", "opus"),
    ("file.av1", "file", "libdav1d", "opus"),
    ("mse.h264", "mse", "h264", "aac"),
    ("mse.hevc", "mse", "hevc", "aac"),
    ("mse.vp9", "mse", "vp9", None),
    ("mse.vp9-opus", "mse", "vp9", "opus"),
    ("mse.av1", "mse", "libdav1d", "opus"),
)
DECODE_CLIP = 20.0  # s of media in each clip (25 fps, 2 s GOPs): long enough to watch
DECODE_PAGE = 40  # s the page waits for "ended" before it logs its end anyway
DECODE_RECT = (
    320,
    180,
    960,
    540,
)  # the page's video element, scaled to 1920x1080 (1/6 in, half the page)
DECODE_SHOWN = 0.75  # share of the clip's pictures the viewer must show
DECODE_SPAN = 1.5  # s: shown pts and the page's currentTime reach the clip's end within
DECODE_AUDIO_DB = -40.0  # dBFS: the recorded media audio must be louder
DECODE_HEADLESS = (
    "headless: no web engine, compositor or viewer to play and show the clips (graphics only)"
)


def _ppm_region(path, rect):
    from PIL import Image

    im = Image.open(path).convert("RGB")
    w, h = im.size
    x, y, rw, rh = rect
    sx, sy = w / 1920, h / 1080
    box = (int(x * sx) + 2, int(y * sy) + 2, int((x + rw) * sx) - 2, int((y + rh) * sy) - 2)
    return list(im.crop(box).resize((64, 36)).getdata())


# The clip's picture (tests/regress/video/make-clips.sh --decode): testsrc2's six bars, top to bottom, and the
# case's label in a dark box near the bottom.
TESTSRC2_BARS = ("red", "green", "yellow", "blue", "magenta", "cyan")


def _bar_of(p):
    """The testsrc2 bar a pixel is in, by its channels (None: none of them)."""
    on = tuple(v > 150 for v in p)
    off = tuple(v < 100 for v in p)
    for name, want in zip(
        TESTSRC2_BARS, ((1, 0, 0), (0, 1, 0), (1, 1, 0), (0, 0, 1), (1, 0, 1), (0, 1, 1))
    ):
        if all(on[i] if want[i] else off[i] for i in range(3)):
            return name
    return None


def whole_picture(path, rect=DECODE_RECT):
    """None if the capture at path shows the whole clip picture filling rect (OSD pixels of
    1920x1080): the six bars in their order across its width, each edge of the rect inside the
    picture (no band of anything else), the label box at the bottom; else what is wrong."""
    from PIL import Image

    im = Image.open(path).convert("RGB")
    sx, sy = im.size[0] / 1920, im.size[1] / 1080
    x0, y0 = rect[0] * sx, rect[1] * sy
    w, h = rect[2] * sx, rect[3] * sy

    def at(fx, fy):
        return im.getpixel(
            (min(int(x0 + fx * w), im.size[0] - 1), min(int(y0 + fy * h), im.size[1] - 1))
        )

    def majority(points):
        names = [_bar_of(at(fx, fy)) for fx, fy in points]
        best = max(set(names), key=names.count)
        return best if names.count(best) * 2 > len(names) else None

    rows = [0.15 + i * 0.05 for i in range(12)]  # 0.15..0.70: above the label
    got = [
        majority(
            [(fx, fy) for fx in ((k + 0.35) / 6, (k + 0.5) / 6, (k + 0.65) / 6) for fy in rows]
        )
        for k in range(6)
    ]
    if got != list(TESTSRC2_BARS):
        return "bars " + ",".join(g or "?" for g in got)
    edges = {
        "left": [(0.015, fy) for fy in rows],
        "right": [(0.985, fy) for fy in rows],
        "top": [((k + 0.5) / 6, 0.02) for k in range(6)],
        "bottom": [((k + 0.5) / 6, 0.985) for k in range(6)],
    }
    for side, pts in edges.items():
        want = (
            [TESTSRC2_BARS[0]] * len(pts)
            if side == "left"
            else [TESTSRC2_BARS[5]] * len(pts)
            if side == "right"
            else list(TESTSRC2_BARS)
        )
        hits = sum(_bar_of(at(*pt)) == wnt for pt, wnt in zip(pts, want))
        if hits * 3 < len(pts) * 2:
            return f"{side} edge: {hits}/{len(pts)} of the picture"
    box = [at(0.3 + i * 0.4 / 40, fy) for i in range(41) for fy in (0.87, 0.89, 0.91, 0.93)]
    white = sum(min(p) > 190 for p in box) / len(box)
    dark = sum(max(p) < 130 for p in box) / len(box)
    if white < 0.02 or dark < 0.3:
        return f"no label box ({white:.0%} text, {dark:.0%} box)"
    return None


def media_facts(d, rect=DECODE_RECT):
    """On the VM host: what one decode case saw (d: viewer.log, media-helper.log, shot-*.ppm and
    media.wav): the helper decoders, each stream's last report (codec, packets, decoded, shown,
    the pts span shown, frames of audio), the end of stream per kind, caps it had no decoder for,
    the captures with a decoded picture at rect (colourful, not black, as the window is before a
    frame, not the decoding-off checkerboard),
    how many consecutive ones changed and how many show the whole clip picture filling rect
    (whole_picture; the last reason one did not), and the recorded audio's level."""
    import glob
    import struct

    out = dict(video=[], audio=[], decoders=[], no_decoder=[], eos=[], opens=[])
    try:
        with open(os.path.join(d, "viewer.log"), errors="replace") as f:
            log = f.read()
    except OSError as e:
        return dict(error=f"no viewer log: {e}")
    if not log.strip():
        return dict(error="the viewer logged nothing while the case played")
    try:
        with open(os.path.join(d, "media-helper.log"), errors="replace") as f:
            helper = f.read()
    except OSError as e:
        return dict(error=f"no media helper log: {e}")
    out["decoders"] = re.findall(r"media \d+: decoder (\S+) \((\w+)\)", helper)
    out["no_decoder"] = re.findall(r"media \d+: no decoder for (.*)", helper)[:4]
    out["eos"] = sorted(set(re.findall(r"media \d+: end of stream \((\w+)\)", helper)))
    out["opens"] = re.findall(r"media \d+: open (\w+) (\S+)", helper)
    out["caps"] = [c[:160] for c in re.findall(r"media \d+: caps \(\w+\) (.*)", helper)][:6]
    # one stream's presentation and decode reports join on (kind, port, session): a port is
    # reused by the next stream, of another codec too
    presentation = {}
    for m in re.finditer(
        r"present (\d+) video session=(\d+)[^:]*: received=\d+ shown=(\d+).*?pts=(-?[\d.]+)\.\.(-?[\d.]+) s",
        log,
    ):
        presentation[("video", m.group(1), m.group(2))] = dict(
            shown=int(m.group(3)), pts=[float(m.group(4)), float(m.group(5))]
        )
    for m in re.finditer(r"present (\d+) audio session=(\d+)[^:]*: frames=(\d+)", log):
        presentation[("audio", m.group(1), m.group(2))] = dict(frames=int(m.group(3)))
    last = {}
    for m in re.finditer(
        r"decode (\d+) (video|audio) session=(\d+)[^:]*: (\S+) (\S+) packets=(\d+) decoded=(\d+) dropped=\d+ errors=(\d+)",
        helper,
    ):
        port, kind, session, codec, mode = m.group(1, 2, 3, 4, 5)
        row = dict(
            codec=codec,
            mode=mode,
            packets=int(m.group(6)),
            decoded=int(m.group(7)),
            errors=int(m.group(8)),
        )
        row.update(dict(shown=0, pts=[-1, -1]) if kind == "video" else dict(frames=0))
        row.update(presentation.get((kind, port, session), {}))
        last[(kind[0], port, codec)] = row
    # the core's exit counters: windows received on video.sock and pictures drawn into them
    m = re.search(
        r"video_packets=(\d+) video_changes=\d+ video_draws=(\d+) video_pictures=(\d+)", log
    )
    out["window"] = (
        dict(packets=int(m.group(1)), draws=int(m.group(2)), pictures=int(m.group(3)))
        if m
        else None
    )
    out["video"] = [v for k, v in last.items() if k[0] == "v"]
    out["audio"] = [v for k, v in last.items() if k[0] == "a"]
    shots = sorted(glob.glob(os.path.join(d, "shot-*.ppm")))
    pics, prev, moving, whole, why = 0, None, 0, 0, None
    for p in shots:
        try:
            r = _ppm_region(p, rect)
        except (OSError, ValueError):
            continue
        n = len(r)
        colour = sum(1 for q in r if max(q) - min(q) > 80) / n
        black = sum(1 for q in r if max(q) < 12) / n
        pattern_ = (
            sum(
                1
                for q in r
                if any(
                    all(abs(q[i] - c[i]) < 6 for i in range(3))
                    for c in ((217, 217, 217), (31, 31, 102))
                )
            )
            / n
        )
        pic = colour > 0.2 and black < 0.9 and pattern_ < 0.05
        pics += pic
        if pic:
            try:
                wrong = whole_picture(p, rect)
            except (OSError, ValueError) as e:
                wrong = str(e)
            whole += wrong is None
            why = wrong or why
        if pic and prev is not None and r != prev:
            moving += 1
        prev = r if pic else None
    out.update(captures=len(shots), pictures=pics, moving=moving, whole=whole, whole_why=why)
    wav = os.path.join(d, "media.wav")
    out["wav_db"], out["wav_s"] = None, 0.0
    if os.path.exists(wav):
        with open(wav, "rb") as f:
            data = f.read()[44:]
        n = len(data) // 4
        x = struct.unpack("<%df" % n, data[: n * 4])
        loud = [v for v in x if v != 0.0]
        out["wav_s"] = round(len(loud) / 2 / 48000, 2)
        if loud:
            rms = math.sqrt(sum(v * v for v in loud) / len(loud))
            out["wav_db"] = round(20 * math.log10(rms), 1) if rms > 0 else None
    return out


def beacon_lines(served, token, case):
    """The page's lines of case, in its order, from the step's server log (tests/regress/http-token.py:
    REQUEST <ip> "GET /TOKEN/CASE/SEQ/LINE HTTP/1.1" 404 -), each line URL-encoded."""
    from urllib.parse import unquote

    got = {}
    pre = f"/{token}/{case}/"
    for m in re.finditer(r'"GET (\S+) HTTP', served or ""):
        path = m.group(1)
        if path.startswith(pre):
            seq, _, line = path[len(pre) :].partition("/")
            if seq.isdigit():
                got[int(seq)] = unquote(line)
    return [got[k] for k in sorted(got)]


def slice_file(src, start, dst):
    """The part of src (the run's viewer log, the media helper's, or its media WAV, all only appended
    to) from byte start on, into dst; a WAV gets a header of its own (48 kHz float stereo) and whole
    frames. A log the helper rotated since (src shorter than start: the marked one is src.1 now) is
    src.1's part from start, then all of src."""
    try:
        with open(src, "rb") as f:
            f.seek(0, os.SEEK_END)
            rotated = not src.endswith(".wav") and f.tell() < int(start)
            f.seek(0 if rotated else max(int(start), 44 if src.endswith(".wav") else 0))
            data = f.read()
    except (OSError, ValueError):
        data, rotated = b"", False
    if rotated:
        try:
            with open(src + ".1", "rb") as f:
                f.seek(int(start))
                data = f.read() + data
        except OSError:
            pass
    if dst.endswith(".wav"):
        import struct

        data = data[: len(data) // 8 * 8]
        data = (
            b"RIFF"
            + struct.pack("<I", 36 + len(data))
            + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 3, 2, 48000, 48000 * 8, 8, 32)
            + b"data"
            + struct.pack("<I", len(data))
            + data
        )
    with open(dst, "wb") as f:
        f.write(data)


def prune_captures(d, at_ms=10000):
    """Keeps of a case's captures (shot-<ms>.ppm) only sheet.png, the one about at_ms after the
    first with a decoded picture (else after the first): the run's contact sheet (decode_sheet)."""
    import glob
    from PIL import Image

    shots = sorted(glob.glob(os.path.join(d, "shot-*.ppm")))
    if not shots:
        return
    ms = [int(re.search(r"shot-(\d+)", os.path.basename(x)).group(1)) for x in shots]
    first = ms[0]
    for x, t in zip(shots, ms):
        try:
            r = _ppm_region(x, DECODE_RECT)
        except (OSError, ValueError):
            continue
        if sum(1 for q in r if max(q) - min(q) > 80) / len(r) > 0.2:
            first = t
            break
    pick = min(range(len(shots)), key=lambda i: abs(ms[i] - (first + at_ms)))
    try:
        Image.open(shots[pick]).convert("RGB").save(os.path.join(d, f"sheet-{ms[pick]:06d}.png"))
    except OSError:
        pass
    for x in shots:
        os.unlink(x)


def decode_sheet(run_dir, out=None, at_ms=10000):
    """build/regress/<run>/decode-sheet.png: per target and decode case, the capture about at_ms
    after its first one with a decoded picture (else after its first capture), labelled
    target/case, for looking at what played. Returns the path, or None without any capture."""
    import glob
    from PIL import Image, ImageDraw

    tiles = []
    for d in sorted(glob.glob(os.path.join(run_dir, "*", "decode", "*"))):
        if not os.path.isdir(d):
            continue
        kept = sorted(glob.glob(os.path.join(d, "sheet-*.png")))
        if kept:
            tiles.append(
                (
                    f"{d.split(os.sep)[-3]} {os.path.basename(d)} @"
                    f"{int(re.search(r'sheet-(\d+)', kept[0]).group(1)) / 1000:.0f}s",
                    kept[0],
                )
            )
            continue
        shots = sorted(glob.glob(os.path.join(d, "shot-*.p?m")))
        if not shots:
            continue
        ms = [int(re.search(r"shot-(\d+)", os.path.basename(x)).group(1)) for x in shots]
        first = ms[0]
        for x, t in zip(shots, ms):
            try:
                r = _ppm_region(x, DECODE_RECT)
            except (OSError, ValueError):
                continue
            if sum(1 for q in r if max(q) - min(q) > 80) / len(r) > 0.2:
                first = t
                break
        pick = min(range(len(shots)), key=lambda i: abs(ms[i] - (first + at_ms)))
        tiles.append(
            (f"{d.split(os.sep)[-3]} {os.path.basename(d)} @{ms[pick] / 1000:.0f}s", shots[pick])
        )
    if not tiles:
        return None
    w, h, cols = 480, 270, 4
    sheet = Image.new("RGB", (cols * w, ((len(tiles) + cols - 1) // cols) * (h + 24)), (16, 16, 16))
    draw = ImageDraw.Draw(sheet)
    for i, (label, path) in enumerate(tiles):
        x, y = (i % cols) * w, (i // cols) * (h + 24)
        try:
            sheet.paste(Image.open(path).convert("RGB").resize((w, h)), (x, y + 24))
        except OSError:
            pass
        draw.text((x + 6, y + 6), label, fill=(255, 255, 255))
    out = out or os.path.join(run_dir, "decode-sheet.png")
    sheet.save(out)
    return out


def decode_plan(row, lane):
    """[(case, path, vcodec, acodec, expected row)] of the decode step, and the SKIP items: headless
    every case; graphics those the target's decode row has absent (its evidence cited)."""
    run, items = [], []
    for case, path, vc, ac in DECODE_CASES:
        exp = (row or {}).get(case)
        if lane == "headless":
            items.append((f"decode.{case}", "SKIP", DECODE_HEADLESS))
        elif exp is None:
            run.append(
                (
                    case,
                    path,
                    vc,
                    ac,
                    dict(present=True, source="not in the expected table's decode row"),
                )
            )
        elif not exp.get("present"):
            items.append((f"decode.{case}", "SKIP", exp.get("source", "absent")))
        else:
            run.append((case, path, vc, ac, exp))
    return run, items


def page_facts(text):
    """The page's lines of a decode case (tv.sh decode-log): its events, the top currentTime, the
    end line, its MSE api and isTypeSupported/canPlayType answers, errors; the media stack's error
    lines; the crash reports."""
    lines = [ln.split("=", 1)[1] for ln in text.splitlines() if ln.startswith("page=")]
    out = dict(
        lines=len(lines),
        events=[],
        errors=[],
        top=0.0,
        end=None,
        api=None,
        supported={},
        canplay={},
        media=[ln.split("=", 1)[1] for ln in text.splitlines() if ln.startswith("media=")],
        sink=[ln.split("=", 1)[1] for ln in text.splitlines() if ln.startswith("sink=")],
    )
    for ln in lines:
        m = re.match(r"event (\w+) t=([\d.]+)(?: code=(\d+))?", ln)
        if m:
            out["events"].append(m.group(1) + (f"({m.group(3)})" if m.group(3) else ""))
            out["top"] = max(out["top"], float(m.group(2)))
            continue
        m = re.match(r"time t=([\d.]+)", ln)
        if m:
            out["top"] = max(out["top"], float(m.group(1)))
            continue
        m = re.match(r"end (\w+) top=([\d.]+)", ln)
        if m:
            out["end"] = m.group(1)
            out["top"] = max(out["top"], float(m.group(2)))
            continue
        m = re.match(r"mse api (\S+)", ln)
        if m:
            out["api"] = m.group(1)
            continue
        m = re.match(r'(isTypeSupported|canPlayType) "(.*)" = (.*)', ln)
        if m:
            out["supported" if m.group(1) == "isTypeSupported" else "canplay"][
                m.group(2).replace('\\"', '"')
            ] = m.group(3).strip('"')
            continue
        if ln.startswith("error"):
            out["errors"].append(ln[:200])
    f = parse(text)
    out["crashes"] = f.get("crashes", "").split()
    return out


def judge_decode(case, path, vcodec, acodec, exp, page, media, crashes_before=None):
    """(decode.CASE, PASS/FAIL, why) from what really happened: the page (playing, currentTime to
    the clip's end, ended unless the case's row says the firmware never ends it, no error), the viewer (the decoder of the case's codec, DECODE_SHOWN of
    the clip's pictures shown with their pts reaching its end, the video's end of stream, the audio
    decoded and recorded above DECODE_AUDIO_DB) and its captures (decoded pictures at the window,
    changing)."""
    check = f"decode.{case}"
    if not media or "error" in media:
        return check, "FAIL", f"no viewer facts ({(media or {}).get('error', 'none')})"
    bad, good = [], []
    ev = page.get("events", [])
    frames = DECODE_CLIP * 25
    if not page.get("lines"):
        bad.append("the page logged nothing")
    if path == "mse" and page.get("api") in (None, "none"):
        bad.append(f"no MSE api in the engine ({page.get('api')})")
    for e in page.get("errors", [])[:2]:
        bad.append(f"page {e}")
    if any(e.startswith("error") for e in ev):
        bad.append("media error " + ",".join(e for e in ev if e.startswith("error")))
    if "playing" not in ev:
        bad.append("no playing event")
    if page.get("top", 0) < DECODE_CLIP - DECODE_SPAN:
        bad.append(f"currentTime reached {page.get('top', 0):.1f} s of {DECODE_CLIP:.0f}")
    # a stream the firmware's engine never ends (the expected table's "ended": W16R's TvRenderer without
    # an audio stream): it plays to its end, and neither "ended" nor the end of stream comes
    no_end = (exp or {}).get("ended", {}).get("expected") is False
    if "ended" not in ev and not no_end:
        bad.append("no ended event")
    vids = [v for v in media.get("video", []) if v["codec"] == vcodec]
    v = max(vids, key=lambda x: x["shown"]) if vids else None
    if not v:
        got = ", ".join(f"{x['codec']}" for x in media.get("video", [])) or "none"
        caps = "; ".join(c[:80] for c in media.get("caps", [])) or "no stream reached the host"
        bad.append(f"no {vcodec} video decoded on the host (video streams: {got}; caps: {caps})")
    else:
        span = v["pts"][1] - v["pts"][0] if v["pts"][0] >= 0 else 0
        if v["shown"] < DECODE_SHOWN * frames:
            bad.append(f"{v['shown']} of {frames:.0f} pictures shown")
        if span < DECODE_CLIP - DECODE_SPAN - 1 / 25:
            bad.append(f"shown pts span {span:.2f} s")
        if v["errors"]:
            bad.append(f"{v['errors']} decode errors")
        good.append(
            f"{vcodec} {v['mode']} {v['shown']}/{v['decoded']} shown/decoded, pts {v['pts'][0]:.2f}..{v['pts'][1]:.2f} s"
        )
        if "video" not in media.get("eos", []) and not no_end:
            bad.append("no video end of stream at the host")
    win = media.get("window")
    if win is not None and (not win["packets"] or not win["pictures"]):
        bad.append(
            f"the viewer got {win['packets']} video window message(s) and drew {win['pictures']} picture(s) "
            f"(video.sock: another client?)"
        )
    if media.get("pictures", 0) < 3 or media.get("moving", 0) < 2:
        bad.append(
            f"captures: {media.get('pictures', 0)} of {media.get('captures', 0)} with a decoded picture at "
            f"the window, {media.get('moving', 0)} changed"
        )
    else:
        good.append(
            f"{media['pictures']}/{media['captures']} captures with a picture, {media['moving']} changed"
        )
    if media.get("whole", 0) < 3:
        bad.append(
            f"captures: {media.get('whole', 0)} with the whole picture filling the video rect "
            f"({media.get('whole_why') or 'no picture'})"
        )
    else:
        good.append(f"{media['whole']} whole")
    if acodec:
        auds = [a for a in media.get("audio", []) if a["codec"] == acodec]
        a = max(auds, key=lambda x: x["frames"]) if auds else None
        if not a:
            bad.append(
                f"no {acodec} audio decoded on the host (audio streams: "
                f"{', '.join(x['codec'] for x in media.get('audio', [])) or 'none'})"
            )
        elif a["frames"] < (DECODE_CLIP - 2) * 48000:
            bad.append(f"{a['frames'] / 48000:.1f} s of {acodec} audio played")
        db = media.get("wav_db")
        if db is None or db < DECODE_AUDIO_DB:
            bad.append(f"recorded media audio {db if db is not None else '-inf'} dBFS")
        elif a:
            good.append(f"{acodec} {a['frames'] / 48000:.1f} s, {db} dBFS")
    new = [c for c in page.get("crashes", []) if c not in (crashes_before or [])]
    if new:
        bad.append(f"crash reports: {' '.join(new)}")
    if bad:
        extra = "; ".join(page.get("media", [])[-2:])
        return check, "FAIL", "; ".join(bad) + (f" | media log: {extra[:300]}" if extra else "")
    if no_end:
        good.append(f"no ended by the firmware ({exp['ended'].get('source', '')[:200]})")
    return (
        check,
        "PASS",
        "; ".join(good) + f"; page {','.join(dict.fromkeys(ev))} to {page['top']:.1f} s",
    )


SMP_APP = "com.example.smpcheck"  # tests/regress/smp (smpcheck.c, app/), packed by tests/regress/smp/pack.sh
SMP_FPS = 30  # the clip pack.sh makes: 8 s of 720p30 H.264
SMP_READY_PER_S = 2  # FRAMEREADY callbacks a second of playing, at least (libpf polls)
SMP_SPAN = 0.5  # s: the last FRAMEREADY within this of the clip's last frame
SMP_HEADLESS = "headless: no viewer of the step's own"
SMP_SOURCE = os.path.join(REGRESS, "smp", "smpcheck.c")


def smp_needs(src=SMP_SOURCE):
    """(library, [(symbol, alternatives...)]) of the SMP check app: the StarfishMediaAPIs library
    smpcheck.c dlopens and the symbols its SYM() lines look up there (any one of each line's)."""
    with open(src) as f:
        text = f.read()
    lib = re.search(r'void \*h = dlopen\("([^"]+)"', text).group(1)
    syms = [
        tuple(re.findall(r'"([^"]+)"', m))
        for m in re.findall(r"^\s*SYM\(\w+,\s*(.*?)\);", text, re.M)
    ]
    return lib, syms


def smp_missing(defined, needs):
    """The first name of each of needs (smp_needs) none of whose names is in defined."""
    return [alts[0] for alts in needs if not any(s in defined for s in alts)]


def smp_plan(expect):
    """[] to run the smp step, else its SKIP item: the expected table's smp row (tv-expect-gen.py
    smp_of) says the firmware's StarfishMediaAPIs library lacks what the app looks up."""
    row = (expect or {}).get("smp") or {}
    if row.get("present") is False:
        return [
            (
                "smp.frameready",
                "SKIP",
                f"no StarfishMediaAPIs as the app calls it: {row.get('source', '')}",
            )
        ]
    return []


def judge_smp(text, media):
    """(smp.frameready, PASS/FAIL/SKIP, why) of the SMP check app's run (tv.sh smp): loaded
    (LOADCOMPLETED), FRAMEREADY (type 0) callbacks at SMP_READY_PER_S or more while it played, never
    going back, the last one at the clip's last frame (ns from libpf's CustomPipeline, ms from the
    base Pipeline's timer), the viewer showed DECODE_SHOWN of the clip's frames in a window, and no
    crash report came while it ran."""
    check = "smp.frameready"
    f = parse(text)
    lines = [ln[len("smp=") :] for ln in text.splitlines() if ln.startswith("smp=")]
    nolib = next((ln for ln in lines if ln.startswith("no library")), None)
    if nolib:
        return check, "SKIP", f"the firmware's StarfishMediaAPIs: {nolib[:160]}"
    summary = next((ln for ln in reversed(lines) if ln.startswith("frames=")), None)
    if not summary:
        why = next((ln for ln in lines if ln.startswith("no ")), None) or (
            lines[-1] if lines else "nothing logged"
        )
        return check, "FAIL", f"no summary after {f.get('wait', '?')} s: {why[:200]}"
    sm = dict(kv.split("=", 1) for kv in summary.split() if "=" in kv)
    frames, fed, ready = int(sm["frames"]), int(sm["fed_ms"]) / 1000, int(sm["frameready"])
    first, last, back = int(sm["first"]), int(sm["last"]), int(sm["backwards"])
    bad = []
    if not any(re.search(r"cb type=22\b", ln) for ln in lines):
        bad.append("no LOADCOMPLETED")
    unit = 1e9 if max(abs(first), abs(last)) > 1e6 else 1e3
    end = (frames - 1) / SMP_FPS
    if ready < SMP_READY_PER_S * fed:
        bad.append(f"{ready} FRAMEREADY in {fed:.1f} s of feeding")
    if back:
        bad.append(f"FRAMEREADY went back {back} times")
    if ready and last / unit < end - SMP_SPAN:
        bad.append(f"the last FRAMEREADY at {last / unit:.2f} s of {end:.2f}")
    v = None
    if not media or "error" in media:
        bad.append(f"no viewer facts ({(media or {}).get('error', 'none')})")
    else:
        vids = [x for x in media.get("video", []) if x["codec"] == "h264"]
        v = max(vids, key=lambda x: x["decoded"]) if vids else None
        if not v:
            bad.append("no H.264 decoded on the host")
        elif v["shown"] < DECODE_SHOWN * frames:
            bad.append(
                f"{v['shown']} of {frames} frames shown on the host ({v['decoded']} decoded)"
            )
    others = sorted(
        {
            m.group(1)
            for ln in lines
            for m in [re.search(r"cb type=(\d+)", ln)]
            if m and m.group(1) != "0"
        }
    )
    before = set(f.get("crashes_before", "").split())
    crashes = [c for c in f.get("crashes", "").split() if c not in before]  # this run's
    if crashes:
        bad.append(f"crash reports: {' '.join(crashes)}")
    facts = (
        f"{ready} FRAMEREADY in {fed:.1f} s, {first / unit:.2f}..{last / unit:.2f} s "
        f"({'ns' if unit == 1e9 else 'ms'}), after the feed {sm.get('after_feed', '?')} other callback(s); "
        f"types seen {','.join(others) or 'none'}"
        + (f"; host h264 {v['decoded']} decoded, {v['shown']} shown" if v else "")
    )
    return (check, "FAIL", "; ".join(bad) + " | " + facts) if bad else (check, "PASS", facts)


def struct_header(words, inputs_len):
    """The probe's 20-byte Encryption/Decryption header: key id, mode, the inputs' length, 0, 0."""
    import struct

    return struct.pack("<5I", words[0], words[1], inputs_len, 0, 0)


def verdict(items):
    """FAIL if any check failed, else PASS (SKIPs do not fail)."""
    return "FAIL" if any(s == "FAIL" for _, s, _ in items) else "PASS"


def counts(items):
    """'PASS/FAIL/SKIP' counts."""
    return "/".join(str(sum(1 for _, s, _ in items if s == v)) for v in ("PASS", "FAIL", "SKIP"))


def short(items):
    """The summary table's cell: the verdict and the counts (PASS/FAIL/SKIP)."""
    return f"{verdict(items)} {counts(items)}"


if __name__ == "__main__":
    # on the VM host: python3 tests/regress/lib/tvsuite.py image PNG -> image_facts as JSON
    import sys

    if len(sys.argv) == 3 and sys.argv[1] == "image":
        print(json.dumps(image_facts(sys.argv[2])))
    elif len(sys.argv) == 4 and sys.argv[1] == "tones":
        # python3 tests/regress/lib/tvsuite.py tones WAV HZ,HZ... -> tone_facts as JSON
        print(json.dumps(tone_facts(sys.argv[2], [int(x) for x in sys.argv[3].split(",")])))
    elif len(sys.argv) == 3 and sys.argv[1] == "sheet":
        # python3 tests/regress/lib/tvsuite.py sheet RUN_DIR -> RUN_DIR/decode-sheet.png
        print(decode_sheet(sys.argv[2]))
    elif len(sys.argv) in (3, 4) and sys.argv[1] == "media":
        # python3 tests/regress/lib/tvsuite.py media DIR [--prune] -> media_facts as JSON (a decode case's
        # viewer); --prune then keeps one capture (prune_captures)
        facts = media_facts(sys.argv[2])
        if sys.argv[3:] == ["--prune"]:
            prune_captures(sys.argv[2])
        print(json.dumps(facts))
    elif len(sys.argv) == 5 and sys.argv[1] == "slice":
        slice_file(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        sys.exit(
            "usage: tvsuite.py image PNG | tones WAV HZ,HZ... | media DIR [--prune] | slice FILE START OUT | "
            "sheet RUN_DIR"
        )
