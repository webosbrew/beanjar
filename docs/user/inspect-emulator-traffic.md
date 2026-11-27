# Inspect emulator traffic (`mode = "mitm"`)

`mode = "mitm"` (the launcher's `NET_MODE=mitm`), the default, runs **mitmweb in tun mode** inside
the emulator's network namespace. Every TCP and UDP packet the guest sends, on any port and to any
host (LG's included), goes through it: each connection is recorded, decrypted when it is TLS and
the guest trusts our CA, and forwarded live to its real destination. DNS lookups and other UDP show
as flows too. Nothing is answered locally and no host is special. Only ICMP (ping) goes out
directly, as with `mode = "direct"`. The settings are in
[docs/dev/architecture/network.md](../dev/architecture/network.md). Started with
`NETSTUB_WEB_HOST=0.0.0.0` (below), the inspector is at **http://HOST-ADDRESS:18083/** (with its
password, below).

## Install mitmproxy

Beanjar does not ship mitmproxy: the inspector runs your own, **mitmproxy 12.0.1 or newer**.
Debian 13 has no package of it; install it with pipx:

```sh
sudo apt install pipx
pipx install mitmproxy
```

Beanjar runs `NETNS_MITMWEB` if that is set in the environment of the start, else `mitmweb` on
`PATH`, else `~/.local/bin/mitmweb` (where pipx puts it, which a systemd user unit's `PATH` often
lacks). Each start reads its version. Without mitmweb, or with one older than 12.0.1, the VM
still starts and its guest goes out directly, uninspected; the reason, with the line above, is
printed by `beanjar start`, shown by `beanjar mitm show NAME` (`status`, and `mitmweb.problem` in
`--json`) and in the manager's Traffic Inspector card. `beanjar mitm show NAME --json` reports the
running mitmweb's `version` and `path` too. A directory `~/.local/share/beanjar/mitmproxy` left
by an earlier Beanjar is no longer used and can be deleted.

## What the guest sees differently

Because every connection goes through mitmweb, which opens its own connection to the server as
soon as the guest connects:

- **A closed port looks open for a moment.** The guest's connection is accepted, then closed at
  once when the server refuses mitmweb's; an unreachable server makes the guest's connection hang
  until the guest's own timeout. No flow is listed for either.
- **DNS is slower.** Lookups through the inspector took about 2.5 to 3.7 times as long as direct
  ones, and about 1% of them were lost under a flood of lookups (1200 in a row); ordinary use
  retries them unnoticed.
- **Lookups fill the flow list.** Every DNS query is a flow, so the 5000 kept flows hold fewer web
  requests than the number suggests.
- **ping is not inspected** (ICMP goes out directly).

Open the inspector before launching the screens you want to see. Useful filters: `~d lgtvsdp.com`
for SDP, `~u authentication` for authentication calls, `~d lge.com` for LG services; LG's hosts
appear only with `lg_services = "real"`, since the default mock answers them inside the VM's
network without a flow (its requests are in `netns-lgmock.log`). Select a
flow to see its request and response headers and body. The inspector keeps flows in memory for the
current VM run, and restarting the VM clears them; `beanjar mitm clear NAME` empties the list
without a restart. It keeps the last 5000 flows: beyond that the
oldest finished ones are dropped, from memory and from the view (`beanjar config set NAME mitm_flows=N`
while the VM is off, `0` for all of them, `unset mitm_flows` for 5000 again; `MITM_FLOWS` for `vm-launcher`). `mitm_flow_bytes=200M`
also bounds the bytes of bodies they hold (off by default; `MITM_FLOW_BYTES`): beyond it the
oldest finished flows go too. Live updates use the same web port.

For example, from the checkout:

```sh
NETSTUB_WEB_HOST=0.0.0.0 DISPLAY_BACKEND=dbus LAYERS=normal,gl,rootssh,devmode host/scripts/vm-launcher start
```

The host bridge listens on `NETSTUB_WEB_HOST` (default the VM's own address, `127.0.1.<slot>`,
this machine only; `beanjar show` and `host/scripts/vm-launcher config ADDRESS` print it) and
`NETSTUB_WEB_PORT` (default `18083`). `NETSTUB_WEB_HOST=0.0.0.0` opens it to the LAN; from another
machine, an SSH tunnel to `127.0.1.<slot>:18083` does the same without that. A `link = "lan"` VM
serves it on its own LAN address as well (`http://LAN-ADDRESS:18083/`, the same password), beside
its TV-service ports; the host itself still uses `127.0.1.<slot>`, since it cannot reach its own
macvlan children. It shows raw device
and session headers, so redact exports before sharing them.

## The password

Every inspector needs a password, on loopback and on the LAN alike. Each VM start makes a random
one, unless `BEANJAR_MITM_PASSWORD` is set in the environment of the start (`beanjar start`, the
manager, `vm-launcher`), which is then used instead. It is kept in an owner-only file in the VM's
run directory, `<vm>/run/mitmweb.password` (the checkout VM's `build/mitmweb.password`), and
removed when the VM stops. It never appears on a command line, in a unit's environment or in a log.

- **Manager:** the Traffic Inspector pane shows the password with **Copy Password**. **Open** (Open
  in Browser) opens a link that logs in by itself; the copy button beside the URL copies the plain
  address. For a
  `link = "lan"` VM the address shown and copied is the LAN one, and "On this host" the local one,
  which Open uses (the browser runs on the host).
- **CLI:** `beanjar show NAME` gives `inspector.url` (the LAN URL for a `link = "lan"` VM, with
  `inspector.local_url` for the host itself) and `inspector.password_file`, the file's
  path rather than the password, because status output ends up in terminals, logs and issues:
  `cat` that file for the password.
- **Browser:** mitmweb shows a login page; enter the password. A link of the form
  `http://ADDRESS:18083/?token=PASSWORD` logs in directly and sets a cookie for that mitmweb run.
- **Scripts:** send `Authorization: Bearer PASSWORD`; without it mitmweb answers 403.

mitmweb holds only an argon2 hash of the password (`mitm_addon.py` reads the file). A password
is set once per start: restarting the VM makes a new one unless `BEANJAR_MITM_PASSWORD` fixes it.

mitmweb runs in a bubblewrap sandbox (`bwrap`, which beanjar needs for the inspector): it sees the
system, its own mitmproxy installation and the library's CA, not your home directory or the VM's
shared folders. Once it runs, the options that load scripts or files, open listeners or change the
password (`scripts`, `save_stream_file`, `map_local`, `confdir`, `mode`, `web_password` and the
like) cannot be changed from its web interface or API, and the commands that take a file path
(`script.run`, `save.file`, `options.load` ...) are refused. Interception, filters, the view's
settings and the other options in its Options menu work as before. To save flows, use the web
interface's download (File, Save), which goes to the browser.

## What the flows mean

- **HTTP flows with a response** were forwarded live. The response is the real server's, errors
  included: nothing is replaced with a local answer.
- **"Streamed body, not recorded"**: a large request or response (over `NETSTUB_STREAM_LARGE`,
  default 1 MiB), or an open-ended binary one, passed through without being kept.
- **TLS handshake errors** in the event log: the guest rejected our certificate (pinning, or a
  trust store without our CA), or the proxy could not reach the server or complete its TLS
  handshake (a 502 flow for a server-side failure). That connection fails; subsequent connections
  to the same named endpoint and port pass through undecrypted and appear as **TCP flows**,
  except LG's hosts with `lg_services = "mock"` (the default), which keep failing instead of
  reaching LG unread (`tls-refused`).
  Without SNI, the bypass applies to the destination address and port. `build/netstub.log`
  records `tls-failed` and `tls-passthrough` events. Automatic bypass rules are bounded to 256
  endpoints per proxy run, with the oldest evicted; manual `ignore_hosts` rules are preserved.
  Bypassing interception cannot repair an unreachable upstream server.
- The proxy does not verify the servers' certificates (`ssl_insecure`), so LG's `*.lgtvsdp.com`
  (private root, CN-only) is decrypted like any other host.
- The guest trusts our CA (the library's `ca/`, or the development library's), which it appends at
  boot to the firmware's system bundle, sdx's `sdp-ca.pem` and the hashed `/etc/ssl/certs`.
  The firmware's hosts file and routes stay stock. With `lg_services = "mock"` (the default)
  the guest's name server is Beanjar's own at `10.0.2.254`: LG's telemetry names do not
  resolve and are not listed, `http://lgtvonline.lge.com/`, LG's SDP server and LG's other web
  hosts are answered by it (never listed), and the lookups
  it forwards are listed from `10.0.2.254` ([LG services](../dev/architecture/network.md#lg-services)).
- The device identity (the VM's provisioned model key) is loaded
  whenever the VM has one (`<vm>/identity/sestore.toml`, including managed development VMs),
  whatever the network is.

## Troubleshooting

- `build/netstub-mitm.log`: mitmweb's startup and proxy errors. The VM's own log
  (`build/qemu.out`) says which mitmweb and version a start found, or why the inspector is off.
- `build/netns-relay.log`: the host-side port relay (the inspector and the forwarded ports),
  including a port it could not bind.
- `build/netns-relay-inner.log`: the namespace-side relay (connections to mitmweb and the guest).
- `build/netns-helper.log`: DHCP helper output.
- `build/netns-uplink.log`: the slirp4netns uplink.
- `build/netstub.log`: TLS handshake failures (`tls-failed`).
- `build/qemu.out`: VM launcher output, including the inspector bind address and the password
  file's path.

The inspector is optional: a missing, too old or failing mitmweb leaves the guest going out
directly, and mitmweb exiting later does the same from then on. Guest SSH/developer ports retain
their loopback-only bindings.
