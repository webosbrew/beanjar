# Getting started

Beanjar runs LG TV firmware that you supply in QEMU VMs. This page covers what a host needs, the
two ways to run Beanjar, and the firmware it takes. [Creating and running VMs](vms.md) follows on
from here.

## Requirements

- Debian 13 (trixie) on the host, or in WSL2 on Windows 11 (see [WSL](wsl.md)).
- An x86_64 machine, which by default runs the whole guest under QEMU's TCG (full software
  emulation, slow), or an arm64 machine with AArch32 at EL0, which runs it under KVM (a Raspberry
  Pi 5 does). The firmware's userland is 32-bit ARM: on an arm64 CPU without AArch32 at EL0, KVM
  runs the kernel and qemu-arm translates only the TV's 32-bit programs inside the guest
  (`hardware.userland`, `auto` by default), so it is still fast without 32-bit Arm KVM; NetCast
  and SimpleSmart firmware cannot use this. On an x86_64 machine with KVM, the experimental,
  opt-in `hardware.guest_kernel = "x86-64"` does the same with an x86-64 guest kernel
  ([below](#the-x86-64-guest-kernel)). Beanjar asks the kernel at each start whether the arm64
  CPU has AArch32 at EL0; `setarch linux32 true` asks the same question, and succeeds exactly when
  it has (without, it fails: "failed to set personality to linux32: Invalid argument").
- Memory for each VM: its firmware model's RAM (1.25 GB for WT1H, 1.5 GB for W19P, for example)
  plus 1 GiB of headroom by default, and under TCG another 1 GiB for QEMU's translation cache; a
  VM whose userland runs under qemu-user gets half its model's RAM again for qemu-arm's own. A
  start that the host's free memory does not cover is refused.
- For the windows (the VM manager and the viewer): GTK 4 and libadwaita 1.7 or newer for Python.
  Without them the CLI, VNC and the web viewer still work.
- Only to build from a checkout, not for the packages: the guest layers are built with the webOS
  SDK (`/opt/arm-webos-linux-gnueabi_sdk-buildroot`), which is x86_64 only, so they are built on
  an x86_64 machine; NetCast 4.5 needs the [lgtv SDK](https://github.com/openlgtv/buildroot-nc4)
  as well.

## Firmware

No firmware, keys or model secrets come with Beanjar. You supply an LG firmware file (`.epk`) or a
directory of its extracted paks. Each supported firmware maps to a target, a file in
[`guest/targets/`](../../guest/targets/) named `<chip>-<release>.toml`; its `models` line lists the
model codes it takes. Your model is supported if its code, the part after `_DTV_` in the
firmware's file name (`WT1H` in `..._DTV_WT1H_...`), is in one of those `models` lines, and the
firmware's webOS release is that file's release. Other firmware is refused when the VM is made, and
a target with `tested = false` is accepted with a warning that it may not boot.

- The manager window's **New VM** and `beanjar create NAME --firmware FILE.epk` take a `.epk` and
  extract it with `epk2extract` (see below). The VM keeps only the paks it uses; the rest of the
  extraction is deleted.
- Extracted paks: `beanjar create NAME --firmware DIR --import` makes them the VM's own (hard links
  where the directory is on the same filesystem, else a copy). Without a manifest (below) Beanjar
  tells the target from the directory's name, which must keep the model code (epk2extract names it
  `<version>-<OTA ID>`, for example `05.06.10.01-HE_DTV_WT1H_AFAAABAA`), and from the release the
  rootfs names.
- A firmware shared by several VMs or hosts can stay in a firmware collection and be used in
  place: `beanjar create NAME --firmware DIR` without `--import` never writes to the directory, and
  so needs a `manifest.toml` in it naming the firmware and its target. A collection is a directory
  of such firmware directories, `/srv/beanjar/firmware` by default (`beanjar host config set
  firmware_dirs=DIR[:DIR...]`), whose firmware `--firmware` takes by name. See
  [Firmware](../../host/core/README.md#firmware) in the manager guide.

### manifest.toml

Beanjar writes `manifest.toml` itself into every VM's own firmware (the `firmware/` directory in the
VM's folder, from a `.epk` or `--import`). The easy way to get one is to make a VM that way and copy
its `firmware/` directory, manifest included, into the collection. To write one by hand, three keys
are required:

```toml
name = "05.06.10.01-HE_DTV_WT1H_AFAAABAA"  # the firmware's name, as epk2extract names its directory
chip = "h13"                               # chip and release: a file guest/targets/<chip>-<release>.toml
release = "afro"
```

The other keys Beanjar writes (`epk`, `epk_size`, `sha256`, `source`, `paks`, `imported`) record
where the paks came from and are optional.

### epk2extract

[epk2extract](https://github.com/openlgtv/epk2extract) has no released binaries, so you build it.
Beanjar looks for `epk2extract` on `PATH`, then in `~/.local/bin`; `BEANJAR_EPK2EXTRACT` or
`beanjar library config set epk2extract=PATH` names another path. It reads its decryption keys
(`AES.key` and the `*.pem` files) from the directory its executable is really in, so keep them
together and link the executable into `~/.local/bin`. On Debian 13, amd64 or arm64:

```sh
sudo apt install git build-essential cmake liblzo2-dev libssl-dev zlib1g-dev
git clone https://github.com/openlgtv/epk2extract.git
cd epk2extract
CMAKE_FLAGS=-DCMAKE_BUILD_TYPE=Test ./build.sh   # optimised for this CPU; the default Debug build is far slower
mkdir -p ~/.local/opt/epk2extract ~/.local/bin
cp build_linux/epk2extract build_linux/*.pem build_linux/*.key ~/.local/opt/epk2extract/
ln -sf ~/.local/opt/epk2extract/epk2extract ~/.local/bin/epk2extract
```

The build directory and the clone can be deleted afterwards. A firmware whose keys the clone does
not have yet needs a newer clone.

## From the Debian packages

Each [release](https://github.com/webosbrew/beanjar/releases) `v<version>` has every package,
the host's and the guest's. Download and install, in a new directory, with no GitHub account
needed (`version` is the newest release on the releases page):

```sh
mkdir beanjar-debs && cd beanjar-debs
version=0.7.0
arch=$(dpkg --print-architecture)
url=https://github.com/webosbrew/beanjar/releases/download/v$version
wget $url/beanjar_${version}_$arch.deb $url/beanjar-qemu_${version}_$arch.deb $url/beanjar-ui_${version}_all.deb $url/beanjar-guest_${version}_all.deb
sudo apt-get install ./beanjar_${version}_$arch.deb ./beanjar-qemu_${version}_$arch.deb ./beanjar-ui_${version}_all.deb ./beanjar-guest_${version}_all.deb
```

`apt-get` installs everything else these packages need from Debian.

The GitHub CLI fetches every package of the latest release at once, the optional ones included:
`gh release download -R webosbrew/beanjar -p "*_$(dpkg --print-architecture).deb" -p '*_all.deb'`
(a recent gh needs no login for this; an older one may ask for `gh auth login`).

`beanjar-qemu` is Beanjar's patched QEMU, which the launcher runs whenever it is installed. A VM
whose TV programs run under qemu-user (an arm64 CPU without AArch32 at EL0), the x86-64 guest
kernel (below) and a host without a GPU render node (WSL) need it; Debian's QEMU, which `beanjar`
also accepts, is enough only for a VM that runs the TV natively on a host with a render node.
`beanjar-vnc` (the TV picture over VNC) and `beanjar-web` (VMs in a browser) are optional, one
more line each in the same shell:

```sh
wget $url/beanjar-vnc_${version}_$arch.deb && sudo apt-get install ./beanjar-vnc_${version}_$arch.deb
wget $url/beanjar-web_${version}_all.deb && sudo apt-get install ./beanjar-web_${version}_all.deb
```

The [packages guide](../../packaging/debian/README.md) describes every package.

The packages install the commands `beanjar` (the CLI; without arguments it opens the manager
window), `beanjar-ui` (the manager window), `beanjar-viewer`, and with the optional packages
`beanjar-vnc` and `beanjar-web`.

### The x86-64 guest kernel

On an x86_64 PC Beanjar by default emulates the whole TV, its kernel included, under TCG: it
works, but slowly. The **experimental**, opt-in x86-64 guest kernel runs the kernel under KVM and
translates only the TV's programs. It is the package `beanjar-guest-x86-64` (amd64), in the same
release as `beanjar-guest` and of the same version. Install it from the same shell as above, then
switch a VM to it ([Settings](vms.md#settings) says what it can boot so far):

```sh
wget $url/beanjar-guest-x86-64_${version}_amd64.deb
sudo apt-get install ./beanjar-guest-x86-64_${version}_amd64.deb
beanjar config set tv1 guest_kernel=x86-64
```

### Groups

`/dev/kvm` and the GPU's render nodes (`/dev/dri/renderD*`) belong to the groups `kvm` and
`render`. KVM is what runs a VM on an arm64 host, and the x86-64 guest kernel; the default x86_64
route (TCG) does not use it. Without access to `/dev/kvm` a VM falls back to TCG. The render node
is the GPU for the guest's 3D and for hardware video decoding and encoding. Add yourself to both,
then log out and in again (a new login is needed for the groups to apply):

```sh
sudo usermod -aG kvm,render "$USER"
```

Check:

```sh
ls -l /dev/kvm /dev/dri/renderD*   # crw-rw---- 1 root kvm ..., crw-rw---- 1 root render ...
id -nG                             # ... kvm render ...
```

Without a render node (no GPU driver, or no `/dev/dri/renderD*` at all), an x86_64 PC draws the
guest's 3D on a surfaceless display, which Mesa renders on the CPU (llvmpipe): it works, but
slowly. Under WSL the same display renders on the GPU through d3d12 ([WSL](wsl.md)). An arm64
host under KVM expects its GPU's render node, `/dev/dri/renderD128`.

### mitmproxy

A VM's default network records its traffic through your own mitmproxy, 12.0.1 or newer, which
Debian does not package. Install it with pipx; Beanjar finds it in `~/.local/bin` by itself, and
`pipx ensurepath` and a new shell put it on your `PATH` too:

```sh
sudo apt install pipx
pipx install mitmproxy
pipx ensurepath
```

Without it the VM still starts, uninspected, and says so. A VM with `mode=direct` (see
[network](vms.md#network)) does not need it. Every networked VM needs `slirp4netns`
(`sudo apt install slirp4netns`).

### Check the install

Before you have firmware, these show that the parts are in place (the comments are the first
lines of a good result):

```sh
beanjar --help        # usage: beanjar [-h] [--library LIBRARY] [--repo REPO] COMMAND ...
beanjar list          # no VMs in /home/<you>/.local/share/beanjar
beanjar host config show   # max_running: (not set: no limit)
/usr/libexec/beanjar/qemu-system-aarch64 --version   # QEMU emulator version 10.0.2
epk2extract           # LG Electronics digital TV firmware package (EPK) extractor version 4.8 ...
mitmproxy --version   # Mitmproxy: 12.3.1 (12.0.1 or newer)
ls -l /dev/kvm        # crw-rw---- 1 root kvm ... (arm64 hosts, the x86-64 guest kernel)
```

For the GPU, `eglinfo -B` (`sudo apt install mesa-utils`) should name your GPU as the renderer:
`llvmpipe` there means Mesa draws with the CPU. Under WSL, check d3d12 as
[WSL](wsl.md#the-gpu) shows. Without a desktop session the windows cannot open (`beanjar` without
arguments says so); the CLI works anyway.

## From a checkout

Install the host build's packages ([Host packages](../dev/guides/build.md#host-packages)), then
build the host programs, and on x86_64 the guest layers for your firmware's target:

```sh
scripts/build.sh host
FW=/path/to/extracted-paks scripts/build.sh guest
```

NetCast 4.5 needs its own guest build directory set up first; see
[Commands](../dev/guides/build.md#commands). An arm64 host builds only the host side and takes the
guest build from the x86_64 machine (the published `build/` directory, or the `beanjar-guest`
package).

Run the CLI and the manager window from the checkout:

```sh
export PYTHONPATH=host/core:host/ui
python3 -m beanjar --help
python3 -m beanjar_ui.manager
```

The rest of these pages write `beanjar`; from a checkout that is `python3 -m beanjar`.

To make Debian packages of the checkout instead, see the
[packages guide](../../packaging/debian/README.md).

## Where things are kept

VMs live in the library, `~/.local/share/beanjar` by default (`--library DIR` or
`BEANJAR_LIBRARY` for another): one directory per VM under `vms/`, with its settings (`vm.toml`),
its state disks, its identity and its logs. This machine's own settings are in
`~/.config/beanjar/config.toml` (`beanjar host config show`). The [manager guide](../../host/core/README.md#library)
lists every file.
