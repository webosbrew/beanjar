# beanjar-web: VMs in a browser


`beanjar-web` (`host/web/`, Debian package `beanjar-web`) shows a VM's TV picture in a browser,
with keyboard and pointer, its sound, the TV remote in a popup window of its own, and for a
`mode = "mitm"` VM its traffic inspector (mitmweb). It works for this
machine's VMs and for VMs on another host (`HOST/NAME`), through the manager's own ssh.

```sh
beanjar-web start                  # as the systemd user unit beanjar-web; prints the URL to open
beanjar-web status                 # whether it runs, its address, the URL (with the token)
beanjar-web stop
beanjar-web enable --listen 127.0.0.1 --auth none   # a unit file instead: back after a reboot
beanjar-web disable
beanjar-web                        # the same as `beanjar-web serve`: in the foreground; prints the URL
beanjar-web serve --port 0         # any free port
beanjar-web start --listen 0.0.0.0 --auth password --certificate cert.pem --key key.pem
beanjar-web serve --auth none      # no token or password: loopback only
beanjar-web serve --size 1920x1080 --fps 60   # beanjar-vnc's options when it starts VNC for a VM
beanjar-web start --hosts rk-2   # only that host's VMs (local: this machine's)
host/web/bin/beanjar-web start     # from the checkout, with the checkout's manager
```

## Commands

- **`serve [options]`** (or no command) runs the server in the foreground and prints the URL. A
  signal (SIGINT, SIGTERM) ends it.
- **`start [options]`** checks the options (and makes the token or password file) here, then runs
  `serve` with them as the transient unit `beanjar-web.service`: `systemd-run --user --unit
  beanjar-web --collect`, `Restart=on-failure` (after 2 s; not after exit status 2, a bad option or
  a port that is taken, which a restart would not fix), every path made absolute, the manager's
  command resolved to an absolute path now (the PC's `beanjar` is a wrapper in `~/.local/bin`,
  which systemd's user environment may not have on `PATH`), and of the environment only `PATH`,
  `LANG`, the XDG directories, `SSH_AUTH_SOCK` (other hosts' ssh) and `BEANJAR_*`. It refuses while
  the unit is active, waits (at most 20 s) until the server listens, then prints what `status`
  prints; a unit that fails is shown with its journal. The unit's `RuntimeDirectory`
  (`$XDG_RUNTIME_DIR/beanjar-web/`) holds `state.json`, which `serve` writes once it listens: its
  address, port, the inspector's port, scheme, auth mode, `--hosts` and the secret's file, never
  the secret; systemd
  removes it when the unit stops. Under the unit the URL is printed without the token, so it is not in the journal.
- **`enable [options]`** is `start` made permanent: the same command line, environment and
  service properties written as the unit file `$XDG_CONFIG_HOME/systemd/user/beanjar-web.service`
  (systemd's own quoting; the token or password file's path, never the secret), with
  `WantedBy=default.target`. It stops a running unit of that name first (start's transient one,
  or an enabled one whose options change), then `daemon-reload`, `enable`, lingering on (a user
  unit starts at boot only for a lingering user) and `start`, and waits until it listens as
  `start` does. **`disable`** is `systemctl --user disable --now` and removes the file. While the
  file exists, `start` without options starts that unit and `start` with options is refused
  (`enable OPTIONS` changes them); `stop` stops it for now and says it is still enabled. A listen
  address must exist at boot: one that is not yet up is exit status 2, which is not restarted.
- **`stop`** stops the unit; **`status`** prints whether it runs (exit status 3 when not), whether
  the unit is transient (`start`'s, gone at reboot) or persistent (`enable`'s), what it
  listens on, the URL to open, with the token read from its file, and `--hosts` when it was given. For `0.0.0.0` or `::` the URL
  is the loopback one (`127.0.0.1`, `[::1]`) and it says it listens on every address.
- One unit, `beanjar-web`, in this version. `BEANJAR_WEB_UNIT=NAME` names another (the tests', so
  they never touch the user's). The output: `journalctl --user -u beanjar-web`.

## Design

```
browser ── HTTP + WebSocket ── beanjar-web ── stdio ── beanjar vnc pipe       ── run/rfb-local.sock ─── beanjar-vnc
                                           ├─ stdio ── beanjar vnc audio-pipe ── run/audio-local.sock ─┤
                                           ├─ stdio ── beanjar vnc video-pipe ── run/video-local.sock ─┤
                                           ├─ stdio ── beanjar remote pipe     ── VM bus: org.beanjar.Display1.Ir ─┘
                                           │           (over ssh for HOST/NAME)
                                           └─ HTTP + WebSocket ── mitmweb, at the address `beanjar mitm connect` gives
                                                       (the VM's own; for HOST/NAME a forward on the shared ssh connection)
```

- **The manager is the only way in.** The web server runs `beanjar ...` for everything: the list,
  starting and stopping VMs, starting VNC, the three pipes, the remote's layout, screenshots, and
  where the traffic inspector is. It opens no VM socket and no
  tunnel of its own (the inspector's TCP connections go where `beanjar mitm connect` says, below); a VM on another host is reached the way every other command reaches it, the CLI
  running that host's beanjar over its shared ssh connection (`host/core/beanjar/remote.py`;
  the pipes use `remote.attach`, the process's own stdio, unbuffered). The pages always say
  `HOST/NAME`, `local/NAME` for this machine, so a bare name never makes the manager ask every
  host.
- **The picture: noVNC over the local RFB socket.** The managed VNC server always has a password,
  and noVNC cannot log in to it (no RSA-AES that neatvnc offers, and neatvnc has no RSA-AES over
  WebSocket: [vnc](vnc.md#authentication-and-the-lan)). So `beanjar vnc start` also opens
  `run/rfb-local.sock`, RFB without authentication on an owner-only (0600) unix socket: the
  file's permissions are the access control ([vnc](vnc.md#design)). `beanjar vnc pipe
  [HOST/]NAME` relays stdin and stdout to it; the web server relays a WebSocket's binary messages
  to that pipe and back. noVNC is Debian's `novnc` package, unpatched, used as a library: our own
  page imports `/novnc/core/rfb.js` (served from `/usr/share/novnc`).
- **Icons: Debian's Bootstrap Icons.** The header's buttons and the list's chrome use Bootstrap
  Icons from the `bootstrap-icons` package, served as they are from `/icons/`
  (`/usr/share/bootstrap-icons/svg`, `--icons DIR`), none copied into the tree. The SVGs are CSS
  masks over the text colour (`.bi`), since they fill with `currentColor`. The popup remote does
  not use them: it draws from `layout.json`.
- **The remote: Display1.Ir.** Through VNC only Home, Back, the volume keys and Mute become IR (the
  core's PC key map). Every other remote button reaches the guest through
  `org.beanjar.Display1.Ir(code, down)`, which the front end that owns the display (beanjar-vnc or
  the GTK viewer) serves on the VM's bus and sends with `wv_ir`
  ([viewer](viewer.md#remote-buttons-from-a-script)). `beanjar remote pipe [HOST/]NAME` reads
  `down CODE` and `up CODE` lines (CODE an IR code or a key's name) and calls it; it answers
  `ready` once connected and `error: ...` for a line it cannot do.
- **A touch screen gets the picture's touches.** For a VM whose model has one (`touch` in
  `/api/vms`, the StanbyME), the page takes touches on the picture before noVNC does (capture
  phase) and sends `touch KIND SLOT X Y WxH` over the same socket (KIND `begin`, `move`, `end` or
  `cancel`, a slot per finger, X Y in the noVNC canvas's WxH frame); `remote pipe` maps them to
  display pixels and calls `org.beanjar.Display1.Touch` ([manager](../../../host/core/README.md#touch)).
  The server ends the touches a socket still holds when it closes. Touches on the lite remote
  stay its keys.
- **The mouse is the pointer or a finger.** On such a VM the Remote button is a menu button (not
  on phones, whose fingers are the touch screen's already, and where it stays the lite remote's
  control): it opens `#input-menu` with two check items, Show remote (the remote window, as the
  plain Remote button toggles it on a model without a touch screen) and Mouse as a finger. There
  is no separate Mouse button: one control for how the page takes input. Off, the default, the mouse is the Magic
  Remote's pointer as before. On, the page takes the left button's events before noVNC and
  `onPointer`: its press on the picture is a `touch begin` in a free slot of its own, motion while
  it is held `touch move`, its release `touch end`, mapped as a finger's is (`framebufferAt`,
  through the turn). A click is a tap and a drag a swipe; hovering with no button held is swallowed
  and sends nothing (a finger can't hover; a right or middle press carries its own position), the
  wheel and the right and middle buttons stay the pointer's. The choice is kept per browser (`beanjar-web-mouse`, `touch`
  or `pointer`).
- **A set that turns shows a turned picture.** For a VM whose model turns to portrait
  (`portrait` in `/api/vms`, the StanbyME), the page opens the remote socket at once and `remote
  pipe` reports the guest's rotation, `rotation N`, at its start and at each change. The page
  turns noVNC's canvas (and the H.264 canvas over it) by (360 - N) % 360 clockwise with a CSS
  transform, scaled to fit, so a phone in portrait gets a portrait picture. Touches are mapped
  back through the turn into noVNC's framebuffer; while the picture is turned the page also takes
  the mouse before noVNC (whose own mapping does not know the turn) and sends `pointer X Y BUTTONS
  WxH`, which `remote pipe` gives `org.beanjar.Display1.Pointer`; the wheel is dropped meanwhile,
  and the keyboard stays noVNC's. More's Portrait check item (`#rotate`, shown for such a VM only) is a two-state toggle:
  landscape is 0, portrait is 270 (what msc reports as portrait on N21D, and what one press gave
  before); checked at any angle but 0, and a press at any angle but 0 (90 or 180, set from the CLI)
  goes to landscape. It posts the angle to `/vm/HOST/NAME/rotate`, which runs `beanjar sensor set rotate`
  ([manager](../../../host/core/README.md#rotation)); the CLI keeps all four angles.
- **The list's preview of a turned set is turned in the browser.** The thumbnail is the panel's
  unturned landscape picture. The server asks `beanjar sensor show rotate --json HOST/NAME` (the guest's last
  report) when it takes a live picture of a `portrait` VM and answers with `X-Beanjar-Rotation`;
  `index.js` puts it on the image as `data-rotation` and `style.css` turns it by (360 - N) % 360
  inside the card's 16:9 box (a quarter turn swaps the box's sides so `object-fit: contain` fits
  the portrait picture with the right aspect). CSS was chosen because the server has no image
  library and the list rows (`beanjar list`, which another change owns) carry no rotation. An Off
  card's kept picture is not turned: the rotation at the moment it was kept is not recorded.
  `screenshot.png` without `?thumbnail=1`, like `beanjar screenshot`, stays the panel's unturned
  pixels.
- **Held buttons are released at every level.** The page sends `up` on `pointerup`,
  `pointercancel` and a lost pointer capture, and for its keyboard keys when the window loses the
  focus; the web server sends `up` for what a socket still holds when it closes; `remote pipe`
  releases its button at the end of stdin; and the core releases a button whose D-Bus client left
  the bus (a killed pipe, a dropped ssh).
- **The layout is the GTK remote's data.** The remotes' keys (boxes, colours, labels, IR codes) are
  `host/core/beanjar/devices/remote_layouts.py`, a module without GTK that the GTK remote
  re-exports. `beanjar remote layout [HOST/]NAME --json` picks the VM's remote as the viewer does
  (its firmware's platform: webOS, NetCast, SimpleSmart) and prints `as_json(layout)`; the web
  server serves that as `layout.json`, so the layout comes from the host that runs the VM. The
  popup draws it with positioned elements in CSS pixels, scaled to its window; keys without a code
  are dimmed and send nothing. Glyphs are text symbols (the GTK remote's are icon names).
- **Sound: beanjar-vnc's audio socket.** See [Sound](#sound) below.
- **Video.** Tight with JPEG, as noVNC asks. The page's Quality control (a slider 0 to 9 in a popover under the
  header's More button, default 3, kept
  in the browser's local storage) is noVNC's `qualityLevel`, which neatvnc turns into JPEG quality
  11*q+1 and applies at once. There is no compression control: neatvnc ignores the client's
  compression level. neatvnc's H.264 only runs for GPU (GBM) buffers, which beanjar-vnc's frames
  are not; H.264 is a stream of beanjar-vnc's own beside RFB, [the H.264 picture](#the-h264-picture).
  `--size` and `--fps` are beanjar-vnc's and apply when beanjar-web starts it.

## The server

aiohttp (`host/web/beanjar_web/server.py`), the pages beside it in `static/`.

### Only some hosts: `--hosts`

`--hosts HOST[,HOST...]`, given once, comma-separated, limits the server to those hosts' VMs. The
names are the `HOST` of `HOST/NAME` exactly as the manager takes them: a host of `beanjar host
list`, or `local` for this machine's own VMs. This machine is a host like any other here: with
`--hosts`, its VMs are left out unless `local` is named (`--hosts rk-2` shows only that
board's VMs; `--hosts local,rk-2` both). There is no other name for this machine: its own
host name is not one the manager knows.

- The list asks each named host alone (`beanjar list --json --host HOST`, concurrently), so a
  host that is not named is never asked and cannot slow the list down; rows of any other host are
  dropped as well. A named host that fails is missing from the list, as `beanjar list` leaves out
  an unreachable host; all of them failing is the list's 502 with their errors. Thumbnails use the
  same list.
- Every route whose path names a host (`{host}` in the route table: every `/vm/HOST/NAME/...`
  page, action, status, stream and picture) is refused with 403 (`host HOST is not served here
  (--hosts)`) by a middleware after authentication and before any handler, so no `beanjar` command
  runs for it. 403 rather than 404: the server never checks that a VM exists (the manager does),
  so an unknown VM of an allowed host still reaches the manager's own error. A route added later
  with `{host}` in its path is covered without a check of its own; `test_hosts.py` walks the route
  table to keep it so, and requires every `/vm/` route to name its host.
- Start-up checks each name against `beanjar host list --json` (not asked for `local` alone): an
  unknown name, a malformed one, an empty one or a second `--hosts` is exit status 2 with the
  configured hosts named, for `serve` and also for `start` and `enable` before a unit is made, so
  a typo cannot silently show an empty list. A host removed from the configuration later is the
  manager's error for that host at run time.
- `start` and `enable` carry it into the unit's command line (`--hosts rk-2`), `serve`
  writes it to `state.json` and `status` prints `only the VMs of --hosts rk-2`.

Without `--hosts` nothing changes: one `beanjar list --json` of every host, and every host's routes.

| Path | What |
|---|---|
| `/` | VM cards grouped by host (`beanjar list --json`, refreshed every 10 s, every 2 s while an operation is pending); an Off card has a labelled Start button, a Running card opens the viewer and has Open and a Stop button (the Stop dialog), its tags, and the reason it runs if it has one (`beanjar status`), Starting/Stopping show a pulsing amber dot; the header filters by name, tag or host and puts running VMs first |
| `/api/vms` | the list's rows, only what a row shows: name, host, state and detail, platform, firmware, display, memory, CPUs, uptime, whether a viewer or beanjar-vnc runs, the GPU headline, `mitm` (its network's mode is mitm), `reason` (why it runs: `{"text", "set", "user", "by"}`, or null), `tags` (an array), and `operation` (the latest start/stop result, or null); no VM paths |
| `/vm/HOST/NAME/` | the viewer page |
| `POST /vm/HOST/NAME/start` | `beanjar start --json HOST/NAME`, in the background: 202 with the pending operation and a `Location` pointing at its status; a second start or stop while pending is 409 without another CLI invocation |
| `POST /vm/HOST/NAME/stop` | `beanjar stop --json HOST/NAME`, with the manager's normal power-off and sync/stop fallback; the same asynchronous response as start. The manager refuses a VM that runs for a reason; `?force=1` is `stop --force`, which the page sends only after its Stop dialog has shown the reason; `?unplug=1` is `stop --unplug` (QEMU killed at once, no logs or sync), the dialog's Unplug, with `"unplug": true` in the operation |
| `/vm/HOST/NAME/operation` | the latest start/stop operation: `{action, status, message}` (and `error` on failure); 202 while pending, 200 on success, 409 with the manager's refusal, 404 before any request or after a web server restart |
| `POST /vm/HOST/NAME/rotate` | the body an angle (0, 90, 180 or 270): `beanjar sensor set rotate --json HOST/NAME ANGLE`, 200 with its JSON once the guest reports the setting, 409 with the manager's refusal (a model that does not turn), 400 for another body |
| `POST /vm/HOST/NAME/vnc` | `beanjar vnc start --json HOST/NAME --port 0` (any free TCP port, so a host program on 5900 does not stop it; the page uses only the local sockets): 200 when it started or already runs, 409 with the manager's message otherwise (for example `a viewer is already open for tv (pid N)`, as the GTK viewer and beanjar-vnc exclude each other) |
| `/vm/HOST/NAME/rfb?page=ID` | WebSocket: RFB in binary messages, to and from `beanjar vnc pipe`; no picture while the page `ID`'s `/video` plays ([below](#no-rfb-picture-under-h264)) |
| `/vm/HOST/NAME/remote` | WebSocket: text messages `down CODE` / `up CODE` (and `touch KIND SLOT X Y WxH` for a touch screen) to `beanjar remote pipe`, its lines back |
| `/vm/HOST/NAME/audio?format=opus\|pcm` | WebSocket: the format's request line to `beanjar vnc audio-pipe`, its stream back in binary messages (another format: 400) |
| `/vm/HOST/NAME/video?bitrate=N&page=ID` | WebSocket: `h264 bitrate=N` (kbit/s, default 10000) to `beanjar vnc video-pipe`, its stream back in binary messages (a bitrate outside 100 to 50000: 400); `page` pairs it with that page's `/rfb` (8 to 64 letters, digits, `-`, `_`; else 400) |
| `/vm/HOST/NAME/layout.json` | `beanjar remote layout HOST/NAME --json` |
| `/vm/HOST/NAME/screenshot.png` | `beanjar screenshot HOST/NAME -` |
| `/vm/HOST/NAME/screenshot.png?thumbnail=1` | a shared 384×216 card preview (`beanjar screenshot HOST/NAME - --frame 384x216`); cached pictures return immediately, refreshing in the background after 5 s; a first picture may wait; eligibility uses the shared VM list, returning 204 unless Running with beanjar-vnc or Off (its kept `--last` thumbnail, fetched once per state, or 204) |
| `/vm/HOST/NAME/inspector.json` | `beanjar mitm show --json HOST/NAME` without the URL and password file: `{mode, status, ready, problem, url}` (problem: why a started VM's inspector is off, no usable mitmweb, with what to install; else null; url: where to open it, on the inspector's own origin with the VM's grant, once it is ready); the manager's failure 502 |
| `/vm/HOST/NAME/mitm/...` (the inspector origin) | the VM's traffic inspector (mitmweb), every method and its `/updates` WebSocket, on the inspector's own listener only ([below](#the-traffic-inspector)); `/vm/HOST/NAME/mitm` redirects to it with the slash |
| `/remote.html?vm=HOST/NAME` | the popup remote |
| `/api/session` | `{"auth": MODE}`: the list shows Log Out for `password` |
| `/login`, `POST /login`, `POST /logout` | `--auth password`'s login page, its form, Log Out |
| `/novnc/` | Debian's noVNC files, as they are |
| `/icons/` | Debian's Bootstrap Icons (SVG), as they are |

A pipe's failure ends its WebSocket with code 4000 and the manager's message as the reason. At
shutdown (SIGINT, SIGTERM) every pipe's stdin is closed and the pipe ended. Pending start/stop
CLI processes and their process groups are terminated and reaped too; the VM remains the
manager's responsibility.

Start returns at the initramfs shell, and stop can wait 90 s for webOS before falling back.
The web server accepts these operations immediately rather than holding an HTTP request open:
the plain JavaScript list can keep refreshing while the manager works. The card shows Starting
or Stopping with a pulsing amber dot over its thumbnail, with no actions while pending,
and shows a refusal or the manager's forced-stop fallback beneath
its real state. Both local and remote VMs use exactly the same CLI path. The manager's stderr
diagnostics (the last 16 KiB) are retained on success too, so a guest refusing power-off and the
manager syncing and stopping it is visible even when the CLI exits successfully. No web timeout
interrupts the manager's own boot or shutdown policy.

Only the latest operation per VM is retained, in memory, until the next request or web server
restart. `/api/vms` always keeps the manager's actual `state`; the pending label is a UI overlay.
After a restart it shows the manager's state without a stale pending label. The controls live on
the list's cards, and the viewer has Start on its VM-off page and Stop VM… in More.

The cards use a responsive grid, one column on a phone. Each has a 16:9 thumbnail, name
and one grey details line (webOS release, uptime, VNC or `GTK viewer open`, the former
monitor mark). A small dot at the top left of the thumbnail shows Running in green,
Starting/Stopping in pulsing amber, and Error or a refusal in red. Its accessible label and
title tooltip include the state and detail. Off has no dot, keeping a large idle library quiet.
Reduced-motion preferences disable the pulse. A Running card's link is keyboard reachable
and highlights on hover or focus.
The card's actions are labelled buttons in its info row, beside the name, always shown and above
the link: an Off card has Start, a Running card Open (when it has a picture) and a destructive
power button, Stop. There is no card menu. They are 36 px high with a pointer and 44 px on a
touch screen, each with an accessible name identifying the VM. Stop opens the Stop dialog
(`static/stop.js`, shared with the viewer): an in-page `<dialog>` in the GTK manager's Shut Down
wording, "Send power-off signal" (`stop`, with its sync-and-stop fallback) or "Unplug (may lose
data)" (`stop?unplug=1`), the button turning destructive and named Unplug for the latter; a VM
that runs for a reason has it in the dialog, which then sends `force=1`. A request the server
refuses (another operation pending, the server gone) shows in the dialog, which stays open; a
refused start shows on its card. Routine success has no extra
message: the state change is the feedback. Refusals
stay red beneath the status; a forced-stop fallback stays grey. Unreachable hosts supply no rows
from `beanjar list --json`, so the page does not keep stale actions for them.

Off cards show only their webOS release in the details line, without stale uptime or viewer
indicators. Their thumbnail area shows the VM's last screen, the thumbnail the manager kept when
it stopped, dimmed (fetched once a state), or a dim TV placeholder without one. Running cards
without a picture show a TV placeholder labelled `No preview`, without repeating the state.

Card previews use `IntersectionObserver` and the page's visibility state: only an on-screen,
Running card with beanjar-vnc requests one, at most once every 5 s, and an Off card once. Other
cards keep a TV placeholder. A shared server cache returns the last PNG at once,
even after its five-second refresh interval; one background task per VM refreshes it for every
page. Only a first picture may wait. Failed refreshes retain the last good picture and wait for
the next interval rather than retrying per page. Off cards fetch `--last` once per state,
including a negative result. A new list's ineligible state takes precedence over a cached PNG.
Refresh tasks and their CLI processes are cancelled at shutdown. The size is 384×216, requested
through the existing screenshot CLI. Eligibility comes from the server's shared manager list,
normally filled by the page's ten-second list poll. Concurrent list requests share a three-second
cache; previews reuse the latest snapshot for up to fifteen seconds (allowing for polling and
network delay), then share a fallback refresh. Preview requests do not list every host for each
card. Start/stop requests invalidate that snapshot at their start and completion. This avoids deliberately launching the screenshot
command's expensive headless viewer for a VM without beanjar-vnc. This is a point-in-time check:
the manager can change between list and capture. The explicit full-size screenshot in the
viewer keeps its existing behavior.

Measured 2026-10-28 on the PC against an RK3399 board's running `w22o-queue`, using a private
loopback web server and read-only list/screenshot requests. Both runs first populated the page's
VM list; browser/service caches started empty in each private server. The user's port 8059
service and board VMs/VNC were left running.

| Preview time to first byte | Before | Shared list and background refresh |
|---|---:|---:|
| First image | 4.8171 s | 2.3135 s |
| Cached image | 0.0013 s | 0.0008 s |
| Stale image, after 5.2 s | 5.1663 s | 0.0011 s |
| Next image, 4 s later | 0.0009 s | 0.0009 s |

The next response contained the refreshed PNG. A second board's `wt1h-afro` was Off without
beanjar-vnc: its first kept `--last` image took 3.3511 s before and 1.2115 s after. This does not
take a live picture or start a viewer on that board. First list requests took 2.2929 s and
2.0509 s respectively; the steady preview path now reuses that work.

SSH sharing was already working. The manager specifies `ControlMaster=auto`, `ControlPersist=60`
and `$XDG_RUNTIME_DIR/beanjar-ssh/%C` (a private per-user temporary directory if no runtime
directory exists). OpenSSH expands `%C` from the connection identity, so list and screenshot
commands share the same host/user/port connection. An isolated control directory measured
end-to-end `beanjar screenshot --frame 384x216` at 3.8159 s cold and 2.8366 s warm; a repeat
measured 3.2749 s cold and 2.4930 s warm. The master was alive immediately after every call.
The SSH production code did not change; command startup/remote work still costs seconds even
with a warm transport. The running web service had the same runtime/config environment as the
shell and an SSH agent available. Its manager subprocesses can use that shared socket directory.
`test_remote.py` also checks the effective OpenSSH configuration with `ssh -G` without a host:
the expanded socket matches across commands, differs across hosts, and persists for 60 s.

### The traffic inspector

A `mode = "mitm"` VM's mitmweb is served at `/vm/HOST/NAME/mitm/` of the inspector's own origin,
for this machine's VMs and other hosts' alike (and `--hosts`). Whoever may open it already drives
the VM, so the server logs in to mitmweb for them: the browser never sees the inspector's password.

- **Its own origin.** mitmweb renders the guest's traffic, which the guest controls; a script that
  got into its page would, on beanjar-web's origin, drive every VM of every host with the user's
  session. So the inspector has a listener of its own on the same address, `--inspector-port N`
  (default `--port` + 1, 8060; any with `--port 0`), serving the inspectors and nothing else
  (`Server.inspector_app`), and the main origin serves no inspector. Its access is a grant per VM:
  an HMAC of `HOST/NAME` with a key made at each start (`Inspectors.grant`).
  `/vm/HOST/NAME/inspector.json` of the main origin, behind its access, gives the URL with
  `?grant=`; the first request with it sets the grant as an `HttpOnly`, `SameSite=Strict` cookie
  (`beanjar_inspector`, `Path=/vm/HOST/NAME/mitm`, `Secure` behind HTTPS) and redirects without
  it, as the main token does. A wrong grant, another VM's or one of a previous start is 403.
  Cookies are not kept apart by port, so the browser sends the main session cookie to the
  inspector origin too: it is `HttpOnly` (the page cannot read it), is never accepted there, and
  is never passed to mitmweb (nor the grant). The page there could still send requests to the
  main origin, which the browser would send the session cookie with (another port of the same
  host is the same site): the main origin refuses a POST or WebSocket whose `Origin` is not its
  own or whose `Sec-Fetch-Site` is `same-site` or `cross-site`, and any other request from
  another site or origin that is not a page opened at the top (`server.foreign`: Fetch Metadata,
  `Sec-Fetch-Mode: navigate` and `Sec-Fetch-Dest: document`), so it can neither read the main
  origin's answers nor act with its cookie. The inspector origin applies the same rules to
  itself. Behind a proxy that gives the inspector another public name
  ([below](#behind-a-reverse-proxy-cloudflare-tunnel)), `--inspector-origin URL` is its origin in
  the URL; otherwise it is the page's host at the inspector's port. Not chosen: a
  `Content-Security-Policy: sandbox` page on the main origin without `allow-same-origin`. Its
  opaque origin breaks mitmweb (12.3.1): its API helper reads the XSRF cookie from
  `document.cookie`, which throws there, so every PUT, POST and DELETE fails; its module scripts
  and `modulepreload` are CORS requests from origin `null`; its `fetch` is `credentials:
  "same-origin"`, so no cookie would authenticate it; and mitmweb refuses a non-GET whose
  `Sec-Fetch-Site` is not `same-origin`. The inspectors share their origin with each other: a
  script in one VM's inspector page can reach another VM's that this browser holds a grant for.

- **Where it is: `beanjar mitm connect --json HOST/NAME`.** It prints one JSON line, the URL from
  which this machine reaches the inspector and its password, then holds until its stdin ends. For
  a local VM the URL is the VM's own address (`127.0.1.N:18083`, `NETSTUB_WEB_HOST/PORT`; with
  `link = "lan"` that address, not the LAN one); for `HOST/NAME` the manager asks the host's
  `beanjar mitm show local/NAME --password --json` (one call: status, address, password, which the
  host reads from the VM's owner-only `run/mitmweb.password` as `inspector.py` does) and adds a
  forward from `127.0.0.1:PORT` to that address to the host's shared ssh connection (`ssh -O
  forward`, as `ares-TOOL`'s), cancelled when it ends. One runs per VM while the route is in use:
  the first request starts it, the others share it, and it ends (its stdin closed) 60 s after the
  last request or WebSocket finished, and at shutdown. A run killed outright leaves a record that
  the next remote run on that machine cancels (`ares.sweep_homes`). The manager refuses a VM
  without mitm, not running, or whose inspector is not ready (`inspector.probe`): 409 with its
  message; no answer within 90 s is 502.
- **Under a prefix as it is.** mitmweb (the user's own, 12.0.1 or newer, `mitmtool.py`; read in
  12.2.3 and 12.3.1, which do not differ in what follows) has no base-path option and honours no
  `X-Forwarded-Prefix`, but its page needs neither: `index.html` loads `./static/...`, the API
  helper turns `/flows` into `./flows`, content downloads are `./flows/...`, the WebSocket is the
  page's path plus `/updates`, and its routing is in the fragment (`#/flows/...`). So requests are
  passed on as they are, below the prefix, and the page must be opened with its slash. The one
  exception is File > Save and Save filtered, `location.replace("/flows/dump...")`: in the page's
  script (`static/index-*.js`, asked for uncompressed) those two literals become `./flows/dump`,
  a plain byte replacement (`mitm.rewrite_script`); a later mitmweb without them is passed on
  unchanged. `check-mitm` counts the two against the installed one.
- **The login.** mitmweb (`tools/web/app.py`, `webaddons.WebAuth`; `web_password` and the Bearer
  check are why 12.0.1 is the floor) lets a request in with
  its signed session cookie `mitmproxy-auth-PORT`, or else checks `Authorization: Bearer PASSWORD`
  (or `?token=`) against `web_password` (an argon2 hash, `mitm_addon.WebPassword`) and sets that
  cookie. Every request here carries the Bearer password and the cookie once mitmweb gave it,
  which is kept in the server, per VM, and never passed to the browser: with a valid cookie mitmweb
  does not check the password at all, so the argon2 check runs once per `mitm connect` (and after
  a mitmweb restart voids the cookie). A GET or WebSocket refused with 403 (the VM restarted: a new
  password and a new mitmweb) ends the `mitm connect` and is tried once more with a new one, as is
  a connection refused. The server reads only `mitm connect`'s first line: it does not follow
  the new line a restarted VM's connect prints, it starts a new connect instead.
- **Cookies and checks.** The browser's cookies are not passed on (beanjar-web's session among
  them) except mitmweb's XSRF cookie, `_mitmproxy_xsrf`. mitmweb sets that one, readable by its
  script, in the `/updates` handshake; it is passed back with `Path=/vm/HOST/NAME/mitm/`, so each
  VM's page reads its own, and the script sends it as `X-XSRFToken` with every PUT, POST and
  DELETE, which mitmweb checks against the cookie (tornado's `xsrf_cookies`). mitmweb has no
  `Host` allowlist (no DNS-rebinding check) to satisfy; it checks a WebSocket's `Origin` against
  its `Host` (tornado's `check_origin`) and refuses a non-GET with a `Sec-Fetch-Site` other than
  `same-origin` or `none`. This server checks the browser's `Origin` for POSTs and WebSockets (the
  middleware) and for PUT and DELETE (the route), sends mitmweb no `Origin`, `Referer`,
  `X-Forwarded-*` or `Authorization` of the browser's, and passes `Sec-Fetch-Site` on.
- **The viewer's link.** More has Traffic Inspector (`binoculars`), opening `inspector.json`'s
  `url` in a new tab (`rel=noopener`), for a Running VM whose list row says mitm, once `inspector.json` says it is ready
  (asked every 15 s until it is, then every minute; hidden again when it is not). While it is off
  for good (`problem`), a dimmed "Traffic Inspector off" item takes its place with the problem
  below it in small selectable text; it is a disabled menu item, reached with the arrow keys and
  read with its text, and a click leaves the menu open. While it is only starting, nothing shows.

Not done: rewriting anything else in mitmweb's pages (a later mitmweb that adds absolute URLs
would need it here; `check-mitm` against the installed mitmweb is where it would show), and the CSP mitmweb sends
(`connect-src 'self' ws:`) is passed on as it is, which allows the WebSocket at `wss:` behind
HTTPS too (CSP Level 3's `'self'`).

## Access

Every POST, including VNC start, login and logout, checks `Origin` against the request's own
scheme and host (or a trusted local proxy's forwarded scheme and host, below), and refuses
another origin or `null` with 403 before running a handler.
`Sec-Fetch-Site: cross-site` or `same-site` is also refused, and so is any other request from
another site or origin that is not a page opened at the top (Fetch Metadata; the traffic
inspector's origin is another port of the same site, [above](#the-traffic-inspector)). Requests
without Origin remain available to non-browser clients. This applies in every authentication mode, including loopback `none`;
start/stop also require the same session as the rest of the VM endpoints. WebSockets retain
the same origin check.

It listens on 127.0.0.1 by default (`--listen ADDRESS`: IPv4, IPv6 or a host name; `--port N`,
8059 by default, 0 for any; the traffic inspector's own listener on `--inspector-port N`, by
default the next port). `--auth` says who may open it:

- **`token`** (the default): a random token (`secrets.token_urlsafe(24)`), made once and kept in
  `web.token` in the manager's config directory (`$XDG_CONFIG_HOME/beanjar/`, beside
  `config.toml`; `--token-file PATH`), so the URL stays the same across restarts and `status` can
  print it. The first request with `?token=` sets it as an `HttpOnly`, `SameSite=Strict` cookie and
  redirects to the same page without it. Every other request needs the cookie (403 otherwise),
  compared in constant time. A new token: delete the file and restart.
- **`password`**: a login page (`/login`, `static/login.html`) asks for the password in
  `web.password` beside the token file (`--password-file PATH`), the file's first line, made when
  it is missing as beanjar-vnc's `vnc.password` is: 20 characters of an alphabet without
  look-alikes ([vnc](vnc.md#authentication-and-the-lan)). It is compared in constant time;
  attempts are taken one at a time and a wrong one is answered after a second, so guessing runs at
  one a second. The right one sets the same kind of cookie, an HMAC of the password: a restart
  keeps the session, a new password (read at the start) ends every one. Pages redirect to the login
  page without it, everything else is 403; the login page, `style.css` and the icons are open. The
  list's Log Out (`POST /logout`) clears the cookie.
- **`none`**: no token or password, only on a loopback address given as a number (`127.0.0.0/8`,
  `::1`); anything else (`0.0.0.0`, `::`, a LAN address, any host name, `localhost` included) is
  refused before it listens. It is beanjar-vnc's rule for a listener without a password
  ([vnc](vnc.md#authentication-and-the-lan)): whoever reaches the server drives the VMs (keys,
  remote, starting VNC), and a host name may resolve elsewhere. Loopback is still open to every
  local user's processes; the token or password also guards against those.

The secret files are one line, owner-only (0600, in a 0700 directory); one that is missing or empty
is made anew. In every mode a WebSocket whose `Origin` is not the server's own is refused, and the
server writes no access log.

**TLS.** `--certificate PATH --key PATH` (PEM; beanjar-web makes none) serve HTTPS, and the cookie
is then `Secure`. Besides keeping the token or password and the picture off the network in clear,
HTTPS makes the page a secure context on any address, so the speaker's Opus path (WebCodecs) works
from a LAN address too, not only from `http://127.0.0.1` ([Sound](#sound)). Without them, a listener
that is not loopback prints a one-line warning that it is plain HTTP.

### Behind a reverse proxy (Cloudflare Tunnel)

Run `cloudflared` on the same machine, pointing the public HTTPS hostname at
`http://127.0.0.1:8059`. Keep beanjar-web bound to loopback:

```sh
beanjar-web start --listen 127.0.0.1 --auth password
# Or --auth token, using the public hostname with the token URL's ?token=... once.
```

Only when the actual socket peer is a loopback address does beanjar-web honour
`X-Forwarded-Proto: http|https`, and with it `X-Forwarded-Host` if present (otherwise `Host`).
POST and WebSocket Origins must match that public origin. Forwarding headers from non-loopback
peers are ignored; `X-Forwarded-For`, `CF-Connecting-IP` and `Forwarded` do not establish trust.
Only one forwarded scheme and host are supported; ambiguous or malformed local forwarding
headers are refused with 400. For forwarded HTTPS, token and password session cookies are
`Secure`, `HttpOnly`, and `SameSite=Strict`, even though the tunnel's local hop uses HTTP.

Cloudflare documents [X-Forwarded-Proto](https://developers.cloudflare.com/fundamentals/reference/http-headers/#x-forwarded-proto)
as the visitor's protocol, overwriting a client-supplied value. Its
[CF-Visitor](https://developers.cloudflare.com/fundamentals/reference/http-headers/#cf-visitor)
contains JSON with a `scheme` field; beanjar-web relies on `X-Forwarded-Proto` instead and does
not interpret that redundant header. `cloudflared` preserves the public `Host` by default.
If [httpHostHeader](https://developers.cloudflare.com/tunnel/reference/origin-parameters/#httphostheader)
overrides it, [cloudflared's HTTP origin proxy](https://github.com/cloudflare/cloudflared/blob/master/ingress/origin_proxy.go)
sets `X-Forwarded-Host` to the original public host.

The traffic inspector has its own listener (`--inspector-port`, 8060 by default), which needs a
public hostname of its own, e.g. `https://inspector.example.org` pointed at
`http://127.0.0.1:8060`, given as `--inspector-origin https://inspector.example.org` so that the
viewer's link names it; without one the link names the main hostname at port 8060, which the
tunnel does not serve. Put it behind the same Access application.

With Cloudflare Access, `--auth none` is supported on the loopback listener. **The Access policy
is then the only lock: the tunnel must only route beanjar-web through an Access application.**
Beanjar-web does not validate Access tokens or add its own login in this mode. Keep `--auth
password` or `--auth token` when the application should require a beanjar-web session as well.
Direct local processes can still reach a loopback listener, as described above.

The Pi serves its web this way: `beanjar-web enable --listen 127.0.0.1 --auth none` (port 8059,
back after a reboot), behind its `cloudflared` and an Access application. It has the RK3399
boards as hosts, so its pages show its own VMs and the boards', or with `--hosts` only the hosts
named ([above](#only-some-hosts---hosts)).

## The pages

- **List.** A grid of TV cards grouped by host, this machine first; one column on a phone.
  A card has its preview or placeholder, name, one details line and a small corner state dot.
  Off cards have a Start button; running cards open the viewer and have Open and Stop buttons;
  pending actions pulse amber. See [the server](#the-server)
  for thumbnail and operation behavior. The header
  keeps the title centred; at its end a filter box (a card shows when every word of it is in its
  name, a tag or its host, `local` or "this machine" for this machine's), Running first (a toggle,
  on by default and kept per browser as `beanjar-web-running-first`: within each host's section
  running VMs first, then starting or stopping ones, then the rest in the manager's order) and
  Refresh; with `--auth password`, Log Out (`box-arrow-right`) sits before Refresh. Below 960 px
  the filter and the toggles take a second header row. An empty list shows "No Virtual
  Machines", a filter that matches nothing "No Matching VMs".
- **Viewer.** A header bar laid out as the GTK viewer's (`host/ui/data/viewer/window.blp`), icon-only
  buttons with tooltips, Bootstrap icons. At the start the remote's Back, Home and Settings keys as
  one linked group (shared edges, as GTK's `linked` box), sent with the layout's `BACK`, `HOME` and
  `MENU` codes (NetCast's Home is its dashboard key; held, they are long presses). In the middle the
  VM's name as the title, and under it its host (another host's), platform and board and firmware
  version ("webOS 1 · WT1H 05.06.10.01"), from the list's row.
  At the end Sound, Save Screenshot (a PNG download named `NAME-TIME.png`), Remote, Fullscreen
  (the picture alone) and More appear in that order.
  More contains Quality, Planes and Stats for nerds (checked while the overlay is shown), Portrait,
  Full remote, Traffic Inspector and Remote in a Window where they apply, and last, after a separator, Stop VM…, which
  opens the list's Stop dialog (shown while the VM is not off). Planes (`layers`) opens a popover of three
  check items, UI, Video and Cursor: beanjar-vnc composes one picture for all its clients, so
  they are the VM's, shared by every page and VNC client, as its note says, and last until
  beanjar-vnc stops. The page sends `beanjar:planes=N` (the hidden `WV_PLANE_*` bits) as noVNC
  clipboard text, asks with `beanjar:planes?` when it connects, and shows what beanjar-vnc answers
  ([vnc.md](vnc.md#planes)). Save Screenshot keeps every plane. The Remote toggle is pressed while the remote shows (floating, or its window); a click then closes it. The toggle uses `static/tv-remote.svg`, a TV remote drawn on Bootstrap Icons' 16px
  grid with a one-pixel outline. The popup remote uses the same
  system icon masks for the keys the GTK layout draws as symbols (power, navigation, volume and
  media), in each key's ink colour; keys it labels with text (CC, GUIDE, BACK...) keep the text.
  The controls the GTK window has no counterpart for: All VMs before the keys, the
  speaker (the GTK viewer's sound is the desktop's), and Quality (a popover) in More. Under the bar noVNC's canvas, scaled to fit
  (`scaleViewport`), with the keyboard and pointer as noVNC sends
  them. Before connecting the page asks for VNC (`POST .../vnc`) and shows the manager's refusal;
  a lost connection says so, with a Reconnect button. Once a stop it sent has returned (or right
  away) the page asks the list for the VM's state: while the VM is off it says "VM NAME is not
  running" with Start beside Reconnect, and the TV keys (the header's and the lite remote's),
  Sound, Remote and Save Screenshot are disabled. Start posts `start`, follows `/operation` until
  the manager has returned, then connects as Reconnect does; a refusal shows with both buttons.
  In portrait windows up to 640 px wide, All VMs and the title stay in a single 48 px header.
  The same system-key, Sound and Remote buttons move to a fixed bottom toolbar, followed by
  More (Quality, Planes, Save Screenshot, Stats for nerds, Fullscreen, Traffic Inspector, Stop VM…). Each visible toolbar control is 44 × 44 px; the
  system keys keep their linked group at the start, and flat controls stay together at the end. The toolbar reserves picture space
  and includes `env(safe-area-inset-bottom)`; the header respects the top safe area. The picture
  still scales to fit between them. Desktop and landscape keep the header layout.
  CSS moves the groups; Save Screenshot and Fullscreen join the shared native overflow popover in portrait. More closes on selection, Escape or an outside click and supports arrow keys; Quality opens above the
  toolbar, stays in the viewport on resize, and Escape returns focus to More.
- **Remote.** On desktop the Remote button (on a touch screen's model, its menu's Show remote)
  shows the remote floating over the picture, as the GTK remote window: `#remote-float` in
  `#screen` (so fullscreen keeps it), drawn in the page by `remote-draw.js` from `layout.json` and
  sending over the viewer's own remote socket. It is drawn in the page rather than as an iframe of
  `remote.html` because a press inside an iframe reaches neither the parent's pointer events (the
  drag would need the frame to post them back) nor leaves the keyboard where it was (the frame takes
  the focus, so the PC's keys would stop reaching noVNC). The remote's own body is its only frame,
  with a round close control on its chin (between the lowest key and the body's bottom edge). A
  press that starts on a key is that key's only, however it moves; a press on the body between
  the keys drags it once it has moved 8 px (the GTK remote's `gtk-dnd-drag-threshold`), and the
  close control never drags. Presses on it keep the focus where it was (`preventDefault` on
  `pointerdown`), so the PC's keys go on reaching the TV. It stays inside the picture's area, 12 px
  from its edges, snaps to an edge within 24 px, and is kept per browser
  (`beanjar-web-remote-float`) as its distance from the nearer horizontal and vertical edges, so it
  keeps its corner as the window changes size and is clamped back into view; its first spot is top
  right under the header. A picture area shorter than the remote scales it down to fit. It hides
  on phones and while the VM is off. More's Remote in a Window opens the remote's own page instead:
  `window.open('/remote.html?vm=HOST/NAME', 'beanjar-remote-HOST-NAME',
  'popup,width=W,height=H')`, sized to the layout (1.25 times, at most 90% of the screen's height);
  the window name keeps one popup per VM. `pointerdown` sends `down`,
  `pointerup` and `pointercancel` send `up`, with pointer capture, so holding a key is a long press
  (the micom repeats it). While it has the focus, the arrow keys, Enter and Backspace are the
  remote's arrows, OK and Back. It closes itself when its opener is gone (checked twice a second),
  and the viewer closes it when it unloads. In narrow portrait windows, the same remote page
  opens in an in-page panel above the toolbar instead, with a Close button and Escape to return
  focus to Remote. Its frame is unloaded when dismissed, ending its input WebSocket; embedded
  remotes do not run the standalone window's opener check. Desktop keeps no such panel: its remote
  floats (above). Embedded, the remote page is
  transparent and fits the remote's body (the layout's `body`) rather than its whole panel, and
  the frame is as wide as that body at its height, so the remote's own body sits directly inside
  the panel's frame.
  On phones that panel is opened by the lite remote's 123 key in portrait and by More's Full remote in
  landscape, whose strip has no 123 key (More's item is hidden in portrait: it would duplicate 123).
  The audit of the other layouts found no further duplicates: Back, Home and Settings leave the bar
  while the lite remote shows them; the bar's Sound button is the page's audio stream, not the TV's
  Mute key; the Mouse button is gone (Remote button menu) and Rotate lives in More.
- **Lite remote (phones).** `#lite` in `viewer.html`, shown on phones by default beside the
  picture: portrait up to 640 px wide, or landscape up to 500 px high (`style.css`'s two media
  queries; desktop never shows it). In portrait it is a bottom sheet on the toolbar, black outside
  its rounded top corners as the letterbox: a grip, then Back, Home and Settings (icon and label);
  a VOL rocker, a round D-pad with OK and a CH rocker; Mute, Input, Play, Pause and 123. Collapsed,
  only its top 21 px stay above the toolbar (the peek strip: the border and the 20 px grip, enough
  for the rounded top and the bar); a negative margin of the sheet's height (`--lite-h`, kept by
  `viewer.js`) takes the rest out of the flow, behind the toolbar, and its keys are `inert`. The
  grip (one bar) is for pointers only: a plain `aria-hidden` element, not focusable, since the
  toolbar's Remote button is the sheet's accessible control. A tap on it toggles the sheet;
  dragged either way, the sheet follows the finger and settles open or collapsed by distance (a
  third of the travel from where it started) or a flick (over 0.5 px/ms and 24 px). Its touch area
  reaches past the bar (a transparent `::before`): collapsed, 28 px up into the letterbox under
  the picture (48 px in all, a finger's width; at phone sizes the 16:9 picture is width-bound and
  leaves far more letterbox than that, so it never covers the picture's own input), and open only
  the 8 px gap down to the keys, since above the open sheet the picture may come close. In landscape
  it is a strip about 224 px wide on the right, clear of `env(safe-area-inset-*)`: Back, Home and
  Settings icons, the D-pad, Vol−, VOL, Vol+ and Mute; closed, it is gone. Opening and closing
  slide (220 ms for the whole way, ease-out, Web Animations on `transform`; no slide with
  `prefers-reduced-motion`). The picture stays centred in the space the sheet leaves, without
  noVNC rescaling every frame: the layout changes once, when the sheet starts to open or has
  finished closing (then `rfb.scaleViewport = true` rescales in the same frame), and meanwhile a
  transform on `#screen` (computed from the framebuffer's aspect, sampled at seven points along
  the way) moves and scales the picture as the sheet moves. Under 6× CPU throttling in headless
  Chromium ten slides kept every frame within 16.8 ms, two rescales per slide and no long task.
  While the sheet is open the bar's own Back, Home and Settings group is hidden; on phones the
  Remote button toggles it with the same slide (`aria-expanded` and `aria-controls="lite"` there,
  titled "Hide remote" or "Show remote", shaded while open; Enter or Space keep the focus on it;
  on desktop it stays the remote panel's `aria-pressed` toggle), and the choice is kept per
  browser (`beanjar-web-lite`); in landscape, closed, the page is exactly the layout above. Keys
  are at least 48 × 48 px,
  fill with the accent and shrink slightly while held, and vibrate 8 ms where `navigator.vibrate`
  exists. Each key is a layout.json name (`data-key`) wired as the bar's keys: its code from
  `layout.json`, `down CODE` on `pointerdown` and `up CODE` on `pointerup`, `pointercancel` or lost
  capture, over the same `/remote` socket; a key the layout lacks is disabled (the service remote
  has no BACK, HOME or INPUT_HUB). A key the layout draws otherwise than the usual remote
  (`data-glyph`/`data-text` against its `glyph`/`text`) takes the layout's words, as NetCast's
  HOME shows "NetCast", or its glyph's icon, as the service remote's ± channel keys.

The RFB connection defines the page's session. Losing it stops the audio context, audio socket,
H.264 decoder and video socket, and cancels media retries. Reconnect restores the selected
picture and the user's sound intent together after RFB connects. An audio-only failure retries
after three seconds while the picture stays connected. An H.264-only failure temporarily uses
VNC and retries H.264 after three seconds, keeping sound and RFB connected. The affected control
turns amber and its tooltip reports the retry; Quality's popover gives the video detail. These
failures do not replace the page with a disconnected message.

The relay has no application-level idle deadline: `Server.rfb()` and `Server.stream()` wait for
bytes or a socket/pipe close; `RfbPause` withholds picture updates under H.264 without closing
RFB. The manager's `remote.attach()` inherits SSH stdio without a stream timeout;
`ConnectTimeout=5` concerns connection setup, `ServerAliveInterval=5` with `ServerAliveCountMax=3`
ends a connection whose host has gone silent for 15 seconds, and `ControlPersist=60` concerns a
master with no remaining sessions. A still or paused RFB connection can therefore be silent while audio stays
busy. The server now sends RFB WebSocket pings after 20 seconds without inbound traffic;
browsers answer with protocol pongs, without sending fake RFB input or waking the guest display.
[Cloudflare documents idle WebSocket closure and recommends ping/pong](https://developers.cloudflare.com/network/websockets/#idle-timeout),
but gives no numeric WebSocket idle deadline. Twenty seconds is our chosen keepalive interval.
The observed picture-disconnected/audio-playing behavior is explained by the former independent
client lifetimes. The specific historical transport failure could not be established from the
read-only service journal queries, which had no retained entries on the affected proxy host;
an idle proxy closure remains an inference, not a measured cause.

## Sound

The speaker toggle (`volume-mute` / `volume-up`) is off until clicked: a browser starts audio only
from a user gesture. The choice is kept in local storage; a page that was on starts with the first
click or key anywhere. The audio WebSocket is open only while it is on, and with it the pipe, so
beanjar-vnc mixes and encodes nothing for a page that is muted.

- **Format.** Opus when the page can decode it: WebCodecs' `AudioDecoder` exists and
  `AudioDecoder.isConfigSupported({codec: "opus", sampleRate: 48000, numberOfChannels: 2})` says so.
  WebCodecs exists only in a secure context: https, or `http://127.0.0.1` and `localhost`, the
  URL beanjar-web prints, or any address with `--certificate` and `--key`. A plain-HTTP LAN
  address is not one, and there the page asks for PCM
  (about 1.5 Mbit/s against Opus's 128 kbit/s). The speaker's tooltip names the format in use. The
  stream's format and the server side: [vnc](vnc.md#audio).
- **Decoding.** `static/audio.js` reads the header, then splits the stream across messages:
  length-prefixed packets go to the `AudioDecoder` (each a key chunk, locally timestamped
  from its Opus TOC duration), whose `AudioData` is copied out as planar float; PCM is converted to float directly. Both go to the
  same AudioWorklet.
- **Jitter buffer** (`static/audio-worklet.js`, in a 48 kHz `AudioContext`): playback starts once
  40 ms are queued. Each underrun raises that target by 20 ms, up to 160 ms; ten seconds
  of uninterrupted playback lower it by 5 ms toward 40 ms. A lowered margin drains gradually
  through interpolation (one extra input frame per quantum), avoiding an abrupt skip.
  Past 200 ms it cuts back to the current target and resets the steady-playback timer.
  Run dry, it plays silence and primes again. Its counters
  (received, played, underruns, skips, buffered, target) and the decoder's are
  `window.beanjarSpeaker.stats` in the console.
- **Browser latency.** The context requests 48 kHz and numeric `latencyHint: 0`, falling
  back to `"interactive"` if rejected. Repeated PC probes reduced baseLatency from 11.625 ms
  to 2.667 ms; native 44.1 kHz did not improve it. These are WSLg observations, not phone
  measurements. The [Web Audio options contract](https://www.w3.org/TR/webaudio/#AudioContextOptions)
  treats the hint as a request and reports the actual baseLatency. The Stats for nerds overlay
  shows actual sampleRate, buffered audio, adaptive target, baseLatency, outputLatency
  and underrun count; unsupported latency properties say “unavailable”. These individual
  values do not constitute a source-to-speaker latency measurement.
- **Resampling.** The guest's 44.1 kHz audio is resampled to 48 kHz in beanjar-vnc by linear
  interpolation, and the browser resamples 48 kHz to the sound card's rate if they differ.

## The H.264 picture

The Quality popover chooses the picture: **VNC**, noVNC's own (Tight JPEG) with the JPEG quality
slider, or **H.264** with a bitrate slider (1 to 20 Mbit/s, default 10). The choice and the values
are kept in local storage; VNC stays the default. The stream and its encoders (NVENC, VA-API, a
V4L2 encoder, x264) are beanjar-vnc's ([vnc](vnc.md#video)); the encoder's name, the frame rate
drawn and the bitrate received appear in **Stats for nerds** (for VNC, the RFB socket's
bitrate). The shared More menu's Stats for nerds entry toggles this compact, translucent
panel in the top-left of the picture area. It also shows stream dimensions and decoded/drawn
frame counts, plus the audio format, buffering and browser latency values described above, or
“Sound off”. Its open state is kept in local storage. Text updates only while open; pointer
input passes through to noVNC except at Close, and toggling returns keyboard focus to the VM.
The Quality popover holds only picture controls and their explanations.

An MJPEG choice (whole-frame JPEGs, decoded with `createImageBitmap`, which needs no
secure context) existed for a few days for hosts without a hardware H.264 encoder and was removed
with beanjar-vnc's JPEG stream ([vnc](vnc.md#video), "No JPEG stream"): the RK3399 has H.264 in
hardware now, and without a secure context the page stays on VNC.

- **Decoding** (`static/video.js`). The page opens `/video?bitrate=N`, reads the header and the
  units across messages, makes the codec string from the first keyframe's SPS (`avc1.PPCCLL`:
  x264's `avc1.42C01F`, NVENC's `avc1.4D401F` at 720p) and decodes with WebCodecs' `VideoDecoder`
  (`optimizeForLatency`), each unit a chunk with its keyframe flag and timestamp. Each `VideoFrame`
  is drawn onto a canvas at the stream's size and closed. `window.beanjarPicture.stats` has the
  counters (units, keyframes, bytes, frames decoded and drawn, the last second's fps and kbit/s,
  whether a hardware decoder is offered).
- **Over noVNC.** The canvas lies exactly over noVNC's (its position and scaled size, letterboxing
  included, followed with ResizeObserver and once a second), with `pointer-events: none`, so the
  pointer and the keyboard still go to noVNC, which sends them as before. The cursor is LG's, drawn
  into the composed picture; noVNC draws none of its own. Fullscreen covers both.
- **The secure context.** `VideoDecoder` exists only in a secure context, as `AudioDecoder` does
  ([Sound](#sound)): https, or `http://127.0.0.1` and `localhost`. Elsewhere, or where
  `VideoDecoder.isConfigSupported` accepts neither `avc1.42C028` nor `avc1.4D4028`, the H.264
  choice is disabled and the popover says why; the page stays on VNC (Tight).
- **Failure.** A stream that ends (beanjar-vnc stopped, the pipe killed, no encoder opens) or a
  decoder error turns H.264 off for the page, back to VNC at its JPEG quality, with a notice over
  the picture for 8 s; the stored choice stays for the next page. A changed bitrate opens a new
  stream (beanjar-vnc reopens its encoder at the new rate, for every client).
- **No RFB picture under H.264.** noVNC stays connected for the keyboard and pointer and as the
  fallback, but gets no picture while the stream plays ([below](#no-rfb-picture-under-h264)).

### No RFB picture under H.264

The page makes a random id (12 bytes from `crypto.getRandomValues`, which needs no secure
context) and puts it on both WebSockets, `/rfb?page=ID` and `/video?...&page=ID`. From the first
bytes of a page's video stream until it ends (closed by the page, failed, or the pipe gone),
beanjar-web pauses that page's RFB picture in its `/rfb` relay (`beanjar_web/rfbpause.py`); other
pages, and other VMs, are not touched. noVNC and neatvnc are not patched.

- **Following the stream.** The relay reads the client's side: ProtocolVersion, the security
  type, ClientInit, then each message by its length: SetPixelFormat, SetEncodings,
  FramebufferUpdateRequest, KeyEvent, PointerEvent, ClientCutText (the extended clipboard's
  negative length too), EnableContinuousUpdates, ClientFence, SetDesktopSize, xvp, neatvnc's NTP
  (160) and QEMU's messages (the extended key event; audio's enable, disable and format). Of the
  server's side it reads only the handshake, for ServerInit's size. Everything is passed on
  unchanged except what is withheld below, and beanjar-vnc's markers are added. A client it cannot follow (RFB 3.3, a security type
  other than None, a message type it does not know) is relayed as it is from then on and never
  paused. PointerEvent is 6 bytes: noVNC sends the 7-byte extended form only to a server that
  announces ExtendedMouseButtons, which neatvnc (0.9.1 and 1.0.3) does not.
- **Pausing.** While paused, the client's FramebufferUpdateRequests and EnableContinuousUpdates
  are withheld. noVNC uses continuous updates with neatvnc (it asks for them in SetEncodings,
  neatvnc answers EndOfContinuousUpdates, noVNC enables them), so the relay sends
  EnableContinuousUpdates off; neatvnc answers EndOfContinuousUpdates again, after which noVNC
  sends an incremental request after each update, and those are withheld. An update already in
  flight still arrives.
- **Resuming.** When the page's last video stream ends, the relay asks for the whole screen with
  one non-incremental FramebufferUpdateRequest, then turns continuous updates on again if noVNC had
  them. noVNC now believes them off and sends an incremental request after each update; the relay
  drops those while continuous updates are on (neatvnc 1.0.3 ignores them; 0.9.1 returns 0 from
  its handler for one, which stalls the client's input for good).
- **beanjar-vnc told.** neatvnc's API shows neither a client's pending requests nor its
  continuous updates, so beanjar-vnc would go on feeding neatvnc every frame, which its damage
  refinery hashes, with nothing sent. At the pause, and first at the resume, the relay sends a
  ClientCutText of exactly `beanjar:rfb-pause` or `beanjar:rfb-resume` (no NUL, no line end),
  part of the local socket's protocol: beanjar-vnc takes it on `--local` only and stops or starts
  feeding neatvnc for that connection ([Paused RFB clients](vnc.md#paused-rfb-clients)); a pause
  during the handshake is sent once ClientInit has gone. A beanjar-vnc without it ignores the
  text (neatvnc has no clipboard handler there).
- **noVNC without updates.** noVNC has no timeout for updates: a page stayed connected with no
  update for 68 s, the input going through, and took the whole screen when the stream ended.
- **JPEG quality.** The page keeps noVNC at the slider's JPEG quality under H.264 too (before, it
  went to 0 to make the stream underneath cheaper), so the picture after a fallback is the chosen
  one at once.
- **A changed bitrate** closes the stream and opens another; when the old one ends first, the RFB
  picture is resumed for that moment, and one whole-screen update comes.

Measured 2026-10-25 on the x86 PC (WSL, TCG), W22O webOS 23 (`w22o-number1`) at 1280x720, headless
Chromium 156 on `http://127.0.0.1` (a secure context), 15 s per mode:

- The home screen navigated with the remote (RIGHT, LEFT every 0.3 s): VNC at JPEG quality 3
  received 0.87 Mbit/s at 31 RFB updates a second. H.264 (NVENC, `avc1.4D401F`, 6 Mbit/s cap)
  drew 20 frames a second from 0.20 Mbit/s, while noVNC's RFB stream at quality 0 still took
  0.63 Mbit/s underneath (before the pause below).
- A still screen re-encoded once a second: about 60 bytes a unit (0.5 kbit/s).
- Headless Chromium decoded in software: `VideoDecoder.isConfigSupported` with
  `hardwareAcceleration: "prefer-hardware"` said no. Every unit received was decoded and drawn.
- The two pictures look alike; H.264 at constant quality 23 is a little sharper than JPEG
  quality 3 around text.
- A `video-pipe` killed under the page: the notice "H.264 stopped (beanjar failed): back to VNC",
  the picture back on noVNC at quality 3. On `http://lan.test` (a host name mapped to 127.0.0.1,
  not a secure context) the H.264 choice is disabled with "WebCodecs needs a secure context".

The RFB pause, measured 2026-10-25 the same way (no other client), with YouTube open in the TV's
browser and RIGHT, LEFT every 0.3 s, 15 s per run: in H.264 mode noVNC's RFB socket took 253, 199
and 288 kbit/s before (at quality 0) and 0 bytes in each of three runs after, H.264 drawing 11 to
13 frames a second either way; VNC alone at quality 3 took 315 kbit/s. A `video-pipe` killed after
8 s, and after 60 s, of H.264: the notice, 484 kB (the whole screen at quality 3) in the first
second, then about 230 to 315 kbit/s as in VNC mode; noVNC never disconnected.

## Packaging

`packaging/debian/build.py` makes `beanjar-web_<version>_all.deb`: `/usr/lib/beanjar/web/beanjar_web`
and `/usr/bin/beanjar-web`, depending on `beanjar (= <version>)` (the pipes and the layouts),
`beanjar-vnc`, `python3 (>= 3.13)`, `python3-aiohttp`, `novnc` and `bootstrap-icons`. Installed, it runs `beanjar`
from the PATH; from the checkout, `host/web/bin/beanjar-web` runs the checkout's manager
(`host/core`), and `--beanjar COMMAND` overrides either.

## Not in version 1

- Several servers as units, or another unit name than `beanjar-web` (besides the tests'
  `BEANJAR_WEB_UNIT`); making a certificate.
- A volume slider: the speaker is on or off; the TV's own volume keys set the level.
- A/V sync compensation for the browser's buffer: the media audio is mixed against the VM's clock
  as heard at beanjar-vnc, so the page plays it after the adaptive jitter buffer and browser output delay.
- The status bar's numbers (CPU, disk, network) of the GTK viewer.
- The same VM in the GTK viewer and the web at once: both front ends take QEMU's single-client
  chardevs. Several browsers on the same VM share beanjar-vnc (`shared`).
- With `--native-cursor` and a neatvnc without VMware's alpha cursor, noVNC shows LG's cursor with
  red and blue swapped: the local socket is plain RFB, and the swap beanjar-vnc does for noVNC
  applies to its WebSocket listener only ([vnc](vnc.md#the-cursor)); with the alpha cursor the
  colours are right. beanjar-web starts VNC without it (LG's cursor in the picture).

## Tests

- `check-vnc` (`tests/integration/vnc/check-vnc.py`): the local socket (None alone, mode 0600, a frame, a key)
  and `Display1.Ir` (edges in fake-qemu's IR log, a code over 255 refused, a held button released
  when its client leaves the bus).
- `check-vnc-pipe` (`tests/integration/vnc/check-pipe.py`): `vnc_pipe`, `audio_pipe` (both formats, fake-qemu's
  tone decoded), `remote_pipe`, `press` and the layout (`host/core/beanjar/pipe.py`) on a
  stand-in VM over fake-qemu and beanjar-vnc.
- `test_remote.py`'s `test_pipes`: `vnc pipe`, `vnc audio-pipe` and `remote pipe` of `HOST/NAME`
  run there with this process's stdio; `remote press` and `remote layout` are forwarded.
- `check-vnc-video` (`tests/integration/vnc/check-video.py`): the video socket ([vnc](vnc.md#tests)); and
  `check-vnc-pipe`'s `video_pipe` (the header and a keyframe first).
- `check-web` (`tests/integration/web/check-web.py`): beanjar-web with an aiohttp client against beanjar-vnc and
  fake-qemu, the manager stood in by `tests/fakes/fake-beanjar.py` (whose pipes are the manager's
  code): the token and its cookie, the token file (0600, the same token at the next run, the
  plain-HTTP warning on a host name), the password mode (the login page, a wrong password refused
  a second later, the right one opening the list and the three WebSockets, Log Out), `none` on
  127.0.0.1 and refused on 0.0.0.0, HTTPS with a self-signed certificate made by the test (skipped
  without openssl), and `start`/`status`/`stop` as a unit of the test's own name (skipped without a
  working `systemctl --user`); the list, VNC started or refused, an RFB handshake and a frame
  through `/rfb`, a cross-origin WebSocket refused, IR edges and a release at close through
  `/remote`, fake-qemu's tone through `/audio` as PCM and as Opus (decoded with libopus) and
  another format refused, the x264 stream through `/video?bitrate=800` (the header, a keyframe
  first, decoded by ffmpeg to fake-qemu's picture) and bitrates out of range refused, the RFB
  pause (fake-qemu `--animate`; a client that asks for updates as noVNC does, with and without
  continuous updates: no update in 3 s while its page's `/video` plays, a key and a pointer move
  reaching fake-qemu meanwhile, beanjar-vnc's `RFB client paused` and `resumed` lines with the
  same `vnc_feeds` (no frame fed to neatvnc) while the video went on, the whole screen within
  0.5 s of the video's end (with update requests the second update: the one asked before the pause
  is answered first) and updates after it, another page's video ignored, the marker over
  beanjar-vnc's TCP listener doing nothing, a bad page id refused), `layout.json` equal to `remote_layouts.as_json`, a 1280x720 screenshot,
  the pages, noVNC and the icons served, and a clean SIGTERM.
  `check-viewer` adds headless Chromium against the same fake display and manager: actual audio
  playback counters and H.264 frames, RFB dropped while media are playing, no media retry until
  reconnect, audio-only and video-only recovery with RFB retained, and an idle WebSocket's server
  ping/pong; and the phone lite remote in portrait and landscape: shown by default, every key at
  least 48 px, a touch on Home and Up sending their IR codes down and up (fake-qemu's input log),
  the Remote button collapsing (portrait) or hiding (landscape) it and opening it, remembered;
  both slides frozen halfway with the picture centred in the space left, ending in the state's own
  layout, and none with reduced motion; in portrait black pixels just outside the sheet's rounded
  corners open and collapsed, a 21 px peek, a grip with one indicator that is no button, has no
  name and is outside the tab order, its touch area 27 px above the collapsed sheet (the picture
  just beyond) and the gap under it open, and touch drags on it either way (the sheet and the
  picture follow; a short one springs back, a long one and a flick settle it), a tap, and Enter and
  Space on the Remote button (which carries `aria-expanded`). For a VM with a touch screen, a finger on the picture sends MultiTouch begin, move and end in slot 0 and no mouse press; with the Mouse toggle on, a click on the turned picture is a tap at the panel's own pixels, the choice, made in the Remote button's menu, survives a reload, a drag on the upright picture is a swipe (begin, moves, end in one slot, no mouse press), hovering sends nothing, the right button stays the pointer's (at the spot clicked), and toggled back a click is the pointer's left button again. For a set that turns (the fake VM made portrait, its rotation 90), the picture is turned 270 degrees, a finger and a click on it reach the panel's own pixels, the list's preview is turned 270 clockwise inside its box for rotation 90 and not at 0, and Portrait in More toggles 90 to 0, 0 to 270 and 270 to 0 with its `aria-checked` following. It prints counters and state assertions without judging screenshots, and skips if
  Chromium or python3-aiohttp is unavailable.
  Start/stop tests change fake-manager state files only: local and remote success, prompt 202
  while held pending, duplicate start/stop refused without another manager invocation, state
  changes in `/api/vms`, memory/limit/already-running refusals as 409 with their messages,
  successful stop fallback diagnostics, GET and invalid names refused, cross-site POSTs refused
  in token/password/none modes (also VNC, login and logout), authentication, a pending CLI ended
  at shutdown, and a restart returning to the manager's state without stale operation metadata.
  Thumbnail tests verify that Starting cards and a Running VM without beanjar-vnc never invoke
  screenshot, that an Off VM gets its kept thumbnail (`screenshot --last`) or 204, and that
  concurrent plus repeated preview requests share one 384×216 capture and the page's list.
  Four stale requests must return within 0.5 s while capture is deliberately held; the completed
  refresh publishes a changed fake-display picture. A failed refresh preserves the last PNG,
  Off results are fetched only once, a new ineligible state returns 204, and shutdown reaps a
  held background capture's CLI process.
  Reverse proxy tests cover forwarded HTTPS with public Host and with a Host override,
  matching POST/RFB Origins and mismatches in all authentication modes, Secure/HttpOnly/Strict
  token and password cookies, and IPv4/IPv6 non-loopback headers ignored by the origin helper.
  Two-viewer tests use distinct shared RFB WebSockets against the same fake display. For both
  incremental requests and continuous updates, they measure two-second windows with A alone,
  B connecting, B requesting a smaller desktop, B disconnecting/reconnecting, then A
  disconnecting/reconnecting. Each live client must receive at least five pixel updates per
  window with no gap over one second, remain connected, and retain the configured geometry.
  Counts, maximum gaps and the resize response are printed; all readers and sockets are closed
  at the end. These are RFB picture checks, not two-browser audio/H.264 tests.
- `test_hosts.py` (`tests/unit/web/`): `--hosts` with the manager stood in: the list asks only the
  named hosts (`list --host`) and shows only their VMs, one failing host left out and all failing
  a 502; every route of the route table naming a host (and each WebSocket's handshake) refused with
  403 for `local`, another configured host and an unknown one, with no `beanjar` command run, while
  the allowed host passes; an unknown, malformed, empty or repeated `--hosts` exit status 2 for
  `serve`, `start` and `enable` (no unit made); `local` alone asks nothing; and without `--hosts`
  one `list --json` and no route refused. `test_unit.py` checks that `start` and `enable` put
  `--hosts` in the unit's command line and that `status` prints it.
- `test_mitm.py` (`tests/unit/web/`): the inspector route against a stand-in mitmweb with 12.2.3's
  login and XSRF rules and a stand-in manager whose `mitm connect` is a real process: no inspector
  route on the main origin, whatever its session; the main origin refusing a same-site fetch, a
  framed page, a POST and a WebSocket from the inspector's origin, but not a page opened there;
  the inspector origin serving nothing else, taking no main session cookie, passing neither it nor
  its grant to mitmweb; the grant from `inspector.json` exchanged for its cookie (`HttpOnly`,
  `SameSite=Strict`, the VM's path), another VM's grant and a missing one refused; the main
  token and `--hosts` before anything runs; a VM without mitm and a stopped one 409 with the manager's
  message, an inspector that does not answer 502 after a second `mitm connect`, failures not
  kept; the page, the API with its query as sent, the script's two `/flows/dump` made relative and
  another script untouched; the password checked once, the Bearer on every request, no
  `beanjar_web` cookie or `Origin` upstream, no `mitmproxy-auth` cookie to the browser; `/updates`
  both ways and its XSRF cookie under the prefix, a PUT with it accepted and without it refused
  by mitmweb, a cross-origin PUT and WebSocket refused here; a VM restart (new password, cookies
  void) answered after one new `mitm connect`; `/mitm` redirected; `inspector.json`; one `mitm
  connect` kept while a WebSocket is open, ended when idle and at shutdown, no process left.
  `test_hosts.py` walks the new routes (`*` methods as GET and PUT) and the `/mitm/updates`
  handshake. `test_remote.py`'s `MitmConnectTest`: `mitm connect HOST/NAME` asks the host once,
  forwards `127.0.0.1:PORT` to the VM's address (`local_url` with link lan) on the shared
  connection while it holds, prints the URL and password, cancels at the end and on SIGTERM;
  refusals (no mitm, not running, not ready, a bad or older answer) add no forward; a killed run's
  forward is cancelled by the next. `MitmShowTest`: `mitm show`'s fields and the password only
  with `--password`; a local `mitm connect` at the VM's own address.
- `check-mitm` (`tests/integration/web/check-mitm.py`): the same route against the real mitmweb
  (the user's own, as `mitmtool.py` finds it; skipped without one of 12.0.1 or newer) run with `mitm_addon.py`'s password as
  a regular proxy on loopback: the page and its assets, File > Save rewritten twice, `/options`
  read and set with the XSRF cookie from `/updates`, a request through the proxy arriving over
  `/updates` and in `/flows`; and headless Chromium (skipped without it) showing that flow in the
  page's list, every request and the WebSocket under the prefix, no script error.
- `test_installed.py`'s `test_web_package`: the package's dependencies and tree.

Measured 2026-10-28 on the x86 PC with fake-qemu `--animate`, beanjar-vnc and neatvnc 0.9.1:
both clients received 60–62 pixel updates in each two-second window, with a maximum observed
gap of 0.053 s, across both clients' connects, disconnects and reconnects. Requesting a 320×180
desktop returned ExtendedDesktopSize reason 1/status 1 (administratively prohibited); both
clients and their reconnect handshakes stayed at the configured 640×360. The web viewer sets
`scaleViewport=true` and `resizeSession=false`: resizing a browser window scales its picture
locally. The protocol resize probe verifies that even a client explicitly requesting another
desktop size does not resize the other stream in the current server.

Measured 2026-10-24 on the x86 PC (WSL, TCG), WT1H afro (`default`), with headless Chromium driven
over the DevTools protocol: the viewer page connected through `vnc pipe` and showed Live TV's
"No Signal" screen at 1280x720, scaled into the page; the popup opened with the normal remote. A
press of the popup's HOME brought up the launcher bar (Premium, My Programmes, LiveTV, Web Browser,
...), and RIGHT in Live TV opened its side panel ("Programmes"), each seen in `beanjar screenshot`.

Measured 2026-10-25 on the x86 PC (WSL, TCG), WT1H afro (`default`), the guest playing
`speaker-test -t sine -f 1000` (through `beanjar ares-shell`): 12 s from `beanjar vnc audio-pipe`
as PCM and as Opus, decoded to WAV, each had a 1000.0 Hz peak at -42.3 dBFS RMS (-39 dBFS peak;
the guest's master volume was low); the longest silence inside was speaker-test's channel switch.
Headless Chromium 156 (`--disable-audio-output`, a fake sink that pulls in real time) on
`http://127.0.0.1`: a click on the speaker turned it to "Sound on: Opus (WebCodecs)"; in 12 s 596
packets were decoded to 572160 frames and the worklet played 568448 with no underrun and no skip.
With `AudioDecoder` removed before the page loads (as on a LAN address) it took PCM: 284640 frames
in 6 s, no underrun.
