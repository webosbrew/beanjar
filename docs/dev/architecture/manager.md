# VM manager and host runtime

Beanjar manages stock webOS, NetCast and SimpleSmart VMs. The Python package in
`host/core/beanjar` owns VM configuration and the host runtime; `host/scripts/vm-launcher` is the
checkout launcher. The [manager guide](../../../host/core/README.md) documents commands and
settings.

## Library and firmware

The library contains configuration, a shared proxy CA, and one directory per VM. Each VM has
its configuration, supplied firmware paks and import manifest, writable state, identity,
runtime files and saved logs. Import resolves the firmware's [target](firmware-targets.md);
unknown model/release combinations are refused.

Firmware belongs to the VM unless an external directory is explicitly selected. Paks are
read-only inputs. An external firmware directory is booted in place; generated cache data
belongs to the VM, not that directory. The GUI and the CLI extract an EPK into the VM's own
firmware, keeping only the paks its target reads; they also accept already extracted paks. The
manager does not build a firmware image or include LG firmware.

A VM's name is its folder's name, `vms/<name>`, and nothing else records it: `beanjar rename`
renames a VM that is off by renaming that folder, the one atomic step that commits it, under the
VM's start lock and the library's slot lock. Its identity, slot (and so its address), settings,
state and logs stay as they are, in the folder; only the public keys' comments, which name it, are
changed before the commit, and its stale `run/unit` is removed. What is derived from the name, its
unit and with link lan its DHCP hostname, follows at its next start. A VM does not change host or
library by a rename; `clone` makes a copy elsewhere, as another TV.

A VM's tags (`metadata.tags`, `beanjar config set NAME tags=...`, `list --tag`) are labels kept with its settings,
copied by `clone` and never read by its launch, so they change while it runs too. `scratch`
(throwaway) and `keep` (do not delete) are conventions only: nothing deletes a VM by its tags
([manager README](../../../host/core/README.md#tags)).

## Launch and lifecycle

The manager runs on the QEMU host: local TCG on x86_64 or KVM on a suitable arm64 host.
The launcher resolves the target, layers and disks, assembles the host share and boot manifest,
and owns QEMU and its helper processes. The host runtime uses a systemd user unit where
available, with a process-session fallback.

Which of the two holds a VM's processes is the runner (`host/core/beanjar/runner.py`). Both run the
same main process, `python3 -m beanjar.unit run`, with the same environment; a runner only starts
it, says whether it is up, and stops it, and `launch.Bg` drives every runner through those three
calls. A start uses the chosen runner; a stop and the running check ask every runner, since a VM
is stopped by the one it was started with. The choice is automatic (systemd where a user systemd
runs, else a session; `VM_SYSTEMD=0` turns systemd off), unless the machine's setting
`beanjar host config set runner=systemd|session` or `BEANJAR_RUNNER`, which wins, names one. A
named runner that cannot run here fails the start rather than falling back, so a forced choice
is never silently ignored. The seam exists for runners other than these two, such as a container
or a native macOS host, which would supply the same three calls.

QEMU's default accelerator is KVM only on an aarch64 host whose `/dev/kvm` the user can open;
otherwise TCG (an aarch64 container or board without KVM no longer picks KVM and fails).

A manager VM and its viewer have separate lifetimes. The viewer attaches to the VM's D-Bus
display and runtime sockets and may close without stopping the TV. Stopping the VM collects
logs, then asks webOS to power the set off (tvpowerd's forced `powerOff`)
and waits for QEMU to exit by itself. Whether the
guest can take that signal is read from QMP (paused, Quick Start+ standby) and from the micom
Beanjar emulates, which notes when the firmware's power stack starts, not from the console. A guest
that cannot (a legacy platform, early boot, standby), whose webOS refuses the call, or that does not
power off in time has its disks synced and QEMU stopped instead; a call that gets no reply is
waited out, since webOS has powered off after one. Unplug kills the VM at once, without logs or a
sync. The GUI's Shut Down dialog offers the signal and Unplug
([manager README](../../../host/core/README.md#stopping-a-vm)).

A running VM may carry a reason (`start --reason`, `status set NAME reason=TEXT`), which `stop` prints and refuses
without `--force`. It is not persistent by design: it is a file in the VM unit's
`RuntimeDirectory`, so it lives exactly as long as the unit and systemd removes it at a stop, a
crash or a reboot, with no cleanup code; a VM without a unit cannot have one
([manager README](../../../host/core/README.md#why-a-vm-runs)).

### Prepared board data

What a start needs from the firmware, the model's board and the VM's board settings is worked out
ahead and kept with the VM, in `<vm>/prepared/` (`host/core/beanjar/prepare.py`): the partinfo
disk's partition map, the image a blank EEPROM becomes (the firmware's own default records
programmed with the board's values, or on webOS 5 and later lowlevelstorage's records from the
firmware's own programs), LLS's probe output for a later debug-mode change, the viewer's local key,
and the chip modules' parameters and host-share files derived from the firmware. It is made when the
VM is created (before it is published, so a failure creates nothing), when `fw`, `broadcast`,
`debug_mode` or `serial` changes, by `identity reset` and a local `clone`, and by
`beanjar prepare NAME`; all of them run the launcher's `prepare`. The GUI's Settings dialog saves
the settings, then prepares in the background (`beanjar prepare NAME --if-stale`, a progress dialog
over Settings): the window stays responsive, the progress dialog can be neither cancelled nor
closed until the work ends (so a closed dialog never leaves the settings saved with their data
unprepared), and a failure is shown in Settings (the settings stay saved; a start prepares again). Shared per-firmware caches stay
where they were: the VM's `FW_CACHE` (the pak digests and the original map) and the host's LLS cache.

A start only reads it. Its record names what it was made from: the inputs (the module's version,
the firmware directory, the target, the broadcast standard, the serial, the debug mode and a digest
of the target's table rows), and the size and mtime of each file it came from (every pak, the
board's `board.cmdline`, the LLS probe and, where the firmware's LLS is a nyx module, the guest
kernel, initramfs and `lgemu.ko` it runs under; the derived-parameter helpers). Checking that
costs a few stats and no firmware read. A record that is missing or stale is made again at the
start, which says so and why (`prepared board data made with another serial: preparing it
again`): the checkout's development and regress VMs have no create step, and an update of the
tree changes every VM's inputs at once, so failing would only move the same work behind a manual
command.

On an RK3399 board with the firmware on NFS this moved about 17 s of a first start (the LLS key's
extraction 10.5 s, the generators 2.6 s, the partition map's digest 1.6 s) to `beanjar create`.

Left at start, as they depend on more than the firmware and the board or are already cached:
the guest layer images (built once per tree into the host cache, `layerimage.py`), the state disks
and the per-VM layer (identity, model key), the host share, the EEPROM's sync with the board's
words and serial, and the proxy's leaf CA check.

## Several VMs at once

VMs run side by side: each binds its host ports on its own address, `127.0.1.<slot>`, at the
TV's stock numbers ([networking](network.md#host-addresses-and-ports)). Everything else of a
running VM was already its own: its `run/` directory with its sockets and pidfiles, its systemd
unit (`beanjar-vm-<tag>-<name>`, in its library's slice `beanjar-<tag>.slice` under
`beanjar.slice`; the regress targets' `beanjar-vm-regress-<target>`) and runtime directory, its state disks (QEMU
locks them), its private D-Bus, viewer and media helper, and its network (a private slirp, or a
namespace of its own).

- **Admission.** `Library.start` (CLI and GUI) starts a VM if the host's `MemAvailable`, less
  what the library's other starting and running VMs may still claim, covers the VM's RAM (`-m`:
  `vm.toml`'s memory, else its model's) and `capacity.memory_headroom_mib()` more (the host setting
  `memory_headroom_mib`, default `capacity.START_HEADROOM_MIB` = 1024 MiB). The default is unchanged;
  hosts may tune it without editing VM settings. A
  VM's claim (`capacity.claim_mib`) is its unit's `MemoryHigh` (KVM: its RAM and the headroom; before
  its unit exists, that sum; a TCG VM has no `MemoryHigh`, so its claim is its RAM, the headroom and
  QEMU's translation-block cache, 1 GiB or `TCG_TB_SIZE`, which a TCG start needs free too) less what the unit holds (`MemoryCurrent`, else QEMU's RSS). The check
  and the taking of the start lock happen under `<library>/.admission.lock`, so two starts at
  the same moment cannot both be admitted on the same free memory. Otherwise it refuses and says
  how much is needed, available and claimed; the GUI greys Start with that reason as its tooltip.
- **How many at once.** `max_running` in this machine's `$XDG_CONFIG_HOME/beanjar/config.toml`
  (`beanjar host config set max_running=N`; unset, no limit) caps the VMs running on the host,
  since they share its CPU, GPU and memory (four GL VMs at once on the PC left services
  unstarted). Every unit up in the `beanjar.slice` tree counts (`vmunits.vm_units`): every library's,
  the development VMs' and the regress harness's. A VM run by the process-session fallback (no user
  systemd) has no unit and is not counted. A start beyond the limit (`Library.start` for the CLI
  and the GUI, `vm-launcher start` with or without `--dir`) is refused before anything changes:
  no QEMU, no helpers, no start lock, no identity or development VM made. The message names the
  running VMs and the limit, and gives the command that raises it; the GUI greys Start with it as
  the tooltip. The VM being started is not counted, so a restart of one that runs needs no free
  place. Races: the launcher asks again under `.running.lock` (`fcntl.flock`, beside the setting)
  and holds it until `systemd-run` has made the unit, so the next start's count includes it; two
  starts at once cannot both take the last place. Only that second check is locked: the early ones
  give the refusal before anything changes, and the lock is not held across a guest build or a boot.
- **Resources.** Each VM's unit has `CPUWeight=50` (the host's own processes, at the default
  100, come first under contention) and, under KVM only, `MemoryHigh` at its RAM and the headroom: above it the
  kernel reclaims and throttles the unit instead of OOM-killing QEMU as `MemoryMax` would. Under
  TCG QEMU adds a TB cache and GL buffers to the RAM (a 2 GiB k8hp-ombre VM reached 3.27 GB anon
  and stalled in D state at `mem_cgroup_handle_over_high`), so the unit has no `MemoryHigh`. The
  user manager delegates `cpu memory pids` to user units on Debian 13. Measured under TCG on
  W18R (1536 MiB, `gl`): QEMU's RSS 2.1 GiB, the unit's peak 2.4 GiB of its 2.5 GiB bound.
- **One start per VM.** The VM's `start.lock` (`{"pid": the starting process}`, made with
  `O_EXCL`) is valid while its starter lives or the VM runs, and is released at stop, so the same
  VM is never started twice; a stale one is removed.
- **Shared caches.** What VMs share is built under an `fcntl.flock` lock beside it, checked
  again once the lock is held, then staged and renamed: the firmware cache's partition maps
  (`partinfo-<key>.img.lock`) and uncompressed images (`<pak>-<key>.img.lock`; `<pak>.src`
  is written by rename), the LLS cache's entries (`build/lls/<firmware>/<key>.lock`), and a
  library's proxy CA (`<library>/ca.lock`).
- **Regression.** Its parallel lanes are development VMs, one `INSTANCE` per target, each with a slot
  of its own; they run `NET_MODE=direct` or `mitm` side by side.

## Several machines

The CLI can drive other machines' VMs, with nothing added to them. A host is a `[hosts.NAME]`
table (`uri = "ssh://[user@]host[:port]"`) in the machine config, beside `max_running`; a call is
the system `ssh` running the host's own `beanjar COMMAND ... --json`, reusing one connection
(ControlMaster/ControlPersist, a socket under `$XDG_RUNTIME_DIR`), with BatchMode and a connect
timeout. No server, daemon or protocol: the remote CLI's JSON and exit status (0, 1, 2) are the
interface, and a remote too old for a command fails with an "update beanjar there" message instead
of a version negotiation. `beanjar/remote.py` holds the hosts, the ssh command line and the name
resolution; `transport` is the one function that touches the network, replaced in the tests.

`list` merges the machines' rows (a failing host is a warning, not a failure). A VM is `NAME`
or `HOST/NAME`; a bare name is looked up everywhere so that no duplicate is hidden, because VM names
are unique per library only. A host is always asked about its own VMs (`list --local`,
`local/NAME`), never about the hosts it has itself, so one machine can manage another that manages
a third without the third's VMs appearing twice. There is no remote viewer: the picture of a remote VM is
`beanjar vnc start` and a VNC client, or `screenshot`, which makes the host print the PNG on stdout
(`screenshot NAME -`) and writes it here. The GTK manager builds its `Library` directly and stays
local. Commands that need a file or directory of the machine they run on (`create`, `key model
install`, the viewer) stay local. The ares tools (`beanjar ares-TOOL -d NAME`, `beanjar/ares.py`)
always run on this machine, with `HOME` a private directory whose device lists name that VM alone;
a remote VM is reached through an `ssh -L` forward of its developer-mode port, made for the one
command, with its developer key read from the host, so only this machine needs the tools and path
arguments stay local. USB devices follow the same split (`beanjar/usb.py`): `io usb attach` on the
VM's host hot-plugs a QEMU `usb-redir` device whose usbredir socket is a unix socket in the run
directory, and `io usb attach`, where the device is, runs `usbredirect` against it through a relay
(local) or an `ssh -L` forward of the shared connection to that socket (remote), and detaches at
the end. `io usb attach --image FILE` makes a disk image a USB stick instead (`blockdev-add` and
`usb-storage`); the image is a file of the VM's host, never copied, as the guest writes to it.
`io usb gamepad attach` is the exception to the split: a virtual Xbox 360 pad
(`beanjar/gamepad.py`, `host/helpers/virtpad`) runs on the VM's host against the socket and stays
after the command, so that `io usb gamepad press` can reach it later. A
hot-plugged device is the running VM's, not a setting: a guest restart keeps it (the unit records
it and puts it back on the new QEMU, `beanjar/lifecycle.py`), a new start of the VM drops it.
See the [manager README](../../../host/core/README.md#several-machines).

## Identity and presentation

VM identity is separate from firmware and state: [identity](identity.md) describes MAC/serial,
SSH keys, model-key storage and the per-VM layer. The platform title comes from the resolved
release (`webOS`, `NetCast`, or `SimpleSmart`), with a marketing version only where known.

The GTK manager and [viewer](viewer.md) use generated layouts from the host build. Core
configuration and runtime operations remain usable through the CLI without GTK.

## Source boundaries

`paths.py`, `targets.py` and the firmware helpers resolve artifacts; `share.py` assembles the
guest's inputs; `qemu.py` plans QEMU; `launch.py` owns runtime processes; `netns.py` implements
network topology. Firmware-derived MStar memory-map and Realtek RPC helpers are runtime
dependencies under `host/helpers/`, alongside the native networking and media
helpers. Guest programs live under `guest/helpers/`.
