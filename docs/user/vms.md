# Creating and running VMs

Each VM is one TV: its own firmware, settings, state disks and identity, in the library. Make
and run them from the manager window or the CLI; both work on the same library, and the window
follows what the CLI changes. `beanjar --help` and `beanjar COMMAND --help` describe every
command, and the [manager guide](../../host/core/README.md) has the details.

## The manager window

`beanjar-ui` (from a checkout, `python3 -m beanjar_ui.manager`) lists the VMs with their state.
**New VM** makes one from a `.epk` or a directory of extracted paks, with pages for its hardware,
display and audio, network and layers; it is the **+** button. The header bar has one icon button
for the selected VM's next step, its tooltip naming it: **Start** (▶) while it is off (its
dropdown: Start and Connect, Start with Reason…), **Connect** (the display icon) while it runs
(Shut Down… and Unplug… in the dropdown), and **Shut Down…** (the power icon) once the viewer is
open (Disconnect and Unplug…). Beside it is the VM menu (⋮): **Settings…** (only while the VM is
off), Rename, Clone, Wipe, New Identity and Delete. The main menu (☰) has the viewer's reconnect
option and About, and the title's tooltip shows the library's folder. Right-clicking a VM in the
list gives every action at once. Closing the window or a viewer never stops a VM.

The Summary, the first page, starts with four tiles: the VM's state (with QEMU's detail, such as
paused or standby, and "Viewer open"), then, while it runs, its uptime, CPU use and QEMU's memory
on the host (with the GPU's use where the host reports it), the figures its list row shows each
second; while it is off, when it last ran (the last write to its console log), and its CPUs and
memory as set. Beside them is a picture of the TV (`beanjar screenshot NAME - --frame 384x216`),
taken at most every 5 seconds while the VM runs, the Summary is shown, the window is active and no
viewer is open; a portrait model's picture is turned as its viewer turns it. Off, it is the last
picture kept, dimmed, or a TV outline before there is one. Clicking it connects the viewer (or
brings an open one to the front) while the VM runs and starts it while it is off. Under them come
the TV (webOS version, board and firmware version, the firmware's source, "not tested yet" for a
target that has never booted), why it runs, and its layers and tags. A narrower window puts the
tiles in a 2x2 grid; a narrow one puts the picture under them.

A banner at the top names one problem at a time, with one button where there is something to do:
a vm.toml that does not read (Open VM Folder), a QEMU that is paused or has shut the guest down, a
start or stop that failed (Show Output; it stays until the next one succeeds), firmware that is
missing (Settings…), and why the VM cannot start now (the host's memory or `max_running`; the full
reason in its tooltip).

The Hardware, Identity and Network cards follow, each with an **Edit** that opens Settings at its
page (only while the VM is off). Audio and free page reporting appear only when they differ from
the defaults (sound as the display has it, free page reporting off). Network has the VM's host
address (or LAN address) with its forwarded ports and a copy button, the connection mode, where the
service ports listen, and the traffic inspector's retention and body size limit. The Traffic
Inspector card has its status, its URL, Copy URL and Open, and folded under them its password, LAN
details, which mitmproxy it runs (or why it cannot, and how to install it) and the proxy's last lines; while the VM is off it is one line,
"Starts with the VM (MITM)", or, without a usable mitmproxy, "Off: mitmproxy not installed" (or too
old) with the reason and the install command under it. A VNC card of the same shape appears while `beanjar vnc` serves the
VM. "Files and runtime details" opens the VM's folder and its logs, and shows the firmware's
location, the systemd unit, the QEMU and viewer process IDs, and how often the guest restarted.

Keyboard: Ctrl+Return starts, Shift+Ctrl+Return connects, Shift+Ctrl+Delete shuts down,
Alt+Return opens Settings, F2 renames, Delete deletes and Ctrl+N makes a new VM.

## From the CLI

```sh
beanjar create tv1 --firmware /path/to/firmware.epk
beanjar start tv1
beanjar viewer open tv1 --remote
beanjar list --local
beanjar stop tv1
```

- `create` defaults to the firmware model's RAM, 4 CPUs, the `normal` and `gl` layers, the NAT
  network through the traffic inspector, and the D-Bus display with sound. `--mem`, `--smp`,
  `--layers`, `--link`, `--mode`, `--display` and `--audio` change them.
- A firmware whose target has never booted (`tested = false` in its file in
  [`guest/targets/`](../../guest/targets/)) is accepted with a warning, "not tested yet: may not
  boot", when the VM is created and each time it starts; New VM marks it the same way.
- `start` returns at the guest's initramfs shell, and the TV boots on to Home in the background.
  Several VMs run at once as far as the host's memory allows (`beanjar host config set
  max_running=N` caps them).
- `list --local` shows this machine's VMs and their state; `show NAME` one VM's settings, address,
  ports and SSH command.
- `status show NAME` says, while the VM runs, what the TV sends out as audio: the sound output
  and digital mode chosen in its settings, the outputs the emulated hardware has open, the mode
  (PCM, auto, passthrough) and the codec and channel count each output sends, and the streams the
  TV's player plays ([manager README](../../host/core/README.md#what-the-tv-sends-out-as-audio)).
- `stop` saves the guest's logs and sends the TV's own power-off; a guest that cannot take it is
  synced and stopped instead. `stop --unplug` cuts the power at once and may lose data.
- `wipe NAME` blanks the state disks (a factory-fresh TV, first-use setup included unless the
  `normal` layer skips it); `delete NAME` removes the VM.
- `identity reset NAME` makes the VM another TV (new MAC, serial and keys, state wiped);
  `clone SRC DEST` copies a VM as another TV with its state kept.

Every command takes `--json` for scripts.

## Settings

`beanjar config show NAME` shows a VM's settings and tags, and `config set NAME KEY=VALUE` or
`config unset NAME KEY` changes them, settings while it is off (`beanjar config set --help` lists
every key):

```sh
beanjar config set tv1 mem=1536 layers=normal,gl,devmode
beanjar config unset tv1 broadcast
beanjar config set tv1 country=CAN    # a country of the board's group (board-profiles.md)
```

The layers add to the stock firmware; the first listed wins where two have the same file:

| Layer | What it does |
|---|---|
| `normal` | Skips the first-use wizard |
| `gl` | Graphics through Mesa (virgl) under the stock compositor |
| `headless` | No graphics; instead of `gl` |
| `devmode` | LG Developer Mode and its SSH, for the webOS SDK tools ([apps](apps.md)) |
| `rootssh` | Root SSH with the VM's keys on port 6622 |
| `hbchannel` | Homebrew Channel, as on a rooted TV; needs `devmode`, instead of `rootssh` |

With `gl`, an app gets no higher an OpenGL ES version than the TV model's own GPU offers (ES 2.0
on the M2, M3, M14 and LM14 sets, 3.0 on H13, 3.2 on the later chips), so asking for more fails at
context creation as on the set. The host can still hold it lower: the guest never gets more than
its driver gives on that host.

A global firmware boots as a US model where one exists; to boot it as another region's model, see
[Boot a global firmware as another region's model](board-profiles.md).

`userland` is how the firmware's 32-bit ARM programs run. `auto`, the default, decides at each
start and the start says which and why: natively under TCG and where KVM's CPU has AArch32 at EL0
(a Raspberry Pi 5), under qemu-arm inside the guest where it has not. `native` and `qemu-user`
force one. NetCast and SimpleSmart firmware only run natively, and do not start on a host that
cannot.

Without 32-bit Arm KVM, `qemu-user` is what keeps a VM fast: the guest kernel still runs under
KVM, and only the TV's own programs are translated, not the whole machine as under full TCG.

Each program under qemu-arm carries qemu-arm's own memory as well, so a start that runs the
userland under qemu-user, for whatever reason, gives the guest half as much RAM again as `mem`,
and a swap disk in the host's RAM as large again: `mem` stays the TV's own memory, the start logs
`memory: 2048 MB for the TV + 1024 MB RAM and 1024 MB host-RAM swap for qemu-user's overhead`,
`beanjar list` shows MEM as `2048+1024`, `beanjar show` shows `tv_ram`, `guest_ram` and
`host_swap`, and the host must have the larger RAM free to start it. The swap is not taken up
front: it costs the host memory only once the guest swaps to it, and it goes away with the VM.
webOS is still told the TV's own memory (the model's board parameters); only the guest kernel sees
the extra RAM and the swap.

`guest_kernel=x86-64` is **experimental**: on an x86_64 machine it boots an x86-64 guest kernel
under KVM (TCG without `/dev/kvm`) and runs the whole ARM userland under qemu-arm, which is much
faster than full TCG. It implies `userland=qemu-user`, is refused with `userland=native` and for
NetCast and SimpleSmart, and only webOS 1 (WT1H) has been booted with it so far. With the Debian
packages, install `beanjar-guest-x86-64` (amd64 only, never installed automatically, on the same
release as `beanjar-guest`: [Getting started](getting-started.md#the-x86-64-guest-kernel)) first:
it carries the x86-64 kernel and pulls in `beanjar-qemu`, which has its qemu-system-x86_64 and
qemu-arm. Without it the setting is refused, naming the package.

```sh
sudo apt install ./beanjar-guest-x86-64_*.deb
beanjar config set tv1 guest_kernel=x86-64
```

## Network

These settings decide a VM's network ([guest networking](../dev/architecture/network.md)):

- `link`: `nat` (the default, behind this host), `lan` (on the host's wired LAN with its own MAC
  and DHCP lease, after `beanjar library config set lan_parent=eth0`) or `off`.
- `mode`: `mitm` (the default), which records the guest's TCP and UDP traffic, DNS included, in a
  traffic inspector (your own mitmproxy 12.0.1 or newer, `pipx install mitmproxy`), or `direct`. See [Inspect emulator traffic](inspect-emulator-traffic.md).
- `lan_access`: `off` (the default) or `on`, with `link=nat`. The guest runs untrusted code, so by
  default it reaches the internet only: not the LAN (private, shared and link-local addresses,
  multicast) and not this host's own addresses. `config set NAME lan_access=on` lets it reach
  them, for a media server, another TV or a development server on the LAN; it applies at the next
  start. A `link=lan` VM is on the LAN anyway.
- `lg_services`: `mock` (the default) or `real`. With `mock` the guest does not reach LG's
  telemetry and cloud services: crash and analytics uploads, viewing beacons, ads, ACR (Live
  Plus), ThinQ and IoT, UEI QuickSet, firmware update checks, remote diagnostics and the voice
  cloud are refused, by name and by address, in `mitm` and `direct` alike; LG's online check and
  LG's SDP server are answered by Beanjar, so the TV is online and its LG account side works
  (signed in, clock set) with every logging and upload switch off; the store, Home content and
  LG Account hosts get empty answers; and the inspector never passes an LG host through unread.
  Nothing reaches LG, whatever model key the VM has. The guest trusts the library's CA for these
  answers in `direct` too. `config set NAME
  lg_services=real` lets the VM talk to all of LG's services as a TV does, for a VM with a real
  model key; nothing is refused or answered then. See [LG services](../dev/architecture/network.md#lg-services).

The guest never reaches this host's loopback (`127.0.0.1` and the rest of `127.0.0.0/8`), whatever
the settings: a service that listens only there (a development server, Docker's API, a local mail
server) is out of its reach. Every `nat` and `lan` VM needs `unshare`, `ip` and `slirp4netns` and
unprivileged user namespaces, `mode=direct` too (`sudo apt install slirp4netns`).

Each VM has its own loopback address, `127.0.1.N`, and its services are on the TV's own port
numbers there: SSAP 3000 and 3001, root SSH 6622, Developer Mode 9922 and its key server 9991, the
traffic inspector 18083 and VNC 5900. `beanjar show NAME` prints them. To reach them from another
machine, `config set NAME listen=0.0.0.0` (with `link=nat`), or use `link=lan`.
`config set NAME forward=8080` relays an app's own guest port as well.

## Identity

Each VM has its own MAC, serial and keys, made when it is created. Without a model key of your own
it has a synthetic one, which LG's servers do not accept. If you have the model key of a matching
TV, install it while the VM is off:

```sh
beanjar key model set tv1 /path/to/model-key
```

Secure-store items beyond the model key go in the VM's `identity/sestore.toml`; see
[Configure secure-store items](sestore.md) and [per-VM identity](../dev/architecture/identity.md).

## Other machines

A VM on another host is `HOST/NAME` in every command once the host is added with
`beanjar host add NAME ssh://[USER@]HOST`; `beanjar list` then asks every host. See
[Several machines](../../host/core/README.md#several-machines).
