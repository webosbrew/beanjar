# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""mitmproxy addon for NET=mitm: live capture of the guest's HTTP(S), with no host treated specially.

The NET=mitm namespace (host/core/beanjar/network/netns.py) loads it into mitmweb, which runs in tun mode
(`--mode tun:mitm0`, the user's own mitmproxy 12.0.1 or newer: host/core/beanjar/network/mitmtool.py). Every
TCP and UDP packet of the guest is routed into mitmweb's TUN device, which reads each connection's
real destination from its packets, and mitmproxy forwards it there itself. There is no host list,
no stub response and no relay. This addon only adds generic behaviours:

- No upstream verification. The proxy is a debug tool: netns.py sets ssl_insecure, so a server's
  certificate is accepted whatever its root or names (LG's *.lgtvsdp.com is CN-only, from a
  private root) and its flows are decrypted.
- Best-effort TLS passthrough. A client or upstream TLS failure leaves that connection failed,
  then bypasses interception for subsequent connections to the SNI (else address) and port.
  Anchored, escaped ignore_hosts patterns preserve manual settings; at most 256 automatic
  entries are retained, oldest first. A bypass cannot repair unreachable servers or certificates
  the guest itself rejects. Each failure and new bypass is logged. With lg_services = "mock" (the
  VM's NET_LG_SERVICES, default mock) an SNI of LG's (any class of lg_hosts.py) is never passed
  through: its connections keep failing (logged as tls-refused) rather than reach LG unread.
- A password for the inspector (WebPassword): mitmweb's web_password, from the file netns.py
  writes for the VM.
- Streaming. Bodies larger than NETSTUB_STREAM_LARGE bytes (default 1 MiB), and responses of
  unknown length that are not textual, are streamed rather than buffered; the flow stays in
  mitmweb without its body.
- A bounded history (FlowLimit): mitmweb keeps at most max_flows flows (vm.toml's
  network.mitm_flows, default 5000, 0 for no limit) and, if max_flow_bytes is set (vm.toml's
  network.mitm_flow_bytes, off by default), at most that many bytes of bodies, and drops the oldest
  finished flows beyond either, from memory and from the web view. Without it mitmweb grew by about 200 MB an hour.
- Frozen options (Freeze): once mitmweb runs, its API may no longer change the options that load
  code or files, open listeners or set the password (FROZEN), nor run the commands that take a
  path. netns.py also runs mitmweb in a sandbox that sees none of the user's files.
- Ready (Ready): the file netns.py waits for (NETNS_MITM_READY) once mitmweb's servers run, after
  mitmproxy's UDP idle timeout is raised (tun_udp_timeout).

Where it depends on mitmproxy's internals it checks the feature, not the version
(tun_udp_timeout).
"""

import collections
import logging
import os
import re
import sys
import time

from mitmproxy import ctx, http

sys.dont_write_bytecode = True  # no __pycache__ in the source tree
import lg_hosts  # noqa: E402  beside this file (mitmproxy loads a script with its directory on the path)

TEXTUAL = ("text/", "json", "xml", "javascript", "x-www-form-urlencoded", "html")


def stream_limit():
    return int(os.environ.get("NETSTUB_STREAM_LARGE", str(1 << 20)))


def log(*fields):
    path = os.environ.get("NETSTUB_LOG")
    if not path:
        return
    with open(path, "a") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + " ".join(str(x) for x in fields) + "\n")


def lg_mock():
    """Whether LG's services are mocked (NET_LG_SERVICES anything but real, as netns.py has it)."""
    return os.environ.get("NET_LG_SERVICES") != "real"


def endpoint(context):
    """'host:port' of a connection: the SNI if the client sent one, else the destination address."""
    if not context.server.address:
        return None
    address, port = context.server.address[:2]
    return f"{context.client.sni or address}:{port}"


class Capture:
    # Bound both our bookkeeping and the regex list on long-running VMs.
    MAX_TLS_BYPASSES = 256

    def __init__(self):
        self._tls_bypasses = collections.deque()

    def requestheaders(self, flow: http.HTTPFlow) -> None:
        length = flow.request.headers.get("content-length", "")
        if length.isdigit() and int(length) > stream_limit():
            flow.request.stream = True

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        if (
            flow.response is None
            or flow.request.method == "HEAD"
            or flow.response.status_code in (101, 204, 304)
        ):
            return
        length = flow.response.headers.get("content-length", "")
        kind = flow.response.headers.get("content-type", "").lower()
        if (
            (int(length) > stream_limit())
            if length.isdigit()
            else not any(t in kind for t in TEXTUAL)
        ):
            flow.response.stream = True
            flow.comment = "Streamed body, not recorded"

    def _tls_failed(self, context, why) -> None:
        key = endpoint(context)
        if not key:
            return
        logging.warning(f"netstub: {why} for {key}")
        log("tls-failed", key, why.replace(" ", "-"))
        cls = lg_hosts.lookup(context.client.sni) if lg_mock() else None
        if cls is not None:
            # lg_services=mock: nothing reaches LG unread; this host's connections keep failing
            logging.warning(f"netstub: {key} is LG's ({cls['class']}): not passed through")
            log("tls-refused", key, cls["class"])
            return
        # NextLayer (mitmproxy 12.2.3 and 12.3.1) matches unbracketed host:port,
        # including IPv6, case-insensitively against address, SNI and Host header.
        pattern = "^" + re.escape(key) + "$"
        patterns = list(ctx.options.ignore_hosts)
        if pattern in patterns:
            return
        # Forget entries removed manually, without restoring the user's removals.
        self._tls_bypasses = collections.deque(p for p in self._tls_bypasses if p in patterns)
        if len(self._tls_bypasses) >= self.MAX_TLS_BYPASSES:
            patterns.remove(self._tls_bypasses.popleft())
        patterns.append(pattern)
        ctx.options.update(ignore_hosts=patterns)
        self._tls_bypasses.append(pattern)
        logging.warning(f"netstub: subsequent connections to {key} bypass TLS interception")
        log("tls-passthrough", key, why.replace(" ", "-"))

    def tls_failed_client(self, data) -> None:
        self._tls_failed(data.context, "client rejected the interception certificate")

    def tls_failed_server(self, data) -> None:
        self._tls_failed(data.context, "upstream TLS failed")

    def tcp_message(self, flow) -> None:
        # Undecrypted hosts (automatic or manual ignore_hosts) can carry long downloads:
        # keep only the latest chunk for display.
        del flow.messages[:-1]


class WebPassword:
    """The inspector's password: NETSTUB_WEB_PASSWORD_FILE (host/core/beanjar/network/netns.py writes it,
    owner-only) becomes mitmweb's web_password, as an argon2 hash, so the password is never on the
    command line, in the environment or in mitmweb's own log. mitmweb then answers 403 to anything
    without it, a login page in a browser; ?token=PASSWORD or Authorization: Bearer PASSWORD logs in
    (the cookie it sets lasts as long as this mitmweb)."""

    def load(self, loader) -> None:
        path = os.environ.get("NETSTUB_WEB_PASSWORD_FILE")
        if not path:
            return
        with open(path) as f:
            password = f.read().strip()
        if not password:
            raise RuntimeError(f"mitm_addon: {path} holds no password")
        import argon2
        from mitmproxy import ctx

        hasher = argon2.PasswordHasher()
        try:
            if hasher.verify(ctx.options.web_password, password):
                return  # already set (this addon reloaded): Freeze keeps the hash as it is
        except (argon2.exceptions.Argon2Error, argon2.exceptions.InvalidHashError):
            pass
        ctx.options.update(web_password=hasher.hash(password))


def flow_bytes(flow):
    """The bodies a flow keeps: request and response content, and TCP, UDP or WebSocket messages."""
    n = 0
    for m in (getattr(flow, "request", None), getattr(flow, "response", None)):
        raw = getattr(m, "raw_content", None)
        if isinstance(raw, bytes):
            n += len(raw)
    websocket = getattr(flow, "websocket", None)
    for messages in (getattr(flow, "messages", None), websocket and websocket.messages):
        n += sum(len(x.content) for x in messages or ())
    return n


class FlowLimit:
    """Keep at most max_flows flows in mitmweb's view (0: all of them) and, when max_flow_bytes is
    set (0: no limit), at most that many bytes of their bodies (flow_bytes). Beyond either, the
    oldest flows that are no longer live are removed with the view's own view.flows.remove, which
    drops them from the web view as well; a live flow (a connection still open) is never removed,
    so no traffic is cut. Removal by hand (the web view's delete or clear) is fine: the next
    flows simply fill up to the limits again."""

    def __init__(self):
        self.sizes = collections.OrderedDict()  # flow id -> flow_bytes, oldest first, as first seen
        self.total = 0

    def load(self, loader) -> None:
        loader.add_option(
            "max_flows",
            int,
            5000,  # beanjar's vmsettings.MITM_FLOWS; netns.py always passes it
            "Flows mitmweb keeps; the oldest finished ones are dropped beyond it. 0: no limit.",
        )
        loader.add_option(
            "max_flow_bytes",
            int,
            0,
            "Bytes of bodies the kept flows may hold; the oldest finished flows are dropped "
            "beyond it. 0: no limit.",
        )

    def _add(self, flow, *_) -> None:
        size = flow_bytes(flow)
        self.total += size - self.sizes.get(flow.id, 0)
        self.sizes[flow.id] = size  # an update keeps its place
        self._trim()

    def _forget(self, fid):
        self.total -= self.sizes.pop(fid)

    def _trim(self) -> None:
        from mitmproxy import ctx

        count, budget = ctx.options.max_flows, ctx.options.max_flow_bytes
        view = ctx.master.addons.get("view")
        if len(self.sizes) > view.store_count() + 64 or (budget > 0 and self.total > budget):
            for fid in [i for i in self.sizes if view.get_by_id(i) is None]:
                self._forget(fid)  # removed by hand
        over = lambda: (
            (count > 0 and view.store_count() > count) or (budget > 0 and self.total > budget)
        )
        if not over():
            return
        for fid in list(self.sizes):
            flow = view.get_by_id(fid)
            if flow is None:
                self._forget(fid)
            elif not flow.live:  # one still open stays: tried again on a later flow
                self._forget(fid)
                view.remove([flow])
            if not over():
                break

    # every hook through which a flow enters the view (mitmproxy.addons.view.View), the ones that
    # end a flow, so that a flow kept while live goes once it is done, and the ones that add to it
    requestheaders = request = response = error = _add
    tcp_start = tcp_end = tcp_error = _add
    udp_start = udp_end = udp_error = _add
    dns_request = dns_response = dns_error = _add
    websocket_message = websocket_end = _add


# How long an idle UDP flow is kept (s). mitmproxy's Python side closes an idle UDP flow
# after mitmproxy.proxy.server.UDP_TIMEOUT (20 s in 12.3.1) while its TUN side keeps that 5-tuple,
# so a client that reuses one UDP socket loses every datagram after a pause of 20 s or more, until
# it has been silent long enough for the TUN side to let go. connman's DNS proxy is such a client:
# its upstream socket to 10.0.2.3:53 lives as long as the guest, so the guest's every lookup timed
# out. Measured on a guest (one socket, a query after each idle gap): with 20 s, the query after 30 s
# idle was lost while those after 0 s, 10 s and 65 s were answered; with 600 s, those after 30, 65
# and 130 s were all answered, and connman's lookups worked again. The same applies to any UDP port
# (an NTP query on one socket after 30 s and 65 s idle was answered with 600 s).
TUN_UDP_TIMEOUT = 600


def tun_udp_timeout(server=None):
    """Raise mitmproxy's UDP idle timeout to TUN_UDP_TIMEOUT (a feature check: the module
    attribute must exist and be a number); a warning naming the version and False if it cannot."""
    import mitmproxy.version

    if server is None:
        try:
            import mitmproxy.proxy.server as server
        except ImportError:
            server = None
    current = getattr(server, "UDP_TIMEOUT", None)
    if isinstance(current, bool) or not isinstance(current, (int, float)):
        logging.warning(
            f"netstub: mitmproxy {mitmproxy.version.VERSION} has no proxy.server.UDP_TIMEOUT; "
            "idle UDP flows (the guest's DNS) may stop working"
        )
        return False
    if current < TUN_UDP_TIMEOUT:
        server.UDP_TIMEOUT = TUN_UDP_TIMEOUT
    return True


# What mitmweb's API (`PUT /options`, `POST /commands/...`) may no longer change once mitmweb runs
# (Freeze). Its API applies any option and runs any command for whoever has the password or runs a
# script on its origin; these would have mitmweb load code, read or write files by path, open
# listeners, or change who may log in. netns.py's sandbox already limits the files mitmweb sees;
# this keeps them from being reconfigured at all.
FROZEN = frozenset(
    (
        # code and files by path
        "scripts",  # loads Python addons
        "save_stream_file",  # writes (or appends to) a flow file
        "hardump",  # writes a HAR file
        "rfile",  # reads a flow file
        "client_replay",  # reads flow files
        "server_replay",
        "map_local",  # serves local files to the client
        "web_static_viewer",  # writes a static viewer
        "protobuf_definitions",  # reads a file
        "proxyauth",  # "@path": an htpasswd file
        # the CA and certificates
        "confdir",
        "certs",
        "client_certs",
        "cert_passphrase",
        "ssl_verify_upstream_trusted_ca",
        "ssl_verify_upstream_trusted_confdir",
        # listeners, the web interface and its password
        "mode",
        "listen_host",
        "listen_port",
        "server",
        "web_host",
        "web_port",
        "web_password",
        "web_debug",
        "web_open_browser",
    )
)
# Every option of mitmweb 12.3.1, with the ones this addon adds, each reviewed for the list above:
# those not in FROZEN may change as before (intercept, the filters, the view's order and columns,
# ignore_hosts and the other proxy behaviour the Options menu offers). An option a later mitmproxy
# adds is frozen too until it is reviewed here, and logged once at the start.
REVIEWED = FROZEN | frozenset(
    """
    add_upstream_certs_to_client_chain allow_hosts anticache anticomp block_global block_list
    block_private body_size_limit ciphers_client ciphers_server client_replay_concurrency
    command_history connect_addr connection_strategy console_focus_follow
    content_view_lines_cutoff dns_name_servers dns_use_hosts_file emudhra emudhra_old
    export_preserve_original_ip http2 http2_ping_keepalive http3 http_connect_send_host_header
    ignore_hosts intercept intercept_active keep_alt_svc_header keep_host_header key_size
    modify_body modify_headers map_remote normalize_outbound_headers onboarding onboarding_host
    proxy_debug rawtcp readfile_filter request_client_cert save_stream_filter
    server_replay_extra server_replay_ignore_content server_replay_ignore_host
    server_replay_ignore_params server_replay_ignore_payload_params server_replay_ignore_port
    server_replay_kill_extra server_replay_nopop server_replay_refresh server_replay_reuse
    server_replay_use_headers show_ignored_hosts showhost ssl_insecure stickyauth stickycookie
    store_streamed_bodies stream_large_bodies strip_ech tcp_hosts tcp_timeout
    tls_ecdh_curve_client tls_ecdh_curve_server tls_version_client_max tls_version_client_min
    tls_version_server_max tls_version_server_min udp_hosts upstream_auth upstream_cert
    validate_inbound_headers view_filter view_order view_order_reversed web_columns web_theme
    websocket max_flows max_flow_bytes
    """.split()
)
# Commands refused beside those that take a path (mitmproxy.types.Path: script.run, save.file,
# export.file, options.load, options.save and the like): browser.start runs a host program.
REFUSED_COMMANDS = frozenset(("browser.start",))


class Freeze:
    """Once mitmweb runs, the options in FROZEN (and any not in REVIEWED) keep their values: an
    update that changes one fails with an OptionsError and is rolled back whole, and mitmweb's API
    answers 400. The check subscribes to the options (OptManager.subscribe), whose subscribers are
    told before any addon's configure hook, so a refused `scripts` is never loaded. A reset
    (options.reset, which mitmproxy does not roll back) has the frozen values put back. Commands
    that take a path, or are in REFUSED_COMMANDS, are refused too; the web interface's own
    commands (export, view.flows.create, the command history) take none."""

    def __init__(self):
        self.frozen = {}

    def running(self) -> None:
        from mitmproxy import ctx

        options = ctx.options
        new = sorted(n for n in options.keys() if n not in REVIEWED)
        if new:
            logging.warning(f"netstub: options not reviewed, frozen: {', '.join(new)}")
        self.frozen = {n: getattr(options, n) for n in options.keys() if n in FROZEN or n in new}
        options.subscribe(self._check, list(self.frozen))
        self._refuse_commands(ctx.master.commands)

    def _check(self, options, updated) -> None:
        from mitmproxy import exceptions

        changed = sorted(
            n for n in updated if n in self.frozen and getattr(options, n) != self.frozen[n]
        )
        if not changed:
            return
        for n in changed:  # a rollback restores them as well; a reset has none
            options._options[n].set(self.frozen[n])
        logging.warning(f"netstub: refused a change of {', '.join(changed)}")
        raise exceptions.OptionsError(
            f"{', '.join(changed)} cannot be changed while this inspector runs"
        )

    def _refuse_commands(self, manager) -> None:
        from mitmproxy import exceptions, types

        for name, command in manager.commands.items():
            if name in REFUSED_COMMANDS or any(p.type is types.Path for p in command.parameters):

                def refused(*_args, _name=name, **_kwargs):
                    raise exceptions.CommandError(f"{_name} is not available in this inspector")

                command.func = refused


class Ready:
    """The ready file netns.py waits for (NETNS_MITM_READY), written once mitmweb's servers run,
    after the UDP timeout is raised when it serves a TUN device (tun_udp_timeout): netns.py routes
    nothing into the device before that file exists."""

    def running(self) -> None:
        from mitmproxy import ctx

        modes = [m for m in ctx.options.mode if m.startswith("tun")]
        if modes:
            tun_udp_timeout()
        ready = os.environ.get("NETNS_MITM_READY")
        if ready:
            with open(ready, "w") as f:
                f.write(f"{len(modes)}\n")


addons = [Capture(), WebPassword(), FlowLimit(), Freeze(), Ready()]
