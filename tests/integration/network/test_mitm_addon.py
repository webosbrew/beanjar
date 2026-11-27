#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Unit tests for host/helpers/network/mitm_addon.py (mode=mitm). No network requests. They need
mitmproxy, the user's own (host/core/beanjar/network/mitmtool.py): Meson runs them with the Python of the
mitmweb it finds (tests/meson.build); under another Python they are skipped."""

import os
import re
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "host/helpers/network"))
import pytest

pytest.importorskip(
    "mitmproxy", reason="no mitmproxy: run with mitmweb's Python (pipx install mitmproxy)"
)
from mitmproxy import connection, http  # noqa: E402
from mitmproxy.addons.next_layer import NextLayer
from mitmproxy.proxy import context
from mitmproxy.proxy.layers import tls
from mitmproxy.test import taddons
import mitm_addon


def flow(request_headers=(), response_headers=None, method="GET", status=200):
    # make() fills in Content-Length from the (empty) body; the headers are set afterwards.
    f = SimpleNamespace(
        request=http.Request.make(method, "https://example.org/x"), response=None, comment=""
    )
    f.request.headers.pop("content-length", None)
    f.request.headers.update(dict(request_headers))
    if response_headers is not None:
        f.response = http.Response.make(status)
        f.response.headers.pop("content-length", None)
        f.response.headers.update(dict(response_headers))
    return f


def tls_context(sni, address=("93.184.215.14", 443)):
    return SimpleNamespace(client=SimpleNamespace(sni=sni), server=SimpleNamespace(address=address))


class Streaming(unittest.TestCase):
    def test_large_request_streams(self):
        f = flow({"content-length": str(2 << 20)})
        mitm_addon.Capture().requestheaders(f)
        self.assertTrue(f.request.stream)
        f = flow({"content-length": "100"})
        mitm_addon.Capture().requestheaders(f)
        self.assertFalse(f.request.stream)

    def test_responses(self):
        cases = [
            (
                {"content-length": str(2 << 20), "content-type": "application/json"},
                "GET",
                200,
                True,
            ),
            ({"content-type": "video/mp4"}, "GET", 200, True),  # open-ended binary
            ({"content-type": "application/json; charset=utf-8"}, "GET", 200, False),
            ({"content-length": "10", "content-type": "image/png"}, "GET", 200, False),
            ({"content-type": "video/mp4"}, "HEAD", 200, False),
            ({"content-type": "application/octet-stream"}, "GET", 204, False),
        ]
        for headers, method, status, streamed in cases:
            f = flow(response_headers=headers, method=method, status=status)
            mitm_addon.Capture().responseheaders(f)
            self.assertEqual(bool(f.response.stream), streamed, (headers, method, status))


class TlsFailure(unittest.TestCase):
    def run_hook(self, hook, contexts, manual=()):
        addon = mitm_addon.Capture()
        with (
            taddons.context(addon) as tctx,
            tempfile.TemporaryDirectory() as d,
            patch.dict(os.environ, {"NETSTUB_LOG": d + "/log"}),
        ):
            tctx.options.update(ignore_hosts=list(manual))
            for c in contexts:
                getattr(addon, hook)(SimpleNamespace(context=c))
            # mitmproxy 12's master logs through its event loop; done() takes its handler off the
            # root logger before taddons closes the loop (the next test would log into it).
            tctx.master.event_loop.run_until_complete(tctx.master.done())
            log = Path(d, "log").read_text() if Path(d, "log").exists() else ""
            return list(tctx.options.ignore_hosts), log

    def test_client_rejection_bypasses_subsequent_connections_once(self):
        hosts, log = self.run_hook(
            "tls_failed_client",
            [tls_context("Pinned.Example.org"), tls_context("Pinned.Example.org")],
        )
        self.assertEqual(hosts, [r"^Pinned\.Example\.org:443$"])
        self.assertEqual(log.count("tls-failed Pinned.Example.org:443 client-rejected"), 2)

    def test_lg_hosts_are_never_passed_through_with_the_mock(self):
        """lg_services=mock (NET_LG_SERVICES unset or mock): an LG host's TLS failure, client or
        upstream, leaves it failing (tls-refused), not passed through; other hosts as before."""
        lg = [tls_context("aic.cdpbeacon2.lgtvcommon.com"), tls_context("US.lgtvsdp.com")]
        for value in (None, "mock"):
            env = {"NET_LG_SERVICES": value} if value else {}
            with patch.dict(os.environ, env):
                if not value:
                    os.environ.pop("NET_LG_SERVICES", None)
                for hook in ("tls_failed_client", "tls_failed_server"):
                    hosts, log = self.run_hook(hook, lg + [tls_context("www.youtube.com")])
                    self.assertEqual(hosts, [r"^www\.youtube\.com:443$"])
                    self.assertIn("tls-refused aic.cdpbeacon2.lgtvcommon.com:443 beacon", log)
                    self.assertIn("tls-refused US.lgtvsdp.com:443 sdp", log)
                    self.assertEqual(log.count("tls-passthrough"), 1)

    def test_lg_hosts_pass_through_with_real_lg_services(self):
        with patch.dict(os.environ, {"NET_LG_SERVICES": "real"}):
            hosts, log = self.run_hook("tls_failed_client", [tls_context("aic.lgtviot.com")])
        self.assertEqual(hosts, [r"^aic\.lgtviot\.com:443$"])
        self.assertNotIn("tls-refused", log)

    def test_upstream_failure_is_logged_by_address_without_sni(self):
        hosts, log = self.run_hook("tls_failed_server", [tls_context(None, ("203.0.113.9", 8443))])
        self.assertEqual(hosts, [r"^203\.0\.113\.9:8443$"])
        self.assertIn("tls-failed 203.0.113.9:8443 upstream-TLS-failed", log)

    def test_no_destination_is_ignored(self):
        hosts, log = self.run_hook("tls_failed_client", [tls_context("x.example", None)])
        self.assertEqual((hosts, log), ([], ""))

    def test_patterns_preserve_manual_settings_and_match_only_endpoint(self):
        hosts, log = self.run_hook(
            "tls_failed_server", [tls_context("a+b.example")], manual=[r"manual\.example"]
        )
        self.assertEqual(hosts[0], r"manual\.example")
        pattern = hosts[1]
        self.assertTrue(re.search(pattern, "A+B.EXAMPLE:443", re.IGNORECASE))
        for other in ("aaab.example:443", "a+b.example:8443", "x.a+b.example:443"):
            self.assertIsNone(re.search(pattern, other, re.IGNORECASE))
        self.assertEqual(log.count("tls-passthrough"), 1)

    def test_ipv6_address_is_escaped_in_mitmproxy_format(self):
        hosts, _ = self.run_hook("tls_failed_server", [tls_context(None, ("2001:db8::1", 443))])
        self.assertIsNotNone(re.search(hosts[0], "2001:db8::1:443"))
        self.assertIsNone(re.search(hosts[0], "2001:db8::2:443"))

    def test_next_layer_honors_fallback_and_manual_patterns(self):
        addon, next_layer = mitm_addon.Capture(), NextLayer()
        with taddons.context(addon, next_layer) as tctx:
            tctx.options.update(ignore_hosts=[r"^manual\.example:443$"])
            addon.tls_failed_client(SimpleNamespace(context=tls_context("pin.example")))
            client = connection.Client(peername=("127.0.0.1", 1000), sockname=("127.0.0.1", 443))
            c = context.Context(client, tctx.options)
            with (
                patch.object(next_layer, "_get_client_hello", return_value=None),
                patch.object(next_layer, "_get_host_header", return_value=None),
            ):
                for host, port, expected in (
                    ("pin.example", 443, True),
                    ("manual.example", 443, True),
                    ("pin.example", 8443, False),
                    ("other.example", 443, False),
                ):
                    c.server.address = ("203.0.113.9", port)
                    c.client.sni = host
                    self.assertEqual(next_layer._ignore_connection(c, b"", b""), expected)
            tctx.master.event_loop.run_until_complete(tctx.master.done())

    def test_existing_exact_manual_rule_is_not_owned_or_evicted(self):
        with patch.object(mitm_addon.Capture, "MAX_TLS_BYPASSES", 1):
            hosts, _ = self.run_hook(
                "tls_failed_server",
                [tls_context("manual.example"), tls_context("a.example"), tls_context("b.example")],
                manual=[r"^manual\.example:443$"],
            )
        self.assertEqual(hosts, [r"^manual\.example:443$", r"^b\.example:443$"])

    def test_automatic_entries_are_bounded_without_evicting_manual_patterns(self):
        with patch.object(mitm_addon.Capture, "MAX_TLS_BYPASSES", 2):
            hosts, _ = self.run_hook(
                "tls_failed_server",
                [tls_context(f"host{i}.example") for i in range(4)],
                manual=["manual"],
            )
        self.assertEqual(hosts, ["manual", r"^host2\.example:443$", r"^host3\.example:443$"])


class WebPassword(unittest.TestCase):
    """mitm_addon.WebPassword: the password file becomes mitmweb's web_password, hashed."""

    def load(self, content):
        from mitmproxy.tools.web.webaddons import WebAddon, WebAuth

        auth = WebAuth()
        with taddons.context(WebAddon(), auth) as tctx, tempfile.TemporaryDirectory() as d:
            env = {}
            if content is not None:
                Path(d, "pw").write_text(content)
                env["NETSTUB_WEB_PASSWORD_FILE"] = d + "/pw"
            try:
                with patch.dict(os.environ, env):
                    if "NETSTUB_WEB_PASSWORD_FILE" not in env:
                        os.environ.pop("NETSTUB_WEB_PASSWORD_FILE", None)
                    mitm_addon.WebPassword().load(None)
                return tctx.options.web_password, auth
            finally:
                tctx.master.event_loop.run_until_complete(tctx.master.done())

    def test_file_becomes_hashed_password(self):
        option, auth = self.load("s3cret-pw\n")
        self.assertTrue(option.startswith("$argon2"), option)
        self.assertNotIn("s3cret-pw", option)
        self.assertTrue(auth.is_valid_password("s3cret-pw"))
        self.assertFalse(auth.is_valid_password("s3cret"))
        self.assertFalse(auth.is_valid_password(""))

    def test_no_file_leaves_mitmweb_default(self):
        option, _ = self.load(None)
        self.assertEqual(option, "")

    def test_empty_file_refused(self):
        with self.assertRaisesRegex(RuntimeError, "no password"):
            self.load("\n")


HASH = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHRzYWx0$w317mQRBxpG4zuYUWInXMBuz/H5Qp1vRW8j9ptJr+rQ"  # the password "pw"


class Freeze(unittest.TestCase):
    """mitm_addon.Freeze: once mitmweb runs, the API cannot change FROZEN options or run commands
    that take a path; everything else the web interface sets still changes. Each test runs a real
    mitmweb master (WebMaster, its own addons, no servers started) with Freeze added."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.marker = os.path.join(self.dir.name, "loaded")
        self.script = os.path.join(self.dir.name, "x.py")
        # a stand-in for a guest-written addon: proof that it was loaded, if it ever is
        Path(self.script).write_text(f"open({self.marker!r}, 'w').close()\n")

    async def loaded(self):
        """Give mitmproxy's script watcher (it loads a script on its first round) time to run."""
        import asyncio

        for _ in range(20):
            if os.path.exists(self.marker):
                return
            await asyncio.sleep(0.05)

    def master(self, test, freeze=True, extra=(), **startup):
        """Run test(master), a coroutine, in a WebMaster whose options are startup, Freeze running unless not
        freeze."""
        import asyncio

        from mitmproxy import options
        from mitmproxy.tools.web.master import WebMaster

        async def run():
            m = WebMaster(options.Options(), with_termlog=False)
            m.addons.add(*extra)
            m.options.update(**startup)
            if freeze:
                f = mitm_addon.Freeze()
                m.addons.add(f)
                f.running()
            try:
                await test(m)
            finally:
                await m.done()

        asyncio.run(run())

    def test_without_it_the_script_loads(self):
        """The control: the same update loads the script when nothing refuses it."""

        async def test(m):
            m.options.update(scripts=[self.script])
            await self.loaded()
            self.assertTrue(os.path.exists(self.marker))

        self.master(test, freeze=False)

    def test_scripts_refused_before_loading(self):
        from mitmproxy import exceptions

        async def test(m):
            with self.assertLogs(level="WARNING"):
                with self.assertRaisesRegex(exceptions.OptionsError, "scripts"):
                    m.options.update(scripts=[self.script])
            await self.loaded()
            self.assertFalse(os.path.exists(self.marker))
            self.assertEqual(m.options.scripts, [])
            # with an allowed option in the same update: all of it is rolled back
            with self.assertLogs(level="WARNING"):
                with self.assertRaises(exceptions.OptionsError):
                    m.options.update(intercept="~q", save_stream_file=self.marker)
            self.assertIsNone(m.options.intercept)
            self.assertFalse(os.path.exists(self.marker))
            # the same values again (an Options menu that sends them all back) are no change
            m.options.update(scripts=[], mode=list(m.options.mode))

        self.master(test)

    def test_frozen_options(self):
        from mitmproxy import exceptions

        changes = {
            "save_stream_file": self.marker,
            "confdir": self.dir.name,
            "client_certs": self.dir.name,
            "mode": ["regular@8080"],
            "web_password": "",
            "listen_host": "0.0.0.0",
            "listen_port": 8080,
            "web_port": 1,
            "map_local": [f"|~q|{self.dir.name}"],
        }

        async def test(m):
            for name, value in changes.items():
                with self.subTest(name), self.assertLogs(level="WARNING"):
                    self.assertIn(name, mitm_addon.FROZEN)
                    with self.assertRaises(exceptions.OptionsError):
                        m.options.update(**{name: value})
            self.assertEqual(m.options.web_password, HASH)
            self.assertEqual(m.options.mode, ["tun:mitm0"])

        self.master(test, web_password=HASH, mode=["tun:mitm0"])

    def test_ui_options_still_change(self):
        async def test(m):
            m.options.update(
                intercept="~u example", intercept_active=True, view_filter="~h example",
                view_order="size", ignore_hosts=["^x$"], web_columns=["path"], anticache=True,
            )  # fmt: skip
            self.assertEqual(m.options.intercept, "~u example")
            self.assertEqual(m.options.ignore_hosts, ["^x$"])

        self.master(test)

    def test_reset_keeps_frozen_values(self):
        from mitmproxy import exceptions

        async def test(m):
            for reset in (lambda: m.commands.call_strings("options.reset", []), m.options.reset):
                with self.assertLogs(level="WARNING"):
                    with self.assertRaises((exceptions.OptionsError, exceptions.CommandError)):
                        reset()
                self.assertEqual(m.options.web_password, HASH)
                self.assertEqual(m.options.mode, ["tun:mitm0"])
            with self.assertLogs(level="WARNING"):
                with self.assertRaises((exceptions.OptionsError, exceptions.CommandError)):
                    m.commands.call_strings("options.reset.one", ["mode"])
            self.assertEqual(m.options.mode, ["tun:mitm0"])

        self.master(test, web_password=HASH, mode=["tun:mitm0"])

    def test_unreviewed_option_frozen(self):
        from mitmproxy import exceptions

        class New:
            def load(self, loader):
                loader.add_option("future_path", str, "", "an option a later mitmproxy adds")

        async def test(m):
            freeze = mitm_addon.Freeze()
            m.addons.add(freeze)
            with self.assertLogs(level="WARNING") as logs:
                freeze.running()
            self.assertIn("future_path", logs.output[0])
            with self.assertLogs(level="WARNING"), self.assertRaises(exceptions.OptionsError):
                m.options.update(future_path="/x")

        self.master(test, freeze=False, extra=[New()])

    def test_path_commands_refused(self):
        from mitmproxy import exceptions

        async def test(m):
            for name, args in (
                ("script.run", ["@all", self.script]),
                ("save.file", ["@all", self.marker]),
                ("options.save", [self.marker]),
                ("options.load", [self.script]),
                ("browser.start", ["chrome"]),
            ):
                with self.subTest(name), self.assertRaisesRegex(exceptions.CommandError, name):
                    m.commands.call_strings(name, args)
            self.assertFalse(os.path.exists(self.marker))
            # set goes through the options: refused there
            with self.assertLogs(level="WARNING"), self.assertRaises(exceptions.CommandError):
                m.commands.call_strings("set", ["scripts", self.script])
            await self.loaded()
            self.assertFalse(os.path.exists(self.marker))
            m.commands.call_strings("set", ["anticache", "true"])  # the others still work
            self.assertTrue(m.options.anticache)
            self.assertIn("curl", m.commands.call_strings("export.formats", []))

        self.master(test)

    def test_every_option_reviewed(self):
        """mitmweb's options, all of them, are in REVIEWED: a newer mitmproxy's new options are
        frozen until they are reviewed here, and this test fails until then."""

        async def test(m):
            self.assertEqual(sorted(set(m.options.keys()) - mitm_addon.REVIEWED), [])

        self.master(test, freeze=False, extra=[mitm_addon.FlowLimit()])

    def test_reload_keeps_the_hash(self):
        """A reload of this addon (mitmproxy reloads a changed script) finds the password set and
        does not touch the frozen option; another password still replaces it (before Freeze)."""
        import argon2

        pw = Path(self.dir.name, "pw")

        async def test(m):
            pw.write_text("pw\n")
            with patch.dict(os.environ, {"NETSTUB_WEB_PASSWORD_FILE": str(pw)}):
                mitm_addon.WebPassword().load(None)
                self.assertEqual(m.options.web_password, HASH)
                pw.write_text("other\n")
                mitm_addon.WebPassword().load(None)
            self.assertTrue(argon2.PasswordHasher().verify(m.options.web_password, "other"))

        self.master(test, freeze=False, web_password=HASH)


class FlowLimit(unittest.TestCase):
    """mitm_addon.FlowLimit: the view keeps max_flows flows, the oldest finished ones go first, a
    live one stays, 0 keeps everything."""

    def run_flows(self, limit, flows, live=(), budget=0, body=0):
        from mitmproxy.addons import view
        from mitmproxy.test import tflow

        v, limiter = view.View(), mitm_addon.FlowLimit()
        with taddons.context(v, limiter) as tctx:
            try:
                tctx.configure(limiter, max_flows=limit, max_flow_bytes=budget)
                made = []
                for i in range(flows):
                    f = tflow.tflow(resp=True)
                    f.request.content, f.response.content = b"q" * body, b"r" * body
                    f.live = i in live
                    made.append(f)
                    v.requestheaders(f)  # the view first, as in mitmweb
                    limiter.requestheaders(f)
                return [f.id for f in made], [f.id for f in v]
            finally:
                tctx.master.event_loop.run_until_complete(tctx.master.done())

    def test_default_is_vmsettings(self):
        # the addon runs in mitmproxy's own Python, without beanjar: its default repeats this one
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "host/core"))
        from beanjar.vm import settings

        with taddons.context(mitm_addon.FlowLimit()) as tctx:
            try:
                self.assertEqual(tctx.options.max_flows, settings.MITM_FLOWS)
            finally:
                tctx.master.event_loop.run_until_complete(tctx.master.done())

    def test_oldest_dropped(self):
        made, kept = self.run_flows(3, 10)
        self.assertEqual(kept, made[-3:])

    def test_live_flow_kept(self):
        made, kept = self.run_flows(3, 10, live={1})
        self.assertEqual(kept, [made[1]] + made[-2:])

    def test_byte_budget(self):
        # 10 flows of 2 x 100 bytes, 700 bytes allowed: the newest three stay
        made, kept = self.run_flows(1000, 10, budget=700, body=100)
        self.assertEqual(kept, made[-3:])

    def test_byte_budget_keeps_live(self):
        made, kept = self.run_flows(1000, 10, live={0}, budget=700, body=100)
        self.assertEqual(kept, [made[0]] + made[-2:])

    def test_byte_budget_off_by_default(self):
        with taddons.context(mitm_addon.FlowLimit()) as tctx:
            try:
                self.assertEqual(tctx.options.max_flow_bytes, 0)
            finally:
                tctx.master.event_loop.run_until_complete(tctx.master.done())

    def test_flow_bytes(self):
        from mitmproxy.test import tflow

        f = tflow.tflow(resp=True)
        f.request.content, f.response.content = b"a" * 10, b"b" * 32
        self.assertEqual(mitm_addon.flow_bytes(f), 42)
        t = tflow.ttcpflow()
        self.assertEqual(mitm_addon.flow_bytes(t), sum(len(m.content) for m in t.messages))

    def test_unlimited(self):
        made, kept = self.run_flows(0, 20)
        self.assertEqual(kept, made)


class Ready(unittest.TestCase):
    """The ready file netns.py waits for, and the UDP idle timeout of a TUN device."""

    def test_ready_file_after_the_udp_timeout(self):
        with tempfile.TemporaryDirectory() as d:
            ready = os.path.join(d, "mitmweb.ready")
            with patch.dict(os.environ, {"NETNS_MITM_READY": ready}):
                with taddons.context(mitm_addon.Ready()) as tctx:
                    tctx.options.update(mode=["regular@8080"])
                    with patch.object(mitm_addon, "tun_udp_timeout") as raised:
                        mitm_addon.Ready().running()
                    raised.assert_not_called()  # no TUN device: nothing to raise
                    self.assertTrue(os.path.exists(ready))
                    os.unlink(ready)
                    tctx.options.update(mode=["tun:mitm0"])
                    with patch.object(mitm_addon, "tun_udp_timeout") as raised:
                        mitm_addon.Ready().running()
                    raised.assert_called_once_with()
                    self.assertTrue(os.path.exists(ready))

    def test_udp_timeout_raised_by_feature(self):
        server = SimpleNamespace(UDP_TIMEOUT=20)
        self.assertTrue(mitm_addon.tun_udp_timeout(server))
        self.assertEqual(server.UDP_TIMEOUT, mitm_addon.TUN_UDP_TIMEOUT)
        server = SimpleNamespace(UDP_TIMEOUT=3600)
        self.assertTrue(mitm_addon.tun_udp_timeout(server))
        self.assertEqual(server.UDP_TIMEOUT, 3600)  # never lowered
        for missing in (SimpleNamespace(), SimpleNamespace(UDP_TIMEOUT="20")):
            with self.assertLogs(level="WARNING") as logs:
                self.assertFalse(mitm_addon.tun_udp_timeout(missing))
            self.assertIn("UDP_TIMEOUT", logs.output[0])

    def test_the_real_attribute(self):
        import mitmproxy.proxy.server as server

        self.assertIsInstance(server.UDP_TIMEOUT, (int, float))


if __name__ == "__main__":
    unittest.main()
