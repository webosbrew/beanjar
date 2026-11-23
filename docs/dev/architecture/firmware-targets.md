# Firmware targets and layers

A firmware is resolved to a `(chip, release)` target. The supported model and release
combinations live in `guest/targets/`, which `host/core/beanjar/targets.py` loads; a target file's
`models` lists the model codes it takes, and `tested = false` marks one that has never booted. The
tables describe hardware and platform contracts, not a separate source tree for each firmware
version.

## Target recognition

`paths.firmware_target` uses the imported manifest when present. Otherwise it reads the model
code and the release from the firmware. webOS names its release family in `rootfs.pak`'s
`etc/starfish-release`. The family is the codename before the hyphen, such as `goldilocks2`,
not the first digit of the version or the model year.

SimpleSmart and NetCast 4.5 have a sysvinit `root.pak`, a `mode.pak` with `RELEASE.cfg`, and
four-letter pak names. `vend.pak`'s graphics library distinguishes SimpleSmart's `libhalgal.so`
from NetCast's `libgal.so`. Unknown model/release combinations are refused.

## The target table

The table is data, `guest/targets/`; `targets.py` keeps the data model, the loader and the rules
that derive values from it (boot.conf, module parameters, the board of a broadcast standard). It
loads every file once, at import, and refuses a file that breaks the schema with its path and key
(`guest/targets/release/afro.toml: nvm.tool_option_words: expected an integer`): unknown keys,
wrong types, missing required keys, a `base` or partition map that does not exist.

| File | What it holds | Python name |
|---|---|---|
| `<chip>-<release>.toml` | one target: its model codes, those still in bring-up, whether it was ever booted (`tested`), its own flags, and its own rows where its firmware line differs | `MODELS`, `BRING_UP`, `TARGETS`, `TARGET_SOCS`, `TARGET_RELEASES` |
| `soc/<chip>.toml` | the SoC: board command line and broadcast boards, SoC module parameters, lgemu devices, the model's hardware facts | `SOCS`, `Soc` |
| `release/<release>.toml` | the release: paks and mounts, state and NVM disks, the EEPROM layout, init system, platform and version, always-used layers, peripheral modules | `RELEASES`, `Release` |
| `partmap/<name>.toml` | a partition map assembled for releases whose own is stale, named for the first row that uses it | `PartmapLayout` |

The soc and release names are the layer directories' (`guest/layers/soc/<chip>`,
`guest/layers/webos/<release>`). The table sits apart from `guest/layers/`: the host reads it,
the guest never does. It describes what the guest build produced for each board, so the
`beanjar-guest` package ships it, at `/usr/share/beanjar/guest/targets` (`paths.Tree.src`), without
`meson.build` and `facts/`, which only the guest build reads; a checkout reads `guest/targets`. A
change the host must understand is a host-guest contract change (`packaging/identity.py`), like any
other. Without the table (no directory, or no target files in it) `targets.py` still imports, and its first use is an error naming
`beanjar-guest` (`targets.Missing`): no VM boots without the guest anyway.

A row with `base` is the row it names with the keys it gives replaced whole: a list, a table such
as `nvm` or `partitions`, or a module list is restated in full, never merged. A row without `base`
is stated whole and must give the required keys. A webOS release's `base` is the release before it
in LG's order, which is how `targets.WEBOS_ORDER` is known (afro has none). A target's `[soc]` names a
`soc/` row as its base or is stated whole; its `[release]` names its release. Values the table
does not hold stay derived from the supplied firmware: partition maps read from a pak, NVM
defaults, Realtek RPC tables and MStar memory maps are not copied from an unrelated build.

Where a firmware's OTA ID ships on US retail models, the chip's default board is a US model of that
firmware and its European board is the VM's `broadcast = "dvb"` choice
([board profiles](../../user/board-profiles.md)).

`guest/targets/o22-queue.toml`, W22O on webOS 26: its own partition map and NVM disk sizes over the
queue release, the LG V4L2 scaler's panel and the stopped DE histogram:

```toml
models = ["W22O"]
bring_up = ["W22O"]
lg_scaler = [3840, 2160]
de_histogram = true

[release]
base = "queue"
nvm_disks = { nvram = "8M", eeprom = "32K" }
partmap = "o22-ponytail"
```

A target that has never booted says `tested = false`; without the key a target is tested, and
`tested = true` is refused as redundant. An untested target is built and shipped in
`beanjar-guest` like any other, and the host learns of the flag from the target table that
package ships, the same table that says the target exists. `scripts/regress` leaves an
untested target out of its default set and runs it only when named with `--target`, as it does a
target whose models are all in `bring_up`. Creating or starting a VM of an untested target, from
the CLI or the manager, prints `warning: <chip>-<release>: not tested yet: may not boot` and goes
on; the manager's New VM marks such a firmware of the collections with "(not tested yet: may not
boot)". Remove the line once the target passes `scripts/regress --headless`.

`first_boot_restart = true` says the target's firmware restarts the set once on the first boot of
a fresh state, as a set of it does at its first setup (W24G spacebuns' google-home-controller
switches sdx to its QA2 server, and eula-service reboots). `scripts/regress` follows that one restart
and fails a target on any other ("Regression runs" in `docs/dev/guides/build.md`).

To add a target, add `<chip>-<release>.toml` with its `models`; add `soc/<chip>.toml` or
`release/<release>.toml` for a new chip or release, preferably as a `base` of the closest one
with what differs, and the layer directories the build needs. A new webOS release's base is the
latest one. Comments say where each value comes from, as in the existing files.
`tests/unit/manager/test_targets.py` loads the whole table; the boot manifests in
`tests/unit/launch/data/boot-conf/` pin what each target's VM is given.

ABI layouts and hardware register addresses belong to the relevant interface. Firmware code
locations recorded during a study are evidence about that build, not portable runtime addresses.

## Layers and the boot manifest

For webOS, the always-used layer names are `common`, `soc`, and `webos`. Their source trees
are `guest/layers/common`, `guest/layers/soc/<chip>`, and `guest/layers/webos/<release>`. Optional layers sit above
them, followed by the per-VM layer and the writable overlay. Base files let a chip or release
reuse another's layer content; see [layer bases](build-and-boot.md#layer-bases).

SimpleSmart and NetCast use their own always-used layers, `simplesmart` and `netcast45`.
Their stock services and graphics boundaries are described in [SimpleSmart](simplesmart.md)
and [NetCast 4.5](netcast45.md). Optional webOS layers are not a promise of legacy support.

### Layer metadata (`layer.toml`)

Each layer declares what the manager needs to know of it in `guest/layers/<name>/layer.toml`
(`tests/regress/tcg/layer.toml` for `tcg-perf`), read by `host/core/beanjar/layermeta.py`:

```toml
description = "Homebrew Channel, elevated; its root SSH on port 6622"   # the dialogs' line
requires = ["devmode"]                    # layers that must be selected with it
conflicts = ["rootssh"]                   # layers that must not; one side declaring it is enough
forwards = [{ guest = 22, host = 6622 }]  # guest TCP ports forwarded to the VM's 127.0.1.N
```

`layermeta.check` is the one test of a selection: every name has a `layer.toml`, every required
layer is there, no two conflict, and a version a selection names (below) is one the layer has. `beanjar create`/`config set`, the dialogs and the launcher
(`qemu.plan`) all use it; a `vm.toml` read back may still name a layer this tree lacks, which the
dialogs show but do not save. The launcher's port forwards are `layermeta.forwards`: the
`forwards` of the selected and the always-used layers (the `webos` layer's SSAP, 3000 and 3001),
on the VM's address or `vm.toml`'s `network.listen` (`SSH_PORT` replaces host port 6622). The guest build publishes each built layer's file as
`build/components/<name>/layer.toml`, and packages install it at the same place under
`/usr/share/beanjar/guest/layers`; a source layer's `layer.toml` is never put into the guest.

A layer may be built once per version: so far only `hbchannel`, once per Homebrew Channel release.
A selection names a version as `NAME@VERSION` (`layers=normal,devmode,hbchannel@0.7.3`); plain
`NAME` is the layer's default version. The layer's directory holds `versions/default` (the
default's name) and `versions/<version>/`; the guest build publishes each version as the
component `build/components/<name>/versions/<version>` (`meson/components.py`) with
`versions/default` beside them. `layermeta.Layer.versions` lists the versions of the source tree
and the published ones, with the published default (what the build made). A version the layer
lacks is refused with the list (`layer hbchannel has no version '0.9.0'; its versions: 0.7.3,
0.8.0-rc1 (plain hbchannel: 0.8.0-rc1)`), as is a version of a layer built once and one layer
named at two versions. The rules in `layer.toml` are the layer's whatever its version, and the
guest knows the layer by its name only (`boot.conf`, the disk serial `layer-hbchannel`):
`share.resolve_layers` takes the version's component. A version not built for the VM's target is
left out like any optional layer that is not built. `beanjar config show` shows
`layer_versions.<name>` and `beanjar list` a `VERSIONS` column (`hbchannel@0.8.0-rc1` for plain
`hbchannel`): the version of the layer, not what the guest's state may have updated it to.

Which always-used layers a target gets (`common`, `soc` and `webos`, or `simplesmart`, or
`netcast45`) is the release's (`always_layers` of its release file), not a layer-to-layer rule. `gl` and `headless`
may both be selected: the higher one's files win.

### Developer Mode (`devmode`)

The `devmode` layer makes a VM behave like a TV with Developer Mode switched on, for the tools that
talk to it: it creates `devmode_enabled` (which gates appinstalld's `dev/install`), runs a jailed
SSH server for `prisoner` on port 9922 with the VM's own developer key, and serves that key on port
9991 for `ares-novacom --getkey`. Its `start-devmode.sh` stands in for the script of LG's Developer
Mode app (`com.palmdts.devmode.service`), which the emulator does not have.

It has no Developer Mode session token. On a real TV the app holds a developer.lge.com login session
and a token that apps can read, together with an expiry counter. The layer has neither: there is no
LG account session to take a token from, and the project does not invent server data. An app that
needs the token (Dev Mode Forever answers `token-unavailable`) reports it unavailable, so session-token
and countdown apps cannot be tested on beanjar. Installing and launching apps does not need the token.

### Homebrew Channel (`hbchannel`)

`LAYERS=...,hbchannel,devmode` (or `hbchannel@<version>`) makes the VM a rooted TV with Homebrew Channel (`org.webosbrew.hbchannel`)
installed and elevated, where a rooted TV has it: as a developer app under
`/media/developer/apps`. Its `layer.toml` requires `devmode`: developer mode keeps
`/media/developer` and makes `sam` scan it, and HBC installs IPKs through appinstalld's
`dev/install`, which is gated on `devmode_enabled`. A system app would not do: on goldilocks `sam`
drops a visible built-in app that is not in the country's customization app list.

`/media` and `/var` are bound from the state disks at `mount-readwrite`, so
`guest/layers/hbchannel/build-layer.sh` stages the install in the layer at its guest paths, under
`/usr/share/beanjar/hbchannel`: the HBC release's IPK data as `dev/install` unpacks it, and every
other file a real install wrote: the app's and its service's LS2 files, as appinstalld writes them
and HBC's `elevate-service` then leaves them, and opkg's records of the package. None of those is
written by hand: they are captured per webOS release by `guest/layers/hbchannel/capture.sh`, which
installs the pinned IPK on a scratch VM of the firmware with `ares-install` (appinstalld's
`dev/install`), runs HBC's `elevate-service` as root and copies the written files out.

The layer is laid out per HBC release: `versions/<version>/release.env` pins the IPK (URL and
sha256; downloaded once into Meson's package cache), `versions/<version>/captured/<name>/` holds the
captures and `versions/<version>/releases` names each webOS release's capture; releases whose installs
wrote the same files share one. Every release that `meson.build`'s `hbchannel_versions` lists is
built and shipped side by side (`hbchannel@0.7.3`, `hbchannel@0.8.0-rc1`); `versions/default` names
the one plain `hbchannel` means: 0.8.0-rc1, the maintainer's release candidate until webosbrew
publishes 0.8.0. Both share this map):

| Capture | Releases | Files |
|---|---|---|
| `afro` | afro, beehive | `/var/palm/ls2-dev/{services,roles}/{pub,prv}`; opkg in `/media/developer/apps/usr/lib/opkg` |
| `dreadlocks` | dreadlocks | `/var/luna-service2-dev/{services.d,roles.d,client-permissions.d,api-permissions.d}` |
| `dreadlocks2` | dreadlocks2 | the same; the service's methods also in the `private` and `public` API groups |
| `goldilocks` | goldilocks, goldilocks2 | and the app's manifest in `manifests.d`; opkg in `/media/developer/apps/var/lib/opkg` |
| `jhericurl` | jhericurl, kisscurl | roles with `"trustLevel": ""` |
| `mullet` | mullet, number1, ombre | the same content, written differently |
| `ponytail` | ponytail | the same content, written differently |
| `queue` | queue | client permissions: the groups of trust level `public` (`/usr/share/luna-service2/allowed_groups.json`); `"trustLevel": "public"` |
| `spacebuns` | spacebuns | as queue, with spacebuns' list |

Elevated means: `Exec` is the app's own `run-js-service` wrapper (third-party jails off: no
jailer, the service runs as root and `checkRoot` answers from `getuid() == 0`), the service's role
allows every name in and out, and it has the `all` client group. Its methods are in the `public`
API group and, from 0.8.0, in `lunabus.query`, which queue's ACG gives other apps (0.7.3's `public`
alone left their `exec` calls denied there).

At each boot, after `mount-readwrite` and before `sam` starts (Upstart: `stopped mount-readwrite and
starting sam`; systemd: `Before=sam.service`), `emulator-hbchannel-install` installs the layer's
HBC: the app, its service and its package into `/media/developer/apps` with opkg's records (HBC's
entry of the shared `status` replaced, the other apps' kept) and the LS2 files into `/var`, and
makes a hub that is already up rescan. It does so when the state has no HBC or a part of it is
missing, an older version (`appinfo.json`'s, compared field by field as numbers), or the same
version from another IPK than the layer's (its hash, in `/var/lib/beanjar`); the layer's own
install gets its LS2 files again at every boot.

A newer HBC in the state stays as it is, LS2 files included. That is what HBC's own update leaves:
its service's `install` method downloads the IPK, forks, installs it through appinstalld's
`dev/install` (into the state disks, over the read-only layer) and runs the new `elevate-service`.
On a TV that updated itself the newer copy is what runs, and so it is here: a VM started on
`hbchannel@0.7.3` that updated itself to 0.8.0 keeps 0.8.0 at the next boot, after a switch to
plain `hbchannel` (0.8.0-rc1, the same version: the layer's copy is installed) and after a switch
back to `@0.7.3`. A layer's newer release (a later default) is installed over an older copy in the
state. To run an older release on a VM whose state has a newer one, wipe its state (`beanjar
wipe`). Checked on queue (W22O 43.21.89), 2026-11-08. Then `emulator-hbchannel` puts the VM's root `authorized_keys` and
SSH host key (the rootssh ones) where HBC reads them (`/home/root/.ssh`,
`/var/lib/webosbrew/sshd`), sets `webosbrew_sshd_enabled`, and runs HBC's `startup.sh` from the
dev install, as a rooted TV's boot hook does. Its Dropbear owns the guest's port 22, which the
launcher forwards from `127.0.1.N:6622`; its `layer.toml` therefore conflicts with `rootssh`. Its
unauthenticated telnet on 23 is not forwarded. With keys provisioned, `startup.sh` sets no
placeholder password.

The launcher assembles `boot.conf` from the target tables and resolved layers. The initramfs
mounts the declared disks, builds the overlay and starts the stock init in its namespace.
It carries the shared kernel modules and boot machinery; firmware layers are supplied through
the host share, not embedded as a second set of defaults in the initramfs.

## Builds and VM state

Each target has a guest build directory. Independent common, SoC, release and feature
components are published under `build/components`; compatibility selectors choose
their variants at VM start. See [the distribution layout](distribution.md).
The kernel and initramfs are common artifacts. The [build guide](../guides/build.md) describes
the SDK choice and extracted-root requirements, including the NetCast SDK exception.

The manager records the resolved target in the firmware manifest. A VM selects firmware and
optional layers, not an independently guessed chip. Its identity and writable state are kept
separately from the read-only supplied paks; see [the manager](manager.md) and
[identity](identity.md).

The [systemd platform contract](webos5.md) covers webOS 5 and 6.
