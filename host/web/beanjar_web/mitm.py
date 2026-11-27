# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""A VM's traffic inspector (mitmweb) at /vm/HOST/NAME/mitm/ of the inspector's own origin
(docs/dev/architecture/web-viewer.md, "The traffic inspector").

mitmweb shows what the guest sends, which the guest controls: a script injected into its page must
not run with beanjar-web's origin, where it could drive every VM. So the inspector is served by a
listener of its own (--inspector-port, or the public --inspector-origin a proxy maps to it), a
separate origin, which serves nothing else (Server.inspector_app). Its access is a grant per VM,
an HMAC of HOST/NAME with a key made at each start: /vm/HOST/NAME/inspector.json of the main
origin (behind its access) gives the URL with ?grant=, which is exchanged for an HttpOnly cookie
under the VM's prefix (GRANT_COOKIE), as the main token is. beanjar-web's own session cookie is
never accepted there and never passed to mitmweb; the main origin refuses what the inspector's
page could send it (server.Server.auth: another site's or origin's fetches, POSTs and
WebSockets). mitmweb's page needs a real origin: it reads its XSRF cookie from document.cookie and
loads module scripts, which a CSP sandbox's opaque origin would refuse.

`beanjar mitm connect HOST/NAME` makes the inspector reachable from here and says where, with its
password, in one JSON line, then holds it (for another host's VM a forward on that host's shared
ssh connection) until its stdin ends. One runs per VM while the route is in use: started by the
first request, shared by the next ones, ended IDLE seconds after the last one finished, at
shutdown, and when the inspector stops answering (a VM restarted: a new password; a shared
connection that ended: no forward), after which the request is tried once more with a new one.

mitmweb (the user's own, 12.0.1 or newer: host/core/beanjar/network/mitmtool.py; 12.2.3 and 12.3.1 read
for what follows, and the same) has no base path option and needs none: its page asks for everything relative to itself (./static/..., its API
through a helper that makes /flows ./flows, the WebSocket at the page's path + /updates), so the
requests are passed on as they are, below the prefix. The exceptions are File > Save and Save
filtered, location.replace("/flows/dump..."), made relative in its script (DUMP_FROM, DUMP_TO).

Its login is done here: each request carries `Authorization: Bearer PASSWORD` and the session
cookie mitmweb gave (mitmproxy-auth-PORT), which is kept here and never reaches the browser.
mitmweb checks the password (argon2) only when the cookie is missing or no longer valid. Of the
browser's cookies only mitmweb's XSRF cookie is passed on; the XSRF cookie mitmweb sets is
passed back with its path made the prefix, so each VM's page reads its own. mitmweb checks a
WebSocket's Origin against its Host; this server has checked the browser's Origin against its
own, and sends mitmweb none.
"""

import asyncio
import hashlib
import hmac
import json
import re
import secrets

import aiohttp
from aiohttp import WSMsgType, web
from yarl import URL

IDLE = 60  # seconds a VM's `mitm connect` is kept after its last request
OPEN_TIMEOUT = 90  # seconds `mitm connect` may take to answer (a host's ssh, its beanjar)
AUTH_COOKIE = "mitmproxy-auth"  # mitmweb's session cookie: mitmproxy-auth-PORT
XSRF_COOKIE = "_mitmproxy_xsrf"
# what is never passed on, either way: hop-by-hop, and what this server sets itself
HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
    "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}  # fmt: skip
REQUEST_DROP = HOP | {
    "cookie", "authorization", "origin", "referer", "forwarded", "x-forwarded-for",
    "x-forwarded-host", "x-forwarded-proto", "x-real-ip", "cf-connecting-ip", "cf-visitor",
    "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions",
    "sec-websocket-protocol", "sec-websocket-accept",
}  # fmt: skip
RESPONSE_DROP = HOP | {"set-cookie"}
SAFE = ("GET", "HEAD", "OPTIONS")
GRANT_COOKIE = "beanjar_inspector"  # the inspector origin's per-VM grant, under the VM's prefix
SCRIPT_RE = re.compile(r"/static/index-[A-Za-z0-9_-]+\.js")  # the page's script, the one rewritten
DUMP_FROM = b'location.replace("/flows/dump'
DUMP_TO = b'location.replace("./flows/dump'


class Unavailable(Exception):
    """The inspector cannot be reached: status, the manager's message."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Upstream:
    """A VM's running `mitm connect`: where the inspector is, its password, mitmweb's cookie."""

    def __init__(self):
        self.proc = None
        self.url = self.password = None
        self.cookie = None  # "mitmproxy-auth-PORT=VALUE" once mitmweb set it
        self.users = 0
        self.timer = None
        self.task = None

    def alive(self):
        return self.proc is None or self.proc.returncode is None


def set_cookie_header(value, prefix):
    """(name, the Set-Cookie value with Path the prefix, without Domain), or (name, None) for
    mitmweb's session cookie, which stays here."""
    first, _, attrs = value.partition(";")
    name = first.split("=", 1)[0].strip()
    if name.startswith(AUTH_COOKIE):
        return name, None
    kept = [a.strip() for a in attrs.split(";") if a.strip()]
    kept = [a for a in kept if a.split("=", 1)[0].strip().lower() not in ("path", "domain")]
    return name, "; ".join([first.strip(), *kept, f"Path={prefix}"])


def rewrite_script(body):
    """The page's script with File > Save's absolute /flows/dump made relative."""
    return body.replace(DUMP_FROM, DUMP_TO)


class Inspectors:
    """The /vm/HOST/NAME/mitm/ routes of a Server (server.py), whose spawn, end and spec it
    uses."""

    def __init__(self, server, idle=IDLE):
        self.server = server
        self.idle = idle
        self.upstreams = {}  # HOST/NAME: Upstream
        self.session = None
        self.key = secrets.token_bytes(32)  # the grants', new at each start
        self.port = None  # the inspector listener's, once it listens
        self.origin = None  # --inspector-origin: its public origin, else this host at port

    # -- access --
    def grant(self, spec):
        """The inspector origin's grant for one VM's inspector (HOST/NAME)."""
        return hmac.new(self.key, spec.encode(), hashlib.sha256).hexdigest()

    def url(self, request, spec):
        """Where the main origin's page opens spec's inspector: the inspector origin (its public
        one, else the page's host at its port), the VM's prefix and the grant."""
        if self.origin:
            base = self.origin
        else:
            from .server import expected_origin

            own = URL(expected_origin(request))
            base = str(own.with_port(self.port)).rstrip("/")
        return f"{base}/vm/{spec}/mitm/?grant={self.grant(spec)}"

    @web.middleware
    async def auth(self, request, handler):
        """The inspector origin's access: a VM's grant (?grant= once, then its cookie) for that
        VM's routes only; this origin's Origin for what is not a GET; nothing from another site or
        origin but a page opened (the main origin's link)."""
        from .server import expected_origin, foreign

        if foreign(request):
            raise web.HTTPForbidden(text="beanjar-web: a request from another site or origin")
        if "host" not in request.match_info or "name" not in request.match_info:
            raise web.HTTPNotFound(text="beanjar-web: the traffic inspector only")
        spec = self.server.spec(request)
        if request.method not in SAFE or request.headers.get("Upgrade", "").lower() == "websocket":
            origin = request.headers.get("Origin")
            if origin and origin != expected_origin(request):
                raise web.HTTPForbidden(text="cross-origin request")
        grant = self.grant(spec)
        if "grant" in request.query:
            if not hmac.compare_digest(request.query["grant"], grant):
                raise web.HTTPForbidden(text="beanjar-web: a wrong or old inspector grant")
            query = {k: v for k, v in request.query.items() if k != "grant"}
            response = web.HTTPFound(str(request.rel_url.with_query(query)))
            response.set_cookie(
                GRANT_COOKIE,
                grant,
                httponly=True,
                samesite="Strict",
                path=f"/vm/{spec}/mitm",  # and below it, not /vm/{spec}/mitmX
                secure=expected_origin(request).startswith("https://"),
            )
            raise response
        if not hmac.compare_digest(request.cookies.get(GRANT_COOKIE, ""), grant):
            raise web.HTTPForbidden(
                text="beanjar-web: open the Traffic Inspector from the VM's page in beanjar-web"
            )
        return await handler(request)

    # -- `mitm connect` --
    async def acquire(self, spec):
        """spec's Upstream, opened (or being opened, waited for) and counted in use; Unavailable
        when it cannot be."""
        up = self.upstreams.get(spec)
        if up is not None and not up.alive():
            await self.drop(spec, up)
            up = None
        if up is None:
            up = self.upstreams[spec] = Upstream()
            up.task = asyncio.ensure_future(self.open(spec, up))
        up.users += 1
        if up.timer:
            up.timer.cancel()
            up.timer = None
        try:
            await asyncio.shield(up.task)
        except BaseException:
            self.release(spec, up)
            if self.upstreams.get(spec) is up and up.task.done():
                del self.upstreams[spec]  # a failure is not kept: the next request asks again
            raise
        return up

    async def open(self, spec, up):
        try:
            proc = await self.server.spawn(["mitm", "connect", "--json", spec], stdin=True)
        except OSError as e:
            raise Unavailable(502, f"cannot run beanjar: {e}") from None
        up.proc = proc
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), OPEN_TIMEOUT)
        except asyncio.TimeoutError:
            line = None
        try:
            answer = json.loads(line) if line else None
            url, password = answer["url"], answer["password"]
            if not (
                isinstance(url, str) and url.startswith("http://") and isinstance(password, str)
            ):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            answer = None
        if answer is None:
            await self.server.end(proc, 1.0)
            err = (await proc.stderr.read()).decode(errors="replace")
            from .server import error_text

            if line is None:
                raise Unavailable(
                    502, f"beanjar mitm connect did not answer within {OPEN_TIMEOUT} s"
                )
            if proc.returncode:
                raise Unavailable(409, error_text(err))
            raise Unavailable(502, "beanjar mitm connect printed no inspector address")
        up.url, up.password = url.rstrip("/"), password

    def release(self, spec, up):
        up.users -= 1
        if up.users == 0 and self.upstreams.get(spec) is up and up.timer is None:
            loop = asyncio.get_running_loop()
            up.timer = loop.call_later(self.idle, lambda: self.expire(spec, up))

    def expire(self, spec, up):
        up.timer = None
        if up.users == 0 and self.upstreams.get(spec) is up:
            task = asyncio.ensure_future(self.drop(spec, up))
            self.server.tasks.add(task)
            task.add_done_callback(self.server.tasks.discard)

    async def drop(self, spec, up):
        """Ends spec's `mitm connect` (its stdin closed: the forward cancelled)."""
        if self.upstreams.get(spec) is up:
            del self.upstreams[spec]
        if up.timer:
            up.timer.cancel()
            up.timer = None
        if up.proc is not None:
            await self.server.end(up.proc, 3.0)

    async def shutdown(self):
        for spec, up in list(self.upstreams.items()):
            if up.task and not up.task.done():
                up.task.cancel()
            await self.drop(spec, up)
        if self.session:
            await self.session.close()

    def client(self):
        if self.session is None:
            self.session = aiohttp.ClientSession(
                auto_decompress=False,
                cookie_jar=aiohttp.DummyCookieJar(),
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=10),
            )
        return self.session

    # -- requests --
    @staticmethod
    def prefix(request):
        return f"/vm/{request.match_info['host']}/{request.match_info['name']}/mitm/"

    async def slash(self, request):
        """/vm/HOST/NAME/mitm: the page's relative URLs need the slash."""
        self.server.spec(request)
        raise web.HTTPFound(self.prefix(request))

    def upstream_headers(self, request, up, script=False):
        headers = {k: v for k, v in request.headers.items() if k.lower() not in REQUEST_DROP}
        cookies = [up.cookie] if up.cookie else []
        if XSRF_COOKIE in request.cookies:
            cookies.append(f"{XSRF_COOKIE}={request.cookies[XSRF_COOKIE]}")
        if cookies:
            headers["Cookie"] = "; ".join(cookies)
        headers["Authorization"] = f"Bearer {up.password}"
        if script:
            headers["Accept-Encoding"] = "identity"
        return headers

    def keep_cookies(self, request, up, headers, out):
        """mitmweb's Set-Cookie headers: its session kept in up, the others into out (a
        CIMultiDict) for the browser, under the prefix."""
        for value in headers.getall("Set-Cookie", []):
            name, passed = set_cookie_header(value, self.prefix(request))
            if passed is None:
                up.cookie = value.partition(";")[0].strip()
            else:
                out.add("Set-Cookie", passed)

    async def handle(self, request):
        spec = self.server.spec(request)
        prefix = self.prefix(request)
        if not request.raw_path.startswith(prefix):
            raise web.HTTPBadRequest(text="beanjar-web: an encoded HOST/NAME in an inspector path")
        rest = "/" + request.raw_path[len(prefix) :]  # auth has checked Origin and the grant
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self.websocket(request, spec, rest)
        body = await request.read()
        for attempt in (0, 1):
            up = await self.upstream(spec)
            try:
                response = await self.forward(request, up, rest, body, last=attempt == 1)
                if response is not None:
                    return response
            finally:
                self.release(spec, up)
            await self.drop(spec, up)
        raise AssertionError("unreachable")

    async def upstream(self, spec):
        try:
            return await self.acquire(spec)
        except Unavailable as e:
            kind = web.HTTPConflict if e.status == 409 else web.HTTPBadGateway
            raise kind(text=f"beanjar-web: the traffic inspector: {e}") from None

    async def forward(self, request, up, rest, body, last):
        """The request to mitmweb and its response back; None (to try again with a new `mitm
        connect`) when unless last it cannot be reached or refuses the login of a GET."""
        path = rest.split("?", 1)[0]
        script = bool(SCRIPT_RE.fullmatch(path)) and request.method == "GET"
        try:
            upstream = await self.client().request(
                request.method,
                URL(up.url + rest, encoded=True),
                headers=self.upstream_headers(request, up, script),
                data=body or None,
                allow_redirects=False,
            )
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
            if not last:
                return None
            raise web.HTTPBadGateway(text=f"beanjar-web: the traffic inspector: {e}") from None
        async with upstream:
            if upstream.status == 403 and request.method in SAFE and not last:
                return None  # a new password (the VM restarted), or a session mitmweb forgot
            out = web.StreamResponse(status=upstream.status, reason=upstream.reason)
            for k, v in upstream.headers.items():
                if k.lower() not in RESPONSE_DROP:
                    out.headers.add(k, v)
            self.keep_cookies(request, up, upstream.headers, out.headers)
            if script and upstream.status == 200:
                data = rewrite_script(await upstream.read())  # asked for without compression
                out.content_length = len(data)
                await out.prepare(request)
                await out.write(data)
                await out.write_eof()
                return out
            if upstream.content_length is not None:
                out.content_length = upstream.content_length
            await out.prepare(request)
            if request.method != "HEAD":
                async for chunk in upstream.content.iter_any():
                    await out.write(chunk)
            await out.write_eof()
            return out

    async def websocket(self, request, spec, rest):
        """mitmweb's /updates: its messages both ways, until either side closes."""
        for attempt in (0, 1):
            up = await self.upstream(spec)
            try:
                theirs = await self.client().ws_connect(
                    URL(up.url.replace("http://", "ws://", 1) + rest, encoded=True),
                    headers=self.upstream_headers(request, up),
                    max_msg_size=0,
                    autoping=True,
                )
            except aiohttp.WSServerHandshakeError as e:
                self.release(spec, up)
                if e.status == 403 and attempt == 0:
                    await self.drop(spec, up)
                    continue
                raise web.HTTPBadGateway(
                    text=f"beanjar-web: the traffic inspector refused its WebSocket: {e.status}"
                ) from None
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
                self.release(spec, up)
                if attempt == 0:
                    await self.drop(spec, up)
                    continue
                raise web.HTTPBadGateway(text=f"beanjar-web: the traffic inspector: {e}") from None
            break
        try:
            from .server import RFB_HEARTBEAT

            ours = web.WebSocketResponse(max_msg_size=0, heartbeat=RFB_HEARTBEAT)
            self.keep_cookies(request, up, theirs._response.headers, ours.headers)
            await ours.prepare(request)

            async def downstream():
                try:
                    async for msg in theirs:
                        if msg.type == WSMsgType.TEXT:
                            await ours.send_str(msg.data)
                        elif msg.type == WSMsgType.BINARY:
                            await ours.send_bytes(msg.data)
                        else:
                            break
                except (ConnectionResetError, RuntimeError):
                    pass
                if not ours.closed:
                    await ours.close()

            down = asyncio.ensure_future(downstream())
            try:
                async for msg in ours:
                    if msg.type == WSMsgType.TEXT:
                        await theirs.send_str(msg.data)
                    elif msg.type == WSMsgType.BINARY:
                        await theirs.send_bytes(msg.data)
            except (ConnectionResetError, RuntimeError):
                pass
            finally:
                await theirs.close()
                await asyncio.gather(down, return_exceptions=True)
            return ours
        finally:
            if not theirs.closed:
                await theirs.close()
            self.release(spec, up)

    async def status(self, request):
        """/vm/HOST/NAME/inspector.json (the main origin): `beanjar mitm show --json` without the
        URL or password file: {mode, status, ready, problem, url}, problem why a started mitm VM's
        inspector is off (no usable mitmweb: what to install) or null, url where to open it (the
        inspector origin, with the VM's grant) once it is ready and the inspector listens, else
        null."""
        spec = self.server.spec(request)
        rc, out, err = await self.server.run(["mitm", "show", "--json", spec], 60)
        from .server import error_text

        if rc:
            return web.json_response({"error": error_text(err)}, status=502)
        try:
            data = json.loads(out)
            answer = {k: data[k] for k in ("mode", "status", "ready")}
            answer["problem"] = (data.get("mitmweb") or {}).get("problem")
            answer["url"] = (
                self.url(request, spec) if answer["ready"] and (self.port or self.origin) else None
            )
        except (ValueError, KeyError, TypeError):
            return web.json_response({"error": "beanjar mitm show printed no state"}, status=502)
        return web.json_response(answer, headers={"Cache-Control": "no-store"})
