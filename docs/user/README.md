# Using Beanjar

These pages are for running Beanjar: installing it, making and running VMs from your own
firmware, and watching and controlling them. Building Beanjar itself and its design are in the
[developer documentation](../dev/README.md).

- [Getting started](getting-started.md): what a host needs, running from a checkout or the Debian
  packages, and the firmware you supply.
- [Creating and running VMs](vms.md): the manager window and the CLI, settings and layers, the
  network, identity, and VMs on other machines.
- [The viewer, VNC, the web viewer and the remote](viewing.md): watching a VM and sending it the
  remote's buttons.
- [Apps](apps.md): Developer Mode with the ares tools, Homebrew Channel, root access and
  second-screen apps.
- [Logs and troubleshooting](logs.md): where each VM's logs are and what to look at first.

Topics:

- [Inspect emulator traffic](inspect-emulator-traffic.md): the traffic inspector of a `mode=mitm`
  VM and its password.
- [Boot a global firmware as another region's model](board-profiles.md): the `broadcast` and
  `country` settings.
- [Your own broadcast on the tuner](tuner.md): a lineup file of channels, guide, pictures and BML
  pages, or a transport stream of yours, on a VM's antenna.
- [Configure secure-store items](sestore.md): a VM's model key and other secure-store values.
- [Run Beanjar on Windows with WSL](wsl.md): KVM, the groups, the GPU through Mesa's d3d12 and
  windows through WSLg.
- [Install the macOS stack with Homebrew](macos.md): Beanjar's QEMU and its graphics stack from
  the tap (not usable yet).

Reference:

- [The VM manager](../../host/core/README.md): every command, setting and file of the library.
- [The viewer](../../host/viewer/README.md): the viewer's options, remotes, audio and video.
