#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The TV feature suite's judging (tests/regress/lib/tvsuite.py) and its expected table's generator
(tests/regress/tv-expect-gen.py) without a VM or a firmware: facts as tests/regress/tv.sh prints
them (`meson test -C build/host --suite regress`, or run directly)."""

import importlib.machinery
import importlib.util
import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REGRESS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "tests", "regress")
sys.path.insert(0, os.path.join(os.path.dirname(REGRESS), "..", "host", "core"))
sys.path.insert(0, os.path.join(REGRESS, "lib"))
import teeref  # noqa: E402
import tvsuite  # noqa: E402
from beanjar import targets  # noqa: E402

loader = importlib.machinery.SourceFileLoader(
    "tv_expect_gen", os.path.join(REGRESS, "tv-expect-gen.py")
)
gen = importlib.util.module_from_spec(importlib.util.spec_from_loader("tv_expect_gen", loader))
loader.exec_module(gen)

FW = "03.70.25.01-HE_DTV_W21P_AFADATAA"
NOW = 1791031888


def expect_table():
    """{target: its row} of the tracked expected table (tests/regress/tv-expect)."""
    return {t: tvsuite.load_expect(t) for t in tvsuite.expect_targets()}


def ctx(checks=None, broadcast="dvb"):
    """A W21P-like target: every check expected unless checks says otherwise."""
    present = {cid: dict(present=True, source="test") for cid in tvsuite.catalogue()}
    for m, keys in (("OSInfo", tvsuite.NYX_OS_INFO), ("DeviceInfo", tvsuite.NYX_DEVICE_INFO)):
        for k in keys:
            present[f"nyx.{m}.{k}"] = dict(present=True, source="test")
    present.update(checks or {})
    return dict(
        target="k7lp-kisscurl",
        fw=FW,
        model="43UP77009LB",
        serial="000RAXX00001",
        mac="a8:23:fe:12:34:56",
        chip="K7LPA0",
        ram=1536,
        broadcast=broadcast,
        now=NOW,
        expect=dict(
            release=dict(name="Rockhopper", version="6.6.0-6", codename="kisscurl-kolovesi"),
            online=dict(url="http://lgtvonline.lge.com/", dns_as_online=True),
            checks=present,
        ),
    )


def sysinfo(key, value):
    return f'sysinfo.{key}={{"{key}":"{value}","returnValue":true}}'


GOOD_SYSINFO = [
    sysinfo("boardType", "K7LP_DVB_EU"),
    sysinfo("firmwareVersion", "03.70.25"),
    sysinfo("modelName", "43UP77009LB"),
    sysinfo("sdkVersion", "6.6.0"),
    sysinfo("UHD", "true"),
    sysinfo("_3d", "false"),
]
UNKNOWN = '{"returnValue":false,"errorCode":-1,"errorText":"Unknown method \\"getAPIVersion\\" for category \\"/\\""}'


def facts(*lines):
    return "\n".join(["uname=6.18.53"] + list(lines) + ["TV FACTS END"])


def items_of(items):
    return {c: (s, why) for c, s, why in items}


class LunaTest(unittest.TestCase):
    def test_sysinfo_values(self):
        r = items_of(tvsuite.judge_luna(facts(*GOOD_SYSINFO), ctx()))
        for k in tvsuite.SYSINFO_KEYS:
            self.assertEqual(r[f"sysinfo.{k}"][0], "PASS", r[f"sysinfo.{k}"])
        bad = [
            sysinfo("boardType", "K7LP_ATSC_US"),
            sysinfo("firmwareVersion", "03.70.24"),
            sysinfo("modelName", "43UP77009LA"),
            sysinfo("sdkVersion", "5.6.1"),
            sysinfo("UHD", "false"),
            sysinfo("_3d", "maybe"),
        ]
        r = items_of(tvsuite.judge_luna(facts(*bad), ctx()))
        for k in tvsuite.SYSINFO_KEYS:
            self.assertEqual(r[f"sysinfo.{k}"][0], "FAIL", r[f"sysinfo.{k}"])
        self.assertIn("expected K7LP_DVB_", r["sysinfo.boardType"][1])

    def test_board_type_from_the_firmwares_soc_name(self):
        """webOS 5+: SoCChipType of tv.hw.json, not chip= (K8LPNN) without its revision."""
        c = ctx()
        c["chip"] = "K8LPNN"
        line = sysinfo("boardType", "K8LPN2_DVB_EU")
        r = items_of(tvsuite.judge_luna(facts(line, "soc_chip=K8LPN2"), c))
        self.assertEqual(r["sysinfo.boardType"][0], "PASS", r["sysinfo.boardType"])
        r = items_of(tvsuite.judge_luna(facts(line), c))
        self.assertEqual(r["sysinfo.boardType"][0], "FAIL")

    def test_ota_id_from_the_firmwares_own_files(self):
        """otaId and nyx's hardware_id are tv.sh's ota_id (W22O: AFABATAA though the collection says AFABATPU)."""
        c = ctx()
        c["fw"] = "04.40.93.01-HE_DTV_W22O_AFABATPU"
        ids = ("tvsp.getSystemInfo.otaId", "nyx.DeviceInfo.hardware_id")
        good = [
            'tvsp.getSystemInfo.otaId={"returnValue":true,"otaId":"HE_DTV_W22O_AFABATAA"}',
            "nyx.DeviceInfo.keys=hardware_id",
            "nyx.DeviceInfo.hardware_id=rc=0 HE_DTV_W22O_AFABATAA",
        ]
        r = items_of(tvsuite.judge_luna(facts("ota_id=HE_DTV_W22O_AFABATAA", *good), c))
        for i in ids:
            self.assertEqual(r[i][0], "PASS", r[i])
        # the collection's name no longer decides
        r = items_of(tvsuite.judge_luna(facts("ota_id=HE_DTV_W22O_AFABATPU", *good), c))
        for i in ids:
            self.assertEqual(r[i][0], "FAIL", r[i])
        self.assertIn("AFABATPU", r[ids[0]][1])
        # no fact, no expectation: not the name's
        r = items_of(tvsuite.judge_luna(facts(*good), c))
        for i in ids:
            self.assertEqual(r[i][0], "FAIL", r[i])
            self.assertIn("did not report", r[i][1])

    def test_sdk_version_is_the_release(self):
        """webOS 1 to 4 answer major.minor.0 (4.10.0 for 4.10.2-29): the release's major.minor."""
        c = ctx()
        c["expect"]["release"]["version"] = "4.10.2-29"
        r = items_of(tvsuite.judge_luna(facts(sysinfo("sdkVersion", "4.10.0")), c))
        self.assertEqual(r["sysinfo.sdkVersion"][0], "PASS")

    def test_skip_only_where_the_table_says_absent(self):
        absent = {
            "mrcu.getAPIVersion": dict(
                present=False, source="no string 'getAPIVersion' in lginput2"
            )
        }
        text = facts(f"mrcu.getAPIVersion={UNKNOWN}")
        r = items_of(tvsuite.judge_luna(text, ctx(absent)))
        self.assertEqual(
            r["mrcu.getAPIVersion"], ("SKIP", "absent: no string 'getAPIVersion' in lginput2")
        )
        r = items_of(tvsuite.judge_luna(text, ctx()))
        self.assertEqual(r["mrcu.getAPIVersion"][0], "FAIL")
        self.assertIn("Unknown method", r["mrcu.getAPIVersion"][1])
        # a check the table does not know fails: the table is out of date
        c = ctx()
        del c["expect"]["checks"]["mrcu.getAPIVersion"]
        r = items_of(tvsuite.judge_luna(text, c))
        self.assertEqual(r["mrcu.getAPIVersion"][0], "FAIL")
        self.assertNotIn("WARN", {s for _, s, _ in tvsuite.judge_luna(text, ctx())})

    def test_incomplete(self):
        self.assertEqual(tvsuite.judge_luna("sysinfo.UHD=x", ctx())[0][1], "FAIL")

    def test_uhd_from_the_boards_panel(self):
        """tvservicemock's releases: the table's value (the board's panel, hw-option-gen's), not
        the model name; a row whose panel could not be had fails."""
        c = ctx()
        c["model"] = "55QNED85JQA"  # no U after the inch digits, yet a UD panel
        c["expect"]["checks"]["sysinfo.UHD"] = dict(
            present=True, source="test", value="true", value_source="panelResolution UD"
        )
        r = items_of(tvsuite.judge_luna(facts(sysinfo("UHD", "true")), c))
        self.assertEqual(r["sysinfo.UHD"][0], "PASS")
        r = items_of(tvsuite.judge_luna(facts(sysinfo("UHD", "false")), c))
        self.assertEqual(
            r["sysinfo.UHD"], ("FAIL", "UHD 'false', expected true (panelResolution UD)")
        )
        c["expect"]["checks"]["sysinfo.UHD"].update(value=None, value_source="no panel")
        r = items_of(tvsuite.judge_luna(facts(sysinfo("UHD", "true")), c))
        self.assertEqual(r["sysinfo.UHD"][0], "FAIL")

    def test_settings_and_region(self):
        text = facts(
            'settings.option.country={"settings":{"country":"GBR"},"returnValue":true}',
            'settings.option.smartServiceCountryCode2={"settings":{"smartServiceCountryCode2":"EU7"},"returnValue":true}',
            'settings.caption.captionEnable={"settings":{"captionEnable":"off"},"returnValue":true}',
            'settings.localeInfo={"settings":{"localeInfo":{"locales":{"UI":"en-US","TV":"en-GB","FMT":"en-GB",'
            '"NLP":"en-GB","STT":"en-US","AUD":"en-GB","AUD2":"en-GB"},"clock":"locale","keyboards":["en"],'
            '"timezone":""}},"returnValue":true}',
        )
        r = items_of(tvsuite.judge_luna(text, ctx()))
        for c in (
            "settings.option.country",
            "settings.option.smartServiceCountryCode2",
            "settings.caption.captionEnable",
            "settings.localeInfo",
        ):
            self.assertEqual(r[c][0], "PASS", r[c])
        r = items_of(tvsuite.judge_luna(text, ctx(broadcast="arib")))
        self.assertEqual(r["settings.option.country"][0], "FAIL")
        self.assertEqual(r["settings.option.smartServiceCountryCode2"][0], "FAIL")
        r = items_of(
            tvsuite.judge_luna(
                facts(
                    'settings.localeInfo={"settings":{"localeInfo":{"locales":{"UI":"en-US"}}},'
                    '"returnValue":true}'
                ),
                ctx(),
            )
        )
        self.assertEqual(r["settings.localeInfo"][0], "FAIL")
        self.assertIn("locales.TV", r["settings.localeInfo"][1])

    def test_two_hubs(self):
        """webOS 1 and 2: the public hub's answer, else the private one's, named."""
        denied = '{"returnValue":false,"errorText ":"Access denied"}'
        good = '{"settings":{"captionEnable":"off"},"returnValue":true}'
        r = items_of(
            tvsuite.judge_luna(
                facts(
                    f"settings.caption.captionEnable={denied}",
                    f"settings.caption.captionEnable.prv={good}",
                ),
                ctx(),
            )
        )
        self.assertEqual(r["settings.caption.captionEnable"][0], "PASS")
        self.assertIn("private hub", r["settings.caption.captionEnable"][1])
        r = items_of(
            tvsuite.judge_luna(
                facts(
                    f"settings.caption.captionEnable={denied}",
                    f"settings.caption.captionEnable.prv={denied}",
                ),
                ctx(),
            )
        )
        self.assertEqual(r["settings.caption.captionEnable"][0], "FAIL")

    def test_global_firmware_takes_the_boards_broadcast(self):
        r = items_of(
            tvsuite.judge_luna(facts("nyx_system=DVB", *GOOD_SYSINFO), ctx(broadcast=None))
        )
        self.assertEqual(r["sysinfo.boardType"][0], "PASS")

    def test_getids_systime_connman(self):
        text = facts(
            'sm.getIDs={ "returnValue": true, "idList": [ { "idType": "LGUDID", '
            '"idValue": "11e6dfcf-9b2e-1154-78d5-1d015c63449e" } ] }',
            f'systime={{"utc":{NOW + 30},"localtime":{{}},"offset":0,"timezone":"Europe/London","TZ":"BST",'
            '"timeZoneFile":"/var/luna/preferences/localtime","returnValue":true}',
            'connman.webos={"returnValue":true,"isInternetConnectionAvailable":true,"wired":{"state":"connected"},'
            '"wifi":{"state":"disconnected"},"wifiDirect":{"state":"disconnected"}}',
            'connman.palm={"returnValue":true,"wired":{"state":"connected"}}',
        )
        r = items_of(tvsuite.judge_luna(text, ctx()))
        self.assertEqual(r["sm.getIDs"][0], "PASS")
        self.assertEqual(r["systime"][0], "PASS")
        self.assertEqual(r["connman.webos"][0], "PASS")
        self.assertEqual(r["connman.palm"][0], "FAIL")
        self.assertIn("isInternetConnectionAvailable", r["connman.palm"][1])
        # webOS 1 and 2: getSystemTime without returnValue; a clock far off fails
        r = items_of(
            tvsuite.judge_luna(
                facts(
                    f'systime={{ "utc": {NOW - 5000}, "localtime": {{}}, "offset": 60, '
                    '"timezone": "Europe/London", "TZ": "BST", "timeZoneFile": "x" }'
                ),
                ctx(),
            )
        )
        self.assertEqual(r["systime"][0], "FAIL")
        self.assertIn("no returnValue", r["systime"][1])

    def test_nyx(self):
        text = facts(
            "nyx.OSInfo.keys=core_os_release webos_release webos_prerelease core_os_kernel_version",
            "nyx.OSInfo.core_os_release=rc=0 6.6.0-6",
            "nyx.OSInfo.webos_release=rc=0 6.6.0",
            "nyx.OSInfo.webos_prerelease=rc=0 ",
            "nyx.OSInfo.core_os_kernel_version=rc=0 6.18.53",
            "nyx.DeviceInfo.keys=product_id wired_addr ram_size wifi_addr serial_number",
            "nyx.DeviceInfo.product_id=rc=0 43UP77009LB",
            "nyx.DeviceInfo.product_id.err=[OGERLOG] fail to open file",
            "nyx.DeviceInfo.wired_addr=rc=0 A8:23:FE:12:34:56",
            "nyx.DeviceInfo.ram_size=rc=0 1.5 GB",
            "nyx.DeviceInfo.serial_number=rc=0 000RAXX00002",
            "nyx.DeviceInfo.wifi_addr=rc=1 Error: Device or value not available",
        )
        c = ctx({"nyx.OSInfo.webos_build_id": dict(present=False, source="x")})
        r = items_of(tvsuite.judge_luna(text, c))
        for k in (
            "nyx.OSInfo.core_os_release",
            "nyx.OSInfo.webos_release",
            "nyx.OSInfo.webos_prerelease",
            "nyx.OSInfo.core_os_kernel_version",
            "nyx.DeviceInfo.product_id",
            "nyx.DeviceInfo.wired_addr",
            "nyx.DeviceInfo.ram_size",
        ):
            self.assertEqual(r[k][0], "PASS", (k, r[k]))
        self.assertEqual(r["nyx.DeviceInfo.serial_number"][0], "FAIL")
        self.assertEqual(
            r["nyx.DeviceInfo.wifi_addr"], ("FAIL", "Error: Device or value not available")
        )
        # the library's messages after the value fail the module's output check
        self.assertEqual(r["nyx.DeviceInfo.output"][0], "FAIL")
        self.assertIn("product_id: [OGERLOG]", r["nyx.DeviceInfo.output"][1])
        self.assertEqual(r["nyx.OSInfo.output"][0], "PASS")
        self.assertEqual(r["nyx.OSInfo.webos_build_id"][0], "SKIP")
        # expected, but nyx-cmd does not list it
        self.assertEqual(
            r["nyx.OSInfo.core_os_name"], ("FAIL", "nyx-cmd -h OSInfo does not list it")
        )

    def test_accepted_firmware_lines(self):
        """W16R: KADP's teardown lines, exactly as the table has them, pass; anything more fails."""
        acc = {
            "nyx.DeviceInfo.output": dict(
                present=True,
                source="libkadaptor .fini_array",
                accept=[
                    "==========================",
                    "[drvMMIO.c][MDrv_MMIO_Close][000532] Mutex taking timeout",
                    "Mutex Name: _MMIO_Mutex",
                ],
            )
        }
        base = [
            "nyx.DeviceInfo.keys=product_id",
            "nyx.DeviceInfo.product_id=rc=0 43UP77009LB",
            "nyx.DeviceInfo.product_id.more=========================== [drvMMIO.c][MDrv_MMIO_Close][000532] Mutex "
            "taking timeout",
            "nyx.DeviceInfo.product_id.err=Mutex Name: _MMIO_Mutex Mutex Name: _MMIO_Mutex",
        ]
        r = items_of(tvsuite.judge_luna(facts(*base), ctx(acc)))
        self.assertEqual(r["nyx.DeviceInfo.output"][0], "PASS")
        self.assertIn("libkadaptor .fini_array", r["nyx.DeviceInfo.output"][1])
        r = items_of(tvsuite.judge_luna(facts(*base[:3], base[3] + " [OGERLOG] fail"), ctx(acc)))
        self.assertEqual(r["nyx.DeviceInfo.output"][0], "FAIL")
        r = items_of(tvsuite.judge_luna(facts(*base), ctx()))
        self.assertEqual(r["nyx.DeviceInfo.output"][0], "FAIL")

    def test_wifi_bt_absent_by_design(self):
        design = {
            k: dict(present=False, design=True, source=tvsuite.DESIGN_REASON)
            for k in tvsuite.DESIGN_ABSENT
        }
        text = facts(
            "nyx.DeviceInfo.keys=wifi_addr bt_addr",
            "nyx.DeviceInfo.wifi_addr=rc=1 Error: Device or value not available",
            "nyx.DeviceInfo.bt_addr=rc=1 Error: Device or value not available",
            "nyx.DeviceInfo.bt_addr.err=sh: hcitool: not found",
        )
        r = items_of(tvsuite.judge_luna(text, ctx(design)))
        self.assertEqual(r["nyx.DeviceInfo.wifi_addr"][0], "PASS")
        self.assertIn("absent by design", r["nyx.DeviceInfo.wifi_addr"][1])
        self.assertEqual(r["nyx.DeviceInfo.bt_addr"][0], "PASS")
        self.assertEqual(r["nyx.DeviceInfo.output"][0], "PASS")  # hcitool's message is the design's
        r = items_of(
            tvsuite.judge_luna(
                text.replace("rc=1 Error: Device or value not available", "rc=0 a8:23:fe:00:00:01"),
                ctx(design),
            )
        )
        self.assertEqual(r["nyx.DeviceInfo.wifi_addr"][0], "FAIL")


class NetworkTest(unittest.TestCase):
    STATUS = (
        '{"returnValue":true,"isInternetConnectionAvailable":true,"wired":{"state":"connected","interfaceName":'
        '"eth0","ipAddress":"10.0.2.15","method":"dhcp","gateway":"10.0.2.2","dns1":"10.0.2.254","onInternet":"yes"}}'
    )
    TEXT = "\n".join(
        [
            "addr=2: eth0: <BROADCAST,UP> mtu 1500     inet 10.0.2.15/24 brd 10.0.2.255 scope global eth0",
            "route=Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT "
            "eth0	00000000	0202000A	0003	0	0	0	00000000	0	0	0 "
            "eth0	0002000A	00000000	0001	0	0	0	00FFFFFF	0	0	0",
            'connman.webos.getStatus={"returnValue":false,"errorText":"Service does not exist"}',
            'connman.palm.getStatus={"returnValue":false}',
            f"connman.palm.getstatus={STATUS}",
            "dns=nslookup rc=0 Server: 127.0.0.1 Address 1: 127.0.0.1 localhost.home Name: example.com "
            "Address 1: 104.20.23.154 Address 2: 2606:4700:10::6814:179a",
            "http=rc=0 cafe0123",
            "TV FACTS END",
        ]
    )
    SERVED = 'REQUEST 10.0.2.15 "GET /cafe0123 HTTP/1.1" 200 -\n'
    HOST = "dns=104.20.23.154 172.66.147.243 \nonline_dns=1.2.3.4 \nonline_http=200\n"

    def test_good(self):
        r = items_of(
            tvsuite.judge_network(
                self.TEXT, ctx(), token="cafe0123", served=self.SERVED, host=self.HOST
            )
        )
        self.assertEqual(
            {k: v[0] for k, v in r.items()},
            {
                "dhcp-lease": "PASS",
                "connman-wired": "PASS",
                "dns": "PASS",
                "http": "PASS",
                "online": "PASS",
            },
        )
        self.assertIn("connman.palm.getstatus", r["connman-wired"][1])

    def test_bad(self):
        r = items_of(
            tvsuite.judge_network(
                self.TEXT, ctx(), token="cafe0124", served=self.SERVED, host=self.HOST
            )
        )
        self.assertEqual(r["http"][0], "FAIL")
        # the VM host offline: the guest saying online is wrong
        r = items_of(
            tvsuite.judge_network(
                self.TEXT,
                ctx(),
                token="cafe0123",
                served=self.SERVED,
                host="dns=104.20.23.154\nonline_dns=\nonline_http=000\n",
            )
        )
        self.assertEqual(r["online"][0], "FAIL")
        r = items_of(
            tvsuite.judge_network(
                self.TEXT.replace("10.0.2.15/24", "169.254.1.2/16"),
                ctx(),
                token="cafe0123",
                served=self.SERVED,
                host=self.HOST,
            )
        )
        self.assertEqual(r["dhcp-lease"][0], "FAIL")
        self.assertEqual(r["connman-wired"][0], "FAIL")
        r = items_of(
            tvsuite.judge_network(
                self.TEXT, ctx(), token="cafe0123", served=self.SERVED, host="dns=\n"
            )
        )
        self.assertEqual(r["dns"][0], "FAIL")

    def test_network_is_the_namespaces(self):
        """The expectations are link=nat's namespace (netns.py): its lease, router, the mock's DNS
        and the TAP's address, where the suite's server runs."""
        from beanjar import netns

        self.assertEqual(tvsuite.NET_GUEST, netns.GUEST)
        self.assertEqual(tvsuite.NET_ROUTER, netns.ROUTER)
        self.assertEqual(tvsuite.NET_DNS, netns.MOCK_DNS)
        self.assertEqual(tvsuite.TEST_SERVER, netns.DHCP_SERVER)

    def test_http_needs_the_guests_own_address(self):
        """The server's log must show the request from the guest itself: one from the host's
        loopback (or anything translated on the way) is not the guest's fetch."""
        for served in (
            'REQUEST 127.0.0.1 "GET /cafe0123 HTTP/1.1" 200 -\n',
            'REQUEST 10.0.2.2 "GET /cafe0123 HTTP/1.1" 200 -\n',
        ):
            r = items_of(
                tvsuite.judge_network(
                    self.TEXT, ctx(), token="cafe0123", served=served, host=self.HOST
                )
            )
            self.assertEqual(r["http"][0], "FAIL", served)
            self.assertIn("from 10.0.2.15", r["http"][1])
        # the old slirp DNS is no longer the namespace's
        r = items_of(
            tvsuite.judge_network(
                self.TEXT.replace('"dns1":"10.0.2.254"', '"dns1":"10.0.2.3"'),
                ctx(),
                token="cafe0123",
                served=self.SERVED,
                host=self.HOST,
            )
        )
        self.assertEqual(r["connman-wired"][0], "FAIL")


class HelpersTest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(tvsuite.fw_version(FW), "03.70.25")
        self.assertEqual(tvsuite.chip_name("M16PB0"), "M16P")
        self.assertEqual(tvsuite.uhd_of_model("49UB850V-ZA"), "true")
        self.assertEqual(tvsuite.uhd_of_model("32LX6900PJA"), "false")
        self.assertEqual(
            tvsuite.reference("Device Unique ID", 2),
            "LG API reference: Device Unique ID on webOS TV 2.x: No",
        )
        self.assertIsNone(tvsuite.reference(None, 4))
        self.assertIsNone(tvsuite.reference("Device Unique ID", 7))  # no column past webOS TV 6.x
        self.assertEqual(tvsuite.short([("a", "PASS", ""), ("b", "SKIP", "")]), "PASS 1/0/1")

    def test_settings_records(self):
        class Fw:
            def find(self, path):
                return ("rootfs.pak", path)

            def read(self, pak, path):
                return (
                    b'{"_kind":"x","category":"option","value":{"country":"GBR"}}\n'
                    b'{"_kind":"x","category":"caption","value":{"captionEnable":"off"}}'
                    b'[{"category":"","value":{"localeInfo":{}}}]'
                )

        self.assertEqual(
            gen.settings_keys(Fw()),
            {"option": {"country"}, "caption": {"captionEnable"}, "": {"localeInfo"}},
        )

    def test_provider_ranking(self):
        class Fw:
            def find(self, path, paks=None):
                return ("rootfs.pak", path.lstrip("/"))

            def read(self, pak, path):
                return b""

        provs = {
            "*": [("/usr/bin/node", "nodejs.json")],
            "com.webos.service.sm": [("/usr/sbin/securitymanager", "sm.json")],
            "com.webos.service.*": [("/usr/sbin/other", "other.json")],
        }
        locs, src = gen.provider(Fw(), provs, "com.webos.service.sm", None)
        self.assertEqual(locs, [("rootfs.pak", "usr/sbin/securitymanager")])
        locs, src = gen.provider(Fw(), provs, "com.webos.service.blegatt", None)
        self.assertEqual(locs, [("rootfs.pak", "usr/sbin/other")])
        locs, src = gen.provider(Fw(), {"*": provs["*"]}, "com.webos.service.blegatt", None)
        self.assertIsNone(locs)
        self.assertIn("no role or services.d file provides", src)

    def test_registrations_per_firmware(self):
        """What an executable registers is read again for each firmware: a full run sees the same
        label (rootfs.pak:/usr/sbin/...) in every target's firmware."""
        import types
        from unittest import mock

        label = "rootfs.pak:/usr/sbin/webos-connman-adapter"
        with mock.patch.object(
            gen, "ls_registrations", lambda data, label: {data.decode(): {"pub": {"/": {"get"}}}}
        ):
            for d, name in (("fw-a", "com.a.svc"), ("fw-b", "com.b.svc")):
                reach = gen.registered(
                    types.SimpleNamespace(dir=d), [(label, name.encode())], name, ""
                )
                self.assertEqual(reach[2], f"{label} ({name}, pub)", d)

    def test_non_utf8_file_names(self):
        """A pak's file names that are not UTF-8 (N21D has a symlink usr/lib/\\xb1\\xfb) are listed,
        followed and read, and go back to their bytes in an unsquashfs list file (tee_init_of)."""
        import shutil
        import subprocess
        import tempfile

        if not shutil.which("mksquashfs") or not shutil.which("unsquashfs"):
            self.skipTest("no squashfs-tools here")
        with tempfile.TemporaryDirectory() as tmp:
            src = os.fsencode(os.path.join(tmp, "src"))
            os.makedirs(os.path.join(src, b"usr/lib"))
            os.makedirs(os.path.join(src, b"usr/\xb1\xfb"))
            with open(os.path.join(src, b"usr/lib/libvocon.so.4"), "wb") as f:
                f.write(b"vocon")
            os.symlink(b"libvocon.so.4", os.path.join(src, b"usr/lib/\xb1\xfb"))
            with open(os.path.join(src, b"usr/\xb1\xfb/" + gen.TEE_INIT.encode()), "wb") as f:
                f.write(b"chmod 666 /dev/tee0\n")
            subprocess.run(
                [
                    "mksquashfs",
                    src,
                    os.path.join(tmp, "rootfs.pak"),
                    "-quiet",
                    "-noappend",
                    "-no-progress",
                ],
                check=True,
                capture_output=True,
            )
            fw = gen.Firmware(tmp)
            odd = os.fsdecode(b"usr/lib/\xb1\xfb")
            self.assertEqual(fw.files("rootfs.pak")[odd], "libvocon.so.4")
            self.assertEqual(fw.find(odd), ("rootfs.pak", "usr/lib/libvocon.so.4"))
            init = os.fsdecode(b"usr/\xb1\xfb/" + gen.TEE_INIT.encode())
            self.assertEqual(fw.read("rootfs.pak", init), b"chmod 666 /dev/tee0\n")
            self.assertEqual(gen.tee_init_of(fw, ["rootfs.pak"]), [f"rootfs.pak:/{init}"])

    def test_super_pak_firmware(self):
        """A super.pak firmware's rootfs and otncabi are logical images: the release and the browser's
        requiredEULA (the eula row) are read from them at their offsets."""
        import json
        import shutil
        import subprocess
        import tempfile

        if not shutil.which("mksquashfs") or not shutil.which("unsquashfs"):
            self.skipTest("no squashfs-tools here")
        with tempfile.TemporaryDirectory() as tmp:

            def image(name, files):
                src = os.path.join(tmp, "src-" + name)
                for path, data in files.items():
                    os.makedirs(os.path.dirname(os.path.join(src, path)), exist_ok=True)
                    with open(os.path.join(src, path), "w") as f:
                        f.write(data)
                out = os.path.join(tmp, name + ".sqfs")
                subprocess.run(
                    ["mksquashfs", src, out, "-quiet", "-noappend", "-no-progress"],
                    check=True,
                    capture_output=True,
                )
                with open(out, "rb") as f:
                    data = f.read()
                return data + bytes(-len(data) % 512)

            apps = "usr/palm/applications/"
            payloads = {
                "rootfs": image(
                    "rootfs",
                    {
                        "etc/starfish-release": "Rockhopper release 9.2.4-4603 (ombre-okapi)\n",
                        apps + "com.webos.app.firstuse-overlay/appinfo.json": "{}",
                    },
                ),
                "fonts": b"F" * 512,
                "otncabi": image(
                    "otncabi",
                    {
                        apps + "com.webos.app.browser/appinfo.json": json.dumps(
                            {
                                "id": "com.webos.app.browser",
                                "type": "native_builtin",
                                "main": "chrome",
                                "requiredEULA": "generalTerms",
                            }
                        )
                    },
                ),
                "otycabi": b"Y" * 512,
                "bsppart": b"B" * 512,
            }
            table, offset, data = [], 1024, bytes(1024)
            for index, (name, payload) in enumerate(payloads.items()):
                table += [
                    f"{name}-linear,,,ro, 0 {len(payload) // 512} linear /dev/mmcblk0p27 {offset // 512}",
                    f"{name},,,ro, 0 1 webos-verity /dev/dm-{index * 2}",
                ]
                data += payload + bytes(512)
                offset += len(payload) + 512
            with open(os.path.join(tmp, "super.pak"), "wb") as f:
                f.write(data)
            text = ' dm-mod.waitfor="/dev/mmcblk0p27" dm-mod.create="' + ";".join(table)
            text += '" root=/dev/dm-1 ro rootfstype=squashfs '
            with open(os.path.join(tmp, "dpmeta.pak"), "wb") as f:
                f.write(text.encode() + b"\0")

            fw = gen.Firmware(tmp)
            self.assertEqual(fw.paks[0], "rootfs.pak")
            self.assertNotIn("super.pak", fw.paks)
            self.assertEqual(gen.release_of(fw)["version"], "9.2.4-4603")
            browser = gen.apps_of(fw, ["rootfs.pak", "otncabi.pak"])["browser"]
            self.assertTrue(browser["present"])
            self.assertEqual(browser["eula"]["terms"], "generalTerms")
            self.assertEqual(browser["eula"]["screen"], tvsuite.TERMS_SCREEN)
            self.assertIn("otncabi.pak:/", browser["eula"]["source"])

    def test_expected_crash_targets(self):
        """W20's browser abort at close is expected on both jhericurl targets, and only with its frames."""
        frames = [
            "__cxa_pure_virtual+0x12",
            "_ZN7Browser12CloseBrowserEb+0x1be",
            "_ZN7Browser33OnFirstUseErrorReportDialogClosedEbRKNSt7__cxx1112basic_stringItN4base18"
            "string16_internals20string16_char_traitsESaItEEE+0x6e",
        ]
        for target in ("k6lp-jhericurl", "k6hp-jhericurl"):
            self.assertIsNotNone(
                tvsuite.expected_crash(target, "browser", "graphics", ("6", frames)), target
            )
        self.assertIsNone(
            tvsuite.expected_crash("k7lp-kisscurl", "browser", "graphics", ("6", frames))
        )
        self.assertIsNone(
            tvsuite.expected_crash("k6hp-jhericurl", "browser", "graphics", ("6", frames[:1]))
        )

    def test_ls_registrations(self):
        """W15M's connection manager: getStatus on the public bus only (a firmware where there is one)."""
        sys.path.insert(
            0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "host", "core")
        )
        from beanjar import paths

        fw = paths.find_firmware("04.06.75.01-HE_DTV_W15M_AFAAABAA") or ""
        if not os.path.isfile(os.path.join(fw, "rootfs.pak")) or not os.access(
            gen.OBJDUMP, os.X_OK
        ):
            self.skipTest("no W15M firmware or SDK objdump here")
        f = gen.Firmware(fw)
        regs = gen.ls_registrations(f.read("rootfs.pak", "usr/sbin/webos-connman-adapter"))
        palm = regs["com.palm.connectionmanager"]
        self.assertIn("getstatus", palm["prv"]["/"])
        self.assertNotIn("getStatus", palm["prv"]["/"])
        self.assertIn("getStatus", palm["pub"]["/"])

    def test_expect_table(self):
        """The tracked table has every target and every check."""
        table = expect_table()
        for target, row in table.items():
            for cid in tvsuite.catalogue():
                self.assertIn(cid, row["checks"], target)
            for cid, exp in row["checks"].items():
                self.assertTrue(exp["source"], (target, cid))
            for role, _, _ in tvsuite.APP_ROLES:
                app = row["apps"][role]
                self.assertTrue(app["source"], (target, role))
                self.assertTrue(
                    not app["present"] or app.get("id") or app.get("kind") == "key", (target, role)
                )

    def test_expect_files(self):
        """Every file of tests/regress/tv-expect parses, is a target's (guest/targets/<target>.toml),
        and each of its checks' sources is in its [sources]."""
        import tomllib

        top = os.path.dirname(os.path.dirname(REGRESS))
        names = os.listdir(tvsuite.EXPECT_DIR)
        self.assertTrue(names)
        for name in names:
            self.assertTrue(name.endswith(".toml"), name)
            self.assertTrue(os.path.isfile(os.path.join(top, "guest", "targets", name)), name)
            with open(os.path.join(tvsuite.EXPECT_DIR, name), "rb") as f:
                doc = tomllib.load(f)
            self.assertTrue(doc["checks"], name)
            for cid, exp in doc["checks"].items():
                self.assertIn(exp["source"], doc["sources"], (name, cid))

    def test_expect_text(self):
        """The generator's file reads back as the row (None left out), checks' evidence once in
        [sources], and the same row gives the same text."""
        import tomllib

        ev = (
            "com.webos.service.tv.systemproperty by tvservice-dvb.pak:/mnt/lg/tvservice/x; 'a' in y"
        )
        row = dict(
            firmware=FW,
            webos=6,
            broadcast=None,
            release=dict(name="Rockhopper", version="6.6.0-6", codename="kisscurl-kolovesi"),
            checks={
                "sysinfo.boardType": dict(present=True, source=ev),
                "sysinfo.modelName": dict(present=True, source=ev),
                "nyx.DeviceInfo.output": dict(present=True, accept=["a", "b"], source="nyx-cmd"),
            },
            apps=dict(home=dict(present=True, id=None, headless=dict(runs=False, source="s"))),
            tee=dict(clients=[], tas=[f"{i:08x}-0000-0000-0000-000000000000" for i in range(4)]),
            audio=dict(webaudio=dict(present=True, level_db=-8.08)),
        )
        text = gen.expect_text("k7lp-kisscurl", row)
        self.assertEqual(text, gen.expect_text("k7lp-kisscurl", row))
        self.assertEqual(text.count(ev), 1)
        self.assertIn(
            '"sysinfo.boardType" = { present = true, source = "com-webos-service-tv-', text
        )
        self.assertIn('    "00000003-0000-0000-0000-000000000000",\n', text)  # one a line
        doc = tomllib.loads(text)
        sources = doc.pop("sources")
        for exp in doc["checks"].values():
            exp["source"] = sources[exp["source"]]
        del row["broadcast"], row["apps"]["home"]["id"]
        self.assertEqual(doc, row)


def state(fg="", lsm_fg="", lsm_state="", running=(), wam="100", lsm="200", extra=""):
    run = json.dumps(
        {"running": [dict(id=i, processid=p) for i, p in running], "returnValue": True}
    )
    sam = json.dumps({"appId": fg, "returnValue": True}) if fg is not None else ""
    return (
        f"{extra}sam_fg={sam}\nlsm_fg={lsm_fg}\nlsm_state={lsm_state}\nrunning={run}\n"
        f"pid.wam={wam}\npid.lsm={lsm}\nuptime=1.0\nTV FACTS END\n"
    )


LAUNCHED = 'launch={"returnValue":true,"processId":"1"}\nfront_wait=3\n'
SHOT = dict(size=[1920, 1080], nonblack=0.6)


class AppsTest(unittest.TestCase):
    ROW = dict(present=True, id="com.palm.app.settings", type="web")

    def test_plan(self):
        expect = {
            "apps": {
                "settings": dict(
                    present=True, id="com.palm.app.settings", type="web", main="index.html"
                ),
                "livetv": dict(
                    present=True,
                    id="com.webos.app.livetv",
                    type="native",
                    main="file:///usr/bin/com.webos.app.inputcommon",
                ),
                "dashboard": dict(
                    present=False,
                    source="no usr/palm/applications/com.webos.app.homeconnect/appinfo.json",
                ),
            }
        }
        run, skips = tvsuite.app_plan(expect, "graphics")
        roles = [r for r, _ in run]
        self.assertIn("settings", roles)
        self.assertIn("browser", roles)  # not in the table: run, and it fails
        self.assertEqual(skips, [("app.dashboard", "SKIP", expect["apps"]["dashboard"]["source"])])
        run, _ = tvsuite.app_plan(expect, "headless")
        self.assertEqual([r for r, _ in run][:1], ["settings"])  # Settings first, in both lanes
        self.assertEqual(tvsuite.headless_of(expect["apps"]["settings"])["runs"], False)
        self.assertEqual(
            tvsuite.headless_of(expect["apps"]["livetv"])["runs"], False
        )  # the stand-in ends
        self.assertEqual(
            tvsuite.judge_app("browser", dict(present=True, id=None), "", "", "")[1], "FAIL"
        )

    def test_good(self):
        L = LAUNCHED + state("com.palm.app.settings", running=[("com.palm.app.settings", "")])
        A = state("com.palm.app.settings", running=[("com.palm.app.settings", "")])
        C = 'close={"returnValue":true}\ngone_wait=1\n' + state("com.webos.app.livetv")
        c, s, why = tvsuite.judge_app("settings", self.ROW, L, A, C, SHOT)
        self.assertEqual((c, s), ("app.settings", "PASS"), why)

    def test_terms_first(self):
        """An app whose terms the set has not agreed to: LG's terms screen in its place (front_id)
        is judged as the app would be; the app itself in front is judged as usual."""
        t = tvsuite.TERMS_SCREEN
        row = dict(
            present=True,
            id="com.webos.app.browser",
            type="native_builtin",
            main="chrome",
            eula=dict(terms="generalTerms", screen=t, source="appinfo.json: requiredEULA"),
        )
        L = LAUNCHED + f"front_id={t}\n" + state(t, running=[(t, "")])
        A = state(t, running=[(t, "")])
        C = 'close={"returnValue":true}\ngone_wait=1\n' + state("com.webos.app.home")
        self.assertEqual(tvsuite.terms_first(row, L), t)
        c, s, why = tvsuite.judge_app("browser", row, L, A, C, SHOT)
        self.assertEqual((c, s), ("app.browser", "PASS"), why)
        self.assertIn("the terms first", why)
        # the terms screen without the table's eula: not the app, not in front
        plain = {k: v for k, v in row.items() if k != "eula"}
        self.assertIsNone(tvsuite.terms_first(plain, L))
        self.assertEqual(tvsuite.judge_app("browser", plain, L, A, C, SHOT)[1], "FAIL")
        # agreed: the browser itself comes up and is judged as any app
        b = "com.webos.app.browser"
        L2 = LAUNCHED + f"front_id={b}\n" + state(b, running=[(b, "")])
        A2 = state(b, running=[(b, "")])
        self.assertIsNone(tvsuite.terms_first(row, L2))
        c, s, why = tvsuite.judge_app("browser", row, L2, A2, C, SHOT)
        self.assertEqual(s, "PASS", why)
        self.assertNotIn("terms", why)

    def test_not_present(self):
        """An app the table has but sam does not know at run time: not launched, its own FAIL."""
        L = (
            'appinfo={"returnValue":false,"errorCode":-101,"errorText":"no app"}\npresent=no\n'
            + state("com.webos.app.livetv")
        )
        row = dict(
            self.ROW, source="rootfs.pak:/usr/palm/applications/com.palm.app.settings/appinfo.json"
        )
        c, s, why = tvsuite.judge_app("settings", row, L, "", "")
        self.assertEqual((c, s), ("app.settings", "FAIL"))
        self.assertIn("app not present", why)
        self.assertIn("rootfs.pak:/usr/palm", why)

    def test_overlay_in_front_by_the_compositor(self):
        L = LAUNCHED + state(
            "com.webos.app.livetv",
            lsm_fg='{"foregroundAppInfo":[{"appId":"com.webos.app.home"}]}',
            running=[("com.webos.app.home", "7")],
        )
        C = "gone_wait=0\n" + state("com.webos.app.livetv")
        row = dict(present=True, id="com.webos.app.home", type="native")
        self.assertEqual(tvsuite.judge_app("home", row, L, L, C, SHOT)[1], "PASS")

    def test_home_stays(self):
        row = dict(
            present=True,
            id="com.webos.app.home",
            type="native",
            stays=dict(source="appinfo.json: 2"),
        )
        L = LAUNCHED + state("com.webos.app.home", running=[("com.webos.app.home", "7")])
        before = (
            'close_before={"id":"com.webos.app.home","processid":"7"}\nclose={"returnValue":true}\n'
        )
        up = (
            before
            + "gone_wait=none\n"
            + state("com.webos.app.livetv", running=[("com.webos.app.home", "7")])
        )
        c, s, why = tvsuite.judge_app("home", row, L, L, up, SHOT)
        self.assertEqual(s, "PASS", why)
        self.assertIn("stays up", why)
        respawned = (
            before
            + "gone_wait=none\n"
            + state("com.webos.app.livetv", running=[("com.webos.app.home", "9")])
        )
        self.assertIn("respawned", tvsuite.judge_app("home", row, L, L, respawned, SHOT)[2])
        gone = before + "gone_wait=1\n" + state("com.webos.app.livetv", running=[])
        self.assertIn(
            "gone after closeByAppId", tvsuite.judge_app("home", row, L, L, gone, SHOT)[2]
        )

    def test_failures(self):
        ok = LAUNCHED + state("com.palm.app.settings", running=[("com.palm.app.settings", "")])
        gone = "gone_wait=1\n" + state("x")
        refused = 'launch={"returnValue":false,"errorText":"no app"}\nfront_wait=none\n' + state(
            "x"
        )
        self.assertIn(
            "launch refused", tvsuite.judge_app("settings", self.ROW, refused, "", gone, SHOT)[2]
        )
        late = LAUNCHED.replace("front_wait=3", "front_wait=none") + state("com.webos.app.livetv")
        self.assertIn(
            "not in front", tvsuite.judge_app("settings", self.ROW, late, "", gone, SHOT)[2]
        )
        crashed = state("com.webos.app.livetv", running=[])
        why = tvsuite.judge_app("settings", self.ROW, ok, crashed, gone, SHOT)[2]
        self.assertIn("not in front after the keys", why)
        self.assertIn("crashed", why)
        wam = state("com.palm.app.settings", running=[("com.palm.app.settings", "")], wam="101")
        self.assertIn(
            "WebAppMgr restarted", tvsuite.judge_app("settings", self.ROW, ok, wam, gone, SHOT)[2]
        )
        black = dict(size=[1920, 1080], nonblack=0.001)
        self.assertIn("black", tvsuite.judge_app("settings", self.ROW, ok, ok, gone, black)[2])
        stays = "gone_wait=none\n" + state("com.palm.app.settings")
        self.assertIn(
            "still running", tvsuite.judge_app("settings", self.ROW, ok, ok, stays, SHOT)[2]
        )
        native = dict(present=True, id="com.webos.app.hdmi1", type="native")
        L = LAUNCHED + state("com.webos.app.hdmi1", running=[("com.webos.app.hdmi1", "10")])
        A = state("com.webos.app.hdmi1", running=[("com.webos.app.hdmi1", "11")])
        self.assertIn("respawned", tvsuite.judge_app("hdmi1", native, L, A, gone, SHOT)[2])

    def test_headless_by_design(self):
        row = dict(
            present=True,
            id="com.palm.app.settings",
            type="web",
            headless=dict(runs=False, source="a web app: no WebAppMgr headless"),
        )
        L = "launch=\nfront_wait=none\n" + state(None)
        c, s, why = tvsuite.judge_app(
            "settings", row, L, state(None), "gone_wait=0\n" + state(None), None, "headless"
        )
        self.assertEqual(s, "PASS", why)
        self.assertIn("absent by design", why)
        # a process that came and went before the look passes too
        up = LAUNCHED + state(None, running=[("com.palm.app.settings", "9")])
        self.assertEqual(
            tvsuite.judge_app("settings", row, up, state(None), "", None, "headless")[1], "PASS"
        )
        self.assertEqual(
            tvsuite.judge_app("settings", row, up, up, "", None, "headless")[1], "FAIL"
        )
        dead = state(None).replace('"returnValue": true', '"returnValue": false')
        self.assertIn(
            "did not answer", tvsuite.judge_app("settings", row, L, dead, "", None, "headless")[2]
        )

    def test_crash_reports(self):
        row = dict(present=True, id="com.webos.app.mystarter", type="qml")
        L = LAUNCHED + state(
            "com.webos.app.mystarter",
            running=[("com.webos.app.mystarter", "")],
            extra="crashes=tPRM.10 \n",
        )
        A = state(
            "com.webos.app.mystarter",
            running=[("com.webos.app.mystarter", "")],
            extra="crashes=tPRM.10 tPRM.11 qml-runner.12 \n",
        )
        C = "gone_wait=1\n" + state(
            "com.webos.app.livetv", extra="crashes=tPRM.10 tPRM.11 qml-runner.12 \n"
        )
        c, s, why = tvsuite.judge_app("home", row, L, A, C, SHOT, before={"tPRM.10"})
        self.assertEqual(s, "FAIL")
        self.assertIn("crash reports qml-runner.12", why)
        self.assertIn("other crash reports meanwhile: tPRM.11", why)
        A2 = A.replace(" qml-runner.12", "")
        c, s, why = tvsuite.judge_app("home", row, L, A2, C.replace(" qml-runner.12", ""), SHOT)
        self.assertEqual(s, "PASS", why)
        self.assertIn("tPRM.11", why)
        native = dict(type="native", main="file:///usr/bin/com.webos.app.inputcommon")
        self.assertEqual(
            tvsuite.crashed(native, {"com.webos.app.i.5", "tPRM.1"}), ["com.webos.app.i.5"]
        )

    def test_expected_browser_crashes(self):
        # the browser's process is the one its launcher script execs (the expected table's process)
        row = dict(
            present=True,
            id="com.webos.app.browser",
            type="native_builtin",
            main="run_webbrowser",
            process=dict(name="chrome"),
            headless=dict(runs=False, source="no compositor"),
        )
        app = [("com.webos.app.browser", "7")]
        L = LAUNCHED + state("com.webos.app.browser", running=app)
        abort = (
            "crashsig.7=chrome 6 __libc_do_syscall+0x5 __cxa_pure_virtual+0x12 "
            "_ZN7Browser33OnFirstUseErrorReportDialogClosedEbRKNSt7__cxx1112basic_stringItN4base18"
            "string16_internals20string16_char_traitsESaItEEE+0x6e _ZN7Browser12CloseBrowserEb+0x1be\n"
        )
        C = "gone_wait=1\n" + state("com.webos.app.home", extra="crashes=chrome.7 \n" + abort)
        c, s, why = tvsuite.judge_app("browser", row, L, L, C, SHOT, target="k6lp-jhericurl")
        self.assertEqual(s, "PASS", why)
        self.assertIn("expected crash report chrome.7", why)
        # the same report on another target, or another signature on W20L, fails
        c, s, why = tvsuite.judge_app("browser", row, L, L, C, SHOT, target="k7lp-kisscurl")
        self.assertEqual(s, "FAIL")
        self.assertIn("crash reports chrome.7", why)
        C2 = "gone_wait=1\n" + state(
            "com.webos.app.home",
            extra="crashes=chrome.7 \ncrashsig.7=chrome 11 TEEC_CloseSession+0x30\n",
        )
        c, s, why = tvsuite.judge_app("browser", row, L, L, C2, SHOT, target="k6lp-jhericurl")
        self.assertEqual(s, "FAIL")
        # headless: the app cannot run; its no-display crash is LG's, any other crash fails
        HL = LAUNCHED + state("", running=app)
        HA = state(
            "",
            extra="crashes=chrome.7 \ncrashsig.7=chrome 11 _ZN7display7DisplayC2ERKS0_+0x3 "
            "_ZN5views26DesktopFactoryOzoneWayland19CreateDesktopScreenEv+0x82\n",
        )
        c, s, why = tvsuite.judge_app(
            "browser", row, HL, HA, "", None, "headless", set(), "k5lp-goldilocks2"
        )
        self.assertEqual(s, "PASS", why)
        self.assertIn("expected crash report chrome.7", why)
        c, s, why = tvsuite.judge_app(
            "browser",
            row,
            HL,
            HA.replace("+0x3", "+0x9"),
            "",
            None,
            "headless",
            set(),
            "k5lp-goldilocks2",
        )
        self.assertEqual(s, "FAIL")
        self.assertIn("crash reports chrome.7", why)

    def test_closed_base_app_comes_back(self):
        row = dict(present=True, id="com.webos.app.livetv", type="native")
        L = LAUNCHED + state("com.webos.app.livetv", running=[("com.webos.app.livetv", "n-807")])
        C = "gone_wait=1\n" + state(
            "com.webos.app.livetv", running=[("com.webos.app.livetv", "n-1895")]
        )
        c, s, why = tvsuite.judge_app("livetv", row, L, L, C, SHOT)
        self.assertEqual(s, "PASS", why)
        self.assertIn("back as pid n-1895", why)

    def test_headless(self):
        row = dict(present=True, id="com.webos.app.livetv", type="native")
        row["headless"] = dict(runs=True, source="test")
        L = LAUNCHED + state(None, running=[("com.webos.app.livetv", "10")])
        C = "gone_wait=2\n" + state(None)
        self.assertEqual(tvsuite.judge_app("livetv", row, L, L, C, None, "headless")[1], "PASS")
        A = state(None, running=[("com.webos.app.livetv", "12")])
        self.assertIn("respawned", tvsuite.judge_app("livetv", row, L, A, C, None, "headless")[2])

    def test_launcher_key(self):
        row = dict(present=True, id=None, kind="key")
        shown = state(
            "com.webos.app.livetv", lsm_state='{"currentState":"home","returnValue":true}'
        )
        hidden = state(
            "com.webos.app.livetv", lsm_state='{"currentState":"none","returnValue":true}'
        )
        self.assertEqual(tvsuite.judge_app("home", row, shown, shown, hidden, SHOT)[1], "PASS")
        self.assertIn("still shown", tvsuite.judge_app("home", row, shown, shown, shown, SHOT)[2])
        self.assertIn(
            "not in front", tvsuite.judge_app("home", row, hidden, shown, hidden, SHOT)[2]
        )

    def test_install_remove(self):
        inst = (
            'devmode_enabled=yes\ninstall={"subscribed":true} {"details":{"state":"installing"}} '
            '{"details":{"state":"installed"}}\nappinfo={"returnValue":true,"appInfo":{}}\nTV FACTS END\n'
        )
        self.assertEqual(tvsuite.judge_install(inst)[1], "PASS")
        self.assertEqual(
            tvsuite.judge_install(inst.replace('"installed"', '"install failed"'))[1], "FAIL"
        )
        rem = 'remove={"details":{"state":"removed"}}\nappinfo={"returnValue":false,"errorText":"x"}\nTV FACTS END\n'
        self.assertEqual(tvsuite.judge_remove(rem)[1], "PASS")
        self.assertEqual(tvsuite.judge_remove(rem.replace("false", "true"))[1], "FAIL")


class DrawTest(unittest.TestCase):
    def paint(self, mode, size=(1280, 720), drop=None, black=False, nowebgl=False):
        from PIL import Image, ImageDraw

        pat = tvsuite.pattern()
        W, H = size
        im = Image.new(
            "RGB",
            size,
            (0, 0, 0) if black else tuple(pat["nowebgl"] if nowebgl else pat["modes"][mode]),
        )
        d = ImageDraw.Draw(im)
        if not (black or nowebgl):
            for p in pat["patches"]:
                if p["name"] == drop:
                    continue
                x, y, w, h = p["rect"]
                d.rectangle([x * W, y * H, (x + w) * W - 1, (y + h) * H - 1], fill=tuple(p["rgb"]))
        path = os.path.join(self.tmp, f"{mode}.png")
        im.save(path)
        return tvsuite.image_facts(path)

    def setUp(self):
        import tempfile

        self.tmp = tempfile.mkdtemp()

    def test_pattern_and_judge(self):
        for mode in ("canvas", "webgl"):
            self.assertEqual(tvsuite.judge_draw(mode, self.paint(mode))[1], "PASS", mode)
            self.assertEqual(
                tvsuite.judge_draw(mode, self.paint(mode, (1920, 1080)))[1], "PASS", mode
            )
        why = tvsuite.judge_draw("canvas", self.paint("canvas", drop="green"))[2]
        self.assertIn("green", why)
        self.assertIn("black", tvsuite.judge_draw("canvas", self.paint("canvas", black=True))[2])
        self.assertIn("no WebGL", tvsuite.judge_draw("webgl", self.paint("webgl", nowebgl=True))[2])
        # the other mode's background: a stale frame does not pass
        self.assertIn("background", tvsuite.judge_draw("webgl", self.paint("canvas"))[2])
        self.assertEqual(tvsuite.judge_draw("canvas", dict(error="x"))[1], "FAIL")

    def test_app_and_judge_share_the_pattern(self):
        with open(os.path.join(tvsuite.TEST_APP_DIR, "index.html")) as f:
            html = f.read()
        self.assertIn('src="pattern.js"', html)
        with open(os.path.join(tvsuite.TEST_APP_DIR, "appinfo.json")) as f:
            self.assertEqual(json.load(f)["id"], tvsuite.TEST_APP)
        self.assertEqual(set(tvsuite.pattern()["modes"]), {"canvas", "webgl"})


TEE_ROW = dict(
    tee=dict(
        clients=[
            "rootfs.pak:/usr/bin/tee-supplicant",
            "bsppart.pak:/bsp/usr/lib/libkadaptor.so.1.0.0",
        ],
        tas=["82d6c0db-17b2-4ffc-b8ead3d5afc0e753"],
        source="test",
    )
)


def tee_guest(huk, crashes="", tamper=None):
    """tv.sh tee's facts as rtk.ko's TA (teeref) would make them under huk (bytes): tamper names an
    answer to spoil."""
    P = tvsuite.PROBE
    n = lambda d: len(d) + teeref.BLOB_EXTRA  # noqa: E731
    header = tvsuite.struct_header(P["header"], len(P["inputs"]))
    data = bytes((i * 13 + 1) & 0xFF for i in range(72))
    box = bytes(32) + data[32:]
    nonce = os.urandom(16)
    key = teeref.cipher_key(huk, header, P["inputs"])
    ct = teeref.stream(key, nonce, box[32:])
    boxed = nonce + teeref._mac(key, b"tag", nonce, ct)[:16] + ct
    image = bytearray((i * 7 + 3) & 0xFF for i in range(P["image"]))
    image[:2] = b"SE"
    lines = [
        "tee_dev=char",
        "lib=/usr/lib/libkadaptor.so.1.0.0",
        f"open.{tvsuite.SSTORE_UUID}=0 0",
        "open.82d6c0db-17b2-4ffc-b8ead3d5afc0e753=0xffff0008 3",
        "probe_open_rc=0",
        "sstore.checkvalid=0",
        "sstore.reencrypt=0 64",
        "sstore.reencrypt.roundtrip=ok",
        f"sstore.newdata=0 {len(P['newdata']) + 4}",
        f"sstore.newdata.out={teeref.new_data(huk, P['newdata_value'], P['newdata']).hex()}",
    ]
    lines += [f"sstore.unsupported.{c:#x}=0xffff000a" for c in (0x1619, 0x400, 0x401, 0x10000)]
    for name, d in (("clearkey", P["clearkey"]), ("key", P["key"])):
        if name == "key":
            lines.append(f"sstore.key.probe=0 {n(d)}")
        lines += [
            f"sstore.{name}=0 {n(d)}",
            f"sstore.{name}.blob={teeref.wrap(huk, d, os.urandom(16)).hex()}",
            f"sstore.{name}.len=0 {len(d)}",
            f"sstore.{name}.get=0 {len(d)}",
            f"sstore.{name}.roundtrip=ok",
            f"sstore.{name}.altered=0xffff3071",
        ]
    lines += [
        "sstore.cipher.encrypt=0 72",
        f"sstore.cipher.data={data.hex()}",
        f"sstore.cipher.enc={teeref.cipher(huk, header, P['inputs'], data).hex()}",
        "sstore.cipher.decrypt=0 72",
        "sstore.cipher.roundtrip=ok",
        "sstore.box.encrypt=0 72",
        f"sstore.box.data={box.hex()}",
        f"sstore.box.enc={boxed.hex()}",
        "sstore.box.decrypt=0 72",
        "sstore.box.roundtrip=ok",
        "sstore.box.altered=0xffff3071",
        "sstore.storemac=0 32",
        f"sstore.storemac.mac={teeref.store_mac(huk, bytes(image)).hex()}",
        "sstore.storecheck=0",
        "sstore.storecheck.altered=0xffff3071",
        "sstore.hmac=0 32",
        f"sstore.hmac.tag={teeref.hmac_of(huk, 4, P['hmac'], P['hmac_p2']).hex()}",
        "sstore.hmaccheck=0",
        "sstore.hmaccheck.altered=0xffff3071",
        "sstore.fails=0",
        "probe_sstore_rc=0",
        "crashes=",
        f"tee_crashes={crashes}",
        "TV FACTS END",
    ]
    if tamper:
        lines = [ln if not ln.startswith(tamper + "=") else tamper + "=0xffff000a" for ln in lines]
    return "\n".join(lines)


class TeeTest(unittest.TestCase):
    def test_reference(self):
        """teeref: what one HUK wraps only it opens; altered, not at all; the ciphers invert."""
        a, b = os.urandom(32), os.urandom(32)
        blob = teeref.wrap(a, b"secret", os.urandom(16))
        self.assertEqual(len(blob), 6 + 176)
        self.assertEqual(teeref.unwrap(a, blob), b"secret")
        self.assertIsNone(teeref.unwrap(b, blob))
        self.assertIsNone(teeref.unwrap(a, blob[:30] + bytes([blob[30] ^ 1]) + blob[31:]))
        h = tvsuite.struct_header((1, 12), 3)
        data = os.urandom(100)
        self.assertEqual(teeref.cipher(a, h, b"abc", teeref.cipher(a, h, b"abc", data)), data)
        self.assertNotEqual(teeref.cipher(a, h, b"abc", data), teeref.cipher(b, h, b"abc", data))
        self.assertEqual(len(teeref.new_data(a, 7, b"xyz")), 7)
        self.assertNotEqual(teeref.store_mac(a, bytes(64)), teeref.store_mac(b, bytes(64)))
        # the store's MAC leaves its own slot out
        img = bytearray(os.urandom(256))
        mac = teeref.store_mac(a, bytes(img))
        img[teeref.STORE_SLOT : teeref.STORE_SLOT + 32] = mac
        self.assertEqual(teeref.store_mac(a, bytes(img)), mac)

    def test_judge(self):
        huk = os.urandom(32)
        items = items_of(tvsuite.judge_tee(tee_guest(huk), TEE_ROW, huk.hex()))
        self.assertEqual({c for c, (st, _) in items.items() if st != "PASS"}, set(), items)
        self.assertIn("tee.open.82d6c0db-17b2-4ffc-b8ead3d5afc0e753", items)
        # another VM's key: every derived answer fails
        other = items_of(tvsuite.judge_tee(tee_guest(huk), TEE_ROW, os.urandom(32).hex()))
        bad = {c for c, (st, _) in other.items() if st == "FAIL"}
        self.assertEqual(
            bad,
            {
                "tee.newdata.out",
                "tee.clearkey.blob",
                "tee.key.blob",
                "tee.cipher.enc",
                "tee.box.enc",
                "tee.storemac.mac",
                "tee.hmac.tag",
            },
        )
        self.assertEqual(
            items_of(tvsuite.judge_tee(tee_guest(huk), TEE_ROW, None))["tee.key.blob"][0], "FAIL"
        )
        crashed = items_of(
            tvsuite.judge_tee(tee_guest(huk, crashes="chrome.5733"), TEE_ROW, huk.hex())
        )
        self.assertEqual(crashed["tee.crashes"][0], "FAIL")
        self.assertIn("chrome.5733", crashed["tee.crashes"][1])
        spoilt = items_of(
            tvsuite.judge_tee(tee_guest(huk, tamper="sstore.checkvalid"), TEE_ROW, huk.hex())
        )
        self.assertEqual(spoilt["tee.checkvalid"][0], "FAIL")
        self.assertEqual(
            items_of(tvsuite.judge_tee("lib=dlopen failed\nTV FACTS END", TEE_ROW, huk.hex()))[
                "tee.client"
            ][0],
            "FAIL",
        )
        # a fact tee-probe did not print: a FAIL that names it, not a crash
        lost = items_of(
            tvsuite.judge_tee(
                "\n".join(
                    ln
                    for ln in tee_guest(huk).splitlines()
                    if not ln.startswith("sstore.clearkey.blob=")
                ),
                TEE_ROW,
                huk.hex(),
            )
        )
        self.assertEqual(
            lost["tee.clearkey.blob"],
            ("FAIL", "the blob opens to the data: tee-probe printed no sstore.clearkey.blob"),
        )
        # no /dev/tee0 (a firmware with a TEE client on a chip without rtk.ko's TEE): the device's FAIL alone
        gone = tvsuite.judge_tee(
            tee_guest(huk).replace(
                "tee_dev=char", "tee_dev=ls: /dev/tee0: No such file or directory"
            ),
            TEE_ROW,
            huk.hex(),
        )
        self.assertEqual(
            gone, [("tee.device", "FAIL", "/dev/tee0: ls: /dev/tee0: No such file or directory")]
        )

    def test_no_node_judge(self):
        """A chip with no TEE module (MStar): absent node, a clean ITEM_NOT_FOUND init, no crash."""
        row = dict(
            tee=dict(
                clients=["bsppart.pak:/bsp/usr/lib/libkadaptor.so"],
                tas=[],
                source="test",
                node=False,
                node_source="no module has tee=1",
            )
        )
        facts = (
            "tee_dev=ls: /dev/tee0: No such file or directory\nlib=/usr/lib/libkadaptor.so\n"
            f"open.{tvsuite.SSTORE_UUID}=init:0xffff0008\nprobe_open_rc=0\n"
            "sstore.fails=7\nprobe_sstore_rc=1\ntee_crashes=\nTV FACTS END"
        )
        items = items_of(tvsuite.judge_tee(facts, row))
        self.assertEqual(set(items), {"tee.device", "tee.init", "tee.crashes"})
        self.assertEqual({st for st, _ in items.values()}, {"PASS"}, items)
        crashed = items_of(tvsuite.judge_tee(facts.replace("tee_crashes=", "tee_crashes=x.1"), row))
        self.assertEqual(crashed["tee.crashes"][0], "FAIL")
        other = items_of(
            tvsuite.judge_tee(facts.replace("init:0xffff0008", "init:0xffff0000"), row)
        )
        self.assertEqual(other["tee.init"][0], "FAIL")
        # a node after all: the table is stale
        node = items_of(
            tvsuite.judge_tee(
                facts.replace("No such file or directory", "x").replace(
                    "tee_dev=ls: /dev/tee0: x", "tee_dev=char"
                ),
                row,
            )
        )
        self.assertEqual(node["tee.device"][0], "FAIL")
        # Realtek's row (node true) keeps the full checks
        huk = os.urandom(32)
        real = dict(tee=dict(TEE_ROW["tee"], node=True))
        self.assertIn("tee.key.blob", items_of(tvsuite.judge_tee(tee_guest(huk), real, huk.hex())))

    def test_no_ta_judge(self):
        """A chip with no module's TAs whose firmware has OP-TEE's client (lgemu.ko tee=1, MStar's
        LM21A family): a character device, every open TEEC_ERROR_ITEM_NOT_FOUND from the TEE, no
        crash."""
        row = dict(
            tee=dict(
                clients=["bsppart.pak:/bsp/usr/lib/libkadaptor.so"],
                tas=["f235e3dc-b4a5-11e8-96f8-529269fb1459"],
                source="test",
                node=True,
                node_source="lgemu.ko makes /dev/tee0 with no TA",
                node_tas=False,
            )
        )
        facts = (
            "tee_dev=char\nlib=/usr/lib/libkadaptor.so\n"
            f"open.{tvsuite.SSTORE_UUID}=0xffff0008 3\n"
            "open.f235e3dc-b4a5-11e8-96f8-529269fb1459=0xffff0008 3\nprobe_open_rc=0\n"
            "sstore.fails=7\nprobe_sstore_rc=1\ntee_crashes=\nTV FACTS END"
        )
        items = items_of(tvsuite.judge_tee(facts, row))
        self.assertEqual(
            set(items),
            {
                "tee.device",
                "tee.open.sstore",
                "tee.open.f235e3dc-b4a5-11e8-96f8-529269fb1459",
                "tee.crashes",
            },
        )
        self.assertEqual({st for st, _ in items.values()}, {"PASS"}, items)
        # no device: the device's FAIL alone
        gone = tvsuite.judge_tee(
            facts.replace("tee_dev=char", "tee_dev=ls: /dev/tee0: No such file or directory"), row
        )
        self.assertEqual([c for c, st, _ in gone], ["tee.device"])
        self.assertEqual(gone[0][1], "FAIL")
        # a TA that opened, or an init failure: FAIL
        opened = items_of(
            tvsuite.judge_tee(
                facts.replace(
                    f"{tvsuite.SSTORE_UUID}=0xffff0008 3", f"{tvsuite.SSTORE_UUID}=0x0 4"
                ),
                row,
            )
        )
        self.assertEqual(opened["tee.open.sstore"][0], "FAIL")
        crashed = items_of(tvsuite.judge_tee(facts.replace("tee_crashes=", "tee_crashes=x.1"), row))
        self.assertEqual(crashed["tee.crashes"][0], "FAIL")

    def test_skip_without_a_client(self):
        row = dict(
            tee=dict(
                clients=[], tas=[], source="no TEE client: none of the 900 ELF files names /dev/tee"
            )
        )
        self.assertEqual(tvsuite.judge_tee("", row), [("tee", "SKIP", row["tee"]["source"])])
        self.assertIsNone(tvsuite.tee_plan(row, "m3-goldilocks"))
        # tee-supplicant alone is no client library
        row = dict(tee=dict(clients=["rootfs.pak:/usr/bin/tee-supplicant"], tas=[], source="x"))
        self.assertIsNone(tvsuite.tee_plan(row, "k7lp-kisscurl"))

    def test_plan(self):
        """The library's path in the guest: bsppart's bsp/ is a lower layer at /."""
        self.assertEqual(
            tvsuite.tee_plan(TEE_ROW, "k7lp-kisscurl"),
            ("/usr/lib/libkadaptor.so.1.0.0", ["82d6c0db-17b2-4ffc-b8ead3d5afc0e753"]),
        )

    def test_expect_table_tee(self):
        """Every target's row says whether its firmware has a TEE client, with the evidence."""
        table = expect_table()
        for target, row in table.items():
            self.assertTrue(row["tee"]["source"], target)
            plan = tvsuite.tee_plan(row, target)
            # a row with a client says whether the VM has the node: a chip module's with its TAs
            # (rtk.ko's tee=1), else lgemu.ko's with none where the rootfs has OP-TEE's
            # supplicant (node_tas false), else none
            if row["tee"]["clients"]:
                module = targets.tee_module(targets.by_name(target)) is not None
                bare = row["tee"].get("node_tas") is False
                self.assertFalse(module and bare, target)
                self.assertEqual(row["tee"].get("node", True), module or bare, target)
                self.assertEqual(
                    bare,
                    not module and f"rootfs.pak:/{targets.TEE_CLIENT}" in row["tee"]["clients"],
                    target,
                )
            # every Realtek firmware from webOS 4 on has a client library (others may: LM21A's; the
            # k2l, k2lp and k3lp of webOS 3 have none, as their rows' evidence says)
            if target.startswith("k") and not target.endswith(("-dreadlocks", "-dreadlocks2")):
                self.assertIsNotNone(plan, target)
            if plan:
                self.assertRegex(plan[0], r"^/usr/lib/lib[^/]+\.so[.0-9]*$", target)
        self.assertEqual(
            tvsuite.tee_plan(table["k7lp-kisscurl"], "k7lp-kisscurl")[0],
            "/usr/lib/libkadaptor.so.1.0.0",
        )


class AudioTest(unittest.TestCase):
    ROW = dict(
        service=tvsuite.AUDIO_SERVICE,
        api=dict(present=True, source="test"),
        webaudio=dict(present=True, source="test"),
        aplay=dict(present=True, source="test"),
        alsa_hw=dict(present=True, device="hw:0,6", source="test"),
    )

    def wav(self, parts):
        """A recording like QEMU's wavcapture: (Hz or 0 for silence, seconds, dBFS peak) parts."""
        import math
        import struct
        import tempfile
        import wave

        f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        f.close()
        self.addCleanup(os.unlink, f.name)
        frames = bytearray()
        for hz, sec, db in parts:
            a = 32767 * 10 ** (db / 20)
            for i in range(int(sec * 44100)):
                s = int(a * math.sin(2 * math.pi * hz * i / 44100)) if hz else 0
                frames += struct.pack("<hh", s, s)
        with wave.open(f.name, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(bytes(frames))
        return f.name

    def facts(self, vols=(10, 5, 5, 10), muted=(False, False, True, False), alsa=("rc=0", "rc=0")):
        names = [n for n, _ in tvsuite.AUDIO_PHASES]
        lines = [
            "pulse=100",
            'warm.down={"returnValue":true}',
            'warm.up={"returnValue":true}',
            "play.aplay=" + alsa[0],
            "hw.suspend_wait=3",
            "play.hw=" + alsa[1],
        ]
        for n, v, m in zip(names, vols, muted):
            lines.append(
                'vol.%s={"returnValue":true,"volume":%d,"muted":%s}'
                % (n, v, "true" if m else "false")
            )
            lines.append("play.%s=rc=0" % n)
        lines += ['down.%d={"returnValue":true}' % i for i in range(tvsuite.AUDIO_STEPS)]
        return "\n".join(lines + ["TV FACTS END"])

    def test_tones_and_judge(self):
        hz = dict(tvsuite.AUDIO_PHASES)
        alsa = dict(tvsuite.ALSA_TONES)
        rec = self.wav(
            [
                (hz["native"], 2, -47),
                (0, 1, 0),
                (alsa["aplay"], 2, -47.3),
                (0, 1, 0),
                (alsa["hw"], 2, -30),
                (0, 1, 0),
                (hz["down"], 2, -62),
                (0, 2, 0),
                (hz["restored"], 2, -47),
            ]
        )
        tf = tvsuite.tone_facts(
            rec, [f for _, f in tvsuite.AUDIO_PHASES + tvsuite.ALSA_TONES] + [tvsuite.WEBAUDIO_HZ]
        )
        self.assertAlmostEqual(tf["tones"][str(hz["native"])]["dbfs"], -50.0, delta=0.1)
        self.assertEqual(tf["tones"][str(hz["muted"])]["seconds"], 0)
        web = self.wav([(tvsuite.WEBAUDIO_HZ, 2, -47)])
        wf = tvsuite.tone_facts(web, [tvsuite.WEBAUDIO_HZ])
        items = tvsuite.judge_audio((self.facts(), tf), ("TV FACTS END", wf), self.ROW)
        self.assertEqual(
            {i: s for i, s, _ in items},
            {
                "audio.native": "PASS",
                "audio.readback": "PASS",
                "audio.volumeDown": "PASS",
                "audio.muted": "PASS",
                "audio.restored": "PASS",
                "audio.aplay": "PASS",
                "audio.alsa-hw": "PASS",
                "audio.webaudio": "PASS",
            },
        )

    def test_alsa(self):
        """aplay to default at paplay's level, aplay to the hw PCM heard; a busy PCM, a quieter
        default, a missing row and an absent route."""
        hz, alsa = dict(tvsuite.AUDIO_PHASES), dict(tvsuite.ALSA_TONES)
        freqs = [f for _, f in tvsuite.AUDIO_PHASES + tvsuite.ALSA_TONES]
        rec = self.wav([(hz["native"], 2, -47), (0, 1, 0), (alsa["aplay"], 2, -52)])
        tf = tvsuite.tone_facts(rec, freqs)
        busy = "aplay: main:828: audio open error: Device or resource busy rc=1"
        items = {
            i: (s, w)
            for i, s, w in tvsuite.judge_audio(
                (self.facts(alsa=("rc=0", busy)), tf), None, self.ROW
            )
        }
        self.assertEqual(items["audio.aplay"][0], "FAIL")
        self.assertRegex(items["audio.aplay"][1], r"-5\.0\d dB from paplay")
        self.assertEqual(items["audio.alsa-hw"][0], "FAIL")
        self.assertIn("busy", items["audio.alsa-hw"][1])
        self.assertIn("3 s waiting", items["audio.alsa-hw"][1])
        # returned 0 but nothing reached the card
        items = dict((i, s) for i, s, _ in tvsuite.judge_audio((self.facts(), tf), None, self.ROW))
        self.assertEqual(items["audio.alsa-hw"], "FAIL")
        row = dict(self.ROW, aplay=dict(present=False, source="no usr/bin/aplay"))
        del row["alsa_hw"]
        items = {i: (s, w) for i, s, w in tvsuite.judge_audio((self.facts(), tf), None, row)}
        self.assertEqual(items["audio.aplay"], ("SKIP", "no usr/bin/aplay"))
        self.assertEqual(items["audio.alsa-hw"][0], "FAIL")

    def test_failures(self):
        hz = dict(tvsuite.AUDIO_PHASES)
        # no volume after the PCM: every phase at the same level, the muted one audible
        rec = self.wav(
            [
                (hz["native"], 2, -12),
                (0, 1, 0),
                (hz["down"], 2, -12),
                (0, 1, 0),
                (hz["muted"], 2, -12),
                (0, 1, 0),
                (hz["restored"], 2, -12),
            ]
        )
        tf = tvsuite.tone_facts(rec, [f for _, f in tvsuite.AUDIO_PHASES])
        items = dict(
            (i, s)
            for i, s, _ in tvsuite.judge_audio(
                (self.facts(vols=(10, 10, 10, 10)), tf), None, self.ROW
            )
        )
        self.assertEqual(items["audio.volumeDown"], "FAIL")
        self.assertEqual(items["audio.muted"], "FAIL")
        self.assertEqual(items["audio.readback"], "FAIL")
        self.assertEqual(items["audio.restored"], "PASS")
        bad = tvsuite.judge_audio((self.facts(), dict(error="no tap")), None, self.ROW)
        self.assertTrue(all(s == "FAIL" for _, s, _ in bad))

    def test_webaudio_level(self):
        """A WebAudio stream on a sink with its own policy volume (W21P's pfeedback, -8.08 dB) is
        judged against paplay's tone plus that level."""
        web = self.wav([(tvsuite.WEBAUDIO_HZ, 2, -55.1)])
        wf = tvsuite.tone_facts(web, [tvsuite.WEBAUDIO_HZ])
        row = dict(
            self.ROW,
            webaudio=dict(present=True, source="test", level_db=-8.08, level_source="test"),
        )
        ((_, st, why),) = tvsuite.judge_webaudio(("TV FACTS END", wf), row, -50.0)
        self.assertEqual(st, "PASS", why)
        self.assertIn("expected -8.08", why)
        ((_, st, _),) = tvsuite.judge_webaudio(("TV FACTS END", wf), self.ROW, -50.0)
        self.assertEqual(st, "FAIL")

    def test_master_readback(self):
        """webOS 5 on: the read-back is master/getVolume's volumeStatus."""
        a = tvsuite.answer(
            '{"volumeStatus":{"volume":10,"muteStatus":true,"soundOutput":"tv_speaker"},'
            '"returnValue":true}'
        )
        self.assertEqual(tvsuite.volume_of(a), (10, True))
        self.assertEqual(tvsuite.volume_of({"volume": 5, "muted": False}), (5, False))

    def test_skips(self):
        row = dict(
            api=dict(present=False, source="no audiod"),
            webaudio=dict(present=False, source="no engine"),
        )
        items = tvsuite.judge_audio(None, ("", None), row)
        self.assertTrue(all(s == "SKIP" for _, s, _ in items))
        self.assertIn("audio.webaudio", [i for i, _, _ in items])

    def test_expect_table_audio(self):
        """Every target's row has the Audio API and WebAudio with their evidence."""
        table = expect_table()
        for target, row in table.items():
            self.assertEqual(row["audio"]["service"], tvsuite.AUDIO_SERVICE, target)
            for k in ("api", "webaudio", "aplay", "alsa_hw"):
                self.assertTrue(row["audio"][k]["source"], target)
            web = row["audio"]["webaudio"]
            if web["present"]:
                self.assertTrue(web["level_source"], target)
                # 0 dB as pcm_output, or the policy level of the device the engine opens
                self.assertLessEqual(web["level_db"], 0.0, target)
                self.assertEqual(
                    web["level_db"] == 0.0,
                    "at 0 dB" in web["level_source"]
                    or "audiod's policy level" not in web["level_source"],
                    target,
                )
            if row["audio"]["alsa_hw"]["present"]:
                self.assertRegex(row["audio"]["alsa_hw"]["device"], r"^hw:\d+,\d+$", target)
        self.assertEqual(table["k7lp-kisscurl"]["audio"]["webaudio"]["level_db"], -8.08)


CLIP = tvsuite.DECODE_CLIP


def decode_dir(
    d,
    codec="h264",
    shown=int(CLIP * 25),
    pts=(0.0, CLIP - 0.04),
    eos=True,
    colours=True,
    audio=True,
    window=True,
    whole=True,
    quiet=False,
):
    """A decode case's viewer directory as decode_viewer leaves it: viewer.log, captures, media.wav."""
    import math
    import struct
    from PIL import Image

    helper = [
        "media 0: open video vdecsink (pid 9, channel 0, pad 0)",
        f"media 0: caps (video) video/x-{codec}, stream-format=avc",
        f"media 0: decoder {codec} (software)",
        f"decode 0 video session=1 (closed): {codec} sw packets={int(CLIP * 25)} decoded={int(CLIP * 25)} dropped=0 errors=0 decode=1.00 ms/packet",
    ]
    lines = [
        f"present 0 video session=1 (closed): received={shown} shown={shown} picked={shown} late_drops=0 lateness avg=1.0 max=2.0 ms pts={pts[0]:.3f}..{pts[1]:.3f} s"
    ]
    if eos:
        helper.append("media 0: end of stream (video)")
    if audio:
        helper += [
            "media 1: open audio adecsink (pid 9, channel 0, pad 0)",
            "media 1: decoder aac (software)",
            "decode 1 audio session=1 (closed): aac sw packets=900 decoded=900 dropped=0 errors=0 decode=0.01 ms/packet",
        ]
        lines.append(
            f"present 1 audio session=1 (closed): frames={int(CLIP * 48000)} slip avg=1.0 max=2.0 ms resyncs=0 nudges=0 underruns=0"
        )
    lines.append(
        f"video_packets={20 if window else 0} video_changes=2 video_draws={300 if window else 0} "
        f"video_pictures={300 if window else 0}"
    )
    for filename, contents in (("viewer.log", lines), ("media-helper.log", helper)):
        with open(os.path.join(d, filename), "w") as f:
            f.write("\n".join(contents) + "\n")
    for i in range(6):
        im = Image.new("RGB", (960, 540), (32, 48, 64))
        if colours:
            # the clip's picture at the page's video rect (160,90 480x270 of a 960x540 capture):
            # testsrc2's six bars and the label box, its text moving; not whole: 1.5 times too large
            # from 40,23 into the rect, as a scaler window in the wrong place draws it
            pic = Image.new("RGB", (480, 270))
            for k, c in enumerate(
                ((255, 0, 0), (0, 255, 0), (255, 255, 0), (0, 0, 255), (255, 0, 255), (0, 255, 255))
            ):
                pic.paste(c, (k * 80, 0, (k + 1) * 80, 270))
            pic.paste((0, 0, 0), (100, 232, 380, 254))
            pic.paste((255, 255, 255), (120 + i * 10, 238, 200 + i * 10, 248))
            if not whole:
                pic = pic.resize((720, 405)).crop((-40, -23, 440, 247))
            im.paste(pic, (160, 90))
        im.save(os.path.join(d, f"shot-{i * 1000:06d}.ppm"))
    n = 48000 * 2
    with open(os.path.join(d, "media.wav"), "wb") as f:
        f.write(b"\0" * 44)
        f.write(
            struct.pack(
                "<%df" % (2 * n),
                *[
                    (0.002 if quiet else 0.3) * math.sin(i / 7.0) if audio else 0.0
                    for i in range(2 * n)
                ],
            )
        )


class SoundGainTest(unittest.TestCase):
    """The decode check's audio floor follows the TV's gain in sound-status (Q16, 65536 unity)."""

    def test_parse(self):
        ss = tvsuite.sound_status
        self.assertEqual(ss("level=0x1 mute=0 gain=2117\n"), (2117, False))
        self.assertEqual(ss("gain=10 mute=0\nlevel=0x2 mute=1 gain=0\n"), (0, True))
        self.assertEqual(ss("gain=99999\n"), (65536, False))
        self.assertEqual(ss("gain=5\ngain=7"), (5, False))  # the last line is not whole
        self.assertIsNone(ss(""))
        self.assertIsNone(ss(None))

    def test_unity(self):
        ok, why = tvsuite.judge_media_level(-30.0, (65536, False))
        self.assertTrue(ok, why)
        self.assertFalse(tvsuite.judge_media_level(-45.0, (65536, False))[0])
        self.assertFalse(tvsuite.judge_media_level(None, None)[0])

    def test_tv_volume(self):
        ok, why = tvsuite.judge_media_level(-53.3, (2117, False))  # -29.8 dB
        self.assertTrue(ok, why)
        self.assertIn("gain 2117", why)
        self.assertFalse(tvsuite.judge_media_level(-71.0, (2117, False))[0])

    def test_mute(self):
        ok, why = tvsuite.judge_media_level(None, (2117, True))
        self.assertFalse(ok)
        self.assertIn("muted", why)
        self.assertFalse(tvsuite.judge_media_level(-20.0, (0, False))[0])
        self.assertIn("yet -20.0", tvsuite.judge_media_level(-20.0, (0, True))[1])

    def test_decode_reads_the_file(self):
        import tempfile

        page = tvsuite.page_facts(PAGE)
        for status, verdict, text in (
            (None, "FAIL", "-inf"),  # no file: unity; set below to a quiet recording
            ("level=0x1 mute=0 gain=2117\n", "PASS", "gain 2117"),
            ("level=0x1 mute=1 gain=0\n", "FAIL", "muted"),
        ):
            with tempfile.TemporaryDirectory() as d:
                decode_dir(d, quiet=status is None or "gain=2117" in status)
                if status:
                    with open(os.path.join(d, "sound-status"), "w") as f:
                        f.write(status)
                m = tvsuite.media_facts(d)
                item = tvsuite.judge_decode("file.h264", "file", "h264", "aac", {}, page, m)
            if status is None:
                self.assertEqual(m["gain"], 65536)
                self.assertEqual(item[1], "FAIL")
                self.assertIn("recorded media audio", item[2])
            else:
                self.assertEqual(item[1], verdict, item)
                self.assertIn(text, item[2])


PAGE = """page=start file media/file-h264.mp4
page=canPlayType "video/mp4; codecs=\\"avc1.4d401f, mp4a.40.2\\"" = "probably"
page=event loadedmetadata t=0.000 size=960x540 duration=8
page=event playing t=0.000
page=time t=1.010
page=time t=19.980
page=event ended t=20.000
page=end ended top=20.000 t=20.000 paused=false ended=true error=none events=playing@0.0,ended@20.0
crashes=
TV FACTS END
"""


class DecodeTest(unittest.TestCase):
    def facts(self, **kw):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            decode_dir(d, **kw)
            return tvsuite.media_facts(d)

    def test_pass(self):
        m = self.facts()
        self.assertEqual(m["pictures"], 6)
        self.assertEqual(m["moving"], 5)
        self.assertEqual(m["whole"], 6)
        self.assertGreater(m["wav_db"], -40)
        page = tvsuite.page_facts(PAGE)
        self.assertEqual(page["end"], "ended")
        self.assertEqual(
            page["canplay"], {'video/mp4; codecs="avc1.4d401f, mp4a.40.2"': "probably"}
        )
        item = tvsuite.judge_decode("file.h264", "file", "h264", "aac", {}, page, m)
        self.assertEqual(item[1], "PASS", item)

    def test_failures(self):
        page = tvsuite.page_facts(PAGE)
        for kw, why in (
            (dict(codec="hevc"), "no h264 video decoded"),
            (dict(shown=40), "40 of 500 pictures"),
            (dict(pts=(0.0, 3.0)), "pts span"),
            (dict(eos=False), "no video end of stream"),
            (dict(colours=False), "captures: 0 of 6"),
            (dict(audio=False), "no aac audio"),
            (dict(whole=False), "captures: 0 with the whole picture"),
            (dict(window=False), "0 video window message(s)"),
        ):
            item = tvsuite.judge_decode(
                "file.h264", "file", "h264", "aac", {}, page, self.facts(**kw)
            )
            self.assertEqual(item[1], "FAIL", kw)
            self.assertIn(why, item[2], kw)
        bad = tvsuite.page_facts(
            PAGE.replace("page=event ended t=20.000\n", "page=event error t=2.000 code=4\n")
            .replace("page=time t=19.980\n", "")
            .replace("top=20.000", "top=2.000")
        )
        item = tvsuite.judge_decode("file.h264", "file", "h264", "aac", {}, bad, self.facts())
        self.assertEqual(item[1], "FAIL")
        for why in ("media error error(4)", "no ended event", "currentTime reached 2.0 s"):
            self.assertIn(why, item[2])
        mse = tvsuite.page_facts(
            PAGE.replace("page=start file", "page=mse api none\npage=start file")
        )
        item = tvsuite.judge_decode("mse.h264", "mse", "h264", "aac", {}, mse, self.facts())
        self.assertIn("no MSE api", item[2])
        item = tvsuite.judge_decode(
            "file.h264", "file", "h264", "aac", {}, page, dict(error="no viewer log")
        )
        self.assertEqual(item[1], "FAIL")

    def test_port_reuse_joins_on_session(self):
        """H.264 then HEVC on the same port: the HEVC stream's presentation must not be credited to
        an H.264 stream that showed nothing (the reports join on kind, port and session)."""
        import tempfile

        page = tvsuite.page_facts(PAGE)
        with tempfile.TemporaryDirectory() as d:
            decode_dir(d)
            full = int(CLIP * 25)
            with open(os.path.join(d, "media-helper.log"), "a") as f:
                f.write(
                    "media 0: open video vdecsink (pid 9, channel 0, pad 0)\nmedia 0: decoder hevc (software)\n"
                    f"decode 0 video session=3 (closed): hevc sw packets={full} decoded={full} dropped=0 "
                    "errors=0 decode=1.00 ms/packet\n"
                )
            with open(os.path.join(d, "viewer.log")) as f:
                log = f.read()
            # the H.264 stream (session 1) showed nothing; the HEVC one (session 3) showed everything
            log = log.replace(
                f"received={full} shown={full} picked={full}", "received=0 shown=0 picked=0", 1
            )
            log += (
                f"present 0 video session=3 (closed): received={full} shown={full} picked={full} late_drops=0 "
                f"lateness avg=1.0 max=2.0 ms pts=0.000..{CLIP - 0.04:.3f} s\n"
            )
            with open(os.path.join(d, "viewer.log"), "w") as f:
                f.write(log)
            m = tvsuite.media_facts(d)
        rows = {v["codec"]: v for v in m["video"]}
        self.assertEqual((rows["h264"]["shown"], rows["hevc"]["shown"]), (0, full))
        item = tvsuite.judge_decode("file.h264", "file", "h264", "aac", {}, page, m)
        self.assertEqual(item[1], "FAIL", item)
        self.assertIn(f"0 of {full} pictures", item[2])

    def test_beacons(self):
        served = "\n".join(
            f'REQUEST 127.0.0.1 "GET /tok/file.h264/{n}/{q} HTTP/1.1" 404 -'
            for n, q in (
                (1, "event%20playing%20t%3D0.000"),
                (0, "start%20file%20media%2Ffile-h264.mp4"),
                (2, "end%20ended%20top%3D20.000"),
            )
        )
        served += '\nREQUEST 127.0.0.1 "GET /tok/file.hevc/0/start HTTP/1.1" 404 -'
        self.assertEqual(
            tvsuite.beacon_lines(served, "tok", "file.h264"),
            ["start file media/file-h264.mp4", "event playing t=0.000", "end ended top=20.000"],
        )
        self.assertEqual(tvsuite.media_facts("/nonexistent")["error"][:14], "no viewer log:")

    def test_codec_claims(self):
        """The generator's evidence: gst-omx's sections and the capability table, or LG's pad caps."""

        class Fw:
            def __init__(self, files):
                self.f = files

            def find(self, path, paks=None):
                return ("rootfs.pak", path) if path in self.f else None

            def read(self, pak, path):
                return self.f[path]

        omx = Fw(
            {
                gen.GSTOMX_CONF: b"[omxh264dec]\nx=1\n[omxh265dec]\n[omxaacdec]\n[omxmpegvideodec]\n",
                gen.CODEC_JSON: json.dumps(
                    {
                        "videoCodecs": [{"name": "H.264"}, {"name": "MPEG2"}],
                        "audioCodecs": [{"name": "AAC"}],
                    }
                ).encode(),
                "usr/lib/gstreamer-1.0/libgstisomp4.so": b"",
                "usr/lib/gstreamer-1.0/libgstmpegtsdemux.so": b"",
            }
        )
        c = gen.codec_claims(omx)
        self.assertTrue(c["h264"][0])
        self.assertFalse(c["hevc"][0])
        self.assertIn("lists no HEVC or H.265", c["hevc"][1])
        self.assertFalse(c["vp9"][0])
        self.assertIn("no [omxvp9dec]", c["vp9"][1])
        lg = Fw(
            {
                "usr/lib/gstreamer-1.0/libgstlxvdecbin-1.0.so": b"\0video/x-h264;video/x-h265;video/mpeg, systemstream;",
                "usr/lib/gstreamer-1.0/libgstadecsink-1.0.so": b"\0audio/mpeg; audio/x-ac3",
                "usr/lib/gstreamer-1.0/libgstisomp4.so": b"",
                "usr/lib/gstreamer-1.0/libgstmatroska.so": b"",
            }
        )
        c = gen.codec_claims(lg)
        self.assertEqual(
            {k: v[0] for k, v in c.items()},
            dict(
                h264=True,
                hevc=True,
                vp9=False,
                mpeg2video=True,
                aac=True,
                libdav1d=False,
                opus=False,
            ),
        )
        row = gen.decode_of(lg, {"mse.h264": (True, "engine")})
        self.assertTrue(row["file.h264"]["present"])
        self.assertFalse(row["file.vp9"]["present"])
        self.assertFalse(row["file.mpeg2"]["present"])  # no tsdemux
        self.assertIn("libgstmpegtsdemux.so", row["file.mpeg2"]["source"])
        self.assertTrue(row["mse.h264"]["present"])
        self.assertFalse(row["mse.hevc"]["present"])
        self.assertEqual(set(row), {c for c, *_ in tvsuite.DECODE_CASES})
        # Opus and AV1 (o22's lxvideodec and lgaudiodec; W16R's gstomx.conf has neither)
        o22 = Fw(
            {
                "usr/lib/gstreamer-1.0/libgstlxvideodec.so": b"\0video/x-vp9;video/x-av1;",
                "usr/lib/gstreamer-1.0/libgstlgaudiodec.so": b"\0audio/mpeg;audio/x-opus;",
                gen.CODEC_JSON: json.dumps(
                    {
                        "videoCodecs": [{"name": "VP9"}, {"name": "AV1"}],
                        "audioCodecs": [{"name": "OPUS"}],
                    }
                ).encode(),
                "usr/lib/gstreamer-1.0/libgstmatroska.so": b"",
            }
        )
        c = gen.codec_claims(o22)
        self.assertTrue(c["libdav1d"][0] and c["opus"][0])
        self.assertIn("lists OPUS", c["opus"][1])
        self.assertFalse(c["aac"][0])  # no AAC in this table
        row = gen.decode_of(o22, {"mse.av1": (True, "engine")})
        self.assertTrue(row["file.av1"]["present"] and row["file.vp9-opus"]["present"])
        self.assertTrue(row["mse.av1"]["present"])
        self.assertFalse(row["mse.vp9-opus"]["present"])  # no MSE evidence given
        m2 = Fw({gen.GSTOMX_CONF: b"[omxvp9dec]\n[omxaacdec]\n"})
        c = gen.codec_claims(m2)
        self.assertFalse(c["opus"][0])
        self.assertIn("no [omxopusdec]", c["opus"][1])

    class EngineFw:
        def __init__(self, data, engine):
            self.data, self.engine = data, engine

        def find(self, path, paks=None):
            return ("rootfs.pak", path) if path == self.engine else None

        def read(self, pak, path):
            return self.data

    def test_mse_claims_webkit(self):
        """QtWebKit: each source buffer's MIME type and codec name must be the engine's strings."""
        qt = gen.WEB_ENGINES[-1]
        both = b"MediaSource addSourceBuffer video/webm vp09 av01 audio/webm opus video/mp4 avc1"
        c = gen.mse_of(self.EngineFw(both, qt))
        self.assertTrue(c["mse.av1"][0] and c["mse.vp9-opus"][0] and c["mse.h264"][0])
        self.assertIn("video/webm, av01, audio/webm, opus", c["mse.av1"][1])
        c = gen.mse_of(self.EngineFw(both.replace(b"av01", b""), qt))
        self.assertFalse(c["mse.av1"][0])
        self.assertIn("names no av01 codec string", c["mse.av1"][1])
        c = gen.mse_of(self.EngineFw(both.replace(b"audio/webm", b""), qt))
        self.assertFalse(c["mse.vp9-opus"][0])
        self.assertTrue(c["mse.vp9"][0])

    def test_mse_claims_chromium(self):
        """Chromium: each source buffer's type must be in StreamParserFactory's table and one of
        its codec patterns must match the buffer's codec string."""
        table = {
            "video/webm": ["vp8", "vp9", "vp09.*", "vorbis", "opus", "av01.*"],
            "audio/webm": ["vorbis", "opus"],
            "video/mp4": ["avc1.*", "hvc1.*"],
            "audio/mp4": ["mp4a.40.*"],
        }
        real = gen.mse_parsers
        try:
            gen.mse_parsers = lambda data: table
            fw = self.EngineFw(b"MediaSource addSourceBuffer", gen.CBE[0])
            c = gen.mse_of(fw)
            self.assertTrue(all(ok for ok, _ in c.values()), c)
            self.assertIn("video/webm av01.0.04M.08 (av01.*)", c["mse.av1"][1])
            del table["audio/webm"]  # webOS 4.5: video/webm takes opus, audio/webm is no type
            c = gen.mse_of(fw)
            self.assertFalse(c["mse.vp9-opus"][0])
            self.assertIn("take no audio/webm", c["mse.vp9-opus"][1])
            self.assertTrue(c["mse.vp9"][0])
            # webOS 5: AV1 only for an app that opts in, which the test app does not
            c = gen.mse_of(
                self.EngineFw(b"MediaSource addSourceBuffer mse_enable_av1=%d", gen.CBE[0])
            )
            self.assertFalse(c["mse.av1"][0])
            self.assertIn("mse.enableAV1", c["mse.av1"][1])
            self.assertTrue(c["mse.vp9"][0])
            pref = b"MediaSource addSourceBuffer mse_enable_av1=%d : is_av1_decoder_enabled_="
            c = gen.mse_of(self.EngineFw(pref, gen.CBE[0]))  # no audio/webm here: that reason only
            self.assertIn("take no audio/webm", c["mse.av1"][1])
            table["video/webm"].remove("av01.*")
            self.assertIn("takes no av01.0.04M.08", gen.mse_of(fw)["mse.av1"][1])
            gen.mse_parsers = lambda data: None
            with self.assertRaises(SystemExit):
                gen.mse_of(fw)
        finally:
            gen.mse_parsers = real

    def test_mse_buffers_are_the_clips(self):
        """MSE_BUFFERS are the MIME types make-clips.sh --decode gives the page's source buffers."""
        with open(os.path.join(REGRESS, "video", "make-clips.sh")) as f:
            text = f.read()
        names = dict(re.findall(r"\b(AVC|AAC|HVC|AV01)='([^']+)'", text))
        mimes, case = {}, None
        for line in text.replace("\\", "").splitlines():
            c = re.search(r'"((?:file|mse)\.[\w-]+)":', line)
            case = c.group(1) if c else case
            m = re.search(r'"mime": "(\w+/\w+); codecs="([^"]+)"', line)
            if m and case and case.startswith("mse."):
                codec = re.sub(r"\$(\w+)", lambda x: names[x.group(1)], m.group(2))
                mimes.setdefault(case, []).append((m.group(1), codec))
        self.assertEqual(mimes, {k: list(v) for k, v in gen.MSE_BUFFERS.items()})

    def test_replace_tables(self):
        """--table decode rewrites the [decode] table of the existing file and nothing else."""
        import tempfile

        text = 'firmware = "FW"\n\n[audio]\nx = 1\n\n[decode]\n"old" = 1\n\n[smp]\npresent = true\n'
        real = gen.TABLE_ROWS["decode"]
        try:
            gen.TABLE_ROWS["decode"] = lambda fw: {"mse.h264": {"present": False, "source": "s"}}
            with tempfile.TemporaryDirectory() as d:
                with open(os.path.join(d, "t.toml"), "w") as f:
                    f.write(text)
                gen.replace_tables(d, "t", "FW", None, ["decode"])
                with open(os.path.join(d, "t.toml")) as f:
                    self.assertEqual(
                        f.read(),
                        text.replace('"old" = 1', '"mse.h264" = { present = false, source = "s" }'),
                    )
                with self.assertRaises(SystemExit):
                    gen.replace_tables(d, "t", "OTHER", None, ["decode"])
        finally:
            gen.TABLE_ROWS["decode"] = real

    def test_mse_parsers_firmware(self):
        """StreamParserFactory tables of webOS 4.5, 6 and 24 engines, where the firmware is here."""
        sys.path.insert(
            0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "host", "core")
        )
        from beanjar import paths

        fws = {
            n: paths.find_firmware(n) or ""
            for n in ("05.40.97.01-HE_DTV_W19P_AFADABAA", "03.70.25.01-HE_DTV_W21P_AFADATAA")
        }
        if not all(os.path.isfile(os.path.join(d, "rootfs.pak")) for d in fws.values()):
            self.skipTest("no W19P or W21P firmware here")
        w19p = gen.mse_parsers(
            gen.Firmware(fws["05.40.97.01-HE_DTV_W19P_AFADABAA"]).read(
                "rootfs.pak", "usr/lib/libcbe.so"
            )
        )
        self.assertNotIn("audio/webm", w19p)
        self.assertIn("opus", w19p["video/webm"])
        self.assertIn("mp4a.40.*", w19p["audio/mp4"])
        w21p = gen.mse_parsers(
            gen.Firmware(fws["03.70.25.01-HE_DTV_W21P_AFADATAA"]).read(
                "rootfs.pak", "usr/lib/libcbe.so"
            )
        )
        self.assertIn("opus", w21p["audio/webm"])
        self.assertIn("av01.*", w21p["video/webm"])
        w24g = paths.find_firmware("02.68.80.01-HE_DTV_W24G_AFABATAA") or ""
        if os.path.isfile(os.path.join(w24g, "rootfs.pak")):  # string views and spans
            cbe = "usr/palm/services/com.palm.webruntime/lib/libcbe.so"
            w24g = gen.mse_parsers(gen.Firmware(w24g).read("rootfs.pak", cbe))
            self.assertEqual(w24g["audio/webm"], ["vorbis", "opus", "Opus"])
            self.assertIn("av01.*", w24g["video/webm"])

    def test_slice_prune_sheet(self):
        import tempfile
        from PIL import Image

        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "all.wav")
            with open(src, "wb") as f:
                f.write(b"\0" * 44 + b"A" * 80 + b"B" * 83)
            tvsuite.slice_file(src, 44 + 80, os.path.join(d, "case.wav"))
            with open(os.path.join(d, "case.wav"), "rb") as f:
                data = f.read()
            self.assertEqual((data[:4], data[44:]), (b"RIFF", b"B" * 80))  # whole 8-byte frames
            tvsuite.slice_file(os.path.join(d, "missing.log"), 0, os.path.join(d, "x.log"))
            self.assertEqual(os.path.getsize(os.path.join(d, "x.log")), 0)
            # the helper's log, rotated since the mark: the rest of .1, then the new file
            log = os.path.join(d, "media-helper.log")
            with open(log, "w") as f:
                f.write("before\nmarked\n")
            tvsuite.slice_file(log, 7, os.path.join(d, "same.log"))
            with open(os.path.join(d, "same.log")) as f:
                self.assertEqual(f.read(), "marked\n")
            os.rename(log, log + ".1")
            with open(log, "w") as f:
                f.write("after\n")
            tvsuite.slice_file(log, 7, os.path.join(d, "rotated.log"))
            with open(os.path.join(d, "rotated.log")) as f:
                self.assertEqual(f.read(), "marked\nafter\n")
            case = os.path.join(d, "h13-afro", "decode", "file.h264")
            decode_dir(case if os.makedirs(case) is None else case)
            tvsuite.prune_captures(case, at_ms=2000)
            self.assertEqual(
                sorted(f for f in os.listdir(case) if "shot" in f or "sheet" in f),
                ["sheet-002000.png"],
            )
            sheet = tvsuite.decode_sheet(d)
            self.assertEqual(Image.open(sheet).size, (4 * 480, 270 + 24))

    def test_plan(self):
        row = {
            "file.vp9": dict(present=False, source="no video/x-vp9 in the sink's caps"),
            "file.h264": dict(present=True, source="caps"),
        }
        run, items = tvsuite.decode_plan(row, "graphics")
        self.assertEqual(
            [c for c, *_ in run], [c for c, *_ in tvsuite.DECODE_CASES if c != "file.vp9"]
        )
        self.assertEqual(items, [("decode.file.vp9", "SKIP", "no video/x-vp9 in the sink's caps")])
        run, items = tvsuite.decode_plan(row, "headless")
        self.assertEqual(run, [])
        self.assertEqual({s for _, s, _ in items}, {"SKIP"})
        self.assertEqual(len(items), len(tvsuite.DECODE_CASES))


class SmpTest(unittest.TestCase):
    NEEDS = [("A",), ("B", "B2"), ("C",)]

    def test_needs_from_the_app(self):
        """The library and the lookups come from tests/regress/smp/smpcheck.c itself."""
        lib, needs = tvsuite.smp_needs()
        self.assertEqual(lib, "libplayerAPIs.so.1")
        self.assertIn(("_ZN17StarfishMediaAPIsC1EPKc",), needs)
        self.assertIn(
            ("_ZN17StarfishMediaAPIs4FeedEPKc", "_ZN17StarfishMediaAPIs4FeedB5cxx11EPKc"), needs
        )
        self.assertEqual(len(needs), 8)

    def test_missing(self):
        self.assertEqual(tvsuite.smp_missing({"A", "B2", "C"}, self.NEEDS), [])
        self.assertEqual(tvsuite.smp_missing({"B"}, self.NEEDS), ["A", "C"])

    def test_plan(self):
        """SKIP only where the row says absent; a firmware with the API, or no row, runs."""
        self.assertEqual(tvsuite.smp_plan({"smp": dict(present=True, source="x")}), [])
        self.assertEqual(tvsuite.smp_plan({}), [])
        ((check, status, why),) = tvsuite.smp_plan(
            {"smp": dict(present=False, source="lib exports no A")}
        )
        self.assertEqual((check, status), ("smp.frameready", "SKIP"))
        self.assertIn("lib exports no A", why)

    def test_missing_symbol_fails_where_the_api_is(self):
        """Run (the row says present), the app's own "no symbol" is a FAIL."""
        check, status, why = tvsuite.judge_smp("smp=no symbol _ZN17StarfishMediaAPIsC1EPKc\n", {})
        self.assertEqual(status, "FAIL")
        self.assertIn("no symbol", why)

    def test_smp_of(self):
        """The generator's row from the library's exported names (imports do not count)."""

        class Fw:
            paks = ["rootfs.pak"]

            def __init__(self, files):
                self.files_ = files

            def find(self, path):
                return ("rootfs.pak", path + ".1.0.0") if path in self.files_ else None

            def read(self, pak, path):
                return b"elf"

        real_exports, real_needs = gen.exports, tvsuite.smp_needs
        tvsuite.smp_needs = lambda: ("libplayerAPIs.so.1", self.NEEDS)
        try:
            gen.exports = lambda data: {"A", "B2"}
            row = gen.smp_of(Fw({"usr/lib/libplayerAPIs.so.1"}))
            self.assertEqual((row["present"], row["missing"]), (False, ["C"]))
            self.assertIn(
                "rootfs.pak:/usr/lib/libplayerAPIs.so.1.1.0.0 exports no C", row["source"]
            )
            gen.exports = lambda data: {"A", "B", "C"}
            self.assertTrue(gen.smp_of(Fw({"usr/lib/libplayerAPIs.so.1"}))["present"])
            gen.exports = lambda data: None  # unreadable: no evidence of absence
            self.assertTrue(gen.smp_of(Fw({"usr/lib/libplayerAPIs.so.1"}))["present"])
            self.assertFalse(gen.smp_of(Fw(set()))["present"])
        finally:
            gen.exports, tvsuite.smp_needs = real_exports, real_needs

    def test_exports_defined_only(self):
        libc = next(
            (
                p
                for p in ("/lib/x86_64-linux-gnu/libc.so.6", "/lib/aarch64-linux-gnu/libc.so.6")
                if os.path.exists(p)
            ),
            None,
        )
        if not libc:
            self.skipTest("no libc to read")
        with open(libc, "rb") as f:
            names = gen.exports(f.read())
        self.assertIn("malloc", names)
        self.assertNotIn("_dl_argv", names)  # ld.so's, imported
        self.assertIsNone(gen.exports(b"not an elf"))

    def test_expect_table_smp(self):
        """webOS 1 (no StarfishMediaAPIs(const char *)) and 2 (Load without user data) lack the API."""
        lacking = ("h13-afro", "m14-beehive", "lm15u-beehive", "mtka5lr-beehive")
        table = expect_table()
        for target, row in table.items():
            self.assertEqual(row["smp"]["present"], target not in lacking, target)
        self.assertEqual(
            table["m14-beehive"]["smp"]["missing"],
            ["_ZN17StarfishMediaAPIs4LoadEPKcPFvixS1_PvES2_"],
        )

    def test_nyx_rows_without_plugin_directory(self):
        """W22 builds link nyx-cmd's OSInfo and DeviceInfo in (no nyxcmd/libnyxcmdModule*.so) and
        name the modules nyx<M>.module: the rows come from usr/bin/nyx-cmd and those modules."""

        class Fw:
            files = {
                "usr/bin/nyx-cmd": b"hardware_id nduid core_os_release webos_release",
                "usr/lib/nyx/modules/nyxOSInfo.module": b"",
                "usr/lib/nyx/modules/nyxDeviceInfo.module": b"",
            }

            def find(self, path, paks=None):
                return ("rootfs.pak", path) if path in self.files else None

            def read(self, pak, path):
                return self.files[path]

        rows = gen.nyx_rows(Fw(), ["rootfs.pak"])
        self.assertEqual(
            rows["nyx.DeviceInfo.hardware_id"],
            (True, "rootfs.pak:/usr/lib/nyx/modules/nyxDeviceInfo.module"),
        )
        self.assertTrue(rows["nyx.OSInfo.webos_release"][0])
        present, src = rows["nyx.DeviceInfo.storage_free"]
        self.assertFalse(present)
        self.assertIn("rootfs.pak:/usr/bin/nyx-cmd has no query storage_free", src)


if __name__ == "__main__":
    unittest.main()
