#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""beanjar-web's traffic inspector route against the real mitmweb (the user's own, as
mitmtool.py finds it; skipped, 77, without one of 12.0.1 or newer): mitmweb run as netns.py runs it but as a
regular proxy on loopback, with mitm_addon.py's password, and beanjar-web in front of it with a
stand-in manager whose `mitm connect` answers with mitmweb's address and password.

Through /vm/local/tv/mitm/ of the inspector's own origin (Server.inspector_app), opened with the
VM's grant, with an aiohttp client: the page and its assets, the API (/flows,
/options), a PUT with mitmweb's XSRF cookie from /updates, a flow recorded through the proxy
arriving over /updates, the rewritten File > Save. With headless Chromium (skipped without it): the
page itself, the flow in its list, its WebSocket at the prefix, no request outside the prefix.
Prints what it checked; no VM, no network beyond loopback."""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

TOP = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(TOP / "host/core"))
sys.path.insert(0, str(TOP / "host/web"))
try:
    import aiohttp
    from aiohttp import web
except ImportError:
    print("python3-aiohttp is not installed: skipped")
    sys.exit(77)
from beanjar.network import mitmtool  # noqa: E402
from beanjar_web import access, server  # noqa: E402

TOOL = mitmtool.check()
if not TOOL.usable:
    print(f"{TOOL.problem}: skipped")
    sys.exit(77)
MITMWEB = TOOL.path
print(f"check-mitm: mitmproxy {TOOL.version} ({MITMWEB})")
CHROME = shutil.which("chromium")
PASSWORD = "e2e-password"

FAKE_BEANJAR = """
import json, sys
assert sys.argv[1:3] == ["mitm", "connect"], sys.argv
print(json.dumps({"url": sys.argv[-2], "password": sys.argv[-1]}), flush=True)
sys.stdin.buffer.read()
"""


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def until(check, timeout=30):
    for _ in range(int(timeout * 10)):
        value = check()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        await asyncio.sleep(0.1)
    raise AssertionError("timed out")


class Browser:
    """headless Chromium over the DevTools protocol (check-viewer.py's way)."""

    def __init__(self, ws):
        self.ws, self.id, self.events = ws, 0, []

    async def cmd(self, method, **params):
        self.id += 1
        ident = self.id
        await self.ws.send_json({"id": ident, "method": method, "params": params})
        while True:
            msg = await asyncio.wait_for(self.ws.receive_json(), 20)
            if msg.get("id") == ident:
                assert "error" not in msg, msg
                return msg.get("result", {})
            self.events.append(msg)

    async def js(self, expression):
        r = await self.cmd("Runtime.evaluate", expression=expression, returnByValue=True,
                           awaitPromise=True)  # fmt: skip
        assert "exceptionDetails" not in r, r
        return r["result"].get("value")


async def browser_checks(tmp, base, page, grant):
    profile = Path(tmp, "profile")
    chrome = subprocess.Popen(
        [CHROME, "--headless", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
         "--disable-background-networking", "--remote-debugging-port=0",
         f"--user-data-dir={profile}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        await until(lambda: (profile / "DevToolsActivePort").exists())
        debug = "http://127.0.0.1:" + (profile / "DevToolsActivePort").read_text().split()[0]
        async with aiohttp.ClientSession() as session:
            tabs = await (await session.get(debug + "/json/list")).json()
            async with session.ws_connect(tabs[0]["webSocketDebuggerUrl"], max_msg_size=0) as ws:
                b = Browser(ws)
                await b.cmd("Network.enable")
                await b.cmd("Runtime.enable")
                await b.cmd("Page.navigate", url=base + page + grant)
                await until(lambda: b.js("document.querySelectorAll('.flow-table tbody tr').length"
                                         " > 0 && document.body.innerText.includes('/e2e')"))  # fmt: skip
                text = await b.js("document.body.innerText")
                urls = [e["params"]["request"]["url"] for e in b.events
                        if e.get("method") == "Network.requestWillBeSent"]  # fmt: skip
                sockets = [e["params"]["url"] for e in b.events
                           if e.get("method") == "Network.webSocketCreated"]  # fmt: skip
                errors = [e["params"] for e in b.events
                          if e.get("method") == "Runtime.exceptionThrown"]  # fmt: skip
                outside = [u for u in urls if not u.startswith((base + page, "data:"))]
                assert not outside, outside
                assert sockets == [base.replace("http", "ws") + page + "updates"], sockets
                assert not errors, errors
                print(f"chromium: the page listed the flow ({len(urls)} requests, all under "
                      f"{page}; WebSocket {sockets[0]}; no script error)")  # fmt: skip
                assert "/e2e" in text
    finally:
        chrome.terminate()
        chrome.wait()


async def main(tmp):
    web_port, proxy_port = free_port(), free_port()
    secret = Path(tmp, "mitmweb.password")
    secret.write_text(PASSWORD + "\n")
    env = dict(os.environ, NETSTUB_WEB_PASSWORD_FILE=str(secret), NETSTUB_LOG=str(Path(tmp, "log")))
    log = open(Path(tmp, "mitmweb.log"), "w")
    proxy = subprocess.Popen(
        [MITMWEB, "--mode", f"regular@{proxy_port}", "--listen-host", "127.0.0.1",
         "--web-host", "127.0.0.1", "--web-port", str(web_port), "--no-web-open-browser",
         "--set", f"confdir={tmp}", "--set", "termlog_verbosity=warn",
         "-s", str(TOP / "host/helpers/network/mitm_addon.py")],
        env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
    )  # fmt: skip
    script = Path(tmp, "beanjar.py")
    script.write_text(FAKE_BEANJAR)
    upstream = f"http://127.0.0.1:{web_port}/"
    s = server.Server([sys.executable, str(script)], access.Access("none"), tmp, tmp)
    s.spawn = spawn_with(s, upstream)
    runner = web.AppRunner(s.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    main_base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    inspector = web.AppRunner(s.inspector_app())
    await inspector.setup()
    isite = web.TCPSite(inspector, "127.0.0.1", 0)
    await isite.start()
    s.inspectors.port = isite._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{s.inspectors.port}"  # the inspector's own origin
    page = "/vm/local/tv/mitm/"
    grant = f"?grant={s.inspectors.grant('local/tv')}"
    target = web.Application()
    target.router.add_get("/e2e", lambda r: web.Response(text="hello"))
    target_runner = web.AppRunner(target)
    await target_runner.setup()
    target_site = web.TCPSite(target_runner, "127.0.0.1", 0)
    await target_site.start()
    target_url = f"http://127.0.0.1:{target_site._server.sockets[0].getsockname()[1]}/e2e"
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as c:
            async with c.get(main_base + page) as r:
                assert r.status == 404, ("the main origin serves no inspector", r.status)
            async with c.get(base + page) as r:
                assert r.status == 403, ("no grant", r.status)

            async def up():
                try:
                    async with c.head(upstream) as r:
                        return r.headers.get("Server", "").startswith("mitmproxy")
                except aiohttp.ClientError:
                    return False

            await until(up)
            # unauthenticated, mitmweb itself refuses
            async with c.get(upstream + "flows") as r:
                assert r.status == 403, r.status
            async with c.get(base + page + grant) as r:  # the grant's cookie, then the page
                html = await r.text()
                assert r.status == 200 and "./static/index-" in html, (r.status, html[:200])
                assert "mitmproxy-auth" not in str(r.headers.getall("Set-Cookie", []))
            assets = [a.split('"')[0] for a in html.split('"./static/')[1:]]
            rewritten = 0
            for asset in assets:
                async with c.get(base + page + "static/" + asset) as r:
                    assert r.status == 200, (asset, r.status)
                    body = await r.read()
                    rewritten += body.count(b'location.replace("./flows/dump')
                    assert b'location.replace("/flows/dump' not in body, asset
            assert rewritten == 2, rewritten
            print(f"the page and its {len(assets)} assets; File > Save relative ({rewritten})")
            async with c.get(base + page + "options") as r:
                options = await r.json()
                assert "web_password" in options and r.status == 200
            ws = await c.ws_connect(base.replace("http", "ws") + page + "updates")
            # the XSRF cookie mitmweb sets in the handshake, under the prefix (as a browser keeps
            # it: the client's jar takes no handshake cookie)
            (cookie,) = ws._response.headers.getall("Set-Cookie")
            first, *attrs = [a.strip() for a in cookie.split(";")]
            assert f"Path={page}" in attrs and first.startswith("_mitmproxy_xsrf="), cookie
            xsrf = first.split("=", 1)[1]
            async with c.put(base + page + "options", json={"anticache": True},
                             headers={"X-XSRFToken": xsrf,
                                      "Cookie": f"_mitmproxy_xsrf={xsrf}"}) as r:  # fmt: skip
                assert r.status == 200, (r.status, await r.text())
            async with c.get(base + page + "options") as r:
                assert (await r.json())["anticache"]["value"] is True
            print("API: /options read and set (PUT with the XSRF cookie from /updates)")
            async with c.get(target_url, proxy=f"http://127.0.0.1:{proxy_port}") as r:
                assert await r.text() == "hello"

            async def flow_update():
                msg = await ws.receive(timeout=10)
                return msg.type == aiohttp.WSMsgType.TEXT and "/e2e" in msg.data

            await until(flow_update, 15)
            async with c.get(base + page + "flows") as r:
                flows = await r.json()
                assert any(f["request"]["path"] == "/e2e" for f in flows), flows
            print("a proxied request seen over /updates and in /flows")
            await ws.close()
        if CHROME:
            await browser_checks(tmp, base, page, grant)
        else:
            print("chromium is not installed: the browser check skipped")
    finally:
        await inspector.cleanup()
        await runner.cleanup()
        await target_runner.cleanup()
        proxy.terminate()
        proxy.wait()
        log.close()
    print("check-mitm: ok")


def spawn_with(s, upstream):
    """Server.spawn, `mitm connect` given mitmweb's address and the password."""
    spawn = s.spawn

    async def wrapped(words, stdin=False):
        if words[:2] == ["mitm", "connect"]:
            words = [*words, upstream, PASSWORD]
        return await spawn(words, stdin)

    return wrapped


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="check-mitm-") as tmp:
        asyncio.run(main(tmp))
