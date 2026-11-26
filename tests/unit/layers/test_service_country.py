#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The upstart job that stores the board's own smart service country (US, USA), run with a
fake luna-send against a copy of sdx's country table."""

import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
JOB = REPO / "guest/layers/webos/dreadlocks/rootfs/etc/init/emulator-service-country.conf"
XML = """<continent>
\t<group code="US" >
\t\t<country fullname="USA"\tcode2="US" code3="USA" code4="USA"/>
\t\t<country fullname="Others"\tcode2="US7" code3="US7" code4="US7"/>
\t</group>
</continent>
"""
# a state file stands for settings: the current code3; sdx_writes makes the second read differ
FAKE = """#!/bin/sh
state=$STATE
case "$*" in
*getSystemSettings*smartServiceCountryCode3*)
	n=$(cat $state.reads 2>/dev/null || echo 0); echo $((n + 1)) > $state.reads
	if [ -n "$SDX_AFTER" ] && [ $n -ge $SDX_AFTER ]; then echo US7 > $state; fi
	echo "{\\"returnValue\\":true,\\"settings\\":{\\"smartServiceCountryCode3\\":\\"$(cat $state)\\"}}" ;;
*getSystemSettings*country*)
	echo "{\\"returnValue\\":true,\\"settings\\":{\\"country\\":\\"$COUNTRY\\"}}" ;;
*setSystemSettings*)
	echo "$*" >> $state.sets
	c3=$(echo "$*" | sed -n 's/.*smartServiceCountryCode3\\\\*":\\\\*"\\([A-Z0-9]*\\).*/\\1/p')
	echo $c3 > $state ;;
esac
"""


def script_body():
    text = JOB.read_text()
    return re.search(r"^script\n(.*?)^end script", text, re.S | re.M).group(1)


def run(code3, country, sdx_after=None, ran=True, normal=False):
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "bin").mkdir()
        for name, body in (("luna-send", FAKE), ("usleep", "#!/bin/sh\n")):
            p = d / "bin" / name
            p.write_text(body)
            p.chmod(p.stat().st_mode | stat.S_IXUSR)
        (d / "sdx.xml").write_text(XML)
        if ran:
            (d / "ran-firstuse").write_text("")
        if normal:
            (d / "emulator-firstuse-done").write_text("")
        (d / "state").write_text(code3 + "\n")
        body = script_body().replace("/usr/palm/sdx/country_codes_v5.xml", str(d / "sdx.xml"))
        body = body.replace("/var/luna/preferences/ran-firstuse", str(d / "ran-firstuse"))
        body = body.replace("/usr/sbin/emulator-firstuse-done", str(d / "emulator-firstuse-done"))
        env = dict(os.environ, PATH=f"{d / 'bin'}:{os.environ['PATH']}", STATE=str(d / "state"))
        env["COUNTRY"] = country
        if sdx_after is not None:
            env["SDX_AFTER"] = str(sdx_after)
        out = subprocess.run(
            ["sh", "-c", body], env=env, capture_output=True, text=True, timeout=60
        )
        sets = (d / "state.sets").read_text().splitlines() if (d / "state.sets").exists() else []
        return out.stdout, sets, (d / "state").read_text().strip()


class ServiceCountryTest(unittest.TestCase):
    def test_stores_the_boards_country(self):
        out, sets, final = run("other", "USA")
        self.assertEqual(final, "USA")
        self.assertIn('"smartServiceCountryCode2":"US"', sets[0])
        self.assertIn("emulator-service-country: US USA", out)

    def test_first_use(self):
        # in FirstUse only where the normal layer completes it (its firstUseDone waits for this)
        self.assertEqual(run("other", "USA", ran=False)[1:], ([], "other"))
        self.assertEqual(run("other", "USA", ran=False, normal=True)[2], "USA")

    def test_left_alone_when_already_set(self):
        _, sets, final = run("DEU", "USA")
        self.assertEqual((sets, final), ([], "DEU"))

    def test_stores_again_once_sdx_has_stored_its_others_row(self):
        out, sets, final = run("other", "USA", sdx_after=3)
        self.assertEqual(len(sets), 2)
        self.assertEqual(final, "USA")
        self.assertIn("US7 -> US USA after sdx", out)

    def test_unknown_country_writes_nothing(self):
        _, sets, final = run("other", "ZZZ")
        self.assertEqual((sets, final), ([], "other"))

    def test_no_others_row_is_stored(self):
        self.assertNotIn("Others", script_body().split("sed -n")[0])


if __name__ == "__main__":
    unittest.main()
