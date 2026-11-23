# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""beanjar-web: VMs in a browser (docs/dev/architecture/web-viewer.md).

An aiohttp server, on loopback by default, behind a token (the URL carries it once, and it becomes
a cookie), a password (a login page) or, on loopback only, nothing (access.py); HTTPS with
--certificate and --key. `beanjar-web start` runs it as a transient systemd user unit, `enable` as a
persistent one (unit.py). Everything about a VM goes through the manager's CLI, `beanjar ...`,
for this machine's VMs (local/NAME) and for other hosts' (HOST/NAME) alike, so the server opens no
VM socket and no tunnel of its own:

- /api/vms: `beanjar list --json`;
- POST /vm/HOST/NAME/start or /stop: `beanjar start` or `beanjar stop`, accepted immediately;
  /vm/HOST/NAME/operation and /api/vms carry progress and the manager's result; /stop?force=1 is
  `beanjar stop --force`, for a VM that runs for a reason the page has shown and had confirmed;
  /stop?unplug=1 is `beanjar stop --unplug` (QEMU killed at once), the stop dialog's Unplug;
- POST /vm/HOST/NAME/vnc: `beanjar vnc start` unless it runs (the manager's refusal otherwise);
- WebSocket /vm/HOST/NAME/rfb?page=ID: binary messages to and from `beanjar vnc pipe` (noVNC's
  RFB), the picture paused while the same page's /video plays (rfbpause.py);
- WebSocket /vm/HOST/NAME/remote: "down CODE" / "up CODE" to `beanjar remote pipe`, and on a VM
  with a touch screen "touch KIND SLOT X Y WxH" (the page's touch events, in noVNC's framebuffer); what is held is released
  and the touches that are down end when the socket drops;
- WebSocket /vm/HOST/NAME/audio?format=opus|pcm: `beanjar vnc audio-pipe`'s stream (its header,
  then Opus packets or PCM; host/vnc/audio.h) as binary messages, after the format's request line;
- WebSocket /vm/HOST/NAME/video?bitrate=N&page=ID: `beanjar vnc video-pipe`'s stream (its header, then
  H.264 access units; host/vnc/video.h) as binary messages, after the request line "h264
  bitrate=N" (kbit/s);
- /vm/HOST/NAME/layout.json: `beanjar remote layout --json` (the GTK remote's data);
- /vm/HOST/NAME/screenshot.png: `beanjar screenshot NAME -`;
- /vm/HOST/NAME/inspector.json: `beanjar mitm show --json`, and where the VM's traffic inspector
  (mitmweb) opens: /vm/HOST/NAME/mitm/... of a listener of its own (--inspector-port), another
  origin, HTTP and its WebSocket through `beanjar mitm connect` (mitm.py).
With --hosts, only the VMs of those hosts (`local`: this machine's): the list asks only them, and a
route naming another host ({host} in its path) is 403 before any `beanjar` runs (hosts_only).
The pages are static files beside this module; noVNC is Debian's, served as it is from /novnc/,
and so are Debian's Bootstrap Icons (bootstrap-icons), from /icons/.
No request is logged.
"""

import argparse
import asyncio
import hmac
import json
import os
import re
import shlex
import signal
import sys

from aiohttp import WSMsgType, web

from . import access
from .mitm import Inspectors
from .rfbpause import RfbPause

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
COOKIE = "beanjar_web"
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
AUDIO_FORMATS = ("opus", "pcm")
VIDEO_BITRATES = (100, 50000)  # kbit/s, as beanjar-vnc takes them
PAGE_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")  # a page's id, pairing its /rfb and /video
EDGE_RE = re.compile(r"(down|up) (0x[0-9a-fA-F]{1,2}|[0-9]{1,3})")
# a touch on the touch screen: kind, slot (0 to 9), x y in a frame of WxH (noVNC's framebuffer)
TOUCH_RE = re.compile(
    r"touch (begin|move|end|cancel) ([0-9]) ([0-9]{1,5}) ([0-9]{1,5}) ([0-9]{1,5}x[0-9]{1,5})"
)
# the pointer while the picture is turned (the page maps it itself): x y buttons in a frame of WxH
POINTER_RE = re.compile(r"pointer ([0-9]{1,5}) ([0-9]{1,5}) ([0-7]) ([0-9]{1,5}x[0-9]{1,5})")
ROTATIONS = (
    "0",
    "90",
    "180",
    "270",
)  # beanjar sensor set rotate's angles (the set turned; 0 is standing)
LOCAL = "local"  # this machine in HOST/NAME (the manager's remote.LOCAL)
DEFAULT_PORT = 8059
CHUNK = 1 << 16
RFB_HEARTBEAT = 20  # seconds: paused or idle RFB still needs traffic through a WebSocket proxy


def expected_origin(request):
    """The browser's origin, trusting one local proxy hop, never a remote peer's headers.

    Cloudflare sends X-Forwarded-Proto; cloudflared preserves Host by default, or puts the
    public host in X-Forwarded-Host when httpHostHeader overrides it. CF-Visitor is redundant.
    A single http/https scheme and host are supported, not comma-separated proxy chains.
    """
    scheme, host = request.scheme, request.host
    peer = request.transport.get_extra_info("peername") if request.transport else None
    headers = request.headers
    if peer and access.is_loopback(peer[0]) and "X-Forwarded-Proto" in headers:
        protos = headers.getall("X-Forwarded-Proto")
        hosts = headers.getall("X-Forwarded-Host", [host])
        if len(protos) != 1 or protos[0] not in ("http", "https") or len(hosts) != 1:
            raise web.HTTPBadRequest(
                text="invalid forwarded origin: one http/https proxy hop required"
            )
        scheme, host = protos[0], hosts[0]
        if not host or any(c.isspace() or c in ",/\\?#@" for c in host):
            raise web.HTTPBadRequest(text="invalid forwarded host")
    return f"{scheme}://{host}"


def foreign(request):
    """Whether a browser sent the request for a page of another site or origin, other than by
    opening a page (a top-level navigation, a link followed): Fetch Metadata. The inspector origin
    (mitm.py) is another origin of the same site, whose page must not use this origin's cookie;
    a request without these headers (not a browser's, or an old one) is not refused here."""
    site = request.headers.get("Sec-Fetch-Site")
    if site not in ("same-site", "cross-site"):
        return False
    return not (
        request.headers.get("Sec-Fetch-Mode") == "navigate"
        and request.headers.get("Sec-Fetch-Dest") == "document"
    )


def error_text(err):
    """The manager's message in its stderr: the JSON {"error"} object (--json), else its text."""
    lines = [line.strip() for line in err.splitlines() if line.strip()]
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and "error" in obj:
            return str(obj["error"])
    return "; ".join(line.removeprefix("beanjar: ") for line in lines) or "beanjar failed"


def row_host(row):
    """A `beanjar list --json` row's host as HOST/NAME names it: local for this machine's."""
    return row.get("host") or LOCAL


class Page:
    """A page's /rfb relays and its /video streams playing: the relays' picture is paused while
    one plays."""

    def __init__(self):
        self.relays = set()
        self.videos = 0


class Thumbnail:
    """One shared picture and at most one refresh, including negative Off results."""

    def __init__(self):
        self.mode = None
        self.png = self.error = self.task = None
        self.rotation = 0  # the guest's rotation when the picture was taken (a portrait model's)
        self.loaded = False
        self.checked = 0
        self.version = 0

    def mode_changed(self, mode):
        if mode != self.mode:
            self.invalidate()
            self.mode = mode

    def invalidate(self):
        self.png = self.error = None
        self.rotation = 0
        self.loaded = False
        self.checked = 0
        self.version += 1


class Relay:
    """An /rfb relay: the page's bytes through RfbPause to `vnc pipe`'s stdin."""

    def __init__(self, proc):
        self.proc = proc
        self.filter = RfbPause()

    def send(self, data):
        if data and not self.proc.stdin.is_closing():
            self.proc.stdin.write(data)

    def pause(self):
        self.send(self.filter.pause())

    def resume(self):
        self.send(self.filter.resume())


class Server:
    def __init__(
        self, beanjar, access, novnc, icons, vnc_args=(), tls=False, hosts=None, inspector=None
    ):
        self.beanjar = list(beanjar)
        self.hosts = tuple(hosts) if hosts else None  # --hosts: only these hosts' VMs
        self.access = access
        self.tls = tls
        self.login_lock = asyncio.Lock()
        self.novnc = novnc
        self.icons = icons
        self.vnc_args = list(vnc_args)
        self.procs = set()  # the pipes running, ended at shutdown
        self.pages = {}  # (HOST/NAME, page id): Page, while it has a /rfb or a /video open
        self.operations = {}  # HOST/NAME: latest start/stop result, until the next request/restart
        self.tasks = set()
        self.thumbnails = {}  # HOST/NAME: Thumbnail; shared by every page
        self.list_lock = asyncio.Lock()
        self.list_cache = None  # (rows, error), shared by list polling and every preview
        self.list_checked = 0
        self.inspectors = Inspectors(self)
        self.inspectors.origin = inspector  # --inspector-origin

    # -- the manager --
    async def spawn(self, words, stdin=False):
        proc = await asyncio.create_subprocess_exec(
            *self.beanjar,
            *words,
            stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self.procs.add(proc)
        return proc

    async def run(self, words, timeout=120):
        """(status, stdout bytes, stderr text) of `beanjar WORDS`."""
        proc = await self.spawn(words)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as e:
            # Background preview refreshes are cancelled at shutdown. End this CLI's children
            # too (e.g. ssh), which otherwise keep its output pipes and the request alive.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.communicate()
            if isinstance(e, asyncio.CancelledError):
                raise
            return 1, b"", f"beanjar {words[0]} did not answer within {timeout} s"
        finally:
            self.procs.discard(proc)
        return proc.returncode, out, err.decode(errors="replace")

    async def end(self, proc, grace=3.0):
        """A pipe's end: its stdin closed, then a wait, then SIGTERM."""
        if proc.stdin and not proc.stdin.is_closing():
            proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), grace)
        except asyncio.TimeoutError:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            await proc.wait()
        self.procs.discard(proc)

    # -- requests --
    @staticmethod
    def spec(request):
        host, name = request.match_info["host"], request.match_info["name"]
        if not (NAME_RE.fullmatch(host) and NAME_RE.fullmatch(name)):
            raise web.HTTPBadRequest(text="invalid HOST/NAME")
        return f"{host}/{name}"

    def allowed(self, host):
        return self.hosts is None or host in self.hosts

    @web.middleware
    async def hosts_only(self, request, handler):
        """--hosts: every route naming a host ({host} in its path) refused for another, before its
        handler runs any `beanjar`."""
        host = request.match_info.get("host")
        if host is not None and not self.allowed(host):
            raise web.HTTPForbidden(text=f"beanjar-web: host {host} is not served here (--hosts)")
        return await handler(request)

    @web.middleware
    async def auth(self, request, handler):
        """The session and this server's Origin for POSTs and WebSockets, in every mode.

        An absent Origin is allowed for non-browser clients. Browsers send it for cross-site
        POSTs (including forms); a null Origin is refused too.
        """
        access = self.access
        own_origin = expected_origin(request)
        origin = request.headers.get("Origin")
        if request.method == "POST" or request.headers.get("Upgrade", "").lower() == "websocket":
            if origin and origin != own_origin:
                raise web.HTTPForbidden(text="cross-origin request")
            if request.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
                raise web.HTTPForbidden(text="cross-site request")
        if foreign(request):
            # another origin's page (the inspector's, another port of this host) fetching with
            # this origin's cookie
            raise web.HTTPForbidden(text="beanjar-web: a request from another site or origin")
        if access.mode == "token" and "token" in request.query:
            if not hmac.compare_digest(request.query["token"], access.secret):
                raise web.HTTPForbidden(text="wrong token")
            query = {k: v for k, v in request.query.items() if k != "token"}
            response = web.HTTPFound(str(request.rel_url.with_query(query)))
            self.set_session(response, request)
            raise response
        open_paths = request.path in ("/login", "/style.css") or request.path.startswith("/icons/")
        if not (access.mode == "password" and open_paths) and not access.allows(
            request.cookies.get(COOKIE, "")
        ):
            if access.mode == "password":
                if request.method == "GET" and (
                    request.path.endswith("/") or request.path.endswith(".html")
                ):
                    raise web.HTTPFound("/login")
                raise web.HTTPForbidden(text="beanjar-web: log in first (/login)")
            raise web.HTTPForbidden(
                text="beanjar-web: open the URL it printed (with its token; beanjar-web status "
                "prints it) first"
            )
        return await handler(request)

    def set_session(self, response, request):
        response.set_cookie(
            COOKIE,
            self.access.session,
            httponly=True,
            samesite="Strict",
            path="/",
            secure=self.tls or expected_origin(request).startswith("https://"),
        )

    async def login_page(self, request):
        return web.FileResponse(
            os.path.join(STATIC, "login.html"), headers={"Cache-Control": "no-cache"}
        )

    async def login(self, request):
        """The login form's password, compared in constant time; attempts one at a time, a wrong
        one answered after a second."""
        form = await request.post()
        given = str(form.get("password", ""))
        async with self.login_lock:
            if not hmac.compare_digest(given.encode(), self.access.secret.encode()):
                await asyncio.sleep(1.0)
                raise web.HTTPSeeOther("/login?failed=1")
        response = web.HTTPSeeOther("/")
        self.set_session(response, request)
        raise response

    async def logout(self, request):
        response = web.HTTPSeeOther("/login")
        response.del_cookie(COOKIE, path="/")
        raise response

    async def session(self, request):
        return web.json_response({"auth": self.access.mode})

    async def page(self, request):
        name = request.match_info.get("page") or "index.html"
        return web.FileResponse(os.path.join(STATIC, name), headers={"Cache-Control": "no-cache"})

    async def viewer(self, request):
        self.spec(request)
        return web.FileResponse(
            os.path.join(STATIC, "viewer.html"), headers={"Cache-Control": "no-cache"}
        )

    async def vm_rows(self, max_age=3):
        """One manager list for concurrent pages. Previews reuse a page's ten-second poll
        through a little scheduling/network slack (15 s), then share a fallback refresh.
        Errors are cached too, so an unavailable host is not retried for every card.
        """
        async with self.list_lock:
            if (
                self.list_cache is not None
                and asyncio.get_running_loop().time() - self.list_checked < max_age
            ):
                return self.list_cache
            if self.hosts is None:
                rows, message = await self.list_rows([])
            else:
                # --hosts: each asked alone (`list --host H`), no other host waited for; what one
                # that fails would have shown is missing, as `beanjar list` leaves out such a host
                found = await asyncio.gather(*(self.list_rows(["--host", h]) for h in self.hosts))
                errors = [m for _, m in found if m]
                rows, message = None, "; ".join(errors)
                if len(errors) < len(found):
                    rows, message = [r for part, _ in found for r in part or ()], None
            if rows is not None:
                rows = [r for r in rows if self.allowed(row_host(r))]
            self.list_cache = rows, message
            self.list_checked = asyncio.get_running_loop().time()
            return self.list_cache

    async def list_rows(self, words):
        """(rows, None) of `beanjar list --json WORDS`, or (None, its error)."""
        rc, out, err = await self.run(["list", "--json", *words])
        if rc:
            return None, error_text(err)
        try:
            rows = json.loads(out)
        except ValueError:
            return None, "beanjar list printed no JSON"
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            return None, "beanjar list printed no list of VMs"
        return rows, None

    async def vms(self, request):
        rows, message = await self.vm_rows()
        if message:
            return web.json_response({"error": message}, status=502)
        # what the GTK manager's rows show (gui.Manager._refresh_vm), nothing of paths or keys
        keep = ("name", "host", "state", "detail", "platform", "firmware", "display", "mem", "smp")
        keep += ("uptime",)
        return web.json_response(
            [
                dict(
                    {k: r.get(k) for k in keep},
                    viewer=bool(r.get("viewer")),
                    vnc=bool(r.get("vnc")),
                    gpu=(r.get("gpu") or {}).get("headline") if r.get("pid") else None,
                    mitm=str(r.get("network") or "").endswith("+mitm"),
                    touch=bool(r.get("touch")),  # a touch screen: the viewer sends touches to it
                    portrait=bool(r.get("portrait")),  # turns to portrait: rotate, turned picture
                    reason=r.get("reason"),  # why it runs (beanjar status), None
                    tags=r.get("tags") or [],  # its tags (beanjar config: tags)
                    operation=self.operations.get(f"{row_host(r)}/{r['name']}"),
                )
                for r in rows
            ],
            headers={"Cache-Control": "no-store"},
        )

    async def vm_action(self, request):
        """Accept promptly; the CLI owns admission, boot and graceful-stop fallback."""
        spec = self.spec(request)
        previous = self.operations.get(spec)
        if previous and previous["status"] == "pending":
            return web.json_response(
                {"error": f"{spec}: {previous['action']} is still in progress"}, status=409
            )
        action = request.match_info["action"]
        operation = {
            "action": action,
            "status": "pending",
            "message": "Starting…" if action == "start" else "Stopping…",
        }
        if action == "stop" and request.query.get("force") == "1":
            operation["force"] = True
        if action == "stop" and request.query.get("unplug") == "1":
            operation.update(unplug=True, message="Unplugging…")
        self.operations[spec] = operation
        if spec in self.thumbnails:
            self.thumbnails[spec].invalidate()
        self.list_checked = 0
        task = asyncio.create_task(self.vm_action_run(spec, operation))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return web.json_response(
            operation, status=202, headers={"Location": f"/vm/{spec}/operation"}
        )

    async def vm_action_run(self, spec, operation):
        proc = None
        completed = False
        try:
            options = ["--force"] if operation.get("force") else []
            options += ["--unplug"] if operation.get("unplug") else []
            proc = await self.spawn([operation["action"], "--json", *options, spec])

            async def tail(stream):
                # Drain both pipes, retaining bounded diagnostics even for a long boot.
                data = b""
                while chunk := await stream.read(CHUNK):
                    data = (data + chunk)[-16384:]
                return data.decode(errors="replace").strip()

            _, err = await asyncio.gather(tail(proc.stdout), tail(proc.stderr))
            await proc.wait()
            completed = True
            if proc.returncode:
                message = error_text(err)
                operation.update(status="failed", error=message, message=message)
            else:
                operation.update(
                    status="succeeded", message=err or f"{spec}: {operation['action']} completed"
                )
        except OSError as e:
            operation.update(status="failed", error=str(e), message=str(e))
        finally:
            self.list_checked = 0
            if proc:
                # A manager may have launcher/ssh children. They belong to its recorded process
                # group, not to the VM's independently managed unit.
                if not completed:
                    for sig in (signal.SIGTERM, signal.SIGKILL):
                        try:
                            os.killpg(proc.pid, sig)
                        except ProcessLookupError:
                            break
                        if sig == signal.SIGTERM:
                            await asyncio.sleep(0.1)
                await proc.wait()
                self.procs.discard(proc)

    async def vm_operation(self, request):
        operation = self.operations.get(self.spec(request))
        if operation is None:
            return web.json_response({"error": "no start or stop requested"}, status=404)
        status = {"pending": 202, "succeeded": 200, "failed": 409}[operation["status"]]
        return web.json_response(operation, status=status, headers={"Cache-Control": "no-store"})

    async def rotate(self, request):
        """`beanjar sensor set rotate` with the body's angle (0, 90, 180 or 270): 200 with its JSON once the
        guest reports it, its refusal as 409."""
        spec = self.spec(request)
        angle = (await request.text()).strip()
        if angle not in ROTATIONS:
            raise web.HTTPBadRequest(text="the angle is 0, 90, 180 or 270")
        rc, out, err = await self.run(["sensor", "set", "rotate", "--json", spec, angle], 90)
        if rc:
            return web.json_response({"error": error_text(err)}, status=409)
        try:
            return web.json_response(json.loads(out))
        except ValueError:
            return web.json_response(
                {"error": "beanjar sensor set rotate said nothing readable"}, status=502
            )

    async def vnc_start(self, request):
        """beanjar-vnc for the VM, started unless it runs; the manager's refusal as 409."""
        spec = self.spec(request)
        rc, out, err = await self.run(
            ["vnc", "start", "--json", spec, "--port", "0", *self.vnc_args], 60
        )
        if rc == 0:
            return web.json_response({"started": True})
        message = error_text(err)
        if "beanjar-vnc is already serving" in message:
            return web.json_response({"started": False})
        return web.json_response({"error": message}, status=409)

    async def layout(self, request):
        rc, out, err = await self.run(["remote", "layout", self.spec(request), "--json"])
        if rc:
            return web.json_response({"error": error_text(err)}, status=502)
        return web.Response(body=out, content_type="application/json")

    async def screenshot(self, request):
        if request.query.get("thumbnail") == "1":
            return await self.thumbnail(request)
        rc, out, err = await self.run(["screenshot", self.spec(request), "-"])
        if rc or not out.startswith(b"\x89PNG"):
            return web.json_response({"error": error_text(err)}, status=502)
        return web.Response(
            body=out, content_type="image/png", headers={"Cache-Control": "no-store"}
        )

    async def thumbnail(self, request):
        """Return the cached picture immediately; stale live pictures refresh in the background.
        A first picture can wait. Eligibility uses the shared list, never a VM socket.
        """
        spec = self.spec(request)
        entry = self.thumbnails.setdefault(spec, Thumbnail())
        now = asyncio.get_running_loop().time()
        if self.list_cache and self.list_cache[0] is not None:
            entry.mode_changed(self.thumbnail_mode(self.list_cache[0], spec))
            if entry.mode is None:
                entry.loaded = True
        stale_list = self.list_cache is None or now - self.list_checked >= 15
        stale_picture = entry.mode == "live" and now - entry.checked >= 5
        if entry.task is None and (not entry.loaded or stale_picture or stale_list):
            task = asyncio.create_task(self.refresh_thumbnail(spec, entry))
            entry.task = task
            self.tasks.add(task)

            def done(task):
                self.tasks.discard(task)
                entry.task = None

            task.add_done_callback(done)
        if not entry.png and entry.task:
            await asyncio.shield(entry.task)  # one disappearing page does not cancel other pages
        if entry.png:
            return web.Response(
                body=entry.png,
                content_type="image/png",
                # the page turns the picture (the viewers' turn; index.js, style.css)
                headers={"Cache-Control": "no-store", "X-Beanjar-Rotation": str(entry.rotation)},
            )
        if entry.error:
            return web.json_response({"error": entry.error}, status=502)
        return web.Response(status=204, headers={"Cache-Control": "no-store"})

    @staticmethod
    def thumbnail_mode(rows, spec):
        vm = next((r for r in rows if f"{row_host(r)}/{r['name']}" == spec), None)
        if vm and vm.get("state") == "Off":
            return "last"
        if vm and vm.get("state") == "Running" and vm.get("vnc"):
            return "live"
        return None

    async def refresh_thumbnail(self, spec, entry):
        version = entry.version
        try:
            rows, message = await self.vm_rows(max_age=15)
            if message:
                entry.error = message
                return
            entry.mode_changed(self.thumbnail_mode(rows, spec))
            if entry.mode is None or (entry.mode == "last" and entry.loaded):
                return  # Off: --last only once, even when the answer is "no thumbnail yet"
            version = entry.version
            words = ["--last"] if entry.mode == "last" else ["--frame", "384x216"]
            rc, png, err = await self.run(["screenshot", spec, "-", *words], 30)
            if version != entry.version:
                return  # a VM action or a new list changed its eligibility while capturing
            if rc == 0 and png.startswith(b"\x89PNG"):
                entry.png, entry.error = png, None
                entry.rotation = 0
                vm = next((r for r in rows if f"{row_host(r)}/{r['name']}" == spec), None)
                if vm and vm.get("portrait"):
                    # live: the guest's report; kept (off): the angle the set stands at
                    entry.rotation = await self.guest_rotation(
                        spec, "rotation" if entry.mode == "live" else "saved"
                    )
            elif entry.mode == "live":
                entry.error = error_text(err)  # a stale picture survives a failed refresh
        except OSError as e:
            entry.error = str(e)
        finally:
            if version == entry.version:
                entry.loaded = True
                entry.checked = asyncio.get_running_loop().time()

    async def guest_rotation(self, spec, key="rotation"):
        """The rotation a portrait model's guest last reported (`beanjar sensor show rotate NAME`, key
        "rotation"), or the angle its set was left at ("saved", for a VM that is off); 0 when there
        is none."""
        rc, out, _err = await self.run(["sensor", "show", "rotate", "--json", spec], 15)
        try:
            angle = json.loads(out).get(key) if rc == 0 else 0
        except (ValueError, AttributeError):
            angle = 0
        return angle if angle in (90, 180, 270) else 0

    async def closing(self, ws, proc, what):
        """When the pipe's stdout ends: the socket closed with its error (code 4000, the manager's
        message as the reason), or normally."""
        try:
            await asyncio.wait_for(proc.wait(), 3)
        except asyncio.TimeoutError:
            pass
        err = (await proc.stderr.read()).decode(errors="replace")
        if not ws.closed:
            failed = proc.returncode not in (0, None)
            reason = error_text(err) if failed else f"the {what} ended"
            await ws.close(code=4000 if failed else 1000, message=reason[:120].encode())

    @staticmethod
    def page_key(request, spec):
        """(HOST/NAME, the page's id) from ?page=, or None without one."""
        page = request.query.get("page", "")
        if not page:
            return None
        if not PAGE_RE.fullmatch(page):
            raise web.HTTPBadRequest(text="page is 8 to 64 letters, digits, - or _")
        return spec, page

    def page_add(self, key, relay=None, video=0):
        if key is None:
            return
        page = self.pages.setdefault(key, Page())
        if relay:
            page.relays.add(relay)
            if page.videos:
                relay.pause()
        if video:
            page.videos += 1
            if page.videos == 1:
                for r in page.relays:
                    r.pause()

    def page_remove(self, key, relay=None, video=0):
        page = self.pages.get(key) if key else None
        if page is None:
            return
        page.relays.discard(relay)
        if video:
            page.videos -= 1
            if page.videos == 0:
                for r in page.relays:
                    r.resume()  # the picture back on RFB, the whole screen at once
        if not page.relays and not page.videos:
            del self.pages[key]

    async def rfb(self, request):
        """noVNC's RFB stream: each binary message to `vnc pipe`'s stdin, its stdout back as
        binary messages, until either side ends; with ?page=ID, no picture while that page's
        /video plays (rfbpause.py)."""
        spec = self.spec(request)
        key = self.page_key(request, spec)
        ws = web.WebSocketResponse(max_msg_size=0, heartbeat=RFB_HEARTBEAT)
        await ws.prepare(request)
        proc = await self.spawn(["vnc", "pipe", spec], stdin=True)
        relay = Relay(proc)
        self.page_add(key, relay=relay)

        async def downstream():
            try:
                while True:
                    data = await proc.stdout.read(CHUNK)
                    if not data:
                        break
                    await ws.send_bytes(relay.filter.server(data))
            except (ConnectionResetError, RuntimeError):
                pass
            await self.closing(ws, proc, "VNC stream")

        down = asyncio.ensure_future(downstream())
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY and not proc.stdin.is_closing():
                    relay.send(relay.filter.client(msg.data))
                    await proc.stdin.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self.page_remove(key, relay=relay)
            await self.end(proc, 1.0)
            await asyncio.gather(down, return_exceptions=True)
        return ws

    async def stream(self, request, words, line, what, started=None, ended=None):
        """A pipe's stream to the page: line to its stdin, its stdout back as binary messages,
        until either side ends. started(), if given, when its first bytes come; ended() when it
        ends (at once when the pipe's output does)."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        proc = await self.spawn(words, stdin=True)
        proc.stdin.write(line.encode())

        async def downstream():
            nonlocal started
            try:
                while True:
                    data = await proc.stdout.read(CHUNK)
                    if not data:
                        break
                    if started:
                        started()
                        started = None
                    await ws.send_bytes(data)
            except (ConnectionResetError, RuntimeError):
                pass
            if ended:
                ended()
            await self.closing(ws, proc, what)

        down = asyncio.ensure_future(downstream())
        try:
            async for _msg in ws:  # nothing is expected from the page
                pass
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            if ended:
                ended()
            await self.end(proc, 1.0)
            await asyncio.gather(down, return_exceptions=True)
        return ws

    async def video(self, request):
        """The TV picture as H.264 ("h264 bitrate=N" to `vnc video-pipe`), its stdout back as
        binary messages. A browser that falls behind holds the pipe up, and beanjar-vnc drops
        units for it, up to a keyframe, rather than wait."""
        spec = self.spec(request)
        try:
            bitrate = int(request.query.get("bitrate", "10000"))
        except ValueError:
            bitrate = -1
        if not VIDEO_BITRATES[0] <= bitrate <= VIDEO_BITRATES[1]:
            raise web.HTTPBadRequest(text="bitrate is 100 to 50000 (kbit/s)")
        key = self.page_key(request, spec)
        playing = False

        def started():  # the page's RFB picture paused from the stream's first bytes
            nonlocal playing
            playing = True
            self.page_add(key, video=1)

        def ended():
            nonlocal playing
            if playing:
                playing = False
                self.page_remove(key, video=1)

        return await self.stream(
            request, ["vnc", "video-pipe", spec], f"h264 bitrate={bitrate}\n", "video",
            started, ended,
        )  # fmt: skip

    async def audio(self, request):
        """The VM's mixed audio: the format's request line to `vnc audio-pipe`, its stdout back as
        binary messages, until either side ends. A browser that falls behind holds the pipe up,
        and beanjar-vnc drops blocks for it rather than wait."""
        spec = self.spec(request)
        fmt = request.query.get("format", "pcm")
        if fmt not in AUDIO_FORMATS:
            raise web.HTTPBadRequest(text="format is opus or pcm")
        return await self.stream(request, ["vnc", "audio-pipe", spec], f"{fmt}\n", "audio")

    async def remote(self, request):
        """The remote's button edges and touches to `remote pipe`; its lines back as text messages.
        A button still held when the socket drops is released, a touch still down ended."""
        spec = self.spec(request)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        proc = await self.spawn(["remote", "pipe", spec], stdin=True)
        held = set()
        touches = {}  # slot -> "X Y" where it is down
        pointer = None  # the last pointer line with its buttons ("X Y B WxH"), while one is held

        async def answers():
            try:
                async for line in proc.stdout:
                    await ws.send_str(line.decode(errors="replace").rstrip("\n"))
            except (ConnectionResetError, RuntimeError):
                pass
            await self.closing(ws, proc, "remote")

        said = asyncio.ensure_future(answers())
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                t = TOUCH_RE.fullmatch(msg.data.strip())
                if t:
                    if proc.stdin.is_closing():
                        break
                    if t[1] in ("end", "cancel"):
                        touches.pop(t[2], None)
                    else:
                        touches[t[2]] = f"{t[3]} {t[4]} {t[5]}"
                    proc.stdin.write(f"touch {t[1]} {t[2]} {t[3]} {t[4]} {t[5]}\n".encode())
                    await proc.stdin.drain()
                    continue
                p = POINTER_RE.fullmatch(msg.data.strip())
                if p:
                    if proc.stdin.is_closing():
                        break
                    pointer = p if p[3] != "0" else None
                    proc.stdin.write(f"pointer {p[1]} {p[2]} {p[3]} {p[4]}\n".encode())
                    await proc.stdin.drain()
                    continue
                m = EDGE_RE.fullmatch(msg.data.strip())
                code = int(m[2], 0) if m else -1
                if not 0 <= code <= 0xFF:
                    await ws.send_str("error: not 'down CODE' or 'up CODE', CODE 0 to 255")
                    continue
                if proc.stdin.is_closing():
                    break
                (held.add if m[1] == "down" else held.discard)(code)
                proc.stdin.write(f"{m[1]} 0x{code:02x}\n".encode())
                await proc.stdin.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            try:
                if not proc.stdin.is_closing():
                    for code in held:
                        proc.stdin.write(f"up 0x{code:02x}\n".encode())
                    for slot, at in touches.items():
                        proc.stdin.write(f"touch end {slot} {at}\n".encode())
                    if pointer:
                        proc.stdin.write(
                            f"pointer {pointer[1]} {pointer[2]} 0 {pointer[4]}\n".encode()
                        )
                    await proc.stdin.drain()
            except (ConnectionResetError, BrokenPipeError):
                pass
            await self.end(proc)
            await asyncio.gather(said, return_exceptions=True)
        return ws

    async def shutdown(self, _app):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.inspectors.shutdown()
        for proc in list(self.procs):
            await self.end(proc, 1.0)

    def app(self):
        app = web.Application(middlewares=[self.auth, self.hosts_only])
        vm = "/vm/{host}/{name}"
        app.router.add_get("/", self.page)
        app.router.add_get(
            "/{page:(index|remote)\\.html|style\\.css|tv-remote\\.svg|(viewer|remote|remote-draw|index|stop|audio|audio-worklet|video)\\.js}",
            self.page,
        )
        app.router.add_get("/api/vms", self.vms)
        app.router.add_get("/api/session", self.session)
        if self.access.mode == "password":
            app.router.add_get("/login", self.login_page)
            app.router.add_post("/login", self.login)
            app.router.add_post("/logout", self.logout)
        app.router.add_get(vm + "/", self.viewer)
        app.router.add_post(vm + "/{action:start|stop}", self.vm_action)
        app.router.add_get(vm + "/operation", self.vm_operation)
        app.router.add_post(vm + "/vnc", self.vnc_start)
        app.router.add_post(vm + "/rotate", self.rotate)
        app.router.add_get(vm + "/layout.json", self.layout)
        app.router.add_get(vm + "/screenshot.png", self.screenshot)
        app.router.add_get(vm + "/rfb", self.rfb)
        app.router.add_get(vm + "/remote", self.remote)
        app.router.add_get(vm + "/audio", self.audio)
        app.router.add_get(vm + "/video", self.video)
        app.router.add_get(vm + "/inspector.json", self.inspectors.status)
        app.router.add_static("/novnc/", self.novnc)
        app.router.add_static("/icons/", self.icons)
        app.on_shutdown.append(self.shutdown)
        return app

    def inspector_app(self):
        """The inspector origin's (mitm.py): each VM's traffic inspector and nothing else, behind
        its grant; shut down with the main app."""
        app = web.Application(middlewares=[self.inspectors.auth, self.hosts_only])
        vm = "/vm/{host}/{name}"
        app.router.add_get(vm + "/mitm", self.inspectors.slash)
        app.router.add_route("*", vm + "/mitm/{path:.*}", self.inspectors.handle)
        return app


def default_beanjar():
    """The core package beside this one in a checkout (host/core), else `beanjar` on PATH."""
    core = os.path.join(os.path.dirname(os.path.dirname(HERE)), "core")
    if os.path.isfile(os.path.join(core, "beanjar", "cli.py")) and not HERE.startswith("/usr/"):
        return f"env PYTHONPATH={shlex.quote(core)} {shlex.quote(sys.executable)} -m beanjar"
    return "beanjar"


COMMANDS = ("serve", "start", "enable", "disable", "stop", "status")


def parser():
    p = argparse.ArgumentParser(
        prog="beanjar-web",
        usage="beanjar-web [serve | start | enable] [options] | beanjar-web disable | "
        "beanjar-web stop | beanjar-web status",
        description="Beanjar's VMs in a browser: the TV picture (noVNC) and a popup remote "
        "(docs/dev/architecture/web-viewer.md). serve (the default) runs it here and prints the URL "
        "to open; start runs serve with the same options as the transient systemd user unit "
        "beanjar-web (gone at reboot); enable writes that unit as a file instead, enabled so it "
        "starts at boot (lingering on), and starts it; disable stops and removes it; stop stops "
        "the unit (an enabled one still starts at boot; start without options starts it again); "
        "status says whether it runs, where, the URL, and whether the unit is persistent.",
    )
    p.add_argument("--listen", default="127.0.0.1", help="an IPv4 or IPv6 address or a host name "
                   "(default 127.0.0.1; 0.0.0.0 or :: for every address)")  # fmt: skip
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"default {DEFAULT_PORT}; 0: any")
    p.add_argument("--inspector-port", type=int, metavar="N",
                   help="the traffic inspector's own listener (another origin; mitm.py): default "
                   "--port + 1, or any with --port 0")  # fmt: skip
    p.add_argument("--inspector-origin", metavar="URL",
                   help="the inspector listener's public origin when a proxy serves it, e.g. "
                   "https://inspector.example.org; default this server's host at the inspector "
                   "port")  # fmt: skip
    p.add_argument("--auth", choices=access.MODES, default="token",
                   help="token (default): a token in the URL, kept in --token-file; password: a "
                   "login page, the password in --password-file; none: loopback only")  # fmt: skip
    p.add_argument("--token-file", metavar="PATH",
                   help=f"default {access.default_token_file()}, made if missing")  # fmt: skip
    p.add_argument("--password-file", metavar="PATH",
                   help="default web.password beside the token file, made if missing")  # fmt: skip
    p.add_argument("--certificate", metavar="PATH", help="serve HTTPS: the certificate (PEM)")
    p.add_argument("--key", metavar="PATH", help="serve HTTPS: the certificate's key (PEM)")
    p.add_argument("--size", metavar="WxH", help="beanjar-vnc's desktop when it starts it")
    p.add_argument(
        "--fps", type=int, metavar="N", help="beanjar-vnc's frame rate when it starts it"
    )
    p.add_argument("--novnc", default="/usr/share/novnc", help="noVNC's files (Debian's novnc)")
    p.add_argument(
        "--icons",
        default="/usr/share/bootstrap-icons/svg",
        help="Bootstrap Icons' SVG files (Debian's bootstrap-icons)",
    )
    p.add_argument(
        "--beanjar", default=None, metavar="COMMAND", help="the manager's command (default beanjar)"
    )
    p.add_argument("--hosts", action="append", metavar="HOST[,HOST...]",
                   help="only these hosts' VMs (beanjar host list; local: this machine's, left out "
                   "unless named); every other host's routes refused")  # fmt: skip
    p.add_argument("--state-file", help=argparse.SUPPRESS)  # start: where serve says it listens
    return p


def prepare(p, a):
    """The options checked, the secret's file made: (Access, ssl context or None). p.error (exit
    2) for what a restart would not fix."""
    if not os.path.isfile(os.path.join(a.novnc, "core", "rfb.js")):
        p.error(f"no noVNC at {a.novnc} (apt install novnc, or --novnc DIR)")
    if not os.path.isfile(os.path.join(a.icons, "house.svg")):
        p.error(f"no Bootstrap Icons at {a.icons} (apt install bootstrap-icons, or --icons DIR)")
    if not 0 <= a.port <= 65535:
        p.error(f"--port {a.port}: 0 to 65535")
    if a.inspector_port is None:
        a.inspector_port = a.port + 1 if 0 < a.port < 65535 else 0
    if not 0 <= a.inspector_port <= 65535 or (a.port and a.inspector_port == a.port):
        p.error(f"--inspector-port {a.inspector_port}: 0 to 65535, not --port")
    if a.inspector_origin:
        a.inspector_origin = inspector_origin(p, a.inspector_origin)
    if a.auth != "password" and a.password_file:
        p.error("--password-file is for --auth password")
    if a.auth == "none" and a.token_file:
        p.error("--token-file is for --auth token and password")
    if bool(a.certificate) != bool(a.key):
        p.error("--certificate and --key go together")
    a.hosts = check_hosts(p, a.hosts, a.beanjar or default_beanjar())
    try:
        acc = access.load(a.auth, a.listen, a.token_file, a.password_file)
    except access.Error as e:
        p.error(str(e))
    tls = None
    if a.certificate:
        import ssl

        tls = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        try:
            tls.load_cert_chain(a.certificate, a.key)
        except (OSError, ssl.SSLError) as e:
            p.error(f"--certificate {a.certificate} --key {a.key}: {e}")
    return acc, tls


def inspector_origin(p, value):
    """--inspector-origin as scheme://host[:port], or p.error."""
    from yarl import URL

    try:
        u = URL(value)
    except ValueError:
        u = None
    if (
        u is None
        or u.scheme not in ("http", "https")
        or not u.host
        or u.path not in ("", "/")
        or u.query_string
        or u.fragment
        or u.user
    ):
        p.error(f"--inspector-origin {value!r}: an origin, http(s)://HOST[:PORT], nothing more")
    return str(u.origin())


def check_hosts(p, given, beanjar_command):
    """--hosts as a tuple of host names, None without it. Each must be local or a host of
    `beanjar host list`: a name not there is p.error, so a typo cannot show an empty list."""
    if not given:
        return None
    if len(given) > 1:
        p.error("--hosts once, the hosts separated by commas: --hosts HOST[,HOST...]")
    names = [h.strip() for h in given[0].split(",")]
    if not all(names):
        p.error(f"--hosts {given[0]!r}: an empty host name")
    for h in names:
        if not NAME_RE.fullmatch(h):
            p.error(f"--hosts: {h!r} is not a host name")
    names = tuple(dict.fromkeys(names))
    if all(h == LOCAL for h in names):
        return names
    import subprocess

    try:
        r = subprocess.run([*shlex.split(beanjar_command), "host", "list", "--json"],
                           capture_output=True, timeout=30)  # fmt: skip
        configured = [h["name"] for h in json.loads(r.stdout)] if r.returncode == 0 else None
        why = error_text(r.stderr.decode(errors="replace")) if r.returncode else None
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, KeyError) as e:
        configured, why = None, str(e) or type(e).__name__
    if configured is None:
        p.error(f"--hosts: cannot read the configured hosts (beanjar host list): {why}")
    for h in names:
        if h != LOCAL and h not in configured:
            known = ", ".join(configured) or "none"
            p.error(f"--hosts: no host named {h} (beanjar host list: {known}; {LOCAL}: this "
                    "machine)")  # fmt: skip
    return names


def where(a, scheme, port, acc, with_token=True):
    """The URL to open, and what it listens on."""
    every = access.url_host(a.listen)[1]
    on = f"every address ({a.listen})" if every else a.listen
    return access.url(scheme, a.listen, port, acc, with_token), f"{on}, port {port}"


def warn_plain(a, tls):
    if not tls and not access.is_loopback(a.listen):
        print(
            f"beanjar-web: warning: plain HTTP on {a.listen}: the "
            f"{'password' if a.auth == 'password' else 'token'} and the picture cross the network "
            "in clear (--certificate and --key for HTTPS)",
            file=sys.stderr,
            flush=True,
        )


def write_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def serve(argv):
    p = parser()
    a = p.parse_args(argv)
    acc, tls = prepare(p, a)
    scheme = "https" if tls else "http"
    warn_plain(a, tls)
    vnc_args = (["--size", a.size] if a.size else []) + (["--fps", str(a.fps)] if a.fps else [])
    server = Server(
        shlex.split(a.beanjar or default_beanjar()), acc, a.novnc, a.icons, vnc_args, bool(tls),
        a.hosts, a.inspector_origin,
    )  # fmt: skip

    async def run():
        import signal

        stop = asyncio.Event()  # a signal from now on ends it cleanly, even before it listens
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        runner = web.AppRunner(server.app(), access_log=None)
        await runner.setup()
        # the traffic inspector's own origin (mitm.py), on the same address
        inspector = web.AppRunner(server.inspector_app(), access_log=None)
        await inspector.setup()
        site = web.TCPSite(runner, a.listen or None, a.port, ssl_context=tls)
        isite = web.TCPSite(inspector, a.listen or None, a.inspector_port, ssl_context=tls)
        for s, n in ((site, a.port), (isite, a.inspector_port)):
            try:
                await s.start()
            except OSError as e:
                await inspector.cleanup()
                await runner.cleanup()
                print(f"beanjar-web: cannot listen on {a.listen} port {n}: {e}", file=sys.stderr)
                return 2
        port = site._server.sockets[0].getsockname()[1]
        server.inspectors.port = isite._server.sockets[0].getsockname()[1]
        url, on = where(a, scheme, port, acc, with_token=not a.state_file)
        if a.state_file:
            # under start: no token in the journal; status reads it from its file
            write_state(a.state_file, {"pid": os.getpid(), "listen": a.listen, "port": port,
                                       "inspector_port": server.inspectors.port,
                                       "scheme": scheme, "auth": a.auth, "file": acc.path,
                                       "hosts": list(a.hosts) if a.hosts else None})  # fmt: skip
            print(f"beanjar-web: listening on {on}, {scheme}, --auth {a.auth}", flush=True)
        else:
            more = f" (listening on {on})" if access.url_host(a.listen)[1] else ""
            print(f"beanjar-web: {url}{more}", flush=True)
        if a.hosts:
            print(f"beanjar-web: only the VMs of --hosts {','.join(a.hosts)}", flush=True)
            if a.auth == "password":
                print(f"beanjar-web: the password is in {acc.path}", flush=True)
        await stop.wait()
        await inspector.cleanup()  # its WebSockets closed before the main app's shutdown ends
        await runner.cleanup()  # the inspectors' `mitm connect`s
        return 0

    return asyncio.run(run())


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    command = argv[0] if argv and argv[0] in COMMANDS else "serve"
    if argv and argv[0] in COMMANDS:
        argv = argv[1:]
    if command == "serve":
        return serve(argv)
    from . import unit

    if command in ("start", "enable"):
        p = parser()
        a = p.parse_args(argv)
        if a.state_file:
            p.error("--state-file is serve's")
        if command == "start" and not argv and unit.persistent(unit.unit_name()):
            return unit.start(a, a.beanjar or default_beanjar(), with_options=False)
        warn_plain(a, prepare(p, a)[1])
        if command == "enable":
            return unit.enable(a, a.beanjar or default_beanjar())
        return unit.start(a, a.beanjar or default_beanjar())
    if argv:
        sys.exit(f"beanjar-web {command} takes no options")
    return {"stop": unit.stop, "disable": unit.disable}.get(command, unit.status)()


if __name__ == "__main__":
    sys.exit(main())
