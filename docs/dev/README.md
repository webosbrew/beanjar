# Developing Beanjar

These pages are for building, testing and packaging Beanjar and for its design. Running it is in
the [user documentation](../user/README.md). The guides say how to do a task today; the
architecture pages record the current contracts and the decisions behind them, one topic each.

## Guides

- [Building](guides/build.md): the host and guest builds, development VMs, regression runs,
  incremental builds, what may be distributed, the Debian packages and the host packages.
- [Testing](guides/testing.md): the unit, integration and regress tiers, and how to write a test.
- [Coverage](guides/coverage.md): line and branch coverage of the host programs.
- [Formatting](guides/formatting.md): clang-format, Ruff and the pre-commit hooks.

## Architecture

The host:

- [VM manager and host runtime](architecture/manager.md): the library, a VM's launch and
  lifecycle, several VMs and several machines.
- [Guest networking](architecture/network.md): `link` and `mode`, the transparent proxy, ports
  and addresses.
- [Per-VM identity](architecture/identity.md): MAC, serial, keys, the model key and the proxy CA.
- [The viewer](architecture/viewer.md): the GTK 4 viewer over a shared C core.
- [Guest and media audio](architecture/viewer-audio.md): the viewer's two PCM inputs.
- [beanjar-vnc](architecture/vnc.md): the TV picture, sound and H.264 over VNC.
- [beanjar-web](architecture/web-viewer.md): VMs in a browser.
- [The GPU in its own process](architecture/gpu-process.md): vhost-user-gpu on ARM hosts.
- [Distribution layout](architecture/distribution.md): the Debian package split and installed
  paths, and the Homebrew tap for macOS.

The guest and the firmware:

- [Build and boot](architecture/build-and-boot.md): the source layout, layers, the guest kernel,
  its modules and the initramfs.
- [Firmware targets and layers](architecture/firmware-targets.md): how a firmware resolves to a
  target, the target table and the optional layers.
- [Partition maps and disks](architecture/partitions.md): the partition names and indices stock
  firmware expects.
- [Video and media transport](architecture/video.md) and
  [media stand-ins](architecture/media-standins.md): compressed media from the vendor pipeline
  to the host decoder.
- [Quick Start+ and suspend](architecture/suspend.md): instant on as suspend-to-RAM.
- [Systemd-era webOS targets](architecture/webos5.md): webOS 5 and 6.
- [NetCast 4.5](architecture/netcast45.md), [NetCast 4](architecture/netcast4.md) and
  [SimpleSmart](architecture/simplesmart.md): the legacy platforms.
- [The open test guest](architecture/test-guest.md): a Buildroot userland of ours that CI boots
  without LG firmware, its checks and the CI job.
- [The 32-bit userland under qemu-user](architecture/qemu-user-bridge.md): the compat bridge, for
  hosts without AArch32 at EL0 (`userland = "qemu-user"`).

## Elsewhere in the tree

- [Debian packages](../../packaging/debian/README.md): building and installing the packages,
  versions, and the arm64 packages.
- [The VM manager](../../host/core/README.md) and [the viewer](../../host/viewer/README.md): the
  user-facing reference, with each component's tests.
- [Guest input](../../guest/initramfs/src/README.md): the remote, keyboard, pointer and touch in
  the guest.
- Guest driver checks: [O22 UCOM](../../tests/regress/lg115x/README.md),
  [LM14](../../tests/regress/mstar/README.md) and [RTK counters](../../tests/regress/rtk/README.md);
  [x86 TCG performance checks](../../tests/regress/tcg/README.md); the Realtek host helpers,
  [host/helpers/rtk](../../host/helpers/rtk/README.md).
