# Guest networking

A VM's network is two settings in its `vm.toml`:

```toml
[network]
link = "nat"        # nat | lan | off
mode = "mitm"       # mitm | direct   (ignored when link = "off")
listen = "0.0.0.0"  # link = "nat" only: where the TV-service ports listen (default: the slot address)
address = "dhcp"    # link = "lan" with mode = "mitm" only: "dhcp" or "A.B.C.D/NN,gateway"
lan_access = "on"   # link = "nat" only: the LAN and this host's addresses too (default "off")
lg_services = "real" # mock | real: LG's online services (default "mock", below)
slot = 1            # assigned, not a setting
```

`link` says where the guest's NIC is attached, `mode` whether its traffic is intercepted. They are
set with `beanjar config set NAME link=lan mode=direct` (and the GUI's Network page), and reach the launcher
as `NET_LINK` and `NET_MODE` (`NET_ADDRESS` for `address`, `NET_LAN_ACCESS` for `lan_access`,
`NET_LG_SERVICES` for `lg_services`). The defaults are `link = "nat"` and `mode = "mitm"`. LG's
own hosts are treated apart only by `lg_services` ([LG services](#lg-services)). Earlier
canned-response, selective relay and zero-egress-default designs, and the single
`NET=off | direct | mitm` setting these two replaced, are historical.

| `link` | `mode` | Guest connection | Host implementation |
|---|---|---|---|
| `nat` | `mitm` | NAT to the internet, every TCP and UDP connection passing through mitmweb (tun mode); ICMP direct | Rootless network namespace, TAP, routing into mitmweb's TUN device and slirp4netns uplink |
| `nat` | `direct` | NAT without the proxy | The same rootless namespace and slirp4netns uplink, without mitmweb |
| `lan` | `direct` | The guest itself on the host's wired LAN as a TV of its own: its own DHCP lease, every port at that address, SSDP and mDNS natively, nothing intercepted, relayed or mocked; this host cannot reach it | A macvlan on `lan_parent` with the VM's MAC, joined to the guest's TAP by tc in a rootless namespace that has no address of its own (below) |
| `lan` | `mitm` | On the LAN at its own address and MAC, with SSDP/mDNS discovery and best-effort web capture | A LAN namespace gets a macvlan on `lan_parent` with the VM's MAC and its DHCP lease or static `address`; the private guest sits behind it, with service and discovery relays (below) |
| `off` | ignored | NIC present with link down and no uplink | Isolated QEMU hub port; QMP sets link down before execution resumes |

Rules the settings enforce:

- `listen` belongs to `link = "nat"` and `address` to `link = "lan"` with `mode = "mitm"`; either one
  elsewhere is a validation error. Changing the link clears it in the same command (`set tv1
  link=lan listen=`).
- `link = "lan"` with `mode = "direct"` needs `lg_services = "real"` ([below](#lan-with-direct):
  nothing can mock LG's services there) and takes no `forward` (every guest port is on its own LAN
  address already).
- `lan` needs the library's `lan_parent` (`beanjar library config set lan_parent=eth0`), the host's wired
  interface, named explicitly and never guessed: a VPN interface such as ZeroTier may sit in the
  same subnet as the LAN. It must exist, and a wireless one is refused, at `config` and at a `lan` start
  (`/sys/class/net/IF/wireless`): macvlan and macvtap generally fail on Wi-Fi.
- `lan` VMs never collide with each other's ports: each has its own LAN address. With macvlan the
  host itself cannot reach its own children unless it adds a host-side macvlan of its own.
- Privileges: a small helper with only `CAP_NET_ADMIN` creates the macvlan on the allowed
  `lan_parent` only, with the VM's MAC, in that VM's namespace, for both modes. The rest of
  Beanjar stays rootless.

The guest's kernel command line keeps `webos.net=mitm|direct|off`, informational.

## What the guest reaches

The guest runs untrusted code (LG's firmware, user apps), so what it can open a connection to on
the host is attack surface. Rules, in every mode:

- **The host's loopback, never.** No setting reaches `127.0.0.0/8` or `::1` of the host. QEMU's own
  user networking (`-netdev user`) maps the router address `10.0.2.2` to the host's `127.0.0.1`
  and has no switch against it, which on one PC let a guest drive an unauthenticated Docker API;
  so no mode uses it. `nat` in both modes runs in the rootless namespace below, whose uplink is
  slirp4netns with `--disable-host-loopback`: `10.0.2.2` is refused. `lan` is in a namespace of its
  own, where the host's loopback does not exist; with `direct` the guest's frames do not even enter
  that namespace's IP stack, and go out on the LAN only.
- **The LAN and the host's own addresses, only with `lan_access = "on"`** (`link = "nat"`; `link =
  "lan"` is on the LAN by definition, and with `direct` reaches the host as any other LAN device
  would, below). slirp4netns opens its sockets in the host's namespace, where
  the host's LAN, bridge (`docker0`, `virbr0`) and VPN addresses are local, so a wildcard service
  there is reachable unless refused. Without LAN access the guest reaches neither the LAN nor them.
- **The internet** as each mode has it: through mitmweb (`mitm`) or straight out (`direct`). DNS is
  slirp4netns's resolver at `10.0.2.3`, which asks the host's own resolver, LAN or loopback, from
  the host side.

They are policy rules of the inner namespace (`netns.block_rules`, `host_rules`; routing, no
netfilter, as the interception), `prohibit` so a refused connection fails at once:

| Pref | To | When |
|---|---|---|
| 90 | `127.0.0.0/8`, `0.0.0.0/8` | always |
| 90 | LG's literal addresses (`lg_hosts.py`); the guest's DNS (UDP and TCP 53, TCP 853) to any server but the namespace's own | `lg_services = "mock"` ([LG services](#lg-services)) |
| 90 | `10.0.0.0/8` (but the guest's own `10.0.2.0/24`), `172.16.0.0/12`, `192.168.0.0/16`, `100.64.0.0/10`, `169.254.0.0/16`, `224.0.0.0/4` | without LAN access |
| 91 | each of the host's addresses outside those ranges (a public address), `/32` | without LAN access |
| 100 | the interception into mitmweb (above) | `mitm`, once mitmweb is ready |

They come before the interception and have no `iif`, so they hold for the guest's forwarded packets
and for mitmweb's own upstream connections, and for the fallback without mitmweb. The namespace's
own addresses (its relays, mitmweb's web port, the DHCP server) are the local table's at pref 0,
before them, as before. The pref 91 list is not a snapshot of the start: the outer process watches
the host's addresses (`netns.HostWatch`: `ip -o monitor address`, and a full read every 30 s) and
writes them to `host-addresses` in the socket directory, and the inner namespace adds and removes
its rules to match within 0.2 s (`netns.HostRules`, from `_wait_guest`'s loop), the new ones before
the old go. A start whose rules cannot be added stops rather than run the guest without them.
IPv6 needs no rule: slirp4netns runs without `--enable-ipv6` and the namespace forwards no IPv6,
so the guest has link-local IPv6 on the TAP at most.

A service for one guest alone therefore runs in its namespace, not on the host: there the TAP's
own address, `10.0.2.254` (`netns.DHCP_SERVER`), is local, so the guest reaches whatever listens
on it, as it reaches the LG services mock below, and nothing else does. `host/scripts/vm-launcher
netns-exec [--dir VM-DIR] COMMAND...` runs a command there (`nsenter` into the running QEMU's
user and network namespaces, `netns.enter_argv`; refused for `link = "off"`). The TV suite's test
server (`tests/regress/http-token.py`, `scripts/regress --tv`) runs so on a port of its own at
`10.0.2.254`, and sees each request from the guest's own `10.0.2.15`: no host address, no LAN
access.

The cost is that every networked VM needs `unshare`, `ip`, slirp4netns and unprivileged user
namespaces, `mode = "direct"` too, which once needed none of them. Debian 13 on amd64 and arm64
(the PC under WSL2, the Raspberry Pi, the RK3399 boards) has them all; the Debian package depends on
slirp4netns and iproute2. A host without user namespaces has `link = "off"` only.

## LG services

`lg_services` (`vm.toml`'s `network.lg_services`, `NET_LG_SERVICES`) is `mock`, the default, or
`real`, one or the other for the whole VM. `link = "lan"` with `mode = "direct"` refuses `mock`
(validation and start): see [`lan` with `direct`](#lan-with-direct). `real` is the network as described elsewhere on this
page, with nothing done about LG: a VM with a real model key behaves as a set does, and the CA is
there for inspection only. `mock` keeps the guest from LG's telemetry and cloud services and
answers LG's online check, LG's SDP server and LG's other web hosts itself, in `mitm` and
`direct` alike, and whether mitmweb runs, is missing or has failed: nothing goes to LG. The
study behind it is
[docs/research/lg-services-block-or-mock.md](../../research/lg-services-block-or-mock.md).

The host classes are one table, `host/helpers/network/lg_hosts.py`: per class its DNS name
patterns, any literal addresses, and what `mock` does (`refuse`, `online`, `answer` or `reach`;
no class is reached at present), each with
a comment naming its row of the study. Refused: crash and analytics uploads (`rdx2.*` on the SDP
domains, `*.rdl.lgtvcommon.com`), beacons (`*cdpbeacon*.lgtvcommon.com`), ads (`lgsmartad.com`),
ACR (`alphonso.tv`, `acrcloud.com`, `gracenote.com`), ThinQ, IoT, push and billing
(`lgtviot.com`, `lgthinq.com`, `ebp2.lgsmartplatform.com`), UEI QuickSet and its Azure IoT hub,
firmware update and its download logs (`snu`, `su`, `su-ssl`, `nsu` and the `-dev` hosts of
`lge.com`, and `156.147.69.32`), remote diagnostics (`rone-*.lge.com`, `165.244.62.249`) and the
voice cloud. Answered: SDP (`lgtvsdp.com`, `nextlgsdp.com`, `wiselg.com`, `wiselg.cn`), and
the rest of LG's domains (Home content, store, LG Account, ngfts: `lge.com`, `lgtvcommon.com`,
`lgappstv.com`, `lgwebostv.com`, `lgsmartplatform.com`). All of them count as LG's for the TLS
rule below.

Enforcement is below mitmweb, in the inner namespace, beside the loopback and LAN rules:

- **Names.** `host/helpers/network/lg_mock.py` runs in the inner namespace on the TAP's own
  address, `10.0.2.254` (`netns.MOCK_DNS`), which the namespace's DHCP server hands out as the
  guest's name server instead of slirp4netns's `10.0.2.3`. On UDP and TCP port 53 it answers
  NXDOMAIN for every name of a refused class, whatever the record type; resolves
  `lgtvonline.lge.com` and every name of an answered class to itself (an A record, no AAAA);
  and forwards every other query unchanged to `10.0.2.3`, from
  `10.0.2.254`, returning slirp's answer as it is (SERVFAIL if none comes in two tries of 4 s).
  The guest's queries to that address are the local table's (pref 0), so no mode sends them
  anywhere else. In `mitm` two more interception rules (pref 100, `from 10.0.2.254/32 ipproto
  udp|tcp dport 53`) route the resolver's own upstream queries into mitmweb, so the inspector
  still lists every lookup that leaves, now from `10.0.2.254`; refused and answered names never
  appear there. `netns-lgmock.log` in the VM's logs records each refused or answered name (once
  a minute at most). A start whose resolver does not come up stops.
- **Around the resolver.** Rules at pref 90 refuse the guest's UDP and TCP port 53 and TCP 853
  (DNS over TLS) to any other address (`iif tap0`, so the resolver's own upstream is not
  matched): a resolver compiled into a program, slirp's `10.0.2.3` or `8.8.8.8`, gets no answer.
  DNS over HTTPS is not covered.
- **Literal addresses.** The table's addresses get `prohibit` rules at pref 90 with no `iif`,
  like the loopback ones: the guest's connection fails at once with "No route to host" (ICMP
  administratively prohibited, not a timeout), in `mitm` before mitmweb sees it, and mitmweb's
  own upstream connections are refused too.
- **The online check.** `lg_mock.py` serves port 80 on `10.0.2.254` with LG's own answer to
  `GET http://lgtvonline.lge.com/` (fetched 2026-11-21): `200 OK`, `Content-Type: text/html`,
  `Server: Apache`, `Accept-Ranges: bytes`, `X-ConnMan-Status: online` and its 347-byte "LDAS
  Service html Test" page, without the `Date`, `Last-Modified` and `ETag` that vary between LG's
  servers, and closing the connection (`lg_mock.ONLINE_RESPONSE`). connman's online check
  (`Ipv4StatusUrl` in `/etc/connman/main.conf` on every release) needs a 200; from W18R on a
  successful lookup alone counts as online (`TakeDnsIpResolutionAsOnline`), which the answer
  gives too. The answer is in the namespace rather than in mitmweb so that `direct`, and `mitm`
  without a working mitmweb, are online the same way. A request on port 80 whose `Host` is an
  answered name gets that name's answer instead (below).
- **SDP and LG's web hosts.** `host/helpers/network/lg_sdp.py`, inside `lg_mock.py`, serves HTTPS
  on port 443 of `10.0.2.254` (and HTTP on 80 by `Host`) and forwards nothing. Its certificate
  for each name the client asks for (SNI) is issued by the library's interception CA
  (`NETSTUB_CA`, `tlsca.leaf`: the system's `openssl ca`, RSA 2048, SHA-256, valid 2000 to 2049
  as the CA), minted on first use into `lgmock-certs/` in the socket directory with one leaf key;
  a client without SNI gets one certificate for every answered domain and its `*.` names. TLS 1.0
  and its ciphers are allowed for the old clients. The guest trusts that CA wherever it is mocked:
  `qemu.py` puts `netstub-ca.pem` in the per-VM layer in `direct` too, and the initramfs adds it to
  `ca-certificates.crt`, `sdp-ca.pem` and NSS as in `mitm`. Clients with their own roots (Node)
  fail the handshake (`tls-failed` in `netns-lgmock.log`); nothing reaches LG either way.

  The answers are data, `host/helpers/network/lg_answers.py`: an ordered list of routes, each a
  host class, methods and a path pattern, with a status, a JSON body and headers. `${now_ms}`,
  `${country}`, `${country3}` and `${ric}` are filled in per request (the country from the TV's
  `X-Device-Country` where the table has it, else the VM's `country` setting, which the launcher
  passes as `lg_mock.py --country`, as LG's server answers from the TV's address, else the host's
  prefix, else US). Every 2xx answer carries `X-Server-Time`
  (Unix milliseconds, the host's clock), from which sdx sets the TV's clock (`clock/setTime`,
  source `sdp`). The shapes are the firmware clients' (sdx, uploadd, pmlogd, firstuse; WT1H, W19P,
  W21A, W22O number1, W24P and queue read statically, and LG's own mock data in W22O's rootfs):

  A TV whose smart service country is still its group's "Others" row of sdx's table (`US7`,
  `EU7`, ...: FirstUse has not stored its country yet) gets no answer: its SDP host
  (`us7.nextlgsdp.com`) does not resolve (SERVFAIL) and a request naming such a country in
  `X-Device-Country` is closed unanswered, both logged as `withheld`. A set has no network until
  FirstUse has stored the country; sdx, answered earlier, takes the real country for a change and
  raises LG's restart prompt (`lg_sdp.first_use_pending`, [country
  setting](../../research/country-setting.md)). With `lg_services = "real"` nothing is withheld.

  | Path (per layout) | Answer |
  |---|---|
  | v5.1 (WT1H): `rest/sdp/v5.1/authentication` (GET, DELETE) | `authentication` with `deviceSecret`, `sessionID`, `deviceUniqueID`; `Set-Cookie` |
  | v5.1: `notice`, `rest/appstore/v5.1/device/hardwarefeature`, `app/update` | empty notice lists, `fck` 0, no updates |
  | v5.1: `services`, `loggingstatus` | notice, country, `account.termsList` empty, `loggingStatus` with every unit off |
  | v7.0 to v14.0 (`rest/sdp/vN/`), 6.5.0, 9.0.0, 11.0.0 (`init/x.y.z/`): `initservices` | `initServices` with `authentication` (`sessionID` and the token layout's `accessToken`, `refreshToken`, `accessTokenDuration`; `blockStolenTV` `N`), `activationDate`, `deviceFeatureResult`, `services`; `blockStolenTV` at the root too; `WMONID` and `JSESSIONID` cookies |
  | `authentication` (any method) | the same `authentication` object; `accessTokenDuration` in milliseconds, one day (sdx refreshes a token with 600 s or less left at once and sends no `initservices` with it) |
  | `authentication/refresh` (POST, 6.5.0 on) | `accessToken` and `accessTokenDuration` at the root, no `authentication` wrapper (sdx's `getRefreshTokenData`) |
  | `services`, `loggingstatus` | `services` (below), `loggingStatus` |
  | `notice`, `eula` (GET), `eulamapping` | empty `noticeList`, `eulaList` (no `eulas`), `eulaMappingList` |
  | `eula` (POST), `deviceinfo` (terms withdrawal), `serverstatus/status` | 200 `{}` (the last for its `X-Server-Time`) |
  | `acrconfig` | 404: no ACR configuration, so no ACR |
  | `getConfig` on LG's other hosts (smartConfig) | sdx's own default, `smartConfigVer` 0.0, no items |
  | anything else | 404 `{}`, logged as `unknown` |

  `services` has `loggingStatus` with `normal_log`, `crash_log` and `crash_log_nosyslog` all
  `switch` `off` and the whitelist not updated (`updateYn` `N`), so uploadd schedules no upload
  ("no server-side consent") and tells rdxd both switches are off; `itemList` with every feature
  switch LG's clients ask for `off`; the TV's country and region code; empty notice, terms, app
  and blocked lists; and a `serverDomain` of version 0, never newer than the firmware's own, so no
  domain list is fetched. Country detection (`country`) is not answered, so the TV keeps the
  country it was set up with. Each request is logged as `answered METHOD URL STATUS (route)` or
  `unknown ...`, its query reduced to the parameter names; no header or body is logged.
- **TLS.** In `mitm` the addon (`mitm_addon.Capture`) passes no host of the table through
  undecrypted: when the guest rejects the proxy's certificate for an LG name (SNI), or the
  upstream TLS fails, the connection fails as before but no `ignore_hosts` bypass is added, so
  later connections fail too instead of reaching LG unread (`tls-refused` in `netstub.log`).
  Other hosts keep the automatic pass-through. mitmweb's sandbox binds `lg_hosts.py` read-only
  beside the addon, and `NET_LG_SERVICES` is in its environment.

Measured on WT1H (synthetic key, 2026-11-21) in `mitm` and in `direct`: `rdx2.lgtvsdp.com`,
`aic.cdpbeacon2.lgtvcommon.com`, `us.ad.lgsmartad.com`, `snu.lge.com` and `qt2-kic.lgtviot.com`
did not resolve, through connman's proxy or asked of `10.0.2.3` or `8.8.8.8` directly;
`www.youtube.com` and `us.lgtvsdp.com` did. Connections to `156.147.69.32:8080` and
`165.244.62.249:6120` failed with "No route to host" within 0.1 s. The online check returned the
page above and connman reported the wired service online (`onInternet: yes`,
`isInternetConnectionAvailable: true`, `dns1: 10.0.2.254`); the boot reached `boot-done` with the
first app launched. In `mitm` the inspector listed the forwarded lookups from `10.0.2.254` and
none of the refused ones; WT1H's sdx still reached `EU7.lgtvsdp.com` (SDP, reached in this
phase). With `lg_services = "real"` (`direct`) the guest's name server was `10.0.2.3` again and
the same names resolved, but for `rdx2.lgtvsdp.com`, which LG's own DNS does not have.
`tests/integration/network/test_netns.py` (`test_reach_lg_services`) checks the resolver, the
bypass refusal, the literal address, the online check and an SDP answer over HTTPS end to end
with a fake guest in both modes, and that `real` has none of them; `tests/unit/launch/
test_lg_sdp.py` the certificates, the answers per SDP layout and the fallback.

With SDP answered (2026-11-21, headless, on the PC): WT1H (synthetic key) in `mitm` and in
`direct` asked for `rest/sdp/v5.1/authentication`, `hardwarefeature` and `services`, all
answered; sdx reported `isDeviceAuthenticated: true` with the session `emulator-session`, the
clock was set from `X-Server-Time` (`systemTimeSource: sdp`, the host's second), uploadd logged
`SET_LOG_SWITCH {"analytics":0,"crash":0}`, no `AUTH.ERR` was logged and the boot reached
`boot-done` with LiveTV as the first app. Its only other LG request, `lgrecommends`' `api`, got
the 404. The inspector had no flow at all: every name WT1H looks up is LG's. W22O number1 with
a real model key (`1D492999_a` of a W22O export) in `mitm` sent `initservices` (answered, twice:
before and after the country was known), `loggingstatus`, `serverstatus/status`, `app/update` and
`acrconfig` (404); `app/appResource`, `app/resources/packagelist`, `device/udid` and nudge's
`reset_log` fell to the 404. sdx was authenticated with the clock from `sdp`, Home launched, and
the names it went on to look up for ThinQ, push, QuickSet, ads and `rdl` were refused. With
`generalTermsAllowed` then set to true through the settings service, as the terms screen does,
rdxd and uploadd went on making analytics tarballs (four in nine minutes, kept in
`/var/spool/uploadd/pending`), and a `kill -SEGV` of `sportsalarm` gave crashd's `CRASHED` and
rdxd's crash report in `/tmp/rdxd/crash`, which uploadd refused (`REPORTTYPE_NOT_FOUND`, "Not
supported to current setting"); no `UPLOAD_SUCCESS` was logged, no `rdx2` name was even looked up,
and the inspector's 274 flows had no LG host (reverse lookups of the guest's own subnet and Hue
discovery only). The reports stay in the guest; nothing sends them. The same VM in `direct`,
its terms still agreed, was authenticated with the clock from `sdp` and Home up; in five minutes
it made three more analytics tarballs and logged no `UPLOAD_SUCCESS` and no `AUTH.ERR`, and
`netns-lgmock.log` had every LG name it asked for answered or refused. WT1H with
`lg_services = "real"` in `mitm` had no resolver of ours and no CA answers: the inspector showed
`EU7.lgtvsdp.com` resolved by LG's DNS and sdx's `authentication`, `services` and `country`
going to LG's server (which refused the synthetic key: 406 and 400), as before this change.

## The proxy's start

mitmweb is the user's own mitmproxy, 12.0.1 or newer (`host/core/beanjar/network/mitmtool.py`:
`NETNS_MITMWEB`, else `mitmweb` on `PATH`, else `~/.local/bin/mitmweb`, pipx's default; the version
from the package metadata beside it, else `mitmweb --version`, once per start). It runs as
`mitmweb --mode tun:mitm0` with `connection_strategy=eager` and `mitm_addon.py`, and makes the TUN
device `mitm0` itself in the inner namespace. Once it is ready the policy rules `iif tap0 ipproto
tcp` and `iif tap0 ipproto udp` look up a table whose one route is `default dev mitm0`: all of the
guest's TCP and UDP, every port, DNS included, and nothing else (ICMP goes to slirp). mitmweb's own
upstream connections do not arrive on the TAP and leave by the main table. What the start found
(version, path, and the problem when it cannot serve) is written to `run/mitmweb.json`, which
`beanjar mitm show` and the manager read.

Whoever reaches mitmweb's API with the password (or a script on its origin) can set any option
and run any command: `PUT /options` is `options.update(**body)` and `POST /commands/NAME` calls any
command. Two layers keep that from reaching the user's files. mitmweb runs under bubblewrap
(`netns.mitmweb_sandbox`): a mount namespace with `/usr` and `/etc` read-only, its installation
(the pipx venv) and interpreter read-only, `mitm_addon.py` and the password file read-only, and
only its confdir (the library's CA), `mitmweb.d/` in the socket directory (the ready file) and
`NETSTUB_LOG` writable; private `/proc` (its own pid namespace), `/dev` (with `/dev/net/tun`) and
`/tmp`; a cleared environment. It keeps `CAP_NET_ADMIN` of the inner namespace for its TUN device
and makes no user or network namespace of its own, so tun mode works as before. Not `$HOME`, not
the VM's shares (the guest writes `out`), not the run directory. Without `bwrap` the inspector is
off (`mitmtool.check`). Then `mitm_addon.Freeze`, at mitmweb's `running`, freezes the options that
load code or files, open listeners or set the password (`FROZEN`: `scripts`, `save_stream_file`,
`hardump`, `rfile`, the replay files, `map_local`, `web_static_viewer`, `protobuf_definitions`,
`proxyauth`, `confdir`, `certs`, `client_certs`, `cert_passphrase`, the upstream trust paths,
`mode`, `listen_host`, `listen_port`, `server`, the `web_*` listener, password, debug and browser
options) and refuses the commands that take a path or start a program. It subscribes to the
options, whose subscribers run before any addon's `configure`, so a refused `scripts` is never
loaded; the update is rolled back whole and the API answers 400. It is a deny-list over an
allow-list: every option of mitmproxy 12.3.1 was reviewed (`REVIEWED`), the rest of them stay
settable because mitmweb's Options menu offers them all, and an option a newer mitmproxy adds is
frozen until it is reviewed (logged at start; `test_every_option_reviewed` fails until then).

mitmweb starts beside QEMU, not before it: the launcher does not wait for it to be ready. Until it
is (its addon's ready file, its web port and `mitm0` all there), the guest's TCP and UDP are held
by policy rules of the inner namespace that drop them (`blackhole`, `netns._hold`), and once it is
the route and interception rules are added and then the hold removed (`netns._wait_guest`). A connection the guest opens meanwhile is not refused and
not reset: its SYN is dropped, TCP sends it again (after 1 s, 3 s, 7 s, ...), and a retransmission
after mitmweb is up is accepted by it. Letting such a connection go directly instead would get it
reset when interception starts, since its later packets would then be delivered to a mitmweb that
has no such connection. A mitmweb that exits, or is not ready within 60 s (`PROXY_WAIT`), has
the hold removed and the guest's traffic goes directly, as with no inspector; so does a start with
no mitmweb or one older than 12.0.1, whose reason is printed and recorded.

`tests/integration/network/test_netns_failopen.py` shows it (mode `late`): a stand-in makes its TUN
device only 1.5 s after the fake guest has its lease and opens its HTTP connection, which is
carried through the device once it is there and completes about 2 s after it was opened. On an RK3399 board mitmweb took about 10 s to listen; the
guest's network is up some 30 s after QEMU starts, so in practice nothing is held, and the start no
longer waits those 10 s.

## `lan` with `mitm`

`netns.py`'s `lan()` puts the whole `nat`+`mitm` arrangement one namespace further in:

```
host ── VM_ADDRESS relay (127.0.1.N: service ports, inspector)
 └─ LAN namespace (unshare -rnm)   lan0: macvlan on lan_parent, the VM's MAC, the lease
     ├─ busybox udhcpc (lan_dhcp.py), released at stop; or the static address
     ├─ relay: the service ports, the inspector and VNC (5900) on 0.0.0.0, the LAN address
     ├─ slirp4netns ── the inner namespace's up0 (10.0.2.0/24)
     └─ inner namespace (unshare -rn): tap0, proxy ARP, mitmweb, the guest's DHCP, QEMU
```

- **The helper.** `beanjar-lan-helper macvlan PARENT MAC PID` (`host/helpers/network/lan-helper.c`),
  installed with the file capability `cap_net_admin=ep` by the package's postinst, is the only
  privileged step. It refuses anything but a physical, wired Ethernet `PARENT` (not wireless, a
  bridge, bond or virtual interface), a unicast MAC and a PID of the calling user in another network
  namespace, then runs `ip link add link PARENT name lan0 address MAC netns PID type macvlan mode
  bridge` with that one capability and an empty environment. lan0 is made inside the LAN namespace
  in one step and never exists on the host side. In a checkout the helper is
  `build/network/beanjar-lan-helper` and needs the same `setcap` to work. Any local user who can run
  the installed helper can put a macvlan with a MAC of their choosing on a wired interface of the
  host into a namespace of their own; that is the boundary of this design.
- **DHCP.** Debian's `busybox udhcpc` in the foreground on lan0, with
  `host/helpers/network/lan_dhcp.py` as its event script, which configures lan0 and writes the
  lease. It runs rootless, as root of the LAN namespace's user namespace, and releases the lease
  (`-R`) when the VM stops. A static `address` is `A.B.C.D/NN,GATEWAY`, and the gateway is then the
  DNS server too. The LAN namespace's own `/etc/resolv.conf` is a bind mount of the lease's DNS, in
  its mount namespace only, which is what slirp4netns's resolver (the guest's 10.0.2.3) forwards to.
- **What the LAN sees.** One host at the lease's address with the VM's MAC: the guest's traffic, and
  mitmweb's upstream connections, leave through slirp4netns from that address. The TV-service ports
  (`layermeta.forwards`, with the VM's own `forward=` ports) are served there by a relay in the LAN namespace; `listen` does not apply.
  The same relay serves the inspector there, at `NETSTUB_WEB_PORT` (18083), behind the same
  password. The host itself cannot reach its own macvlan children, so the host-side relay keeps the
  same ports on `VM_ADDRESS`, and the inspector on `NETSTUB_WEB_HOST` (by default `VM_ADDRESS`).
  VNC (`beanjar vnc start`, on demand) is there too, at 5900: the relay joins it to `vnc.sock` in the
  socket directory, on which beanjar-vnc listens beside `VM_ADDRESS` ([vnc](vnc.md)). The guest stays
  on its private 10.0.2.0/24. Dedicated discovery helpers carry IPv4 SSDP and mDNS between that
  network and the LAN; this is protocol forwarding, not a general Ethernet broadcast bridge.
- **Hostname.** The DHCP request carries the VM's name as its hostname (option 12,
  `udhcpc -x hostname:NAME`; the manager passes `NET_LAN_HOSTNAME`), so a router that registers DHCP
  hostnames gives the VM a name in the LAN's own domain (`tv1` as `tv1.home.arpa` on a router whose domain
  is `lan`; the domain is the router's). The name is made a DNS label: lower-case, every character
  outside `a-z`, `0-9` and `-` (a name allows `.` and `_`) becomes `-`, none at either end, at most 63
  characters; two names that map to one label collide at the router. `beanjar show` shows it as
  `lan_hostname`. A VM renamed (`beanjar rename`) sends its new name from its next start. The
  checkout's own VM (`host/scripts/vm-launcher`) sends none.
- **Status.** The lease is in `run/lan.json` while the VM runs; `beanjar show` (`lan_address`),
  `beanjar list` (its ADDRESS column) and the GUI's summary (LAN address) show it. The inspector's
  URL is the LAN one: `beanjar show`'s `inspector.url`, with `inspector.local_url` for the host
  itself, and the GUI's Traffic Inspector, whose Copy URL copies the LAN URL and whose Open in
  Browser, the browser being on the host, opens the local one (shown as "On this host"). VNC's is
  shown the same way, while it runs: `beanjar show`'s `vnc.url` and `vnc.local_url`, and the
  GUI's VNC group.
- **Cleanup.** A stop releases the lease and deletes lan0. lan0 also belongs to the LAN namespace,
  and every process there is started with `PR_SET_PDEATHSIG`, so a killed wrapper takes the
  namespace, and lan0, with it. The kernel may keep such a namespace, and with it lan0 and the
  VM's MAC on the parent, until its last TCP sockets are torn down: measured on the Pi, about four
  minutes after a stop that left the deletion to the namespace. A macvlan cannot come up while
  another on the parent has its MAC, so a start waits up to a minute for that, then says why.
  `tests/integration/network/test_netns_lan.py` checks a stop and a kill, with a veth pair as the LAN.

## `lan` with `direct`

The guest's own NIC on the LAN, as a TV plugged into it, with nothing of Beanjar's in its traffic:

```
host ── (nothing of the VM: no relay, no address; beanjar-vnc on VM_ADDRESS as always)
 └─ LAN namespace (unshare -rnm), no address but lo
     lan0: macvlan on lan_parent, the VM's MAC ── tc ── tap0 ── QEMU (virtio-net, the same MAC)
```

- **The wire.** `netns.lan()` sees `NET_MODE=direct` once lan0 is up (the same helper call, the same
  wait for the VM's MAC as `mitm`) and runs `_lan_direct` instead of the DHCP client, slirp4netns,
  the inner namespace and the relays: it makes `tap0`, turns `allmulticast` on for lan0 (a macvlan
  passes only the multicast groups on its own list, and the guest's IGMP joins never reach it, so
  without it mDNS and SSDP would not arrive), disables IPv6 on lan0 before it comes up (the
  namespace must not send its own link-local, router or neighbour solicitations with the guest's
  MAC) and joins the two with tc (`netns.join_commands`): a `clsact` qdisc on each and a `matchall`
  filter on each one's ingress whose `mirred egress redirect` sends every frame on to the other.
  The redirect happens at ingress, before the namespace's IP stack, so no address, route or policy
  rule of that namespace ever applies to the guest's frames. QEMU runs in the LAN namespace with
  the same `tap` netdev as every other mode, the guest's NIC already having the VM's MAC (`MAC` or
  the board's), which is lan0's.
- **Why not macvtap.** A macvtap's character device, `/dev/tapN`, is made by the kernel owned by
  the host's root, mode 0600, whatever namespace the interface is in: measured on WSL2 (kernel
  6.18), a macvtap made in a rootless namespace showed as `crw------- 65534 65534` there and opening
  it was refused. Handing QEMU its descriptor would need a helper with `CAP_DAC_OVERRIDE` or a udev
  rule, a new privilege on the host. The macvlan and tc do the same job with the privilege `mitm`
  already has: `beanjar-lan-helper macvlan`, unchanged. tc needs only `CAP_NET_ADMIN` of the
  namespace (it works rootless) and the kernel's `sch_ingress` (clsact), `cls_matchall` and
  `act_mirred` (modules in the WSL2 kernel, loaded on first use from the rootless namespace); a
  start without them stops with that reason.
- **The guest's address.** The guest asks the LAN's DHCP server itself, with its own MAC and
  whatever hostname its firmware sends (Beanjar's `NET_LAN_HOSTNAME` does not apply), and takes whatever DNS that server gives; a static address is set on the
  TV, not in `vm.toml` (`address` is refused). `netns.LeaseWatch` reads the frames on tap0 (a
  packet socket, which sees both directions before the redirect) for the server's DHCPACK to the
  guest's MAC (`netns.guest_lease`) and writes `RUN_DIR/lan.json` with `source = "guest"`, so
  `beanjar show` (`lan_address`), `list` and the GUI show it. It only reads. A guest with a static
  address has no `lan.json`.
- **What reaches it.** Every port of the guest, at its own LAN address, from any host of the LAN:
  nothing is forwarded or relayed, so `forward` is refused and the layers' ports are not on
  `VM_ADDRESS` (`VM.listeners()` is empty, and no start conflicts with another over them). This
  host is the exception: a macvlan's parent never reaches its children, so neither the host's
  tools nor its browser reach the guest. `beanjar ares-TOOL -d NAME` on the VM's own host says so;
  from another machine `beanjar ares-TOOL -d HOST/NAME` asks that host for the guest's LAN address
  (`_ares`'s `lan`, from `lan.json`) and connects there itself, without a forward through the
  host, root on the guest's port 22. VNC (beanjar-vnc) does not use the guest's network and stays
  on `VM_ADDRESS`; `--listen` may open it as with `nat`.
- **What it reaches.** The LAN and through its router the internet, as any device there. The
  host's loopback, never: it is in another namespace and the guest's frames enter no IP stack of
  the host's. The host's address on `lan_parent` is unreachable too (the same parent rule, the
  other way round). The host's other addresses (a Wi-Fi address on the same LAN, a VPN, `docker0`)
  are reached only as the LAN routes to them, exactly as from another device of the LAN: a service
  the host opens to the LAN is open to the guest. That is the point of the mode; `nat` without
  `lan_access` is the one that keeps the guest away from the LAN.
- **LG's services.** `mock` cannot hold here, and the setting refuses it (`vm.settings.validate`,
  `qemu.plan` and `Library.start`: "needs lg_services=real"). Every means `mock` has sits in the
  inner namespace's IP stack: the resolver at `10.0.2.254` handed out by the namespace's own DHCP
  server, the pref 90 `prohibit` rules for LG's literal addresses and for other resolvers, and the
  SDP answers on `10.0.2.254:443`. In this mode the guest's DHCP, DNS and connections are the LAN's,
  and its frames are redirected before any IP stack of ours sees them (the LAN namespace has no
  address; `tests/integration/network/netns_lan_cases.py`, `LanDirect`, checks it has none and
  that the guest's lease and connections are the LAN server's). Refusing names and addresses there
  would mean steering the guest's DNS and LG's addresses out of the wire into a stack of ours
  (tc rewriting of MAC and IP addresses): interception again, which this mode is defined
  without. Partial enforcement (dropping LG's literal addresses with tc, say) would leave names
  through and call it mock. So `lan` + `direct` is `real` only, stated, and `lan` + `mitm` is the
  LAN mode that keeps the mock.
- **Cleanup.** As `mitm`'s: a stop deletes lan0 and `lan.json`; every process of the namespace dies
  with its parent; the next start waits for the MAC.

`tests/integration/network/netns_lan_cases.py` (`LanDirect`) runs it end to end with a veth pair as
the LAN and the fake guest on the TAP: the guest's lease and its unicast renewal from the LAN's
DHCP server, its connection to a LAN server arriving from its lease, a LAN client reaching a guest
port at the lease, `lan.json` from the ACK, no address in the LAN namespace, and nothing left after
the stop. `tests/unit/launch/test_netns_lan_direct.py` covers the tc commands and the ACK reading.

## Ownership and interception

`host/core/beanjar/network/netns.py` owns the namespace and networking helpers. The `mitm` path
uses routing rather than netfilter, and mitmweb's own tun mode rather than any mitmproxy
internals. The proxy's inspector is available at the configured
host port (normally the VM's address, `127.0.1.<slot>:18083`, and with `link = "lan"` the VM's LAN
address too), behind a password made for each
start (or `BEANJAR_MITM_PASSWORD`), which the VM's owner-only `run/mitmweb.password` holds and the
manager shows ([inspect emulator traffic](../../user/inspect-emulator-traffic.md#the-password)).
`mode = "direct"` runs the same namespace without the proxy: no mitmweb, no inspector relay, no
password, no CA in the guest.
`link = "off"` is the topology used when a run must have no guest network egress.

`mode = "mitm"` requests best-effort outbound capture. A missing proxy or failed
proxy startup leaves the routed guest network usable without interception. The
ingress interception rules are installed only after proxy readiness; if that
process exits, the rules are removed while the guest continues running. Existing
connections may fail and require a client retry. Beanjar does not install the
optional proxy; the user does (`pipx install mitmproxy`).

TLS handshake failures also allow later connections to the same endpoint through
undecrypted, except to LG's hosts with `lg_services = "mock"` ([LG services](#lg-services)).
The failed connection is not replayed. These automatic bypasses are
bounded and preserve manual `ignore_hosts` rules. The inspector distinguishes
decrypted HTTP flows from passthrough TCP flows; it cannot capture traffic while
the proxy is unavailable.

## Discovery on LAN with MITM

`discovery.py` runs one helper beside `lan0` and one beside the guest TAP. They
exchange framed messages and fixed-guest service tunnels over a private Unix
socket. Namespace startup waits for each helper's readiness. Their lifetime is
the VM's, independently of mitmweb; the system `python3-dnspython` package supplies
the DNS codec without the optional proxy.

SSDP searches and mDNS queries cross in both directions. Temporary query sockets
correlate private replies with the requesting client; normal mDNS multicast,
legacy queries and QU replies keep distinct routing. Genuine advertisements and
goodbyes cross too. Multicast loopback controls, source checks and a bounded,
locked transmission cache prevent reflection. Guest-origin queries were checked
live against real LAN services, including Google Cast and Home Assistant.

Only verified guest advertisements and actual guest descriptions admit dynamic
TCP/UDP listeners. Each listener is ready before its advertisement is published,
and every backend is the fixed guest address. SSDP LOCATION and HTTP endpoint
references are translated to the LAN address. The HTTP proxy discovers DIAL's
application port from its actual `Application-URL` header. A registered SSDP
description is treated as bounded text even without a MIME type: queue's Gramlink
XML omits `Content-Type` and carries a plain `X_LGLINK-LOCATION` address. Other
untyped binary resources stream unchanged. HEAD responses preserve their empty
body without guessing the transformed GET length.

`discovery_dns.py` preserves compressed names, IDs, questions, QU/cache-flush bits,
record TTLs and service identities. Guest A records become the LAN IPv4 address;
guest AAAA records are omitted because this topology does not forward IPv6
services. A bounded host cache handles separate A and SRV packets. Actual SRV
TTLs govern listeners, positive refreshes cannot shorten an existing lifetime,
and a goodbye retires a port only after its other SSDP/mDNS references end.
Listener counts, clients, sessions, framed packets and rewritable bodies are
bounded; established service streams remain independent of discovery refreshes.

On the Pi's queue firmware, external native Windows probes received 44 SSDP
replies and five mDNS replies. All five advertised device descriptions returned
HTTP 200 with no private-address leaks; a normal Avahi browser resolved the
emulator's AirPlay, display, GHP and HAP services. AirPlay's read-only `/info`
returned HTTP 200, and SSAP accepted a WebSocket upgrade on port 3001. DIAL's
YouTube GET returned HTTP 200 with the stock-required YouTube Origin; an absent
Origin returned the stock 403. A separate LAN SSAP client then completed normal
PROMPT pairing, with consent accepted through the emulator's UI. Foreground-app
and audio queries succeeded; the advertised pointer-input WebSocket used the LAN
address on port 3001. RIGHT, HOME, BACK and volume buttons visibly changed the
Home screen selection, recent-items panel and volume overlay. No vendor
permission or application policy changed. The user's phone also listed the
emulator; its own pairing remains unverified. Media playback was not exercised.

## Host addresses and ports

Every VM has a slot N, and every host port of it is on its own loopback address, `127.0.1.N`
(Linux routes all of `127.0.0.0/8` to the loopback), at the TV's stock numbers. Several VMs run
side by side this way, and each is reached as a TV of its own, ares included, without port
arithmetic.

The layers' ports are the `forwards` of the selected layers' `layer.toml`
([layer metadata](firmware-targets.md#layer-metadata-layertoml)).

| Port on `127.0.1.N` | Guest | What | Override |
|---|---|---|---|
| 3000 | 3000 | SSAP, the second-screen API over WebSocket (`webos` layer: every webOS VM). The stock xinetd takes it from localhost only on most releases (`only_from = 127.0.0.1`; W21P has none), so a forwarded connection is closed there, as on a TV | `listen` (the address) |
| 3001 | 3001 | The same over TLS, open to every client: the second-screen clients' port. The certificate is the VM's own | `listen` (the address) |
| 6622 | 22 | Root SSH (`rootssh` layer, or Homebrew Channel's with `hbchannel`, whose `layer.toml` conflicts with `rootssh`); 22 is privileged on the host | `SSH_PORT` (the number); `listen` (the address) |
| 9922 | 9922 | Developer mode's jailed SSH, `prisoner` (`devmode` layer) | `listen` (the address) |
| 9991 | 9991 | Developer mode's key server, `ares-novacom --getkey` | `listen` (the address) |
| 18083 | (inside the namespace) | The `mitm` inspector (mitmweb), with a password (`run/mitmweb.password`) wherever it listens; with `link = "lan"` also on the LAN address | `NETSTUB_WEB_HOST`, `NETSTUB_WEB_PORT`, `BEANJAR_MITM_PASSWORD` |
| 5900 | none | The TV picture over VNC, [beanjar-vnc](vnc.md) (`beanjar vnc start NAME`), on the host, always with the VM's password (`identity/vnc.password`, `beanjar vnc password show NAME`); with `link = "lan"` also on the LAN address; a held port is refused with its holder named | `beanjar vnc start NAME --port N` (with `lan`, this host's listener only); `--listen ADDRESS` (`nat` only); `beanjar vnc password set NAME` |

Homebrew Channel's telnet (guest port 23, unauthenticated root) has no host port: it stays inside
the guest.

- **A VM's own ports.** `forward` (`vm.toml`'s `network.forward`, the launcher's `FORWARD`)
  adds guest TCP ports of the VM's choosing, an app's server under test, to the same list:
  `beanjar config set NAME forward=8080,3000`. `layermeta.forwards` appends them after the
  layers' (as layer `forward`), so they take every path the TV-service ports take: `direct`'s
  `hostfwd`, `mitm`'s `NETNS_FWD` and both relays, on `listen` with `nat` and on the LAN address as
  well as `VM_ADDRESS` with `lan`; `capacity.listen_conflict` checks them at a start like the others,
  `lan` VMs' too (on `VM_ADDRESS`, the host side of their relay). An item is `PORT` or `HOST:GUEST`
  (`8081:80` is guest 80 on host 8081, since the forward path carries both numbers anyway). Refused,
  when set and again at a start (a later change of the layers or `SSH_PORT` included): a host port a
  selected layer has (on webOS, 3000 and 3001 are the webos layer's), 5900 (VNC), 18083 (the
  inspector), one given twice, and one below 1024, which an unprivileged host process cannot bind.
  A guest port a layer forwards may be forwarded again from another host port. Settings change only
  while the VM is off, so a change applies at its next start; `unset forward` removes them all
  (none is the default). The ports carry no password of ours: whatever listens in the guest
  answers whoever reaches the address. On a `lan` VM a discovery listener (DIAL's port, say) on the
  same port as a `forward=` one cannot bind there.

- **Addresses.** `127.0.1` is the default prefix. A library's `config.toml` `addresses`
  (`beanjar library config set addresses=127.0.2`; three octets inside `127.0.0.0/8`, not `127.0.0`) gives its
  VMs, and those of its development libraries, `<prefix>.N` instead, so the VMs of two libraries
  run side by side. It changes only while every VM of the library is off. Everything derives
  the address from it: `keys.address`, the launcher's `ADDRESS_PREFIX` and so `VM_ADDRESS`, the
  forwards, the relay and the inspector, the `known_hosts` pins (rewritten at the VM's next start;
  no alias for the old address is kept), `beanjar show` and its ares profile, `list`, the GUI and
  `vm-launcher config ADDRESS`. The rest of this section says `127.0.1.N` for the default.
- **Slots.** A VM's slot is `network.slot` in its `vm.toml` (`vm.identity.ensure_slot`, `keys.address`),
  from 1 to 254. It is assigned when the VM is made: the lowest slot no VM of its slot domain
  holds, chosen under the domain's lock (`<library>/.slots.lock`) together with the rename that
  publishes the VM, so two VMs made at once never share one. It is kept while the VM exists and
  is not a setting. The slot domain is a library and the development libraries inside it
  (`<library>/development-*`: the checkout's VM and each `INSTANCE`, which regression uses), so a
  checkout VM never shares an address with a manager VM. A VM made before slots gets one once,
  written back, when its library is opened or it is started. A VM folder outside any library is
  alone in its domain (slot 1).
- **Both modes of `nat`.** `qemu.plan` turns `SLOT` (the launcher's variable from `vm.toml`) into
  `VM_ADDRESS`. `direct` forwards with QEMU's `hostfwd=tcp:127.0.1.N:PORT-:GUEST`; `mitm`'s
  host-side relay (`beanjar-relay`, `host/helpers/network/relay.c`) binds the same address and ports and bridges them into the
  namespace, the inspector included unless `NETSTUB_WEB_HOST` names another address. `link = "off"`
  forwards nothing, and a `lan` + `direct` VM's ports are on its own LAN address, not forwarded.
- **Reaching a VM.** Everything that says how to reach a VM uses its address: the launcher's
  notes, `beanjar show`'s `ssh` commands, `beanjar ares-TOOL -d NAME` (the ares tools
  with a device list of the VM alone: `127.0.1.N`, port 9922, user `prisoner`), `beanjar list`, the GUI's
  summary and `host/scripts/vm-launcher config ADDRESS [--dir VM]`. Each VM's `identity/known_hosts`
  pins its host keys as `[127.0.1.N]:6622` and `[127.0.1.N]:9922`, so two VMs' pins never
  overwrite each other.
- **The LAN.** The TV-service ports are one list, `layermeta.forwards` (the `forwards` of the
  selected and always-used layers, then the VM's own `forward=`; a new service port joins there). `vm.toml`'s `network.listen`
  (`link = "nat"` only; `beanjar config set NAME listen=0.0.0.0`, the launcher's `LISTEN_ADDRESS`) moves them all to `0.0.0.0`
  or one of the host's addresses, for ares or a second-screen client on another PC; unset, they
  stay on `127.0.1.N`. They need no password of ours: the SSH servers take keys only, the key
  server's key is encrypted with the guest's passphrase, and SSAP pairs a client only when it is
  accepted on the TV's screen. With one address the `ssh` commands and `known_hosts` use it; with
  `0.0.0.0`, `127.0.1.N` still reaches them from the host. The ports stay the TV's, so
  `Library.start` (`capacity.listen_conflict`) refuses a start whose ports overlap a starting or
  running VM's (the same address, or `0.0.0.0` on either side), naming it, or a listener of the
  host's, naming its holder. The inspector and VNC keep their own addresses and password rules.

The library CA and per-firmware trust-store handling are described in [identity](identity.md).
Host interception alone does not establish that every guest TLS client trusts the CA; legacy
platforms have their own layer and trust-store boundaries. The
[traffic inspection guide](../../user/inspect-emulator-traffic.md) covers normal use and
troubleshooting.

## Identity and validation

MAC, serial and model-key provisioning belong to VM identity, not the network mode. A synthetic
identity is not a valid LG server credential.

Regression with `NET_LINK=off` captures the guest NIC, validates the pcap and checks it for recognized
LG contacts after the VM stops. An empty valid capture is consistent with the link-down
topology. Network capture results do not establish application TLS or authentication success.
