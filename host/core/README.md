# Beanjar: the VM manager

Beanjar (application ID `org.webosbrew.beanjar`) keeps LG webOS, NetCast and SimpleSmart
TV VMs, each with its own firmware, in one directory, the library, and starts, stops,
wipes and views them; see the [manager architecture](../../docs/dev/architecture/manager.md).
Run it on the machine that runs QEMU: the Pi for KVM, the x86_64 workstation for TCG. The
CLI is the core package `beanjar` (this directory) and needs only Python 3.11 or newer. The GUI is
the package `beanjar_ui` (`host/ui`) over it, and also needs GTK 4 and libadwaita (1.7 or
newer) for Python (`python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`) and its layouts, which the
checkout's host build makes (`scripts/build.sh host`: `build/manager/beanjar.gresource`).

```sh
export PYTHONPATH=host/core:host/ui     # from the checkout: the core package and the windows'
python3 -m beanjar create tv1 --firmware 05.06.10.01-HE_DTV_WT1H_AFAAABAA  # from firmware_dirs, in place
python3 -m beanjar create tv2 --firmware /srv/beanjar/firmware/05.06.10.01-HE_DTV_WT1H_AFAAABAA
python3 -m beanjar create tv3 --firmware ./my-paks --import   # any directory of paks, made the VM's own
python3 -m beanjar create tv4 --firmware ./firmware.epk       # extracted; only its target's paks kept
python3 -m beanjar start tv1          # returns at the initramfs shell; the VM runs on
python3 -m beanjar start tv2 --reason "camera bisect" --by agent-3   # why it runs, until it stops
python3 -m beanjar viewer open tv1 --remote  # the viewer, options passed through
python3 -m beanjar list
python3 -m beanjar stop tv1           # logs saved, webOS powers the set off; --unplug: kill at once
python3 -m beanjar_ui.manager         # the window
```

`--firmware` is a firmware of the host's collections, used in place; with `--import`, any directory
of paks (`epk2extract` output, or another VM's `firmware/`), which become the VM's own; or a `.epk`
(a path ending in `.epk`, with or without a `/`), extracted into the VM's own `firmware/` (see
Firmware).

The supported targets are the files of [`guest/targets/`](../../guest/targets/) (see the [project README](../../README.md#supported-firmware)).
Build the guest artifacts for the imported firmware's target before starting it. webOS and
SimpleSmart use the webOS SDK; NetCast 4.5 requires the lgtv SDK and its initial cross-file
setup described in [Building](../../docs/dev/guides/build.md#commands). Legacy UI checks are separate
from the webOS regression suite.

Several VMs run at once. Each has a slot N (`vm.toml`'s `network.slot`, assigned when it is made
and kept while it exists), and all its host ports are on its own address, `127.0.1.N`, at the TV's
stock numbers: SSAP 3000 and 3001 (webOS), root SSH 6622, developer mode 9922 and its key server
9991, the mitm proxy's inspector 18083 (docs/dev/architecture/network.md, "Host addresses and ports"). A start is refused only
when the host's free memory (`MemAvailable`, less what other starting and running VMs may still
claim) does not cover the VM's RAM and the host's memory headroom (1 GiB by default; under TCG
also QEMU's 1 GiB translation-block cache); under KVM each VM's unit is held to the RAM and
headroom with `MemoryHigh`, under TCG it has
none (docs/dev/architecture/manager.md, "Several VMs at once"). `beanjar host config set max_running=N`
also caps how many VMs may run at once on this host, every library's, the development VMs' and the
regress harness's together; a start beyond it is refused, naming the VMs that run.

To reach a VM from another PC (ares, a second-screen app), `beanjar config set NAME listen=0.0.0.0` (or one
of the host's addresses) makes its TV-service ports, SSAP 3000 and 3001, root SSH 6622 and developer
mode 9922 and 9991, listen there instead of `127.0.1.N`; `beanjar config unset NAME listen` returns them to the VM's
own address. No password is added: the SSH servers take keys only, the key server's key is encrypted
with the guest's passphrase, and SSAP pairs a client only when it is accepted on the TV's screen,
as on a TV. The mitm inspector and VNC keep their own settings and password rules
(`NETSTUB_WEB_HOST`, `vnc start --listen`). The ports are the TV's, so two VMs cannot listen on
overlapping addresses (`0.0.0.0` overlaps every address): a start that would collide fails, naming
the other VM or the port's holder.

An app's own ports are reached the same way: `beanjar config set NAME forward=8080,3000` relays guest
TCP ports 8080 and 3000 on the VM's address as its TV-service ports are (on `listen` with `link=nat`,
on its LAN address too with `link=lan`), and `8081:80` puts guest port 80 on host port 8081. A port that
a layer forwards (3000 and 3001 on webOS, 6622, 9922, 9991), 5900 (VNC), 18083 (the inspector) or one
below 1024 is refused when set; it applies at the next start, and `unset forward` removes them.

The network is two settings (docs/dev/architecture/network.md): `link`, where the VM's NIC is attached
(`nat`, behind this host, the default; `lan`, on the host's wired LAN as a TV of its own; `off`, no
network), and `mode`, whether its TCP and UDP go through the recording proxy (`mitm`, the
default: mitmweb in tun mode, the user's own mitmproxy 12.0.1 or newer, `pipx install mitmproxy`;
[inspect emulator traffic](../../docs/user/inspect-emulator-traffic.md#install-mitmproxy)) or not (`direct`); `beanjar config set NAME link=nat mode=direct`. `link = "lan"` with `mode =
"mitm"` puts the VM on the library's `lan_parent` (`beanjar library config set lan_parent=eth0`) with its own
MAC and a DHCP lease (or the static `address`), its TV-service ports on that LAN address; the
package installs the one privileged helper it needs (docs/dev/architecture/network.md, "`lan` with
`mitm`"). `lan` with `direct` joins the guest's own NIC to a macvlan on `lan_parent` with the same
helper and nothing in between: its own DHCP lease, every port at that address, no inspector or
relays, unreachable from this host, and `lg_services = "real"` required (the same document, "`lan`
with `direct`"). The other combinations run in a
rootless network namespace with slirp4netns as its uplink, and the guest never reaches this host's
loopback; with `link = "nat"` it reaches the LAN and this host's own addresses only with
`lan_access = "on"` (`beanjar config set NAME lan_access=on`; docs/dev/architecture/network.md,
"What the guest reaches"). `lg_services = "mock"`, the default, refuses LG's telemetry and cloud
hosts and answers LG's online check in the namespace (`host/helpers/network/lg_hosts.py`,
`lg_mock.py`); `"real"` reaches LG as a TV does (docs/dev/architecture/network.md, "LG
services").

## This machine's settings

`$XDG_CONFIG_HOME/beanjar/config.toml` (`~/.config/beanjar/config.toml` by default), shown and set
with `beanjar host config show`, holds what is one for the whole host, every library's alike:
`max_running`, how many VMs may run at once (a whole number from 1; unset, the default, is no
limit), `memory_headroom_mib`, the positive whole number of MiB allowed beyond each VM's RAM
(unset: 1024), and `firmware_dirs`, the host's firmware collections (a list of absolute directories;
`host config set firmware_dirs=DIR[:DIR...]`; unset, `["/srv/beanjar/firmware"]`), where
`create --firmware NAME`, the launcher's `FW` name and `scripts/regress` look a firmware up, the
first collection first. `.running.lock` beside it is held while a start checks the limit and
starts its unit.

`beanjar host config set memory_headroom_mib=896` tunes admission on this machine and the
soft `MemoryHigh` limit of newly started KVM units. Tune it with VMs Off. It does not change guest RAM. Existing
units retain their limit; their actual `MemoryHigh` remains the reservation when another VM
is admitted. Lower values permit starts with less host memory available, so validate the
host's intended workload before lowering it. `beanjar host config unset memory_headroom_mib`
restores 1024. For a remote host, run the command there over SSH; `host config` always acts
on the machine where Beanjar is running.

## Library

`~/.local/share/beanjar` by default (`$XDG_DATA_HOME`), or `--library DIR`, or
`BEANJAR_LIBRARY`.

```
config.toml            repo (the checkout: only `library config set repo=DIR` stores it; `--repo DIR` is for one command and writes nothing), auto_reconnect (GUI option), epk2extract,
                       mac_prefix (the new VMs' MAC prefix, below), lan_parent (the host's wired interface for
                       link = "lan", e.g. "eth0"; named, never guessed; a wireless one is refused),
                       addresses (the first three octets of its VMs' addresses, default "127.0.1")
.slots.lock            held while a new VM takes its slot
ca/                    the mitm proxy's CA, shared by the library's VMs (mode 0700; below); ca.lock beside it
vms/<name>/start.lock  {"pid": the starting process}, while the VM is started or runs
vms/<name>/vm.toml     the VM's settings (below)
vms/<name>/firmware/   the VM's firmware: rootfs, tvservice-dvb, otncabi-dvb, otycabi-dvb, fonts, smartkey .pak;
                       manifest.toml (where they came from); cache/ with its partition map (partinfo-*.img)
vms/<name>/firmware-cache/  only with fw an external directory: its partition map
vms/<name>/state/      the state disks (db8 cmn_data appstore nvram eeprom .img)
vms/<name>/identity/   the VM's own files, mode 0700: its SSH keys and known_hosts, sestore.toml
                       (including its SDP model key; below), vnc.password (beanjar vnc's; vnc password),
                       lgudid.toml (its LGUDID; below)
vms/<name>/run/        qemu.pid qmp.sock viewer-qmp.sock console.sock dbus.sock cursor.sock video.sock ir.sock
                       (the micom's IR receiver, beanjar/devices/ir_relay.py: ir-relay.log; QEMU's port is
                       ir-qemu.sock) shell.sock (beanjar shell's relay, beanjar/access/guestshell.py:
                       shell-relay.log; QEMU's port is shell-qemu.sock) capture.sock (the guest's
                       captures, beanjar/display/capture_relay.py: capture-relay.log; QEMU's port is
                       capture-qemu.sock; its on-demand headless viewer capture-sink.pid,
                       capture-sink.log) media-<n>.sock dbus-daemon.pid viewer.pid unit; share/ (what
                       the guest sees, below)
vms/<name>/logs/       console.log webos.log qemu.out netstub.log viewer.log; saved/<time>-stop/
```

VM files use grouped TOML. Reads use Python's `tomllib`; edits require
`python3-tomlkit` and preserve comments, ordering and formatting.

```toml
[firmware]
directory = "firmware"
# line = "atsc"       # a global firmware as another region's model (docs/user/board-profiles.md)
# debug_mode = "release"   # "debug" (the default) | "event" | "release": the TV's debug status

[hardware]
cpus = 4
# memory_mib = 1536
# free_page_reporting = "on"   # "on" | "off" (the default)
# userland = "qemu-user"       # "auto" (the default) | "native" | "qemu-user"
# guest_kernel = "x86-64"      # "arm64" (the default) | "x86-64" (experimental)
# hwdec = "none"               # "auto" (the default) | "none" (software only, locked) | a device type

[display]
backend = "dbus"

[audio]
backend = "dbus"

[network]
link = "nat"          # nat | lan | off
mode = "mitm"         # mitm | direct; ignored with link = "off"
# listen = "0.0.0.0"  # link = "nat" only: its TV-service ports there instead (the LAN too); unset: 127.0.1.1
# forward = "8080,8081:80"  # the guest's own TCP ports on its address too (8081:80: host 8081, guest 80)
# address = "dhcp"    # link = "lan" only: "dhcp" (the default) or "A.B.C.D/NN,GATEWAY"
# mitm_flows = 5000   # mode = "mitm": flows the inspector keeps, the oldest finished ones dropped; 0: all
# mitm_flow_bytes = "200M"  # mode = "mitm": and the bytes of bodies they may hold; unset: no limit
slot = 1              # assigned when the VM is made: its ports are on 127.0.1.1

[layers]
enabled = ["normal", "gl"]

[identity]
# Generated when creating the VM.
# Conversion preserves the existing MAC and serial.

[advanced]
env = {}

[metadata]
# Creation time is maintained by the manager.
# tags = ["scratch"]  # config set NAME tags= / create --tag: labels for list --tag (Tags, below)
```

Only `firmware.directory` is required. Paths are relative to the VM directory;
`firmware` selects its own firmware. `firmware.broadcast` (`dvb`, `atsc` or `arib`) boots a
global firmware, whose paks name no broadcast standard, as that standard's model: the chip's board
for it (`targets.Soc.broadcast_boards`). Without it such a firmware boots the chip's default board,
a US model where the firmware ships on US models (`dvb` then boots the European one;
docs/user/board-profiles.md). A standard the chip has no board for, or another than the
paks', is refused. `firmware.model` (a retail model name, `42LX3QPUA`) boots the firmware as another
retail model of its chip, one whose hardware differs from the default board's: the chip's board of
that model (`targets.Soc.model_boards`; W22O's OLED Flex), whose broadcast standard it then is. A
model the chip has no board of is refused, naming those it has, and so is one of another standard
than `firmware.broadcast` or the firmware's. `firmware.country` (an ISO 3166 alpha-3 code, `CAN`) is the TV's country, one of
the board's own RMM country group in the firmware's configd tables (`countries.py`; webOS 5 on): a
new EEPROM's lowlevelstorage factory `countryName`, and the guest's `emulator-country` sets it
through LG's settings on a state that has another (docs/user/board-profiles.md, "Choose the TV's
country"). Omitted settings default to four CPUs, DBus
display, `link = "nat"` with `mode = "mitm"`, and the `normal` and `gl` layers. Audio defaults to DBus
with a DBus display and `none` otherwise. Omitted `hardware.memory_mib` follows the
firmware model's RAM at each launch. Every VM has a virtio-balloon for the
guest's memory statistics (the viewer's memory meter); it is never inflated, so the guest keeps all
of its memory. `hardware.free_page_reporting = "on"` has it also report the guest's free memory
back to this host; that is off by default. `hardware.userland` is how the firmware's 32-bit ARM
programs run: `"native"` in AArch32 EL0, or `"qemu-user"`, under qemu-arm in the guest with their
system calls through the guest kernel's compat table (docs/dev/architecture/qemu-user-bridge.md).
`"auto"`, the default, picks one at each start and says which and why: native under TCG and where
KVM's CPU has AArch32 at EL0 (a Raspberry Pi 5), qemu-user where it has not (an arm64 CPU without
it, Apple silicon) and with the x86-64 guest kernel. NetCast and SimpleSmart always run native: an
explicit `"qemu-user"` is refused for them, and where the host cannot run them natively they do not
start. `hardware.guest_kernel = "x86-64"` is experimental: an x86-64 guest kernel on QEMU's q35
machine (KVM where `/dev/kvm` opens, else TCG) with the whole ARM userland under qemu-arm, on x86-64
hosts only; it implies `userland = "qemu-user"` (`"native"` is refused with it), is refused on
NetCast and SimpleSmart, and a start says that it is experimental. `network.mitm_flows` (default 5000, 0 for no
limit) is how many flows the mode=mitm inspector keeps in memory; beyond it the oldest finished
ones are dropped. `network.mitm_flow_bytes` (bytes, or K, M, G such as `"200M"`; unset, the
default, for no limit) also bounds the bytes of bodies those flows hold. The platform and its version are derived from
the selected firmware. Unknown keys and invalid types or values are errors, and so are
`network.listen` with `link = "lan"` and `network.address` without it.
`network.slot` (1 to 254) is the manager's, not a setting: `config set NAME` does not take it, and a VM
without one gets the lowest free one of its library once, when the library is opened or the VM
starts.

`advanced.env` holds additional launcher environment values, for example
`env = { PMLOG_DEBUG = "1" }`. Values may be strings, numbers or arrays of strings
(joined with commas). Settings and runtime paths owned by the launcher cannot be
overridden here; checkout-only experiments such as `ROOTFS` are also rejected.
`metadata.created` is the creation timestamp string. Existing CLI setting names
(`mem`, `smp`, `fw`, etc.) remain shortcuts to these grouped fields.

Only the grouped schema is read: a flat file, or one with the pre-`link` `network.mode = "off"`, is
rejected.
Library
`config.toml` and firmware `manifest.toml` retain their existing formats.

`identity.mac` and `identity.serial` are the TV's identity (docs/dev/architecture/identity.md): the board MAC, which is also
the NIC's and from which LG's code derives the NDUID, and the serial number that tvservice reports.
`create` (and the GUI's New VM) makes both: the library's `mac_prefix` (`config.toml`; default
LG's `a8:23:fe`, or a locally administered prefix such as `02:42:be`, with `beanjar library config
set mac_prefix=...`) followed by three random octets, and a random serial of the shape `501RAXX00001`,
neither one another VM of the library has. A `vm.toml` without them gets them at its next start,
written back, with a warning: the VM then has another NDUID than its state was made with. They
reach the guest as `webos.ethaddr=` and `webos.serial=` on the kernel command line, which
`guest/initramfs/rootfs/init` puts in place of `board.cmdline`'s `ethaddr=` and `serialNum=`; the EEPROM's
serial follows at the next boot. Development launches without `--dir` use a managed VM with persisted identity.
Migration preserves the legacy board MAC and serial; new development VMs get
the same generated identity treatment as other managed VMs.

A VM's files are in `identity/`, not in `vm.toml`: plain files, private keys 0600 and the rest
0644 (docs/dev/architecture/identity.md; `beanjar/keys.py`).

- **SSH keys**, made with `ssh-keygen` at `create` (and New VM); one that is missing is made again
  at the next start, with a warning:
  - `id_ed25519` (+ `.pub`): the VM's client key. Its public half may always log in as root, so
    the manager and the tests can ssh in; the private half never leaves the host.
  - `authorized_keys`: more keys that may log in as root, made with none. Every key in it goes
    into the guest, which can read it, and your own keys identify you (a code host publishes its
    users' public keys, and a key's comment is often `user@host`), so yours go in only if you
    add them: by hand, or with `beanjar identity authorized-keys add NAME --mine` (your
    `~/.ssh/*.pub`, certificates left out). The guest gets every key without its comment. It is
    read at each start; `beanjar identity authorized-keys show NAME` shows what the guest gets and
    `... reset NAME` empties it again. A VM made before this default may still hold your keys: `show`
    marks them `mine`.
  - `rootssh_host_key`, `devmode_host_key` (+ `.pub`): the host keys of root SSH (ed25519) and
    developer mode (RSA), in Dropbear's format (`host/helpers/ssh/dropbear-hostkey.py`).
  - `devmode_rsa` (+ `.pub`): the developer key, unencrypted PEM. The guest encrypts it at boot with
    its own passphrase (the first six characters of its NDUID, what `getPassphrase` answers) and
    serves that on port 9991, as a TV does, for `ares-novacom --getkey`.
  - `known_hosts`: both host keys at their forwarded ports on the VM's address (`[127.0.1.N]:6622`,
    `[127.0.1.N]:9922`), written at create and each start, so no first connection has to be trusted
    blindly and no two VMs' pins collide.

  With `rootssh` (or `hbchannel`, which needs `devmode`) and `devmode` in `layers`, `beanjar show NAME` has the commands (`ssh`), with
  the key server's URL and, for developer mode, the form that runs the ares tools against the VM
  (see ares tools):

  ```
  ssh -p 6622 -i <vm>/identity/id_ed25519 -o UserKnownHostsFile=<vm>/identity/known_hosts -o StrictHostKeyChecking=yes root@127.0.1.N
  ssh -p 9922 -i <vm>/identity/devmode_rsa -o UserKnownHostsFile=<vm>/identity/known_hosts -o StrictHostKeyChecking=yes prisoner@127.0.1.N
  http://127.0.1.N:9991/webos_rsa
  beanjar ares-TOOL -d NAME [ARGS...]
  ```

  (6622 is `SSH_PORT`'s default; the ports are forwarded on the VM's address with `link = "nat"`,
  or on `listen`'s, which these commands and `known_hosts` then use when it is one
  address; with `0.0.0.0` they keep `127.0.1.N`, and another PC uses the host's LAN address.)
- **Secure-store items** ([format](../../docs/user/sestore.md)),
  `identity/sestore.toml`: exact names map to text, hex bytes or files. The root `missing` policy
  chooses existing fallback behavior or a missing-item error.
- **The SDP model key**, `identity/sestore.toml`: `beanjar key model
  set tv1 FILE` sets one (44 characters of base64 for 32 bytes, as `host/core/beanjar/modelkey.py`
  checks; the value is never printed), `beanjar key model unset tv1` removes it and `beanjar
  key model show tv1` says whether there is one; set and unset while the VM is off. The GUI's Settings has the
  same on its Network page (Model Key: install from a file, clear; done at Save, a bad file
  refused before anything is saved). With the default missing policy a VM without one has
  libhal_crypto's built-in model secret; `missing = "error"` disables that fallback.
- **The LGUDID**, `identity/lgudid.toml`: LG's unique device ID (`sm` `deviceid/getIDs`, nyx
  `DeviceInfo`), with the random UUID it was made from, as a set's factory makes them (a UUIDv5 of
  that UUID and the MAC; `beanjar/board/macadr.py`). Made at `create`, or at the next start (prepare)
  of a VM without one, with a warning. Where the firmware reads it (goldilocks2 on:
  `Release.lgudid`), prepare puts it with the MAC into the macadr partition's image,
  `prepared/macadr.img`, which the launcher attaches as `/dev/mmcblk0p10` with its writes discarded
  ([the LGUDID](../../docs/research/lgudid.md)). `wipe`, `snapshot reset` and a change of MAC keep
  it; `identity reset` and `clone` make a new one.

The mitm proxy's CA is the library's, `ca/` (`host/helpers/network/tlsca.py`: `ca.key` 0600, `ca.pem`,
valid from 2000 so a guest clock in the past accepts it), made with the first VM and shared by all:
one CA to trust in a browser or wherever the proxy's leaves are checked. One that is missing is
made again at the next create or start, with a warning. It is mitmweb's confdir. With `mode =
"mitm"` its certificate (never its key) goes into each VM's layer, and the guest appends it to the
firmware's own trust stores at boot. The proxy does not verify the servers it talks to
(`ssl_insecure`; it is a debug tool), so LG's SDP servers, from LG's private root, are decrypted
like any other host. A server it cannot reach or complete TLS with fails once and is then passed
through undecrypted.

At each start the launcher assembles `run/share/vm/` afresh from `identity/` with what the guest
needs of it: `etc/emulator/sestore.bin`, provisioned into the kernel store at boot; with `rootssh`, the host key and
root's `authorized_keys` (with `id_ed25519.pub`) under `etc/emulator/rootssh/` and
`home/root/.ssh/`; with `hbchannel`, the same under `etc/emulator/hbchannel/` and `home/root/.ssh/`; with `devmode`, the host key and the plain developer key under
`etc/emulator/devmode/`; with `mode = "mitm"`, the proxy CA's certificate and its subject hash as
`etc/emulator/netstub-ca.{pem,hash}`. It gives it to the guest as a read-only 9p share (tag `vm`),
which `guest/initramfs/rootfs/init` stacks on top of every other layer. Nothing else of `identity/` reaches the
guest, the client key never. The rootssh, hbchannel and devmode layers themselves hold no key and are the
same for every VM. Development VMs use the same directory layout, in a separate development library.

The guest sees only its runtime tree, `run/share/` (`beanjar/guestside/share.py`; the checkout VM's
`build/share/`), never the checkout, `identity/`, the CA or the firmware directory:

| Host | 9p tag | Guest | What |
|---|---|---|---|
| `run/share/vm/` | `vm`, read-only | `/run/vm` (initramfs only) | the per-VM layer above |
| `run/share/host/` | `host`, read-only | `/host`, `/mnt/host` in webOS | made afresh at each start: `boot.conf`; `layer-sources`, where each layer's image was made from (the layers themselves are read-only disks, `layerimage.py`: the selected variants under `build/components`, or `/usr/share/beanjar/guest/layers` in packages, for the firmware's target); `binds.conf`; `guest/scripts/{save-logs,check-m1,time-boot}.sh`; whatever `host/scripts/vm-launcher put [--dir VM] PATH` adds while it runs |
| `run/share/out/` | `out`, read-write | `/host/out`, `/mnt/host/out` | what the guest writes: `logs/<name>/` (saved to `logs/saved/` at stop and start, or by `host/scripts/vm-launcher logs`: its regular files and directories only, never following the guest's links, the rest listed in the saved directory's `skipped.txt`; then removed), tests' results, and what `host/scripts/vm-launcher put-out [--dir VM] SRC DEST` copies in (never through the guest's links). The host reads the guest's files there only with `host/scripts/vm-launcher get-out [--dir VM] SRC [DEST]` (a regular file only, never through a link, never from a FIFO) and removes them with `host/scripts/vm-launcher rm-out [--dir VM] PATH...` (never through a linked directory); kept across starts |

The host share's files are hard links to the checkout's where the library is on the same
filesystem, copies otherwise.

`firmware/manifest.toml` records the firmware target import resolved and where the VM's paks came
from: name, chip (the SoC, `h13`), release (the webOS release family, the codename of `etc/starfish-release`: `afro`), epk, epk_size, sha256 (of a
`.epk`), source (the directory they were linked or copied from), paks, imported. The launcher takes
the VM's target from it.

A VM is started by `host/scripts/vm-launcher start --dir <vm-dir>`, which you can also run by hand: TCG on
x86_64, KVM on the Pi, with every runtime path in the VM directory. It boots what is built and
does not run the build. The VM runs as the transient systemd user service
`beanjar-vm-<tag>-<name>` (`run/unit`), or, without a user systemd, in a session of its own. `<tag>`
is its library's: the library directory's name and six hex digits of its path
(`vmunits.library_tag`, e.g. `beanjar_1a2b3c`), so VMs of one name in two libraries never collide.
Every VM unit runs in `beanjar.slice`, one child slice per library, `beanjar-<tag>.slice`; the
regress harness's VMs are `beanjar-vm-regress-<target>` in `beanjar-regress.slice`.
`systemctl --user status beanjar.slice` lists the processes of all of them, and
`python3 -m beanjar list --all` lists them with the library's own VMs: other libraries' (the
development VMs) and the regress targets, read from the units in `beanjar.slice`, whose `LIBRARY`
column is the tag. `host/scripts/vm-launcher console --dir <vm-dir> 'command'` types on its
serial console; `host/scripts/vm-launcher qmp --dir <vm-dir> '<json>'` sends one QMP command to its
`run/qmp.sock`.

## Host runtime

The launcher is part of this package (stdlib only), with `host/scripts/vm-launcher` as its command line
(`host/scripts/vm-launcher --help`; each module's docstring has the details):

| Module | What it is |
|---|---|
| `launch.py` | `host/scripts/vm-launcher`: `run` (boot in the foreground, serial console on stdio), `argv` (print the QEMU command of `run`, a dry run), `start`/`stop` (the checkout's VM in the background, or a VM directory with `--dir`), `status --dir`, `console`, `qmp`, `reset-state`, `firmware-cache`, `config FW\|FW_NAME\|TARGET\|PAKS\|TARGETS\|RENDERNODE` |
| `qemu.py` | the QEMU command line from the launcher's environment (`FW MEM SMP LAYERS NET_LINK NET_MODE ROOTFS STATE DISPLAY_BACKEND AUDIO`, ...): x86_64 TCG or aarch64 KVM (`ACCEL`), the paks, the state disks, the network, the port forwards, the display and audio |
| `unit.py` | a VM's processes as one transient systemd user service (`python3 -m beanjar.unit run`), or a setsid session without one; its private D-Bus for `display = "dbus"`; the guest sync at stop |
| `netns.py` | `link = "nat"` and `"lan"`, both modes (a rootless namespace: `unshare -rn`, slirp4netns, ip, mitmweb for `mitm`, beanjar-relay (`host/helpers/network/relay.c`) for the port bridges, and the policy rules that keep the guest off the host's loopback and, without `lan_access`, the LAN) and `link = "off"` (the NIC's link set down over QMP) |
| `console.py` | the serial console bridge |
| `dbus.py` | the VM's private D-Bus through Gio, the core's only use of gi: QEMU's display size, the front end's `Screenshot`, `SetUIInfo` of the secondary display, and the connection the remote, pointer and touch pipes call through |
| `screensaver.py` | native screensaver state and real-window verification over the console |
| `cache.py` | the checkout VM's `ROOTFS=uncompressed` images (`host/scripts/vm-launcher firmware-cache`); the paks' digests that the partition map's cache is keyed by |
| `keys.py` | a VM's SSH keys in its identity directory: made at create, again at a start that misses one, all anew for `identity reset` (`renew`); known_hosts; the VNC password (`ensure_vnc_password`, `set_vnc_password`); root's authorized_keys for the guest; the proxy's CA (`ensure_ca`) |
| `paths.py` | where everything is: the checkout, its `build/`, the default firmware, the host's firmware collections (`firmware_dirs`). The one place to change for an install prefix |


## Firmware

A VM boots either a firmware used in place or one of its own.

- **In place** (`create --firmware NAME|PATH`, the GUI's New VM picking one of the collections, or
  Directory… without "Import into the VM's Folder"): `vm.toml`'s `firmware.directory` names the
  directory, absolute. It is never written, so it may be a shared, read-only collection (an NFS
  export, the same path on every host); what the launcher derives from it (the partition map, the
  paks' `.src` digest stamps: `beanjar/board/partmap.py`, `beanjar/firmware/cache.py`) goes into the VM's
  `firmware-cache/`. A value with a `/` is a path, used as given; a bare name is looked up in the
  host's `firmware_dirs` (This machine's settings). The firmware needs a `manifest.toml` with its
  `name`, `chip` and `release` (other keys, such as `paks`, are ignored), and the target's paks:
  `create` refuses it otherwise, so its target is never guessed from the rootfs. A `super.pak`
  firmware cannot be used in place; import it. Deleting the VM leaves the firmware alone.
- **Its own** (`create --firmware DIR --import`, or `create --firmware FILE.epk`, `--import` or
  not; the GUI's New VM the same): `vms/<name>/firmware/`,
  with the paks, `manifest.toml` and `cache/` (the partition map); deleting the VM deletes it.
  - a directory of extracted paks: its paks are hard links in the VM's `firmware/` when the
    directory is on the library's filesystem (no more space: nothing writes a pak), else copies.
    Another VM's `firmware/` is such a directory.
  - a firmware file (`.epk`): `epk2extract -c -n` (on `PATH`, `~/.local/bin`,
    `BEANJAR_EPK2EXTRACT`, or `config.toml`'s `epk2extract`) runs in the new VM's scratch directory.
    Only the target's paks (`targets.pak_files`: those the VM attaches and those its partition map
    is made of) are moved into its `firmware/`, with the `.epk`'s name, size and sha256 in
    `manifest.toml`; everything else epk2extract wrote (other paks such as `kernel`, `tzfw` and
    `lxboot`, `kernel.pak`'s unpacked intermediates) is deleted with the scratch before the VM is
    published, so a failure or Ctrl-C leaves nothing either.

An imported firmware is resolved to its firmware target first, by its manifest or else by the model
code in its name and its release. webOS uses the rootfs's `etc/starfish-release`; legacy firmware
uses `root.pak`, `mode.pak`'s `RELEASE.cfg` and the vendor graphics library to distinguish
SimpleSmart from NetCast 4.5. The supported targets are the files of
`guest/targets/` (`targets.MODELS`: WT1H with afro is `h13-afro`); firmware without one is refused.

Settings can change `firmware.directory` later (`config set NAME fw=/path`, or Settings'
"Directory in Place…"). A relative `fw` is relative to the VM's folder; `config unset NAME fw`
goes back to the VM's own `firmware/`. In `vm.toml`, `fw` is always a directory, never a name to
look up.

New VM makes the VM in a scratch directory of the library (`vms/.new-*`) and renames it into place
at the end: Ctrl-C, SIGTERM, a failure or the GUI's Cancel leaves nothing behind.

## Commands

Every command is `VERB NAME` or `NOUN... VERB NAME`: the verb comes before the VM, and nothing but
the command's arguments and options follows `NAME` (`-d NAME` for `ares-TOOL`, as the ares tools
take it). `NAME` is always the VM (`NAME`, `HOST/NAME` or `local/NAME`, see Several machines). A group
without its verb (`beanjar config`, `beanjar vnc password`) prints its help and exits with status 2;
a VM's name where the verb goes, or a verb where the VM goes, is an error that names the right form.
The global options, `--json`, `--library DIR` and `--repo DIR`, go anywhere on the line, except in
`shell`'s command (the words after its `NAME`), in an `ares-TOOL`'s arguments, which are the SDK
tool's (before `ares-TOOL` they are beanjar's), and after `--`. Durations carry a unit: `200ms`,
`1.5s`, `2m`. `--repo DIR` names the checkout for that command only and never writes the library; without `--repo` a command uses the stored checkout, else the checkout the running code comes from (in memory, never written); an installed package uses its own files; when no checkout is known at all the command refuses and names `beanjar library config set repo=DIR`. `beanjar --help` and `beanjar COMMAND... --help` describe each one; the guideline the
commands follow is in [AGENTS.md](../../AGENTS.md#follow-the-command-line-guideline).

| Command | What it does |
|---|---|
| `list [--all] [--local \| --host HOST] [--tag TAG ...]` | With hosts configured (see Several machines), every machine's VMs, under a `HOST` column (`local` for this machine). Each VM's state: Off, Starting (until `debug shell` is on the serial console), Running or Stopping, with a QMP run state other than running (e.g. `paused`, or `standby`: Quick Start+'s, the guest's `webos.standby` port open) and `+viewer`, its network (`nat+mitm`, `nat+direct`, `lan+direct`, `off`, ...), its address, and while it runs its `GPU`: the busiest engine's percent, `on` without usage counters, or `off` (as `show`'s `gpu`), its `TAGS`, and its `REASON` (see Why a VM runs), cut to 32 characters (`--json`'s `reason` has it whole). `--tag` lists only the VMs that have that tag, every one given when repeated (see Tags). Stale pidfiles and stale start locks are removed. |
| `create NAME --firmware NAME\|PATH [--import] [--mem --smp --layers --link --mode --display --audio] [--tag TAG ...]` | `--firmware` is a firmware of the host's collections (`firmware_dirs`) or, with a `/`, a directory of extracted paks, used in place (it needs a `manifest.toml`); with `--import` its paks become the VM's own `firmware/` (hard links, or a copy). A `.epk` file is extracted into the VM's own `firmware/`, keeping only the paks it uses. See Firmware. `platform` is its target's. `--layers` is comma-separated. Defaults: no `mem`, the VM has its firmware's model's RAM at each launch (`nvm.board_ram`: 1.25 GB for WT1H, W15M and W16R, 1.5 GB for W17H, W18R and W19P), 4 CPUs, `normal,gl`, link `nat` and mode `mitm`, `dbus`, and audio `dbus` (`none` without the dbus display). |
| `start NAME [cmdline words] [--reason TEXT [--by LABEL]]` | Checks the host's free memory (`MemAvailable` covers the VM's RAM and `memory_headroom_mib`, default 1 GiB), takes the VM's start lock and boots it, beside any others. The launcher's output is progress, on stderr. `--reason` says why it runs (see Why a VM runs). |
| `status show NAME` / `status set NAME reason=TEXT [--by LABEL]` / `status unset NAME reason` | A running VM's status, what lasts only while it runs: `reason`, why it runs: shown, set, or unset. See Why a VM runs. |
| `log show NAME FILE` | One of the VM's host-side logs as it is now, its bytes on stdout; with `--json` an object with its `text` (invalid UTF-8 replaced) and `path`. `FILE` is `logs/NAME` (the launcher's: `console.log`, `webos.log`, `qemu.out`, ...) or `run/NAME` (its helpers' while it runs: `media-helper.log`, `media-sessions.log`, `tuner.log`, ...), a `.log` or `.out` file or a rotated `.log.N`; no other file of the VM's. `HOST/NAME` reads a VM's log on another host, as the tests that measure a VM there do. |
| `stop NAME [--unplug \| --no-fallback] [--force]` | The power-off signal, or with `--unplug` a kill at once (see Stopping a VM). Either way the start lock is released, and a viewer left open is closed. A VM that runs for a reason is refused, the reason printed, unless `--force`. |
| `show NAME` | One VM: its state, settings, slot and address, with link lan `lan_address` (its lease, while it runs) and `lan_hostname` (the DHCP hostname it sends: its name as a DNS label, see [network](../../docs/dev/architecture/network.md)), its identity directory, how to reach it (`ssh`), its inspector and VNC (`url`, with link lan `local_url` for this host, and `password_file`), and `gpu`: `off`; `on (usage unavailable)` for host GL whose driver counts no engine time (WSL's d3d12); or `on (v3d) render 12% bin 2%, 310 MiB`, the busy engines and resident memory from the kernel's DRM fdinfo of the VM unit's processes over 0.1 s (`beanjar/display/gpu.py`; `--json` has every engine). The manager's Summary shows the same, each second. |
| `delete NAME` | Remove a VM that is off, with its firmware, state, snapshots and logs (an external `fw` is left alone). |
| `rename NAME NEW` | Renames a VM that is off, in place: the same TV under another name, in its own library on its own host (`HOST/NAME NEW` runs there; `NEW` is a bare name, a VM never changes host: `clone` copies one). Its folder `vms/NAME` becomes `vms/NEW`, with everything in it as it was: `vm.toml` (its MAC, serial, slot, so its address and ports, and every setting), `identity/` (the keys, the model key, `known_hosts`, the VNC password), `state/`, its own `firmware/`, `logs/` and the last thumbnail. Only the comments of the public keys that name the VM (`beanjar NAME`) follow, and `run/unit` (its last unit, stale while it is off) goes. What is derived from the name follows at the next start: its unit `beanjar-vm-<tag>-NEW`, and with link lan its DHCP hostname (`lan_hostname`), so a router that registers hostnames knows it as `NEW` from then on. Refused while it is not off, while a viewer or beanjar-vnc is open for it or its unit is up, for a `NEW` that is invalid (as `create`) or taken, and for a development VM (named after its checkout and `INSTANCE`). The rename of the folder is the commit, under the VM's start lock and the library's slot lock: interrupted, it leaves the VM whole under one of the two names. The GUI's VM menu has it too (Rename…, F2). |
| `prepare NAME` | Works out again what the VM's start needs from its firmware and board (the partition map, the new EEPROM chip, the local key, the chip modules' parameters: [prepared board data](../../docs/dev/architecture/manager.md#prepared-board-data)), only while it is off. `create` and a change of `fw`, `broadcast`, `model`, `country`, `debug_mode` or `serial` do it, and a start whose data is missing or stale does it first and says so; this does it ahead, for instance after an update of the tree. |
| `wipe NAME [--part P ...]` | Deletes the VM's state disks (`host/scripts/vm-launcher reset-state`), only while it is off. Without `--part`, every disk. nvram and eeprom hold the TV's settings. |
| `snapshot create NAME SNAP` / `snapshot list NAME` / `snapshot show NAME SNAP` / `snapshot delete NAME SNAP` / `snapshot reset NAME SNAP` | Saves the state of a VM that is off (every state disk and its secure-store items) as the snapshot `SNAP`, in its own folder, and puts it back with `reset`; the VM's identity stays. See [Snapshots](#snapshots). |
| `config show NAME` | The VM's config, its `vm.toml`: its settings and its tags; a key it leaves at the default is shown as `(default)` (`null` in JSON). `beanjar config set --help` lists every key, its values and its default (from `vmsettings.SETTING_HELP`, next to the keys it describes). |
| `config set NAME KEY=VALUE...` | Change `vm.toml`. `tags=TAG[,TAG...]` replaces the VM's tags, whether it runs or not (see Tags); every other key changes only while the VM is off: `fw broadcast model country mem smp free_page_reporting userland guest_kernel hwdec debug_mode layers link mode display audio mac serial listen forward address mitm_flows mitm_flow_bytes`, `layers` comma-separated, each `NAME` or `NAME@VERSION` for a versioned layer (`hbchannel@0.7.3`; Homebrew Channel) (`config set tv1 mem=1536 layers=normal,gl`; `fw=/path` boots that directory in place; `platform` follows `fw`'s target). `mac` is six hex octets, unicast; `serial` 1 to 16 letters and digits (upper-cased). `listen` an IPv4 address for the TV-service ports, `link=nat` only; `forward` the guest's own TCP ports relayed beside them, `PORT` or `HOST:GUEST` comma-separated (`forward=8080,8081:80`), refused on a host port a layer has, 5900, 18083 or below 1024; `address` `dhcp` or `A.B.C.D/NN,GATEWAY`, `link=lan` only. `free_page_reporting` `on` or `off` (the default). `userland` `auto` (the default: native or qemu-user by the host, at each start), `native` or `qemu-user` (refused on NetCast and SimpleSmart; its qemu-arm is `QEMU_ARM` or beanjar-vendor's qemu-user). `guest_kernel` `arm64` (the default) or `x86-64` (experimental: x86-64 hosts only, q35, userland qemu-user; refused with `userland=native` and on NetCast and SimpleSmart; its QEMU is the vendored qemu-system-x86_64 or `QEMU_X86_64`). `hwdec` `auto` (the default: a hardware decoder where one opens, a viewer's preference taken), `none` (software only, locked against the viewers' preferences, a short codec list, refused with `MEDIA_SANDBOX=off`; docs/user/vms.md) or a device type (`cuda v4l2request vaapi vulkan`); the launcher's `MEDIA_HWDEC`, which `advanced.env` does not take. `model` a retail model the chip has a board of (upper-cased; W22O: `42LX3QPUA`, the OLED Flex), refused naming the chip's models where it has no board of it, or where its standard is not `broadcast`'s or the firmware's; wipe the TV state when changing it (its EEPROM was made for the old board). `country` an ISO 3166 alpha-3 code of the board's own RMM country group in the firmware's configd tables (upper-cased; a US board's `USA`, `CAN` or `MEX`), checked against the firmware with the board of the VM's `broadcast` (a change of `fw` or `broadcast` checks it again); anything else is refused naming the board's countries and, for a country of another group, that group and its broadcast standard; refused before webOS 5, whose firmware does not tie a board to a group; applied at the next start (docs/user/board-profiles.md, "Choose the TV's country"). `debug_mode` `debug` (the default, `debugMode=3`: a development set, with faultmanager's crash popup), `event` (4: event logs, the popup stays) or `release` (5, what a retail set runs: no crash popup, and the set reboots when tvservice or lginit_app exits); the kernel's `debugMode=` word and the EEPROM's debug status follow it at the next start (webOS 4.x keeps the kernel's word only; webOS 1 to 3.9 also write `debug` to the EEPROM), `unset debug_mode` brings `debug` back. `mitm_flows` a number, 5000 by default, 0 for no limit; `mitm_flow_bytes` a size such as `200M`, `0` for no limit (the default). `link=lan` with a `listen`, or another link with an `address`, is refused: unset it first. An empty value of a key that `unset` takes is refused, naming `unset`. |
| `config unset NAME KEY...` | Back to the default: `tags` (none, whenever), and while the VM is off `fw` (the VM's own `firmware/`), `broadcast` (the firmware's own, else the chip's default board), `model` (the board of the broadcast standard), `country` (the board's own on a new TV state; a state keeps the country it has), `listen` (the VM's own address), `forward` (none), `address` (dhcp), `mitm_flows` (5000) and `mitm_flow_bytes` (no limit). The other keys have no unset: set them to a value. |
| `viewer open NAME [viewer options]` / `viewer close NAME` | Open (`display = "dbus"` only, one per VM) or close the viewer. It is `build/viewer/beanjar-viewer` of the checkout, or `BEANJAR_VIEWER`. |
| `screenshot NAME FILE [--frame WxH]` | Save the TV picture of a running VM (`display = "dbus"`) as a PNG, with its video windows and LG's cursor in and no window chrome; prints the path. Default frame 1280x720 (the viewer's picture area at its default size, which Save Screenshot gives), the TV picture letterboxed into it; `--frame` is 64 to 8192 each way. Whoever owns the VM's display takes it: an open viewer or beanjar-vnc owns `org.beanjar.Display1` on the VM's bus and answers its `Screenshot(path, width, height)` method (`gdbus call --address unix:path=VM/run/dbus.sock --dest org.beanjar.Display1 --object-path /org/beanjar/Display1 --method org.beanjar.Display1.Screenshot /tmp/x.ppm 1280 720` writes a PPM); with neither, a headless viewer runs once and exits (about 2 s). A VM that is off is an error, as is a viewer or beanjar-vnc started before this command, which does not own the name (close it and open it again). `--json` has `file`, `width`, `height` and `taken_by` (`viewer`, `vnc` or `headless`), and how the frame shows the guest's display: `source_width` and `source_height` (QEMU's display, the guest's graphic plane, 1920x1080 on most boards), `scale` (frame pixels per display pixel, 0.666667 for 1080p in 1280x720) and `offset_x`, `offset_y` (the letterbox bars, 0 when the aspects match); `null` when unknown. The PNG records the display's size (a `tEXt` chunk `beanjar-source`, `1920x1080`), so `HOST/NAME`, `--last` and `elements` give it too. |
| `elements NAME` / `elements --from-png PNG` | The TV picture's text and controls as a list (OCR and UI-element detection), for scripts and tests. Captures through `screenshot`, including remote hosts, then runs the optional user-installed `textshot` on this PC. `--image` reads a local PNG without a VM or library. Refs belong to this frame only; `[dimmed]` means low contrast, not disabled; go-textshot does not report focus on webOS today, so `[focused]` will rarely appear. See [Reading screen elements](#reading-screen-elements). |
| `vnc start NAME [--port N] [--listen ADDRESS] [beanjar-vnc options]` / `vnc stop NAME` | Serve the TV picture over VNC on the VM's address, port 5900, and with link lan also on its LAN address, port 5900 ([beanjar-vnc](../../docs/dev/architecture/vnc.md); `display = "dbus"`, not while the viewer is open), or stop it. Always behind the VM's own password (`vnc password`); `show` shows its file under `vnc` (and `vnc_password_file`). `--port` moves this host's listener (with link lan only that one); `--port 0` takes any free port (what beanjar-web uses; `show` shows it); `--listen` is for link nat. It is `build/vnc/beanjar-vnc`, `/usr/bin/beanjar-vnc` installed, or `BEANJAR_VNC`; its log is `logs/vnc.log`. |
| `vnc pipe NAME` | Relay stdin and stdout to the running beanjar-vnc's local RFB socket (`run/rfb-local.sock`, owner-only, no RFB authentication): an RFB client on stdio. For `HOST/NAME` it runs there with this process's stdio. The [web viewer](../../docs/dev/architecture/web-viewer.md) carries noVNC over it. |
| `vnc audio-pipe NAME` | Relay stdin and stdout to the running beanjar-vnc's audio socket (`run/audio-local.sock`, owner-only): send `opus` or `pcm` and a newline, read a 16-byte header, then the VM's mixed audio (the guest's and the media's, 48 kHz stereo) as length-prefixed 20 ms Opus packets or s16le PCM ([vnc](../../docs/dev/architecture/vnc.md#audio)). It ends at the end of stdin; for `HOST/NAME` it runs there with this process's stdio. The web viewer's speaker plays it. |
| `vnc video-pipe NAME` | Relay stdin and stdout to the running beanjar-vnc's video socket (`run/video-local.sock`, owner-only): send `h264` (optionally `bitrate=N`, kbit/s) and a newline, read a 32-byte header naming the encoder, then the TV picture as H.264 access units, each with a 16-byte header (length, keyframe flag, timestamp) ([vnc](../../docs/dev/architecture/vnc.md#video)). It ends at the end of stdin; for `HOST/NAME` it runs there with this process's stdio. The web viewer's H.264 picture plays it. |
| `remote pipe NAME` / `remote press NAME KEY... [--hold DURATION]` / `remote layout NAME` | The TV remote's buttons through the display's front end (beanjar-vnc or the viewer: `org.beanjar.Display1.Ir`, [viewer](../../docs/dev/architecture/viewer.md#remote-buttons-from-a-script)). `pipe` reads `down CODE` / `up CODE` lines (an IR code such as `0x7c`, or a key's name such as `HOME`), answers `ready` and `error: ...`, and releases what it holds at the end of stdin; `press` presses keys in turn (each held `--hold`, default 100ms); `in Quick Start+ standby the micom reports no keys, so both send a POWER key-down (IR 0x08) as QMP `system_powerdown` (the wake line) instead of IR, and drop its key-up ([suspend](../../docs/dev/architecture/suspend.md)); on a model with a touch screen `pipe` also takes `touch KIND SLOT X Y WxH` lines ([Touch](#touch)); `layout` is the VM's remote, the viewer's for its firmware (`--json`: its keys, boxes, colours and codes). |
| `remote click NAME X Y [--button left\|middle\|right] [--frame WxH] [--settle DURATION] [--hold DURATION]` / `remote move NAME X Y [--frame WxH]` | The Magic Remote's pointer through the display's front end (beanjar-vnc or the viewer: `org.beanjar.Display1.Pointer`), at X Y of a screenshot: the 1280x720 frame `screenshot` and `elements` report (`--frame` for `screenshot --frame`), whole pixels or an element's `centre`. Each call stands alone: a wake movement, the pointer to X Y, and for `click` a wait of `--settle` (default 300ms), then the button held `--hold` (default 100ms). `--json` has the display pixel it went to (`display_x`, `display_y`) and the frame's geometry as `screenshot --json`. See [Pointer](#pointer). |
| `remote tap NAME X Y [--hold DURATION] [--frame WxH]` / `remote swipe NAME X Y X2 Y2 [--time DURATION] [--steps N] [--frame WxH]` | The touch screen of a model that has one (the StanbyME, `lm21u`) through the display's front end (`org.beanjar.Display1.Touch`), at X Y of a screenshot as `remote click`: `tap` holds one finger for `--hold` (default 100ms), `swipe` moves it to X2 Y2 in `--time` (default 300ms) over `--steps` moves (default 10). A model without one is an error. See [Touch](#touch). |
| `vnc password show NAME` / `vnc password set NAME [FILE]` / `vnc password reset NAME` | The VM's VNC password, in the owner-only `identity/vnc.password`: made at random (20 characters, none easily confused) the first time VNC starts or it is asked for, then kept across VNC and VM restarts. `show NAME` (the VM's) says where it is (`vnc_password_file`); `vnc password show` prints it alone (the only form that prints it, also with `--json`); `set` reads a new one from a prompt on a terminal, else stdin's line (no `FILE`, or `-`), or a one-line `FILE`, never argv (1 to 255 characters of printable ASCII, no space at either end); `reset` makes a new random one. A running `beanjar vnc start` keeps the old one until it restarts. Clients may give any user name, an empty one too ([vnc](../../docs/dev/architecture/vnc.md#authentication-and-the-lan)). `identity reset` makes a new one; `wipe` keeps it. |
| `identity reset NAME` | Makes a VM that is off another TV: every state disk wiped (as `wipe` without `--part`), a new MAC (the library's `mac_prefix`) and serial in `vm.toml`, every key pair of `identity/` made anew (the SSH client key, both host keys, the developer key; `known_hosts` rewritten), a new random VNC password and a new LGUDID. The model key, `authorized_keys` and the library's CA are kept. It asks nothing, as `wipe` does. |
| `clone SRC DEST` | Makes `DEST` a copy of the VM `SRC`, which must be off, as another TV. Either may be on another host (`HOST/NAME`; `SRC` is looked up as other commands do, a bare `DEST` is this machine's); this machine drives the copy and relays it, so a copy between two hosts passes through it as a tar stream, never as a file. Kept: every setting of `vm.toml` but its slot (`DEST` gets the lowest free one of its library), the state disks (first use, region, installed apps such as Homebrew Channel), an own `firmware/`, and `identity/` but what a new identity replaces: `sestore.toml` with the model key, `tee-huk` (what the guest's secure store holds is encrypted with it), `authorized_keys` and any other file there. New, as `identity reset` makes them (`vmidentity.next_identity`, `vmidentity.renew_keys`): the MAC (`DEST` library's `mac_prefix`) and serial, every key pair, the VNC password, the LGUDID; `known_hosts` is written at the first start. The state is not wiped: the guest brings what it derives from the identity in line at boot (the NDUID from the MAC; the developer key, encrypted again with the new passphrase; root's `authorized_keys` and the host keys of root SSH, Homebrew Channel and developer mode, from the per-VM layer), and the EEPROM's serial records, which a start does not rewrite on LLS's chips (webOS 5 on), are rewritten by the clone. Refused when `SRC` is not off, `DEST` exists, or `DEST`'s host has no `SRC`'s in-place firmware directory (the shared collection is at the same path on every host). `DEST` is assembled in a hidden `vms/.clone-DEST-*` on its host and renamed into place once `SRC` is seen off again; a failure, Ctrl-C, SIGTERM or SIGHUP removes it, and a clone killed outright leaves only that hidden directory, which is no VM. What this machine makes for `DEST` (`vm.toml`, the new keys, the EEPROM) is staged in its own library's `vms/.clone-stage-*` and removed at the end. A host needs only its installed `beanjar` (`show`, `config show`), `sh` and GNU tar. `SRC`'s snapshots are not copied. |
| `identity authorized-keys show NAME` / `identity authorized-keys reset NAME` / `identity authorized-keys add NAME --mine` | The public keys that may log in to the VM as root over SSH (`rootssh`, `hbchannel`), and so go into the guest: `identity/authorized_keys` and the VM's client key, each with its type, fingerprint and whose it is (`vm`, `mine`: one of your `~/.ssh/*.pub`, `other`). `reset` removes every key but the VM's own, which is what a new VM has; `add --mine` adds your own `~/.ssh/*.pub` without their comments (local only: it refuses `HOST/NAME`). A change reaches the guest at its next start. |
| `key model show NAME` / `key model set NAME FILE` / `key model unset NAME` | The VM's SDP model key in `identity/sestore.toml` (below): whether it has one, set it from `FILE` (local only: it refuses `HOST/NAME`), or unset it; set and unset while it is off. The value is never printed. |
| `key list NAME` | The item names in `identity/sestore.toml`, and its `missing` policy; never a value. |
| `library config show` / `library config set KEY=VALUE...` / `library config unset KEY...` | The library's `config.toml`: shown (with `mac_prefix`'s default), or set: `repo auto_reconnect epk2extract mac_prefix lan_parent addresses` (`mac_prefix` three hex octets, unicast; `lan_parent` an existing wired interface of this host, `unset lan_parent` removes it; `addresses` three octets inside 127.0.0.0/8 but not 127.0.0, the prefix of the VMs' addresses, its development libraries' too, changed only while all of them are off: two libraries with different prefixes run their VMs side by side, and each VM's `known_hosts` pins follow at its next start). The GUI has no page for these; only auto_reconnect is in its main menu. |
| `host config show` / `host config set KEY=VALUE...` / `host config unset KEY...` | This machine's `config.toml` (above): shown, or set: `max_running=N`, how many VMs may run at once on this host, counting every beanjar VM up in `beanjar.slice` (every library's, the development VMs', the regress harness's); `unset max_running` removes the limit. `start` (and the GUI's Start, `vm-launcher start`) beyond it is refused before anything is launched. `firmware_dirs=DIR[:DIR...]`, the host's firmware collections; `unset firmware_dirs` goes back to `/srv/beanjar/firmware`. It reads and makes no library. |
| `host add NAME URI` / `host remove NAME` / `host list` / `host show NAME` | The remote hosts of this machine's `config.toml` (see Several machines): add one (`ssh://[USER@]HOST[:PORT]`), remove it, list the names and URIs (an array with `--json`), or show one: its `beanjar --version --json` (asked now) and its `drift` from this beanjar (see Versions across machines). They need no library. |
| `ares-TOOL [--root] -d NAME [ARGS...]` | Runs the webOS SDK tool `ares-TOOL` (`ares-install`, `ares-shell`, `ares-launch`, `ares-novacom`, ...) against the running VM alone, as the developer-mode user `prisoner`, or with `--root` as root over the VM's root SSH, the other arguments passed on as they are, and exits with its status (see ares tools). |
| `shell [-t \| -T] NAME [[--] CMD [ARG...]]` | A root shell inside webOS on the running VM, or one command, over the VM's own `webos.shell` port: no SSH, developer mode, network or layer (see Shell). Exits with the command's status. |
| `mitm show NAME [--password]` | The traffic inspector of a running `mode = "mitm"` VM: its mode, status (`Running`, `Inspector is not ready`, `Inspector off: REASON`, `Not running`, ...), whether it is ready, its URL (with `link = "lan"` the LAN one, and `local_url`, the host's way in), its password file, and `mitmweb`: the `version` and `path` the start found and the `problem` that kept the inspector off (no mitmweb, older than 12.0.1, or it failed; the guest then goes out directly); the password itself only with `--password`. |
| `io usb attach NAME [--id ID] [--image FILE [--readonly] \| --device VENDOR:PRODUCT\|BUS-DEVICE [--sudo] \| --camera [--pattern NAME]]` / `io usb detach NAME ID` / `io usb list NAME` | USB devices plugged into a running VM over usbredir: `attach` without a source adds a `usb-redir` device with a unix socket in the run directory and prints the socket; with `--image` a disk image as a USB stick; with `--device` or `--camera` it does the lot for a device of this machine, or for the virtual camera, until Ctrl-C. `detach` unplugs one, `list` shows each with whether a client is on its socket and whether the guest has the device (see [USB devices](#usb-devices)). |
| `io usb gamepad attach NAME [--id ID]` / `io usb gamepad press NAME CONTROL... [--hold DURATION] [--id ID]` / `io usb gamepad detach NAME [--id ID]` | A virtual gamepad, a wired Xbox 360 controller (045e:028e), in a running VM: `attach` plugs it in and returns, the pad stays until `detach` or the VM stops; `press` presses and releases each control in turn (`a b x y lb rb lt rt back start guide ls rs up down left right`, each held `--hold`, default `100ms`). See [USB devices](#usb-devices). |
| `io soundbar attach NAME [--formats FORMAT,...]` / `io soundbar detach NAME` / `io soundbar show NAME` | A sound bar on the running VM's HDMI ARC input, on its CEC bus: `attach` plugs one in with the audio formats it reports (default `lpcm,ac3,eac3,dts`), `detach` switches it off and unplugs it, `show` shows it, what it agreed with the TV (ARC, System Audio Mode, volume) and the TV's `getCECState` (see [Sound bar](#sound-bar)). Not kept across a restart. |
| `io mic play NAME FILE` | Plays a sound file (`-`: stdin) once to the running VM's built-in far-field microphone, on a model that has one; returns when it has been sent (see [Built-in microphone](#built-in-microphone)). |
| `mitm connect NAME` | Makes the inspector reachable from this machine and keeps it so until stdin ends, across the VM's restarts (`stop` and `start`, a guest reboot): prints one JSON line, `{"url", "password"}`, and another whenever they change (a restarted VM's inspector has a new password and no flows); stderr says when the inspector is gone and when it is back. Every 5 s it asks mitmweb with the session cookie of its first login, and when that fails asks for the VM's state again (at most every 10 s). For `HOST/NAME` it asks the host's `mitm show --password` and adds a forward from `127.0.0.1:PORT` here to the inspector there to the host's shared ssh connection; those requests keep that connection open (`ControlPersist` ends an idle one after 60 s, and the forward with it), and should it end anyway the same forward is added to a new one. It is cancelled at the end (also on SIGTERM, SIGHUP, Ctrl-C; one left by a killed run is cancelled by the next remote run, as `ares-TOOL`'s). Refused for a VM without mitm, not running, or whose inspector is not ready. beanjar-web serves the inspector through it ([web viewer](../../docs/dev/architecture/web-viewer.md#the-traffic-inspector)). |
| `mitm clear NAME` | Empties the inspector's flow list, as its page's Clear does (`POST /clear` with the password and tornado's XSRF pair: the `_mitmproxy_xsrf` cookie and an equal `X-XSRFToken` header). For `HOST/NAME` through a forward as `mitm connect`'s, for the one request. |

### Reading screen elements

`beanjar elements NAME` captures the same TV picture as `beanjar screenshot`, then
runs the optional [go-textshot CLI](https://github.com/devcell-sh/go-textshot)
on this PC. `NAME` may be `HOST/NAME`: only the PNG crosses SSH, and the remote host
needs no screen reader. `beanjar elements --from-png PNG` reads an existing local PNG
without looking up a VM or creating a library. This command does not start a VM.
A capture can run the screenshot path's one-shot headless viewer.

Install `textshot` separately and put its binary on `PATH`, or set
`BEANJAR_TEXTSHOT` to the executable's path. Beanjar does not build, bundle or
install it. With no executable the command exits 1 with `go-textshot is not installed`
and this install hint:

```sh
go install github.com/devcell-sh/go-textshot/cmd/textshot@latest
```

The inspected v0.2.0 requires Go 1.26 and cgo's C compiler to build. Debian 13's
Go 1.24 can download 1.26 automatically with `GOTOOLCHAIN=auto` during the user's
install; Meson never downloads a toolchain or builds Go code. No Go toolchain is
needed to run an already built `textshot`.

Beanjar reads a frame through the recognition server when there is one (below), else invokes
`textshot -json -threads 4 PNG`, inheriting its environment; both give the same elements.
The runtime and models normally download on first use to go-textshot's own
user cache (`$XDG_CACHE_HOME/textshot`, normally `~/.cache/textshot` on Linux).
Set `TEXTSHOT_CACHE_DIR` to override that directory. Beanjar supplies no cache
flag or fallback directory. The inspected release's pinned icon model download
returns HTTP 404; its error is shown unchanged, with no beanjar workaround or
alternate model source.

CPU is the default and requires no GPU package. For optional CUDA, set
`TEXTSHOT_ONNXRUNTIME_LIB` to a compatible ONNX Runtime GPU library and set
`LD_LIBRARY_PATH` for its providers, CUDA/cuDNN and driver libraries (see the
study). These variables pass through; relative cache/library/loader paths are resolved
against the caller's working directory before inference runs in disposable scratch. Upstream can fall back to CPU;
OCR remains on CPU. Beanjar adds no GPU dependency. When `TEXTSHOT_ONNXRUNTIME_LIB` is the
library of a pip `onnxruntime-gpu` wheel (`site-packages/onnxruntime/capi/`), beanjar adds the
wheel's own `site-packages/nvidia/*/lib` to `LD_LIBRARY_PATH`, so a venv with the wheel and its
CUDA and cuDNN wheels is all the setup:

```sh
python3 -m venv ~/.cache/textshot/ort-gpu   # any place outside the checkout
~/.cache/textshot/ort-gpu/bin/pip install onnxruntime-gpu==1.30.0 nvidia-cudnn-cu13 nvidia-cublas \
  nvidia-cuda-runtime nvidia-cufft nvidia-curand nvidia-cuda-nvrtc
export TEXTSHOT_ONNXRUNTIME_LIB=$(echo ~/.cache/textshot/ort-gpu/lib/python3*/site-packages/onnxruntime/capi/libonnxruntime.so.1.30.0)
```

#### The recognition server

The textshot CLI loads its models for every image, which is most of a reading's time with
CUDA. `textshot-serve` (`host/tools/textshot-serve/`, built on the same go-textshot v0.2.0 with
no changes to it) keeps them loaded: it runs the CLI's own `-json` code (its element type and
image loading, copied unchanged) with the CLI's options and defaults, so its elements are the
CLI's, byte for byte, and answers on a unix socket. `beanjar elements` and the viewer's
Elements layer (host/viewer/README.md) start it on first use and share it; it exits after 5
minutes without a request. There is one per configuration (the server, `TEXTSHOT_ONNXRUNTIME_LIB`,
`TEXTSHOT_CACHE_DIR`, `LD_LIBRARY_PATH`), at `$XDG_RUNTIME_DIR/beanjar-textshot-UID/`, with its
log beside its socket. With a CUDA runtime its element detector is on the GPU, else on the CPU
(upstream's own fallback). When there is no server, or it cannot start, beanjar runs the CLI
per frame as before; the CLI is still required.

It is found as `BEANJAR_TEXTSHOT_SERVE` (a path or name; `none` never uses a server), else
`textshot-serve` on `PATH`, else the checkout's `build/textshot-serve`. Meson does not build it;
with Go (as for textshot above):

```sh
go -C host/tools/textshot-serve build -o ../../../build/textshot-serve .
```

Measured on the x86 PC with Home and a Settings page at 1280x720, models loaded: about 3.5 to
4 s a frame on the CPU, 0.7 to 1.2 s with the element detector on CUDA (OCR stays on the CPU and
is then the limit), against 6 to 7 s and 4 to 5 s with a process per frame
([study](../../docs/research/textshot.md#a-server-that-keeps-the-models-loaded-2026-11-25)).
While reading frame after frame it keeps about five CPU cores busy (ONNX Runtime's threads).

Default output is a flat YAML-like sequence, in go-textshot's reading order:

```yaml
- screen [size=1280x720]
- button "Programme Tuning" [ref=e8] [box=141,187,627,231] [highlighted]
- text "Multi Audio" [ref=e14] [box=141,372,265,400] [dimmed]
- icon [ref=e3] [box=31,111,75,155]
- progress "42%" [ref=e9] [box=100,100,180,120] [value=42] [unit="percent"]
```

The example illustrates the format rather than a complete detection. Names use
JSON-style double-quoted escaping; the whole scalar is single-quoted when YAML
punctuation requires it (with internal apostrophes doubled). Every element has
`[ref=eN]` using its upstream per-frame ID, including ID 0, and
`[box=x0,y0,x1,y1]` in original TV-picture pixels, with exclusive upper bounds.
Refs are valid for that one frame only. True style flags appear in the stable
order `dimmed`, `highlighted`, `focused`; false flags are absent. `dimmed` is
go-textshot's `disabled`, renamed for what it measures: low-contrast text. It does
not mean the control is disabled; webOS draws an alert's unfocused button grey, so
it is `[dimmed]` while the focused one is not. **go-textshot does not report focus
on webOS today**, so `[focused]` will rarely appear. No focus is inferred from
highlight or dimmed state. These are pixel heuristics,
not accessibility metadata or proof that remote keys can activate a control.

The spelling follows the official Playwright aria snapshot
[renderer](https://github.com/microsoft/playwright/blob/57decd26b623ab0291a4839ff976bbc94165ba32/packages/isomorphic/ariaSnapshotRenderer.ts)
and [YAML escaping](https://github.com/microsoft/playwright/blob/57decd26b623ab0291a4839ff976bbc94165ba32/packages/isomorphic/yaml.ts),
read from a local clone. Deliberate departures: this is a flat pixel list with a
synthetic `screen` header, not a DOM accessibility tree; upstream `icon` and
`progress` kinds are retained rather than guessing ARIA roles; `text` keeps its
label, ref and box on one line rather than Playwright's `text: value`. The box
uses **corners**, whereas Playwright's box uses x,y,width,height. Flags follow
geometry rather than preceding refs. `size`, `highlighted`, `focused`, `value`
and `unit` are extensions; these lines are not Playwright assertion templates.
All labels are preserved, including long labels that Playwright may omit.

`--json` returns `width`, `height`, `elements`, and the frame's geometry as
`screenshot --json` gives it (`source_width`, `source_height`, `scale`, `offset_x`,
`offset_y`; `null` for a PNG that does not record it). Each element retains all
upstream fields (`id`, `key`, `kind`, `group`, `x0`, `y0`, `x1`, `y1`, `score`,
`text`, optional `value`/`uom`, `style`, `file`), enriched with `box`, `centre`
(which can contain half pixels) and `unit` (an alias of `uom`). Absent text,
value and unit become `""`, `null` and `""` respectively. The style object is
unchanged. `file` can refer to a temporary capture already removed after reading.
The upstream CLI exposes neither theme nor backdrop, so both are omitted;
beanjar does not parse diagnostic stderr for them. There is no `--annotate`
option because upstream's CLI does not provide annotation output.

No Debian packaging is added for the reader. Packaging it separately would need
the compiled external CLI, its runtime/model distribution or first-use download
policy, licence notices and a resolution of upstream's missing icon asset. The
old `vm-screenread` wrapper was removed.

Targeted checks, with an already populated cache:

```sh
TEXTSHOT_CACHE_DIR=/path/to/cache meson test -C build/host --no-rebuild --suite elements
```

The formatter/routing tests need no models. The CLI fixture test skips with
`go-textshot is not installed` when the executable is missing, and reports an
incomplete cache separately. The archived Go tests run offline, skip without a
local Go >= 1.26, and never download a toolchain or modules. Their model test
also skips if the pinned runtime/models are missing or mismatched.

### Pointer

`beanjar remote click NAME X Y` clicks the TV picture where a screenshot shows X Y,
so a script can click a box `elements` reports (its `centre`). `remote move` only
moves. `NAME` may be `HOST/NAME`. Both need the VM's front end, beanjar-vnc
(`beanjar vnc start NAME`) or the viewer; with neither they say so and exit 1.
A front end older than this command answers that it has no `Pointer`: update
beanjar on that host and restart its beanjar-vnc.

The path is the Magic Remote's, as the viewer's and a VNC client's pointer: the front
end's `org.beanjar.Display1.Pointer(x, y, buttons)` (display pixels, a button mask,
bit 0 left) sets QEMU's virtio tablet, and the initramfs's bridge
(`guest/initramfs/src/virtio-pointer.c`) gives it to libim as a builtin M-RCU,
`LGE M-RCU - Builtin [3]`, scaled to LG's 1920x1080 pointer space. Buttons that a
caller holds are released when it leaves the bus.

- **Coordinates.** X Y are in the screenshot's frame: the guest's display (QEMU's
  console, the graphic plane: 1920x1080 on every webOS board here but MStar M2, M3
  and k8ap's 1280x720) scaled to fit, centred, the rest black. The point maps
  through the picture's box to the display pixel under it, and the bridge scales
  that to its own range, so 1080p and 720p displays map alike. A point in a
  letterbox bar or off the frame is an error, and nothing is sent. Cancel of an
  alert at `916,561` in 1280x720 goes to `1374,842` of 1920x1080.
- **Wake.** LG's cursor hides after a short idle: within 2 to 4 s on W22H ombre and
  W22O number1, 10 to 20 s on WT1H. A target with the M-RCU wakeup (W20L jhericurl,
  kisscurl and later releases) shows it again on a cursor-on key the bridge sends
  only with motion, and evdev
  drops a position that did not change. A press with no motion on a hidden cursor
  activated the element under it on WT1H, W22O number1 and once on W22H ombre, but on
  another ombre boot it landed where the cursor had been before (an alert closed
  without its button's action). So each call first moves 4 display pixels beside the
  target, then onto it.
- **Settle.** 300 ms between arrival and press by default (hover, then click). On
  W22H ombre `--settle 0ms --hold 0ms` worked as well; the default leaves room for slower
  guests (TCG).
- **Measured 2026-10-29 on the x86 PC** (beanjar-vnc with no client): system alert
  (`createAlert`, two buttons) clicks ran the clicked button's action on W22H ombre and
  W22O number1, over Home and over apps; Home tiles and cards launched their apps
  (ombre, W22O, WT1H's launcher bar); clicks inside web apps (WT1H's user guide, a
  homebrew app on ombre) and LG's browser (W22O) acted. Right after one fresh ombre
  boot, three alert clicks over Home closed the alert without its action, the cursor
  ending above the target at about two thirds of its height; later identical clicks
  on that boot worked. The cause was not found. On WT1H `createAlert` answered with an
  `alertId` but showed nothing, so its alerts were not tried.
- **Audio Guidance.** From W17H on, Audio Guidance `"on"` turns the pointer off;
  the emulator-settings
  step turns it off once per state, and a user who turns it on again disables
  clicks.

### Touch

A model with a touch screen (`targets.Soc.touch`: the StanbyME, `lm21u`) gets QEMU's
`virtio-multitouch-pci` beside the tablet, and the initramfs's bridge
(`guest/initramfs/src/virtio-touch.c`) gives it to libim as the panel, `LGE Touch Device`
([guest input](../../guest/initramfs/src/README.md#touch-screen)). `vm-info.toml` and `list
--json` say `touch = true` for it. The front end's `org.beanjar.Display1.Touch(kind, slot, x, y)`
(kind 0 begin, 1 move, 2 end, 3 cancel; slots 0 to 9; display pixels) calls QEMU's
`org.qemu.Display1.MultiTouch`; one client's touches at a time, ended when it leaves the bus.

- **From a script.** `remote tap` and `remote swipe` take a screenshot's coordinates as
  `remote click` does; a point in a letterbox bar starts no touch, and a moving finger is held
  to the picture's edge.
- **From the viewers.** The GTK viewer sends its touchscreen's events, and the web viewer a
  phone's or tablet's touches on the picture (`touch` lines on `remote pipe`); a mouse stays the
  Magic Remote's pointer in both.
- **Measured 2026-11-04 on the x86 PC** (TCG, StanbyME global firmware): a tap on Home's
  settings icon opened Quick Settings, a tap on Apps opened the app list's terms; a mouse click
  stayed `BTN_LEFT` on the M-RCU.

### Rotation

A model that turns to portrait (`targets.Soc.portrait`: the StanbyME, `lm21u`) is turned from the
host: `beanjar sensor set rotate NAME ANGLE` (ANGLE 0, 90, 180 or 270; `off` is 0) turns the set itself, and
waits until the guest reports what webOS made of it; `beanjar sensor show rotate NAME` prints the rotation the
guest last reported. Other models refuse it, and so does a VM started before the rotation port
existed. The guest's framebuffer stays the panel's landscape, as on the set, where the panel itself
turns.

- **The sensor.** The StanbyME's firmware (N21D, webOS 6 and webOS 24) turns its UI by its motion
  sensor service msc, which reads lgemu's BMA422 on `/dev/i2c-3`
  (`guest/kernel/modules/lgemu/accel.c`): Home, the running apps and the compositor's views turn by
  msc's orientation, and `option.screenRotation` stays off. `rotate` writes the sensor's gravity
  vector for ANGLE (`rotation.gravity`) to lgemu's `accel_mg` over the console and keeps ANGLE in
  the VM's `state/sensor-rotation`, which the next start gives the sensor (`accel_mg=` in
  boot.conf), as the set stays where it was turned.
- **The guest is the truth.** The initramfs's `guest/initramfs/rootfs/bin/beanjar-rotation`
  subscribes to msc's `getOrientationInfo` (the compositor's own subscription) and writes each
  orientation as its angle (`rotation.ORIENTATIONS`: landscape 0, reversed_portrait 90,
  reversed_landscape 180, portrait 270) to the `webos.rotation` virtio-serial port as `rotation=N`,
  which QEMU keeps in `run/rotation` (truncated at each start). `rotate` waits for that line, and
  the viewers read it; nothing on the host keeps a copy of its own. Before msc's first report a
  viewer shows 0. At 180 the webOS 24 firmware's msc keeps the orientation it had ("don't support
  reversed_landscape"; `rotation.REFUSED`): `rotate` turns the sensor, reports the rotation the
  guest still has and says so; the webOS 6 firmware turns to 180.
- **Which way.** The compositor draws the UI turned clockwise by the angle (measured: at 90 the
  text runs down the panel), so the UI is upright on a set turned that far
  counter-clockwise: 90 is the set turned a quarter counter-clockwise, 270 a quarter clockwise.
  The viewers turn the panel's picture by (360 - ANGLE) % 360 clockwise (`rotation.picture_turn`),
  and their Rotate buttons turn the set a quarter clockwise (`rotation.clockwise`: 0, 270, 180,
  90).
- **Input.** Touch and the pointer keep panel pixels: the viewers map their own touches and
  clicks back through the turn, and `remote tap`/`click` take a screenshot's coordinates, which
  are the panel's (screenshots stay unturned).
- `vm-info.toml` and `list --json` say `portrait = true` for such a model; `remote pipe` reports
  `rotation N` at its start and at each change, and takes `pointer X Y BUTTONS WxH` lines (the web
  viewer's pointer while the picture is turned).

### Light and temperature

Two of the board's sensors are set from the host on a running VM, over the console into lgemu's
module parameters (`host/core/beanjar/devices/sensors.py`). Neither is kept across a restart.

- **Ambient light.** `beanjar sensor set light NAME CH0 [CH1]` sets the light sensor's two channels in raw
  counts (0 to 65535; one value sets both); `beanjar sensor show light NAME` prints them. pqcontroller reports
  CH0 as `luminance` and CH1 as `visibleLuminance` (`getEyeqSensorData` subscribed) and, with
  energy saving on auto, moves `backlightValue` by them. The sensor is lgemu's `light1` or `light2`
  device (`guest/kernel/modules/lgemu/light.c`) at 7-bit 0x10 on the channel of the firmware's
  libdile_i2c `LIGHT_SENSOR` rows (`targets.Soc.light_bus`: 5 on LM21, K7 and K8, 1 on O22), present
  where the board's own tool option digitalEye says the set has one (`lls.digital_eye`, decoded as
  factorymanager does at prepare time; `targets.light_dev`): `eye_sensor` the first type,
  `OTP_eyeSensor` the second. The StanbyME (digitalEye off) has none, and older chips, which
  read it through the micom, none yet. A start has 6000 on both channels.
- **Measured 2026-11-06 on the x86 PC** (TCG): `Eyeqsensorcheck` true on W21A (type 2), W24P
  (type 2) and W22O mullet (type 1), false before. Subscribed, `sensorData` followed each value set
  on W24P and W22O (W21A's pqcontroller does not stream). On W22O `backlightValue` held 48 in the
  dark and climbed about 2 a second to 100 at 6000; it settles at 49 for 400 counts, 55 for 1500
  and 60 for 3000. W24P's stayed at the user's value.
- **SoC temperature.** `beanjar sensor set temperature NAME CELSIUS` (-40 to 200) sets what
  `/proc/thermalinfo/soc_temperature` reads (45 at a start), which fancontroller reports each
  second (`NL_CHIP_THERMAL {"soc_temperature":N}`); webOS 23 and later also read `iddq_core` and
  `iddq_scpu` there (150 and 90 mA). Every chip whose firmware has fancontroller has the directory
  (`targets.Soc.thermal`: lm21*, k7, k8*, o22*; lgemu `thermal.c`). On a set it is built into the
  SoC's power-management driver, with no module of its own. No board has fan control, so the
  overheating warning and shutdown stay unreachable. fancontroller's own view is its
  `getThermalReport` (`soc.currentTemperature`) and, at debug level, `getSoCCurrentTemperature`'s
  line each second; its `NL_CHIP_THERMAL` lines come once at its start, from the last saved report.

### Presence (ToF motion sensor)

`beanjar sensor set presence NAME present|absent|MM` sets who is in front of the set's ToF motion sensor,
an AMS TMF8821: someone who keeps moving at MM millimetres (100 to 5000; `present` is
1500), or nobody (`absent`, the state at a start); without a value it prints it. It is lgemu's
`tof` device (`guest/kernel/modules/lgemu/tof.c`) at 7-bit 0x41 on channel 1, libdile_i2c's
`TMF882X_SENSOR_N` row in the O22 firmwares (`targets.Soc.tof_bus`), present where the board's own
tool option `supportMotionDetectSensor` (ToolOpt4 bits 14-15, `lls.motion_sensor`;
`targets.tof_dev`) is `ToF_TBD_22Y`: the W23O and W24G DVB boards (OLED55C36LA, OLED55C44LA). The
W22O boards and the US defaults say `off`, and the StanbyME's one-bit item is 0 (and would choose
msc's micom-relayed sensor, which is not modelled).

msc drives the sensor only while `option.lifeOnScreenUsingMotionSensor` is on,
`general.lifeOnScreenMode` is `allEnabled` and tvpower's sub state is `always on display`,
`active standby` or `always ready`; otherwise `getMotionStatus` stays 0. Its motion status is a
pulse: 1 for one result when a zone's distance changes and then holds for 2, 4 or 6 results (the
sensitivity), so a person standing still is no motion. The model steps the person an eighth
further every 10 results. nopd's no-motion timers restart on each pulse; alwaysready turns the
panel on from active standby on one (`onAoDByMotion`), and the Life On Screen app turns it off
with `noMotionAutoOff` when its `addNoMotionAndNoOperationTimer` (5 minutes or longer) expires.

- **Measured 2026-11-06 on the x86 PC** (TCG, W23O 13.40.76 DVB, `always on display`):
  `getMotionStatus` subscribed gave 15 pulses in 20 s at 1500 mm and at 3000 mm, none absent.
  `addNoMotionTimer` and `addNoMotionAndNoOperationTimer` with interval 10 did not expire in 30 to
  40 s with someone present and expired 7 to 8 s after `absent`. From `active standby`, `present`
  turned the panel on (`onAoDByMotion`) 6 s later. W22O mullet (board `off`) has no `tof` device.
- **Life On Screen end to end** (same set, display `egl-headless`): `present` in Always Ready
  brought the panel on with Life On Screen running; about 5 minutes after `absent` nopd's timer
  expired and the app powered the set off with `noMotionAutoOff`, back to Always Ready. It
  needs a GL display (`egl-headless` or `dbus`): with display `none` WebAppMgr aborts at its first
  page ("software rendering is not supported in Ozone-Wayland") and every web app dies with it.

### Tuner

`beanjar io tuner list NAME` lists the physical channels of the VM's lineup and which of them are
mapped and what the set is tuned to; `beanjar io tuner map NAME RF...` and `unmap NAME RF...|all`
change the map, kept in the VM's state for its next starts. An RF is a channel number, or C and
the number for a cable channel (`7`, `C78`; `beanjar/tuner/band.py` has each standard's plans),
and carries its own delivery system: US air 8VSB, US cable QAM annex B, DVB-T, DVB-C annex A,
ISDB-T; so air 7 and cable C7, both on 177 MHz, are two RFs. Only boards whose SoC data loads
`dtv_soc` have the tuner (`targets.Soc.modules`; `k7lp`, `k8lp`, `k8lpn` but its number1, `k8lpn2`,
`k24` ponytail and queue and `o22` ponytail and queue). The lineup is a TOML file
(`beanjar/tuner/lineup.py`, the format in [docs/user/tuner.md](../../docs/user/tuner.md)): the
built-in one of the board's broadcast standard, `beanjar/tuner/builtin-lineups/atsc.toml` (air RF 7, 14 and 33
with four virtual channels, cable C78 with one), `arib.toml` (UHF 27, one service with the BML
pages of `builtin-lineups/arib-bml`) or `dvb.toml` (DVB-T on UHF 21 with two services, DVB-C on C38 with
one), unless `beanjar io tuner lineup set NAME FILE` gave the VM its own: checked
against the board's standard, then copied with the files it names into the state's
`tuner-lineup/`; `lineup show NAME` prints the one in effect (TOML; `--json` its values) and
`lineup reset NAME` goes back to the built-in one. Mapping a channel makes its transport stream
with TSDuck and ffmpeg into the cache's `tuner/` (keyed on the lineup's values and its files'
content, so an edit makes it again): for ATSC PAT, PMT, the PSIP tables (MGT, TVCT on the air or
CVCT on cable, RRT, STT, EIT-0 to 3 and their ETTs) and each service's picture and sound, 5
Mbit/s, 20 s, looped; for DVB (`beanjar/tuner/dvb.py`) PAT, PMT, NIT with the delivery
descriptor and the logical channel numbers, SDT, EIT present/following and a day's schedule,
TDT and TOT, MPEG-2 video and MPEG-1 layer II sound, 10 s.

`beanjar io tuner map NAME RF --stream FILE` puts a transport stream of the user's on one RF
instead (`beanjar/tuner/user_stream.py`): refused if it is not whole 188-byte packets, is scrambled in its
first seconds, or has no PAT or PCR; warned about if its NIT or PSIP is of another standard than
the board's, its delivery descriptor names another frequency, or it has no clock tables. It is
copied into the state's `tuner-streams/` and looped at its PCR's rate, its tables as they are but
the clock (STT; TDT and TOT) set to now; at the wrap each PID restarts at a PES or section start,
the continuity counters run on and the first PCR after it has the discontinuity indicator
(`tuner.RecordedStream`). `unmap` removes the copy.

Beside QEMU a helper (`python -m beanjar.tuner.antenna`, `run/tuner.log`, `run/tuner-status.json`) sends
the map and, while frontend 0 is tuned within 1 MHz of a mapped channel with that channel's
delivery system, its stream at its mux rate, nulls dropped and the clock's time (STT; TDT and
TOT) set to now, over
the `webos.tuner` virtio-serial port (`include/webos-tuner.h`). In the guest the initramfs's
`virtio-tuner` pump hands it to `dtv_soc.ko`, which makes `webos_tv`'s frontend lock on a mapped
channel and feeds its four software demuxes; `/proc/dtv_soc` shows the map, the tune and the
started section filters. The stock `legacy-broadcast` service runs unmodified above it.

The picture and sound: `dtv_soc.ko` also has LG's video decoder node, `/dev/video20`, which reads
the MPEG-2 headers of the demux's video decoder filter for the events that make legacy-broadcast
lift its mute, and passes the decoder filters' packets and the PCRs to the same pump through
`/dev/dtv-soc-av`. The pump sends each running decoder's stream as a session on a free
`webos.media.<n>` port, timed on the PCR, and the media helper decodes it as it does an app's
(`run/media/media-sessions.log` names them `tuner-vdec0` and `tuner-adec0`); the viewer shows the video in
the scaler window connected to the decoder's VDEC port.

The helper is a feeder that needs neither TSDuck nor ffmpeg when a feed serves it: a generator on
another machine connected to `run/tuner-feed.sock`, which it tells the channels the map wants and
the guest's tunes, and whose map and stream frames it passes on to the guest whole. Without a feed
it makes and sends the streams itself. `beanjar io tuner map HOST/NAME RF...` uses that for a VM
on a host without the tools (the Pi, the ROCK Pi boards): it makes the streams here, keeps the map
on the host (no streams made there; of an own lineup the host keeps only the file, of a stream
only its name, and this machine the files, in the library's `remote/HOST/NAME/`) and, while the VM runs, starts a detached feed here (`python
-m beanjar.tuner.antenna feed`, its log in the cache's `tuner/feeds/`) that reaches the socket through a
forward of the host's shared ssh connection (`ares.Forward`). Only the tuned channel crosses the
network, under 5 Mbit/s. The feed ends when the VM stops (the helper ends with QEMU and closes the
connection) and when `unmap` leaves nothing mapped; a VM started again is fed again by the next
`map` from here. `beanjar io tuner list HOST/NAME` shows whether a feed of this machine runs.

```sh
beanjar io tuner map tv 7 14 33 C78
beanjar io tuner list tv --json
tests/regress/tuner/run-scan.py tv        # an automatic scan by Luna: PASS or FAIL with numbers
tests/regress/tuner/run-watch.py tv       # Live TV's picture and sound after it: PASS or FAIL
beanjar io tuner map rk-1/tv 7 C78      # made here, fed over ssh
tests/regress/tuner/run-watch.py rk-1/tv  # measured from here: its logs and picture through beanjar
beanjar io tuner lineup set tv mine.toml
beanjar io tuner map tv 30 --stream rec.ts
```

### USB devices

A USB device of this machine, or of the machine `beanjar` runs on, is plugged into a running VM
with usbredir (`beanjar/devices/usb.py`), the protocol QEMU's `usb-redir` device speaks and
`usbredirect` (Debian's `usbredirect` package) serves for a local device:

```sh
beanjar io usb attach tv --device 046d:082d          # lsusb's VENDOR:PRODUCT, or BUS-DEVICE (1-4)
beanjar io usb attach pi/tv --device 1-4 --sudo      # a VM on another host: the device stays here
```

`io usb attach` with `--device` (or `--camera`, below) feeds the device from here, so it holds
until Ctrl-C (or SIGTERM, SIGHUP), until the device is unplugged here, or until
`usbredirect` ends, and unplugs the device from the VM whatever ends it. It prints one line once
the guest has the device (`--json`: `name`, `id`, `socket`, `device`, `bus_device`, `product`);
status 0 when the device was unplugged, 1 when `usbredirect` ended on its own, 130 or 128+N on a
signal. `usbredirect` opens `/dev/bus/usb/BBB/DDD` read-write, which is root's by default: `--sudo`
runs it through `sudo` (which may ask for the password on the terminal), or a udev rule gives the
user the device. With several devices of one VENDOR:PRODUCT, `--device` takes BUS-DEVICE.

The steps, each also a command of its own:

- **`io usb attach NAME [--id ID]`** (no source), on the VM's host: QMP `chardev-add` of a listening unix socket
  `run/usb-ID.sock` (a socket, not TCP, since QEMU runs in the VM's network namespace with
  `mode = "mitm"`) and `device_add usb-redir` with it on the launcher's `qemu-xhci`
  (`usb-bus.0`), QEMU device id `usb-ID`; prints the socket. ID is 1 to 16 letters, digits, `_` or
  `-`, by default the lowest free number. The guest sees nothing until a usbredir client connects
  to the socket and sends its handshake; it sees the device unplugged when the client goes.
- **The way to the socket**, on the machine with the device: `usbredirect --to` takes only
  `host:port`, so `attach --device` gives it `127.0.0.1:PORT`: for a local VM a relay in its process
  to the socket; for `HOST/NAME` a forward of the host's shared ssh connection to the socket there
  (`ssh -O forward -L 127.0.0.1:PORT:SOCKET`, as `ares-TOOL`'s forwards; usbredir's plain TCP is
  carried inside ssh), cancelled at the end, or by the next remote run should `attach` be killed
  outright. No socat.
- **`usbredirect --device BUS-DEVICE --to 127.0.0.1:PORT -k`**. `attach` watches the device's sysfs
  entry, as `usbredirect` does not end when the device is unplugged.
- **`io usb detach NAME ID`**: `device_del`; QEMU destroys the chardev with the device, and its
  socket. `io usb list NAME` shows what is attached: `CLIENT` connected (a client on the socket),
  `GUEST` attached (the device on the guest's bus).

A guest restart keeps what is attached, as a TV keeps what is in its ports. The restart ends QEMU
and the VM's unit starts a new one (`beanjar/lifecycle.py`, docs/dev/architecture/gpu-process.md):
`attach` and `attach --image` record each device in the unit's `RuntimeDirectory` (`usb.json`,
in attach order; `detach` takes it out), and the unit puts them back on the new QEMU, before its
guest boots, at the same sockets and on ports in the same order. The client of `attach --device`
ends with the old QEMU; `attach` then waits while the VM restarts (`io usb list` says `restarting`; `--json` has
`restarting` and `restarts`, how many times the unit started QEMU again) and starts the client
again for the new QEMU. A client of an `attach` without a source has to connect again by itself. A new start
of the VM has none of them.

A virtual webcam takes the device's place with `--camera`: `beanjar-virtcam`
(`host/helpers/virtcam`, installed in `/usr/lib/beanjar/virtcam`), a Logitech C920 (046d:082d) as
LG's camera service accepts it, showing GStreamer's test pattern (`--pattern`, a `videotestsrc`
pattern; default `smpte`) with the running time. It runs here, where `gst-launch-1.0` is, also for
`HOST/NAME`, in `usbredirect`'s place and through the same way to the socket, and prints its
stream counters (frames, packets, late wake-ups, skipped and dropped packets) to stderr every 10 s.
`tests/regress/virtcam/run-camera.py NAME` checks it end to end in the guest.

```sh
beanjar io usb attach tv --camera                    # until Ctrl-C
beanjar io usb attach rk-1/tv --camera --pattern ball
```

A virtual gamepad is a wired Xbox 360 controller (045e:028e) with that controller's own
descriptors (from a published USB capture of a genuine one), so the guest's own `xpad` driver
binds it and LG's PDM lists it as an `XPAD` device. Unlike `--device` and `--camera` it is not held
by the command: `io usb gamepad attach` adds a `usb-redir` device on the VM's host and starts
`beanjar-virtpad` (`host/helpers/virtpad`, installed in `/usr/libexec/beanjar`) against its
socket in a session of its own, so the pad stays plugged in for scripts to press its controls.
For `HOST/NAME` the whole command runs on that host, which needs the program installed.

```sh
beanjar io usb gamepad attach tv                     # returns once the guest has the pad
beanjar io usb gamepad press tv a a down start       # each pressed and released in turn
beanjar io usb gamepad press tv lt --hold 2s         # a trigger pulled all the way for 2 s
beanjar io usb gamepad detach tv
```

- **`io usb gamepad attach NAME [--id ID]`**: `io usb attach`'s `usb-redir` device, then
  `beanjar-virtpad --unix run/usb-ID.sock --control run/usb-ID-pad.sock`, its output in
  `run/usb-ID-pad.log` and its pid in the unit's `RuntimeDirectory` (`gamepad-ID.json`); returns
  when the guest has the device (30 s at most). The program answers the standard requests from the
  controller's descriptors, sends its power-up messages (LED, headset) and then one 20-byte input
  report per change, as the controller does, and takes the LED and rumble messages the driver
  sends. When the guest restarts QEMU does too, and the program connects again to the device's
  socket, which the new QEMU has back; with no socket for 180 s (the VM stopped) it ends.
- **`io usb gamepad press NAME CONTROL... [--hold DURATION] [--id ID]`**: each control pressed and
  released in turn, held `--hold` and released as long; a trigger (`lt`, `rt`) is pulled all the
  way. Reports reach the guest only while its driver polls the pad, which `xpad` does while the
  pad's input device is open (webOS's surface manager opens it); otherwise the presses go nowhere,
  as on a real pad, and `press` warns (`--json`: `reports`, the input reports sent, and `polled`).
- **`io usb gamepad detach NAME [--id ID]`**: ends the program, which unplugs the pad
  (`device_disconnect`), then removes the device as `io usb detach` does. `io usb detach NAME ID`
  of a gamepad's device ends its program too; `io usb list` shows the device with `KIND`
  `gamepad`. `--id` picks a pad when the VM has more than one.

`tests/regress/virtpad/run-gamepad.py NAME` checks it end to end in the guest.

A disk image is plugged in as a USB stick instead, with no client: `io usb attach --image`, on the
VM's host as the other steps.

```sh
beanjar io usb mkimage stick.img --fs ntfs --size 64M   # fat32, exfat or ntfs; --label, --overwrite
beanjar io usb attach tv --image stick.img [--readonly] [--id ID]
beanjar io usb attach pi/tv --image /srv/sticks/fat.img # a path on pi: the image is not copied
beanjar io usb detach tv 1
```

- **`io usb attach NAME --image FILE [--readonly]`**: QMP `blockdev-add` of FILE as a raw image
  (block node `usb-ID-disk`, read-only all the way down with `--readonly`, when QEMU takes only a
  shared lock on the file) and `device_add usb-storage` with it on `usb-bus.0`, `removable`, as a
  stick reports itself. FILE is a regular file this user may read, and write without
  `--readonly`; the guest writes to it, so it is used in place and never copied. For `HOST/NAME`
  it is a path on that host, which must be absolute (the host's beanjar runs elsewhere); put the
  image there first (`scp`, or `io usb mkimage` run there). QEMU's file lock refuses a second writer
  of one image (another VM, another attachment). IDs are shared with the `usb-redir` devices.
- **`io usb detach NAME ID`**: `device_del`, then `blockdev-del` of its node, which releases the
  image. Like pulling a stick out: eject it in webOS first unless the test is the pulling.
- **`io usb list NAME`**: `KIND` is `redir`, `gamepad` (a `usb-redir` device the virtual gamepad
  serves) or `storage`; `SOURCE` the socket, or the image and whether it is read-only (`--json`:
  `kind`, and `image` and `readonly` for a stick).
- **`io usb mkimage FILE --fs fat32|exfat|ntfs [--size 64M] [--label BEANJAR] [--overwrite]`**, on this machine
  (no VM): an MBR (`sfdisk`) with one partition from sector 2048 to the end, type `0c` for FAT32
  and `07` for exFAT and NTFS, the layout LG's PDM mounts, and in it `mkfs.vfat -F 32`,
  `mkfs.exfat` or `mkfs.ntfs -Q` (Debian: util-linux, dosfstools, exfatprogs, ntfs-3g; found in
  `/usr/sbin` too). The filesystem is made in a file of its own beside FILE (`mkfs.exfat` cannot
  write at an offset) and copied in with its holes kept; FILE is replaced only when all went well,
  and an existing one only with `--overwrite`. The size is whole MiB, 2 MiB or more; `mkfs` decides
  what is too small for its filesystem.

Checked on the x86 PC (TCG): W24P (`k8lpn2-ombre`) mounted an NTFS image read-write (a file
written in the guest was in the image after the detach), a FAT32 one with `--readonly` read-only,
and an exFAT one, each listed by PDM's `getAttachedStorageDeviceList` as `USB_STORAGE` and gone from
it after the detach; W21P (`k7lp-kisscurl`, webOS 6) mounted FAT32 as `tfat`. Attached while W21P
was still starting (load 40), PDM listed the drive unmounted; attached again once it had settled,
it mounted.

QMP serves one client at a time; these commands wait up to 15 s for one that holds it (`list` and
`show` hold it briefly). A device stays across a guest reboot (a machine reset in the same QEMU)
and Quick Start+ standby, but not across a stop and start: it is QEMU's, not one of the VM's
settings. An `attach --device` killed outright (SIGKILL) leaves its `usb-redir` device with no
client, which the guest does not see; `io usb detach` removes it. The guest kernel's USB class drivers are per
release ([build and boot](../../docs/dev/architecture/build-and-boot.md), "Peripheral modules").

### Built-in microphone

A TV model with LG's far-field microphone (`targets.Soc.mic`: the OLED C2 boards of the O22
family) always has it: `lgemu`'s capture card `LGSIC` (`mic.c`) with the five PCMs LG's echo
canceller front end opens, `hw:1,0` the microphone pair and `hw:1,1` to `hw:1,4` its silent
references, and a `webos.mic` virtio-serial port whose host side is `run/mic.sock`.
`beanjar io mic play NAME FILE` decodes a sound file (`-` for stdin) with ffmpeg to 48 kHz stereo
S16_LE, a mono file on both channels at its own level, and sends it to that socket; the guest's
pump (`guest/initramfs/src/virtio-mic.c`) writes it to `/dev/lgemu-mic`, and `lgemu` plays it at
real time into every capture of `hw:1,0`, at the capture's rate. A sound that follows silence is
heard 20 ms after it arrives; the writer is held back while it is half a second ahead, so the port's
flow control paces the sender, and `play` returns when the whole file has been sent, with up to
about 4 s of it still in the socket's and the virtio queue's buffers. With nothing sent the
microphone hears silence; no microphone of the host is ever captured. A model without the
microphone has no port, and `play` says so.

```sh
beanjar io mic play tv hey-lg.wav --json
```

### Sound bar

`beanjar io soundbar attach NAME [--formats FORMAT,...] [--arc-only]` plugs a sound bar into a
running VM's HDMI ARC input and switches it on; `io soundbar detach NAME` switches it off and
unplugs it; `io soundbar show NAME` shows it and the TV's view of it. The bar has the two links a
real one has, each where the board has ours, and it is not a setting: a stop and start, or a guest
reboot, has no bar, as after unplugging it.

- CEC: an HDMI-CEC audio system on the emulated CEC bus (`lgemu`'s `cec.c`, logical address 5,
  physical address `PORT.0.0.0` on the firmware's own ARC input, `system.arcPort`): it answers the
  device queries, the ARC handshake, System Audio Mode, the audio status and the volume keys, and
  Request Short Audio Descriptor with the CTA-861 descriptors of its formats, those the TV asks for.
  Boards whose CEC is `lgemu`'s adapter (`cec=1`: the O22 family and the lm21 boards).
- eARC: the receiver at the other end of the board's eARC transmitter (`lgemu`'s `earc.c`,
  `/dev/earc0`, `earc=1`: the K8 family, W22H to W24P, and the O22 family), which arccontroller
  enables with the user's eARC Support on and polls every 50 ms. The bar answers with an eARC
  connection and its capability: the HDMI 2.1 eARC capabilities data structure, version 1 and block
  1 with a CTA-861 audio data block (the same descriptors) and a speaker allocation data block (2.0,
  5.1 or, for `eac3-atmos`'s 8 channels, 7.1). A bar is an eARC one by default; `--arc-only` makes it
  one without eARC, whose discovery fails (the connection stays IDLE_2), so that the TV, which then
  calls it ARC mode, starts ARC over CEC with eARC Support on; it needs the CEC bus.

```sh
beanjar io soundbar attach tv --formats ac3,eac3,dts
beanjar io soundbar show tv --json
beanjar io soundbar detach tv
```

| `--formats` | Descriptor (format, channels, rates, byte 3) |
|---|---|
| `lpcm` | `09 07 07`: LPCM, 2 channels, 32/44.1/48 kHz, 16/20/24 bit |
| `ac3` | `15 07 50`: AC-3, 5.1, 32/44.1/48 kHz, 640 kbit/s |
| `eac3` | `55 07 00`: E-AC-3, 5.1, 32/44.1/48 kHz |
| `eac3-atmos` | `57 07 01`: E-AC-3, 7.1, 32/44.1/48 kHz, Joint Object Coding (Atmos) |
| `dts` | `3d 06 c0`: DTS, 5.1, 44.1/48 kHz, 1536 kbit/s |
| `aac` | `35 07 50`: AAC LC, 5.1, 32/44.1/48 kHz, 640 kbit/s |

The default is `lpcm,ac3,eac3,dts`; `eac3` and `eac3-atmos` exclude each other. Over CEC the TV
asks only for the formats its own profile names (W22O: LPCM, AC-3 and E-AC-3), so `arcSADs` lists
those of the bar's that it asked for; over eARC it reads the whole capability, and arccontroller
names the codecs its model takes (W22H, W22O: DD+, DD and PCM, and Atmos for Joint Object Coding).
The descriptors and the capability's data blocks are checked with `edid-decode` in the unit tests.

The host sets `lgemu`'s `soundbar` parameter (`PORT,SADS`, empty for none) and `earc_bar`
(`PORT,CAPABILITY` in hex, `PORT,arc` for an ARC-only bar, empty for none) over the VM's shell port,
and reads `soundbar_status` (`arc=`, `system_audio=`, `volume=`, `mute=`; `nobus` without the
adapter) and `earc_status` (`enabled=`, what arccontroller set from eARC Support, and `status=`, the
connection: 0 IDLE_1, 1 IDLE_2, 4 EARC; `absent` without the node); for the TV's side eim's
`cec/getCECState` (`arcActivation`, `arcSADs`, `arcProfile`) and arccontroller's `getARCState`
(`arcActivation` `ARC`, `eARC` or `None`, `earcCapability`, `earcATMOS`, the descriptors of
`advancedEarcCapability`). `--json` has them as `cec` and `earc` (whether the board has each link),
`link` (`earc` or `arc`), `port`, `sads`, `formats`, `status`, `earc_status` and `tv` (with
`tv.arccontroller`). On a board with neither link `attach` says so. While the bar is attached, its
HDMI cable's 5 V shows on its input of the HDMI receiver (`v4l2-hdmi.c`, `/dev/video12`, where the
board has it): on the hardware port whose EDID, as eim wrote it, gives the bar's physical address
(W22O: HDMI 2 is the receiver's port 3). The ARC input is the config service's `system.arcPort`,
or on the 43.x builds the system profile's `arc.port`.

What the TV does with it is the user's settings, which stay theirs. With eARC Support
(`sound/eArcSupport`) on and the output on the HDMI(ARC) device (`soundOutput` `external_arc`), it
connects the bar over eARC; CEC (HDMI-CEC, SIMPLINK, `other/simplinkEnable`, off on a new set) is
not needed for that. With eARC Support off, or with an ARC-only bar, it starts ARC over CEC, which
needs HDMI-CEC on. Measured on the PC (x86-64 guest kernel):

| Firmware (board) | eARC bar, eARC Support on | `--arc-only` bar | detach |
|---|---|---|---|
| W22H 43.22.73 (`k8hp-queue`, no CEC bus) | `arcActivation` `eARC`, `earcCapability` DD+, DD, PCM, `earcATMOS` with `eac3-atmos` | not possible (no CEC bus) | `None`, output back to the TV speaker |
| W22O 13.30.56 (`o22-number1`) | `eARC`, as W22H | IDLE_2, then ARC over CEC: `arcActivation` `ARC`, CEC `arcActivation` true | `None`, output back to the TV speaker |
| W22O 43.21.89 (`o22-queue`) | `eARC`, Atmos | not checked | `None`, output back to the TV speaker |

With the bar on eARC, `status show` reports what the firmware writes for the eARC output (an AC-3
clip): W22H writes `Sndout EARC OnOff` 1, so the output is `earc`, and E-AC-3 encoded
(`2 5 0 0 1`) in `auto` and in `passThrough` alike, PCM in `pcm`; W22O 13.30.56 writes the ARC and
eARC Outputtype together and never `EARC OnOff`, so the output stays `arc`: E-AC-3 in `auto`, AC-3
bit-exact in `passThrough`, PCM in `pcm`; W22O 43.21.89 writes `EARC OnOff` 1. The host still gets
the decoded stereo PCM: there is no IEC 61937 path. Without eARC Support, with HDMI-CEC on, ARC
over CEC as before: W22O 13.30.56 `arcActivation` true, `arcSADs` `150750550700` for
`ac3,eac3,dts`. `arcSADs` keeps the last list after `detach`, as libcecp keeps it.

### ares tools

`beanjar ares-TOOL -d NAME [ARGS...]` runs a webOS SDK tool against one VM, which needs the
`devmode` layer and must be running:

```
beanjar ares-install -d tv1 app_1.0.0_all.ipk
beanjar ares-shell -d tv1
beanjar ares-launch -d pi/tv1 com.example.app
beanjar ares-novacom -d tv1 --getkey
beanjar ares-shell --root -d pi/tv1 -r 'id; ls /var/luna'
beanjar ares-push --root -d tv1 script.sh /tmp/
```

Either implementation works, unchanged: the Rust ares-cli (webosbrew/ares-cli-rs) or the
official Node.js CLI (webos-tools/cli). The tool is `ares-TOOL` on `PATH`, else
`~/.cargo/bin/ares-TOOL`; `TOOL` is lowercase letters and `-`. `-d NAME` (`--device NAME`,
`-d=NAME`, `--device=NAME`, once, before any `--`) and `--root` (before any `--`) are beanjar's and
are not passed on; every other argument is the tool's, untouched, and so are stdin, stdout, stderr and the exit status (beanjar
execs it, so `ares-shell` is interactive). Without `-d`, `--help` is this help; with it, the tool's.

The tool runs with `HOME` set to `run/ares-home/` of the VM (owner-only, written at each run), whose
device lists name the VM alone, as the default device: `.webos/ose/novacom-devices.json` (the Rust
tools') and `.webos/tv/novacom-devices.json` (the official ones', profile `tv`), each
`{"name": NAME, "host": the VM's address, "port": 9922, "username": "prisoner", "privateKey":
{"openSsh": "devmode_rsa"}, "passphrase": "", "files": "sftp", "default": true, ...}`, and
`.ssh/devmode_rsa`, a copy of the VM's developer key. `APPDATA` (which the official tools prefer to
`HOME`) and `ARES_DEVICE` (the Rust tools' `-d`) are unset; the rest of the environment is kept.
The user's own `~/.webos` is never read or written. Neither implementation checks a device's host
key, so none is pinned (`ssh` in `show` pins it). A device list the tool changes (`ares-novacom
--getkey`, `ares-setup-device`) is written over at the next run.

`--root` makes the device root instead of prisoner: `"username": "root"`, the port of the VM's root
SSH (the guest's 22, which a layer forwards when its `layer.toml` says so: `rootssh`, or
`hbchannel` with `devmode`; `SSH_PORT`, default 6622), and `"privateKey": {"openSsh":
"id_ed25519"}`, a copy of the VM's client key in `.ssh/id_ed25519`. It needs no `devmode`. A local
VM's `HOME` for it is `run/ares-home-root/`, so that a root run and a prisoner run at the same time
never share a device list. A VM without such a layer is refused, naming the layers to add (stop the
VM, `beanjar config set NAME layers=...`, start it again; settings change only while it is off).
With the Rust ares-cli, `ares-shell -r CMD` passes the command's stdout, stderr (kept apart), stdin
and exit status through without a terminal, so it runs scripts (`ares-shell --root -d tv1 -r sh <
script.sh`); `ares-push` and `ares-pull` copy as root over SFTP.

LG's `luna-send` watches its stdin for a hang-up and exits 0 at once, before its reply is printed
(every luna-send build from webOS 6 to 26 has the watch; measured live on W22O 33.31.61.01 only; the
binaries of the others were read, not run, and no real set was reachable). It is not about stdout being a terminal. A caller with no stdin (a
script, a test harness) gives the Rust ares-cli an ended stdin, which it forwards as EOF, so the
remote command's stdin is hung up from the start. `beanjar ares-shell -r CMD` therefore runs CMD
with `exec </dev/null;` in front when its own stdin is /dev/null, closed, or a pipe or socket at
end of file; data piped in (`-r 'cat > f' < local`), an open stdin and the interactive shell are
left alone. Inside the guest, a luna-send run with `</dev/null` (as the tests' guest scripts do)
is fine for the same reason.

A VM on another host (`HOST/NAME`, or a bare name found there) is reached from this machine: the
tool always runs here, so it needs installing on this machine only, and its path arguments
(`ares-install`'s package, `ares-push`'s sources, `ares-pull`'s destination) are this machine's
paths as they are. beanjar asks the host's beanjar one internal query over the host's shared
connection (see Several machines), `beanjar _ares local/NAME [--root] --json`: the VM's layers,
whether it runs, its address there, the port to forward to (developer mode's 9922; with `--root`
its root SSH port, none without a layer that forwards it) and its developer key (with `--root` its
client key). The VM is refused with the same messages as a local one; a host whose beanjar
predates the query is named, to update.
It then adds a forward to the host's shared connection, `ssh -O forward -L
127.0.0.1:PORT:ADDRESS:9922` with that connection's `ControlPath` (with `--root`, the root SSH
port for 9922; the connection is started first if it is not running), where `PORT` is a free port
of this machine and `ADDRESS` the VM's address on the host, so no second SSH handshake is made.
The tool runs as beanjar's child with `HOME` a temporary owner-only directory (under
`$XDG_RUNTIME_DIR`) laid out as `run/ares-home/` is, whose device lists name `127.0.0.1:PORT` and
whose `.ssh/devmode_rsa` (or `.ssh/id_ed25519`) is the fetched key. Each run has its own forward
and directory, so runs against one VM or several hosts at once do not collide. When the tool ends
the forward is cancelled (`ssh -O cancel`) and the directory removed, also on an error, Ctrl-C,
SIGTERM or SIGHUP. A beanjar killed outright (SIGKILL) leaves both: its directory
(`beanjar-ares-PID-*`, PID gone) names its forward, and the next remote ares run cancels that
forward and removes the directory. Until then the forward keeps listening on `127.0.0.1:PORT`
here, as long as the shared connection lives: it closes 60 seconds after its last use, but every
other call to the host (a `beanjar list`, the web service's) keeps it open, so in practice the
next ares run is what removes it. Connection errors of a forwarded connection are the master's, which runs in the background,
so they are not shown; the tool reports its own. The exit status is the tool's (128+N for its
signal N). The host needs nothing but its `beanjar` and `AllowTcpForwarding` in its sshd (the
default).

### Shell

`beanjar shell NAME` is an interactive root shell inside webOS, and `beanjar shell NAME -- CMD
[ARG...]` runs one command there, on any running VM with the default layers: no SSH, developer
mode or network. It is the debug console's `webos-enter` (webOS's namespaces and root, its PATH,
`HOME=/home/root`), reached over a virtio-serial port of its own, `webos.shell`, that every VM
has, apart from the serial console:

```
beanjar shell tv1
beanjar shell tv1 -- ls /media/developer
beanjar shell tv1 sh -c 'ps | grep [s]urface'
tar c app | beanjar shell pi/tv1 -- tar x -C /tmp
```

- Everything after `NAME` is the command (a leading `--` is dropped); `-t` and `-T` come before
  `NAME`.
- With no command and a terminal on stdin the session gets a terminal of its own (a pty of
  webOS's devpts, `/dev/pts/N` inside it): its size follows this one's, the keys go to it raw, so
  Ctrl-C and Ctrl-Z reach the shell's foreground job and full-screen programs (`top`, `vi`) work.
  `-t` gives a command a terminal too; `-T` never gives one.
- Otherwise stdin, stdout and stderr are passed through apart, without a terminal, and the end of
  stdin is the command's. SIGINT, SIGQUIT and SIGTERM that beanjar gets go on to the command's
  process group; a third Ctrl-C gives the session up.
- The exit status is the command's (`128+N` for its signal `N`); 255 when the session ended
  without one: the VM stopped or restarted, the connection was lost, the session could not start
  (the reason on stderr). An error before the session (no such VM, not running) is status 1.
- Several sessions run at once, on one VM or several. A session whose client goes away (killed,
  a closed connection) is given up in the guest: its process group gets SIGHUP, then SIGKILL 3 s
  later.
- `HOST/NAME` asks the host's `beanjar show` where the VM's run directory is and adds a forward
  of the host's shared ssh connection to its `shell.sock` (`ssh -O forward`, as `ares-TOOL` and the
  tuner feed do), cancelled at the end.

How it works: the launcher starts the relay (`python3 -m beanjar.access.guestshell`, a child that ends
with the VM, log `run/shell-relay.log`), which holds QEMU's socket for the port
(`run/shell-qemu.sock`, which takes one client) and serves `run/shell.sock` to any number of
clients. The initramfs's agent (`guest/initramfs/src/virtio-shell.c`, log `/run/virtio-shell.log`
in the initramfs) runs each session's command with `webos-enter`. Both speak the frames of
`include/webos-shell.h`: sessions multiplexed on the one port, with stdin, stdout, stderr, the
terminal's size, signals and the exit status, and a window of 256 KiB per direction and session,
so a slow reader holds up only its own session. The agent starts before webOS and serves until
the VM stops; when webOS restarts, its sessions end with the status their commands got.

Access: whoever can open `run/shell.sock` (mode 0600, the VM's owner) is root in the VM, the same
trust as the serial console's `console.sock`. A remote session's forward listens on
`127.0.0.1:PORT` of this machine while it runs, as `ares-TOOL`'s forwards do.

`beanjar ares-shell` is unchanged: the developer-mode user over SSH (with `--root`, root over the
VM's root SSH), with the `devmode` layer.

### Stopping a VM

`stop NAME` saves the guest's logs, then sends the power-off signal: tvpowerd's `powerOff` with
the force reason `cpuCommand`, typed on the initramfs console into webOS. The set runs its own
shutdown (logs backed up, db8 stopped, filesystems remounted read-only, the micom's power-off,
never Quick Start+'s standby), and the guest's `webos-start` turns it into the machine's
power-off, so QEMU exits by itself.
From the call to QEMU's exit that took 7 to 13 s on the Pi's KVM, and on the x86 PC's TCG 5 to
26 s for WT1H and 30 s for W22O (webOS 23); the stop waits `poweroff.POWER_OFF_TIMEOUT` (90 s). The VM shows as Stopping meanwhile, so its own power-off is
the stop's.

The signal needs a guest that can take it now (`VM.power_off_refusal`, which the GUI shares): a
webOS target (`targets.takes_power_off`: NetCast 4.5 and SimpleSmart have no tvpowerd), QEMU running
it past the initramfs shell, not paused or in Quick Start+'s standby (QMP), and webOS's power stack
started. The last is read from the micom Beanjar emulates, not from the console: lgemu's micom
sees the firmware's first read of the power state (0xB1), which the power stack makes as it starts
(tvservice on WT1H, tvpowerd on W22O), 0.3 to 0.8 s before tvpowerd answers, and the `ready=1` word of `run/micom-status` carries it
to the host (`include/webos-ir.h`; the manager's own file is read, nothing is typed on the
console). The micom's other traffic does not mark it: WT1H writes `23 02` just before, but W22O's
first `23` comes minutes late while tvpowerd already answers.

The call is typed on the console only when the stop runs. It is a refusal, and the stop falls back
at once, only when tvpowerd replies `"returnValue": false`, luna-send reports an error (the service
does not exist) or webOS is not running. No reply, or a luna-send that timed out, is not: webOS has
been seen to power off after a call that printed none, so the stop waits for QEMU to exit. Where the
guest cannot take the signal, or refuses it, or where QEMU still runs when the timeout passes,
`stop` says why on stderr and stops the VM as before the signal existed: its logs saved (unless
they were), the guest's disks synced from the initramfs shell, QEMU's unit stopped (SIGTERM,
SIGKILL 20 s later). The unit's own `ExecStop` sync stays for stops that come from systemd, such as
the host shutting down. `--no-fallback` fails instead and leaves the VM running; a later stop of
the same QEMU does not send the signal again but waits for it once more (`run/power-off`, removed
when webOS refuses the call). The launcher's `stop --power-off` exits 4 when QEMU still runs after
the timeout and 5 when webOS refused the call.

`stop --unplug` is a TV losing its power: SIGKILL of everything in the VM's unit (`systemctl
--user kill --signal=SIGKILL`; without systemd, of QEMU's process group), no logs and no sync, so
what webOS has not written to its disks is lost.

### Why a VM runs

A running VM can carry a reason, one line of up to 300 characters saying why it runs, so that
another agent or the user can see whose it is before stopping it: `start NAME --reason TEXT`, or
`status set NAME reason=TEXT` on a VM that runs (`status unset NAME reason` clears it, `status
show NAME` shows it). It is the VM's status, which lasts only while it runs, where its config
(`config`) is kept. With it go the time it was set, the caller's user and an optional `--by LABEL`
(an agent's or a script's name). `list` shows it in its `REASON` column, `show` as `reason.*`,
`--json` as `reason`
(`{"text", "set", "user", "by"}`, or `null`), and the web viewer on the VM's card. `HOST/NAME`
forms run on that host, as other commands do.

`stop` refuses a VM that has a reason, printing it, unless `--force` (which prints it too). A
script that stops its own VM passes `--force` or clears the reason first. The GUI's Shut Down
dialog shows the reason and its OK is the confirmation (it passes `--force`); the web viewer's Stop
shows it in its confirmation and then stops with `--force`.

A reason is not persistent: it is `reason.json` in the VM unit's `RuntimeDirectory`
(`$XDG_RUNTIME_DIR/beanjar-vm-<tag>-<name>/`), which systemd makes when the unit starts and
removes when it stops, whether by a stop, a crash or the host's reboot. Nothing clears it, and a
new start has none. A VM without a unit (the setsid fallback without a user systemd) cannot have
one: `start --reason` is refused there before anything starts.

### What the TV sends out as audio

`status show NAME` on a running VM also reports its audio, as `audio` in `--json` (`null` when it
is off). It is read when asked, over the VM's shell port: the guest's `/proc/lgemu/sound_out`, the
table where the emulated hardware keeps what the firmware selected for its sound outputs, and the
TV's own `soundOutput` and `soundOutputDigital` settings; the streams come from the media helper's
`media-streams` file in the VM's `run/media` directory. `beanjar/devices/soundout.py` decodes the table per line of
firmware ([docs/research/tv-sound-outputs.md](../../docs/research/tv-sound-outputs.md) has the
numbers).

| Key | Value |
|---|---|
| `selected` | the TV's setting: `{"output": "external_optical", "digital": "passThrough"}`, or `null` |
| `outputs` | the outputs the hardware has open, each `{"name", "id", "codec", "channels", "bypass", "derived"}`; `null` where the hardware does not say (W18R, whose HAL keeps the connections to itself; webOS 5 and 6) |
| `mode` | `pcm`, `auto`, `forced-ac3`, `forced-eac3` or `passthrough`, as far as the hardware's values tell, else `null` |
| `arc` | whether ARC is on, `null` where the line has no such value |
| `earc` | whether eARC is on (webOS 7 and later: `Sndout EARC OnOff`, written while the TV has an eARC connection; the output then shows as `earc`), else `null` |
| `path` | which kind of table it was: `outputtype` (webOS 7 and later), `spdif-outputtype` (webOS 5 and 6), `realtek`, `mstar` or `lx` |
| `streams` | the audio streams the TV's player sends to the host: `{"port", "kind", "element", "caps", "codec", "channels", "rate"}` |
| `generation`, `writes`, `values` | the table's change and write counts and its raw values |
| `error` | why the guest did not answer (still starting, no shell relay), else `null` |

An output's `codec` is the one the firmware writes for it on webOS 7 and later (`derived` false).
On the older lines it is `derived` from the mode: `pcm` for PCM, the forced codec, or for a
passthrough the codec and channels of the one open audio stream. In `auto` it stays `null` there,
since what auto sends for each input codec is the sound processor's choice. On webOS 7 and later
PCM, auto and passthrough write the same values until a compressed stream plays, so `mode` is
`null` until then; `selected` still shows the setting. The channel count is `null` where the
firmware writes none.

### What the OLED Flex's lighting shows

On a VM whose board has the OLED Flex's gaming LED micom (lgemu's `ledmicom`: a board of tool
type LX3Q, `model=42LX3QPUA`), `status show NAME` also reports its lighting, as `lighting` in
`--json` (`null` when the VM is off or its board has no micom). It is read when asked, over the
VM's shell port: the guest's `/proc/lgemu/led_micom`, what the stock lightingcontroller wrote to the
micom per command byte, and the TV's own lighting settings. `beanjar/devices/lighting.py` decodes it
([docs/research/led-lighting.md](../../docs/research/led-lighting.md) has the commands).

| Key | Value |
|---|---|
| `selected` | the TV's settings: `{"enable": "on", "mode": "staticMode1", "brightness": 8}`, or `null` |
| `on` | whether the micom was last told to light up (command 0x21), `null` before it was told |
| `brightness` | the last brightness sent (0x2a, 1 to 10), else `null` |
| `mode` | the mode command sent last: `videoSyncMode`, `soundSyncMode`, `dynamic`, `peaceful`, `staticMode1` to `staticMode4`, `welcomeCeremony` or `factory`, else `null` |
| `color` | a static mode's colour, `RRGGBB`, else `null` |
| `writes`, `commands` | the micom's write count and, per command byte, `{"command", "count", "order", "data", "truncated"}`: the last payload in hex |
| `error` | why the guest did not answer, else `null` |

### Tags

Tags are short labels on a VM, kept with its settings in `vm.toml` (`metadata.tags`) so they last
until changed: `create --tag TAG` (repeatable), `config set NAME tags=TAG[,TAG...]` (in place of
the ones it has), `config unset NAME tags`, and `list --tag TAG` (repeatable: VMs with all of them; filtered on this machine, every host's rows
alike). A tag is letters, digits and dashes, at most 32, starting with a letter or digit; it is
kept lower-case, each once, in the order given. `list` shows them in `TAGS`, `show`, `config show`
and `--json`
as `tags` (an array), and the web viewer on the VM's card. They are labels, not settings: a
running VM's tags change too, and its launch never reads them. `clone` copies them and `rename`
keeps them. `HOST/NAME` forms run on that host.

Two tags are conventions, which nothing enforces or acts on:

| Tag | Means |
|---|---|
| `scratch` | Throwaway: safe to delete once it is stopped (an experiment's or a test's VM). |
| `keep` | Do not delete: a VM someone relies on (a reference set, a long study). |

Beanjar never deletes a VM by its tags; an agent cleaning up asks `list --tag scratch` and
deletes what it made.

### Snapshots

A snapshot saves a VM that has been set up (first use done, apps installed, settings as wanted) so
that it can be put back before a test run, without the first-use setup a `wipe` brings back:

```sh
beanjar snapshot create tv1 home       # tv1 is off
beanjar snapshot reset tv1 home        # later: tv1 as it was, everything since gone
beanjar snapshot list tv1
beanjar snapshot delete tv1 home
```

What it holds: every file of `state/` (the state disks, the EEPROM and NVM images) and
`identity/sestore.toml`, the secure-store items whose data the state's secure store holds, in
`snapshots/SNAP/` of the VM's folder, with `snapshot.toml` (when it was made, the firmware, serial
and MAC it is of). It is a full copy: each file a reflink where the filesystem has them (btrfs,
XFS), else a sparse copy, which writes only the data and leaves holes and blocks of zeros as holes.
`show` and `list` give its space on disk (`disk_bytes`) and its files' size (`size_bytes`); `create`
says how it was made (`copy`: `reflink`, `copy` or both).

What it does not hold: the VM's identity. `vm.toml` (MAC, serial, slot, settings, tags), the key
pairs, `authorized_keys`, `known_hosts`, the VNC password and the LGUDID are never saved or put back, and a
reset keeps the model key the VM has now (in whatever secure-store item it is). A reset is refused
for a snapshot of another serial (after `identity reset` or `config set serial=`) or another
firmware than the VM boots now. `SNAP` is a name as a VM's (letters, digits, `.`, `_`, `-`).

`create` and `reset` need the VM off, and hold its start lock while they copy, so no start comes
between; `list`, `show` and `delete` work while it runs. A snapshot is assembled in a hidden
`snapshots/.new-*` and renamed into place; `delete` renames it aside before it removes it. `reset`
copies the snapshot into a hidden `snapshots/.reset-*`, which, once whole, is renamed
`snapshots/.reset`: the commit. Then `state/` and `sestore.toml` are swapped with it (a few renames,
with Ctrl-C, SIGTERM and SIGHUP held off) and the old state removed. Interrupted before the commit,
the VM is as it was; killed after it (SIGKILL, a power cut), the next `start`, `wipe` or `snapshot
create`/`reset` finishes the reset first, so a VM never starts half-restored. `delete NAME` removes
the VM's snapshots with it, `rename` keeps them, and `clone` does not copy them. With `HOST/NAME`
every verb runs on the VM's host, where its snapshots are. Design and measurements:
[vm-snapshots](../../docs/research/vm-snapshots.md).

### Using the CLI from scripts and agents

Every command follows the same rules:

- **Output.** The result goes to stdout; progress (the launcher's output, a wipe's steps) and
  warnings go to stderr. With `--json`, which every command but `shell` takes, stdout is one JSON
  document: `list` an array of VMs, `show` one object with the same fields, `config show`, `library
  config show` and `host config show` an object of their keys (`null` for a default, except `host config`'s
  `firmware_dirs` and `memory_headroom_mib`, which always report their effective values), and the other commands an object
  naming the VM and what was done (`start`: `name`, `state`, `pid`, `reason`; `status`: `name`,
  `reason`). `vnc password set` and `reset` give the password's file, never the password; only
  `vnc password show` prints it.
- **Text.** Without `--json`, `list` prints aligned columns under a header row, `show` and the
  config commands one `KEY: value` line each (a nested table's keys joined with a dot: `ssh.root`;
  `-` for none). No colour.
- **Exit status.** 0 when it is done, 1 when the operation fails (the VM is not off, the host lacks
  the memory, no VM of that name), 2 for a usage error (an unknown command or option, a missing
  argument, a KEY that is not one, a group without its verb, a wrong order).
- **Errors.** Without `--json` an error is one line on stderr, `beanjar: MESSAGE`. With `--json` it
  is one JSON object on stderr, `{"error": MESSAGE}`, with `"key"` when it concerns one setting and
  `"usage": true` for a usage error; stdout stays empty.
- **No prompts without a terminal.** Only a terminal on stdin is asked anything (a VNC password).
  Otherwise `vnc password set NAME` reads stdin.
- **Order.** The verb comes first, then `NAME`, the VM's positional argument everywhere, then
  the command's arguments and options; `--json`, `--library` and `--repo` go anywhere (but in
  `shell`'s command); a duration carries its unit (`250ms`, `2s`); a secret is never an argument
  (`vnc password set` and `key model set` read a file or stdin).
- **Version.** `beanjar --version` prints `beanjar VERSION` and `beanjar-guest VERSION` and exits 0
  (an installed package: its version, such as `0.6.0+git109.geba1f197817a`; a checkout: the commit
  it holds, from git or the `source-identity.toml` a regression run writes, followed by
  `(checkout)`, or `(checkout, uncommitted changes)`). With `--json` it prints one object:
  `name` (`beanjar`), `version`, `commit` (the full commit it was built from, `null` when unknown),
  `source` (`package` or `checkout`), `dirty`, and `guest`, the guest payload's `{"version",
  "commit"}` (`beanjar-guest`'s, or a checkout's `build/guest-installation.toml`).

### Native screensaver

`beanjar screensaver start NAME [--timeout DURATION] [--json]` and
`beanjar screensaver stop NAME [--timeout DURATION] [--json]` control the native screensaver
through the initramfs root console; they need neither a viewer nor root SSH. `HOST/NAME`
uses the existing host dispatch. The timeout defaults to 60s (range 5s to 300s, e.g. `--timeout 2m`).
The default leaves margin for cold Flutter fallback startup: a controlled failed-ad recovery
on webOS 10.3.1 took 31.48 seconds including host SSH, returning Active with no saver windows.
Completion requires both the native power state and SAM's actual screensaver window types;
an accepted launch alone is insufficient. JSON reports `action`, `changed`, `fallback`,
the native `power` reply, and `screensaver_windows` (app IDs).

This path is verified on webOS 10.3.1 only. If native Ready persists for five seconds with
no screensaver window and the video-ad app has exited, the helper launches the real builtin
`com.webos.app.screensaver` once. Stop can recover that Ready state, request native screen-on,
then close only the builtin screensaver normally and wait for its actual window to disappear.
Stop tolerates the recorded Screen On processing report only after its own accepted wake.
Overlapping helper commands are refused. A builtin already running without a window is
allowed to finish launching; if another real saver races this helper's fallback, the helper
normally closes only its own builtin and verifies its window is gone before reporting success.
Already Active with no transition and no screensaver window is unchanged. Unsupported API
schemas, other power states, unrelated transitions and API errors fail explicitly. The VM
must be running webOS, outside standby, pause, startup and pending shutdown. These commands
do not change automatic stop/power behavior or ad/privacy settings.

## Several machines

One `beanjar` can drive the VMs of other machines over plain SSH. Nothing runs on the other
machines but their own `beanjar`: no server, no daemon, no protocol. A remote call is the system
`ssh` running `beanjar COMMAND ... --json` there, whose JSON is read here; there is no remote
viewer (use `beanjar vnc start`).

Requirements: key-based (non-interactive) ssh login to each host, and `beanjar` installed on it
and on the `PATH` of a non-interactive login. A host's `beanjar` that is too old for what is asked
(`screenshot NAME -`, added with this feature) fails with a message saying to update it; there is
no version negotiation.

**Versions across machines.** A command sent to a host runs that host's code, which may be another
commit's than this machine's. Before it, `beanjar` asks the host's `beanjar --version --json` (kept
for 60 seconds in the shared connection's directory; a host that does not answer within 10 seconds
is skipped, and kept so for the minute) and compares it with its own: the host's
`beanjar` with this one, and the host's guest payload with the host's `beanjar`. Each difference is
one `beanjar: warning: ...` on stderr, naming both versions and the command that fixes it, and with
`--json` an item of a `drift` array added to the command's object (its result on stdout, or its
error on stderr): `host`, `package` (`beanjar` or `beanjar-guest`), `found` and `expected` (each
`{"version", "commit"}`) and `fix`. A difference never stops the command: the Pi keeps a guest
built elsewhere, and a package release pairs a host with the newest guest of its `0.Y`. A host
whose `beanjar` predates this says only its version, whose `g<hex>` is compared. `vnc pipe`,
`remote pipe` and `mitm` do not ask. `beanjar host show NAME` asks now and shows the same.
Where a refusal comes from such a difference, its message says so: `start`'s module parameter
check names the host's `beanjar` and its guest payload when they come from different commits, and
the command that installs or builds the matching guest.

**Hosts.** Each is a table of this machine's `config.toml` (`$XDG_CONFIG_HOME/beanjar/config.toml`,
next to `max_running`), made by `beanjar host add`:

```toml
[hosts.pi]
uri = "ssh://user@vmhost"
```

`ssh://[user@]host[:port]`, where `host` may be an alias of `~/.ssh/config` (or `[IPv6]`); no other
scheme. A host's name is 1 to 32 letters, digits, `_` or `-`; `local` is this machine and cannot be
a host. The host must be in `known_hosts` already (ssh never asks).

**SSH.** `ssh -T -o BatchMode=yes -o ConnectTimeout=5 -o ServerAliveInterval=5 -o
ServerAliveCountMax=3 -o ControlMaster=auto -o ControlPersist=60
-o ControlPath=$XDG_RUNTIME_DIR/beanjar-ssh/%C [-p PORT] [-l USER] -- HOST beanjar ARGS`
(`/tmp/beanjar-ssh-UID` without `XDG_RUNTIME_DIR`), so the calls of one minute share a connection.
A host that goes silent (down, its sshd stuck) ends the connection, the shared one included, after
15 seconds without an answer, so no call waits on a dead connection.
Arguments are quoted for the remote shell. The remote's stderr (progress) is passed on, each line
prefixed `HOST: `; with `--json` it is passed as it is, after the command ends. A remote exit status
of 0, 1 or 2 is ours; ssh's own failure (status 255) and a missing `beanjar` (127) are an error
naming the host.

**list.** With hosts configured, `list` asks every host in parallel and lists this machine's VMs
first, then each host's in config order. Text has a `HOST` column first (`local` for this machine);
`--json` rows always have a `host` key (`null` for this machine's, the host's name otherwise). A
local VM whose firmware does not answer within 3 seconds (a collection on a network mount whose
server is gone) is warned about once per collection and listed without its platform and RAM. A
host is asked `list --local`: its own VMs only, never those of hosts it has itself, so a machine
reached both directly and through another host (the PC managing the boards that the Pi also
manages) is listed once, under its own name.
A host that fails (unreachable, no `beanjar`, output that is not JSON, a non-zero status, no answer
within 30 seconds) is a `beanjar: warning: host NAME: REASON` on stderr and its VMs are left out; the exit status is 0
unless every source failed (then 1, with nothing on stdout). `--all` is forwarded, so it means each
machine's whole `beanjar.slice`. `--local` asks no host and prints what `list` printed before
hosts existed; `--host HOST` lists one host (`--host local` this machine). The GTK manager builds its
library directly and ignores hosts. In a remote row `library`, `unit`, `fw` and `run_dir` are paths
on that host.

**Addressing.** The commands that take a VM (`show start stop delete rename prepare wipe
screenshot elements shell`, and `config`, `status`, `snapshot`, `vnc`, `screensaver`, `remote`, `identity`,
`key`, `sensor`, `io tuner`, `io usb`, `mitm`) take `NAME` or `HOST/NAME`; `local/NAME` is this machine's. A bare `NAME` is looked up on this machine and on every
host (a read-only `config show local/NAME`, in parallel), so a duplicate is never hidden: found in one
place, the command runs there; in several, it is an error listing the `HOST/NAME` forms; nowhere,
`no VM named NAME`. With `--library DIR` given, a bare `NAME` is that library's VM and no host is
asked (the GTK manager, which lists one library, always passes it). A host that cannot be asked is warned about and left out, so with a host down
a bare name still works but is not proven unique; `local/NAME` and `HOST/NAME` ask nobody. With no
host configured nothing is asked.

The same subcommand then runs on the host as `local/NAME` (its own VM, never one of its own hosts')
and the same options (without
`--library` and `--repo`, which are this machine's paths), and its stdout, stderr and exit status
(0, 1, 2) are relayed. Exceptions:

- `show` always reads the host's JSON and adds `host` (first); its `dir`, `identity`, `ssh.*`,
  `inspector.password_file` and `vnc.password_file` are paths and commands on that host, as it
  reports them.
- `screenshot HOST/NAME FILE` runs `screenshot NAME -` there (the PNG alone on stdout) and writes
  `FILE` here; `--json` has `host`, and `taken_by` is `null`. `screenshot NAME -` also works on one
  machine, writing the PNG to stdout.
- `vnc password set HOST/NAME [FILE]` reads the password here (FILE, stdin or a prompt) and sends
  it on the remote's stdin, never as an argument.
- `viewer open` and `viewer close` of a remote VM are refused, pointing at `beanjar vnc start
  HOST/NAME` and the VM's address in `show`.
- `ares-TOOL -d HOST/NAME` runs the tool on this machine, through an ssh port forward to the
  VM's developer-mode port on the host (with `--root` its root SSH port; see ares tools).
- `shell HOST/NAME` runs here, through an ssh port forward to the VM's `shell.sock` on the host
  (see Shell).
- `io usb attach HOST/NAME --device|--camera` runs here, where the device is, asking the host's
  `io usb attach`, `list` and `detach` and forwarding to the socket there (see USB devices).
- `io tuner map|unmap HOST/NAME` makes the streams here and feeds them to the host (see Tuner).
- `create`, `key model set NAME FILE` and `identity authorized-keys add NAME --mine` are local
  only for now (a directory, a file, the keys of this machine): they refuse `HOST/NAME`.
- `clone SRC DEST` takes a `HOST/NAME` for either and runs here, asking the hosts' `beanjar show`
  and copying over their shells (see Commands).

## GUI

`python3 -m beanjar_ui.manager [--library DIR] [--ui-dir DIR]` is a libadwaita window laid out like
VirtualBox or GNOME Boxes:
- the VM list in a sidebar: each VM's name, firmware, memory and CPUs, and its state (a green
  dot while it runs, with its uptime and CPU%; a display icon while its viewer is open). A
  status page offers New VM while there are none. Double click or Enter on a
  VM connects to it, or starts it. In a narrow window the list slides in from the header's
  sidebar button
- the actions for the selection: Start, Connect or Shut Down… as one icon button in the header bar
  (its tooltip names the action; the rest in its dropdown); Settings…, Rename, Clone, Wipe, New
  Identity and Delete in the header's VM menu (⋮); every action in the list's context menu; New VM
  as the header's + button (Ctrl+N); the main menu (☰) has the reconnect option and About; the
  title's tooltip is the library's folder
- beside the list, the details: Summary (grouped firmware, hardware, connections, the traffic inspector of a mitm VM, and identity), Console (a live tail of the serial console) and Output
  (the last operation's); results show as toasts

New VM and Settings have a page per category, picked in a sidebar: General (name, firmware),
Hardware, Display & Audio, Network (link and mode; in Settings also the TV's MAC address and serial
number, the service ports' listen address with link nat or the LAN address with link lan, and
its model key, shown as present or not, never its value) and
Layers; in a narrow window the sidebar comes first and a category opens its page. An error shows over every page, names its
page and marks it in the sidebar.

General's Firmware group picks the source, each with a chooser button: in New VM a firmware file
(`.epk`) to extract or a directory of extracted paks, either one becoming the VM's own `firmware/`;
in Settings the VM's own firmware or a directory used in place (an explicit `fw`). The group says
what the choice costs (the `.epk`'s or the paks' size, whether they are hard links or a copy, the
space free in the library). A New VM that extracts or copies runs in the
background with its progress over the dialog (`python3 -m beanjar.newvm`, the GUI's own; Cancel,
or closing the progress, cancels it); once done both close. After a failure or a cancel nothing is
left, and Close goes back to New VM, with the refusal in its banner.

Layers lists the checkout's optional layers (`guest/layers/`, compatible `build/components` variants; installed packages use `/usr/share/beanjar/guest/layers`) with a switch each, in the order they go into `layers`: the first is stacked on
top, so its files win where two layers have the same file (gl and headless); Up and Down change
it. The layers every VM gets (webos, soc, common) follow, fixed. A layer in the
VM's `layers` that has no `layer.toml` here is shown, but Save refuses it until it is switched off.
Each line is the layer's `layer.toml` description, with what it needs and what it cannot be
combined with; `create`, `set`, Save and the launcher refuse a selection that breaks those rules
([layer metadata](../../docs/dev/architecture/firmware-targets.md#layer-metadata-layertoml)).

### Homebrew Channel

The `hbchannel` layer makes a VM a rooted TV with Homebrew Channel installed and elevated, for
testing real apps: the app is a developer app, as on a rooted TV, and its service runs as root. Its SSH server is root SSH on `127.0.1.N:6622` (`SSH_PORT`), with the VM's own
keys: the client key `id_ed25519` and the `authorized_keys` above log in, and the host key is
`rootssh_host_key`, so `known_hosts` pins it as for `rootssh`. The layer is built once per
Homebrew Channel release: `layers=normal,devmode,hbchannel@0.7.3` picks one, plain `hbchannel`
is the default (0.8.0), and an unknown version is refused with the list. `config show` shows
`layer_versions.hbchannel` and `list` a `VERSIONS` column. HBC can update itself in the VM as on a
TV (its service's `install`, which writes into the VM's state); a newer copy there then stays
whatever the layer's version, until `beanjar wipe`. `beanjar show NAME` prints the
same `ssh` command. It needs `devmode` (HBC installs apps through developer mode's
`dev/install`) and cannot be combined with `rootssh` (both are the guest's port 22); its
`layer.toml` says so, and `set` and the launcher refuse a selection that does not follow it. Its root telnet (guest port 23) is not forwarded. HBC's placeholder
password (`alpine`) is not set, since the keys are provisioned
([firmware targets](../../docs/dev/architecture/firmware-targets.md#homebrew-channel-hbchannel)).

Shortcuts: Ctrl+N New VM, Ctrl+Return Start, Ctrl+Shift+Return Connect,
Alt+Return Settings, Delete Delete, Ctrl+Q quit, F10 the main menu. Shut Down, Wipe, New Identity
and Delete ask first. The style follows the desktop's light or dark setting.

Shut Down… asks how, as VirtualBox's close dialog does: "Send power-off signal", the default, or
"Unplug (may lose data)" (`stop --unplug`). The dialog opens at once, with whether the guest can
take the signal already read from QMP and the micom's `run/micom-status` (Stopping a VM; nothing
is typed on the guest's console). Where it cannot, the signal's radio is greyed out and says why
(for example "webOS is still starting"), and Shut Down runs `stop`, which syncs the disks and
stops the VM instead. With the signal the window runs `stop --no-fallback` and shows the VM as
Stopping; if it has not powered off in time, or webOS refused the call, the window asks whether to
wait longer or unplug it, instead of a toast. Closing
the window or a viewer never stops a VM.

Start is greyed out, with the reason as its tooltip, when the host's free memory does not cover the
selected VM (other VMs may run). The summary shows each VM's address. With "Reconnect the Viewer When the VM Restarts"
(main menu) on, a viewer you opened comes back after the VM is restarted; one you close stays closed. The
viewer is its own process, as with `viewer open`, so it and the VMs outlive the window. The window finds
them again from their pidfiles.

The window follows the library whoever changes it: a VM the CLI (or an agent, or another window)
makes, starts, stops, sets or deletes, a VM folder moved into `vms/` or removed, a `vm.toml` that
is edited by hand. Each shows within about a second: file monitors on `vms/`, each VM's
folder and its `run/` (armed again when one is made anew) trigger a refresh, and a timer
refreshes anyway every 5 s; each second it only updates the running VMs' uptime and CPU%, so an
idle window costs next to nothing. A VM whose `vm.toml` does not read is listed with the reason, and only Delete and
its folders are offered; the others are unaffected, and a refresh that fails is logged to stderr
rather than stopping the timer. systemd is asked from a thread, never on the main loop.

Below the library's VMs, "Running Elsewhere" lists, read-only, the VMs up in `beanjar.slice` that are
not the library's, as `list --all` does: another library's (a development library's), with that
library, and a unit of none (a regress target), with its run directory; each with its state and
address. A library VM there has Shut Down, which opens the same dialog and runs `beanjar --library <its library> stop`.

The window's and the dialogs' layouts are Blueprint files, `host/ui/data/manager/*.blp` (the window and a row of
its VM list; the confirmation of Shut Down, Delete, Wipe and New Identity; New VM and Settings, and a row of
their layer list; New VM's progress), templates of the
classes in `host/ui/beanjar_ui/manager/widgets.py`; `gui.py` holds what they do. The host build compiles them into
`build/manager/beanjar.gresource`. To try out an edited layout without a build, give the GUI the
directory of `.blp` files, which it compiles at start with blueprint-compiler (Debian's):

```sh
PYTHONPATH=host/core:host/ui python3 -m beanjar_ui.manager --ui-dir host/ui/data/manager
```

`--ui-dir` also takes a directory of compiled `.ui` files (all of them), e.g. from
`blueprint-compiler batch-compile /tmp/ui host/ui/data/manager host/ui/data/manager/*.blp`, run in the
checkout.

## Tests

`meson test -C build/host --suite manager` (`--suite launch`, `--suite ui`) runs these, or `pytest` on
them ([testing](../../docs/dev/guides/testing.md)):
- `tests/unit/manager/test_manager.py`: the core and CLI (and the GUI's New VM worker) against fakes of
  epk2extract (its `rootfs.pak` is `trust-rootfs.pak`, a tiny afro SquashFS with two trust bundles),
  the launcher (`fake-vm`, which runs
  the real `beanjar.unit`), QEMU (`qemu-system-fake`: pidfile, QMP) and the viewer. No VM and no GUI.
- `tests/unit/launch/`: the host runtime. The QEMU command lines of `qemu.py`, pinned in
  `launch-argv.json` (recorded from the shell launchers it replaced; `test_argv.py`), and
  (`tests/integration/launch/test_launcher.py`, with the guest build installed) the real
  `host/scripts/vm-launcher start|status|stop --dir` with the fake QEMU, as a unit and through the
  setsid fallback; the
  console bridge; the per-VM layer; firmware file reads through `unsquashfs`; a real uncompressed image of the
  checkout's `ROOTFS=uncompressed` (with squashfs-tools).
- Every test that starts a unit names it `beanjar-vm-test<pid>-...` (or `beanjar-vm-test<pid>_<hash>-...`,
  a VM of a test library named `test<pid>`) and stops it however it ends,
  a SIGTERM included; one left by a test process that was killed is swept by the next run
  (`tests/lib/testunits.py`).
- `tests/integration/ui/check-gui.py`: the window, driven with direct calls, on a private Xvfb, with the
  layouts of `build/manager/beanjar.gresource` (`BEANJAR_GRESOURCE`: the Meson test's own). For
  screenshots (the window, with each dialog over it):
  `xvfb-run -a tests/integration/ui/check-gui.py --shots build/manager`.
- `tests/integration/ui/check-gui-reactive.py`: the window following changes made from outside it (the
  CLI in another process, folders moved in and removed, a `vm.toml` that does not read, a `run/`
  made anew, with only the monitors running), within 3 s each, and another library's VM listed
  elsewhere and shut down from there.


### MITM inspector in the manager

The Summary of a VM whose network is mitm has a Traffic Inspector card: that VM's own mitmweb
(each such VM runs one, on its own port and with its own password; only the CA certificate it signs
with is the library's, shared by its VMs, as the card says). Its heading has the inspector's
status and URL with Copy URL and Open in Browser (logged in with the password); folded under it,
the password with Copy Password, and with link lan the VM's leased LAN address. A link lan VM's inspector is also on
that address: its URL is the LAN one (Copy URL), with the VM's own address beside it as "On this
host", which Open in Browser uses, since the host cannot reach its own macvlan children. A VM that is off shows one line,
"Starts with the VM (MITM)", never a URL from an earlier run; a VM with mode direct or link off has
no card. The folded details say which mitmproxy the inspector runs, or why it cannot and how to
install it (mitmproxy is the user's own, 12.0.1 or newer), and end with a bounded tail of the
proxy's log; creating, saving or starting a mitm VM without a usable one says so too. The inspector's retention and body
size limit are settings, shown on the Network card.

The endpoint comes from the running QEMU's environment, so custom ports and settings edited during
a run do not point the window at another service. Wildcard bind addresses are presented as
loopback addresses for local use. Background HTTP checks determine readiness; an inspector that
does not answer cannot be opened. The full mitmweb interface opens in the system browser.

### VNC in the manager

While `beanjar vnc start` serves the selected VM, its Summary has a VNC card after the traffic
inspector: the address (with link lan the LAN one, and the VM's own as "On this host") with Copy
Address and Open (the system's vnc:// handler), and folded under it the password with Copy
Password. The window does not start VNC itself.


### Web viewer

`beanjar-web` (package `beanjar-web`, or `host/web/bin/beanjar-web` from the checkout) serves the
VMs in a browser. `beanjar-web start` runs it as the systemd user unit `beanjar-web` and prints the
URL to open (`status` prints it again, `stop` stops it; `beanjar-web serve` runs it in the
foreground). It listens on 127.0.0.1:8059 (`--listen`, `--port`); `--auth token` (the default, a
token in the URL kept in `~/.config/beanjar/web.token`), `password` (a login page,
`web.password` beside it) or `none` (loopback only); `--certificate` and `--key` serve HTTPS. The list opens a running
VM's picture (noVNC, through `vnc pipe`) with Back, Home, Settings, Screenshot, Fullscreen, a
speaker toggle (the VM's sound, through `vnc audio-pipe`, muted until clicked) and a
Quality control, and Remote opens the TV remote in a popup window (through `remote pipe`; holding a
key is a long press). It starts VNC for a VM itself and shows the manager's refusal while the GTK
viewer holds it. A VM on another host works the same way, over the manager's ssh
([web viewer](../../docs/dev/architecture/web-viewer.md)).


### VM Summary

The Summary (host/ui/beanjar_ui/manager/summary_ui.py; what it shows, for users, is in
[docs/user/vms.md](../../docs/user/vms.md)) opens with four stat tiles and the TV's picture. The
tiles' live figures are the ones the list rows compute every second (uptime and CPU% from
/proc/PID/stat, the GPU's use from gpu.Sampler) plus QEMU's resident memory
(capacity.rss_mib); "Last run" for a VM that is off is the newer modification time of
logs/console.log and logs/qemu.out (summary_facts.last_run). The picture is `beanjar screenshot
NAME - --frame 384x216` while the VM runs and `--last` once when it is off, the same commands the
web list runs, each in a worker thread, one at a time per VM, at most every 5 s and only while the
Summary is shown, the window is active and no viewer is open (picture.Pictures). With neither a
viewer nor beanjar-vnc running, a screenshot runs a headless viewer for about 3 s; one costs about
0.25 s of CPU on the development PC, so the 5 s refresh takes about 5% of a core while it is seen.
A portrait model's picture is turned by rotation.status (what `beanjar sensor show rotate --json`
reports). Clicking it opens the viewer, or sends an open viewer SIGUSR1, which it presents its
window on (only when /proc says it catches the signal).

The window's breakpoints set the layout: four tiles in a row beside a 240x135 picture from 1061 sp,
the tiles 2x2 beside a 256x144 picture down to 641 sp, and below that the tiles 2x2 with the
picture the full width under them and the cards in one column. A problem banner above the page
(summary_facts.problem) shows one problem at a time. The Hardware card shows audio and free page
reporting only when they differ from vmconfig's defaults; the Identity card has MAC and serial copy
controls and shows only whether a custom model key is installed; the Network card has the address
with the forwarded ports (VM.forwards). Files and runtime details keep full paths, the systemd
unit, process IDs and the guest's restarts (lifecycle.restarts) in a collapsed section.
