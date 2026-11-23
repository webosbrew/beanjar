# The viewer, VNC, the web viewer and the remote

A running VM's TV picture, sound and remote reach you through one front end at a time: the
desktop viewer on the VM's host, a VNC client, or a browser through the web viewer. All of them
need the VM's default `display=dbus`.

## The viewer

```sh
beanjar viewer open tv1 --remote
beanjar viewer close tv1
```

The manager window's **Connect** does the same. The viewer runs on the machine that runs the VM
(its picture is shared with QEMU through GPU buffers, which cannot cross the network); for a VM on
another machine, use VNC or the web viewer.

- The header bar has the remote's **Back**, **Home** and **Settings** keys, **Save Screenshot**
  (a PNG in `~/Pictures`), **Fullscreen** (F11) and the **Remote** toggle (also the PC's Menu
  key). The status bar shows the guest's CPU, disk and network.
- The mouse is the Magic Remote's pointer where the platform has one. The keyboard is a USB
  keyboard on the TV, except Home or Super (the remote's HOME), Escape (BACK) and the volume and
  mute keys.
- The picture keeps the TV's aspect ratio at any window size; `--size 960x540` sets the starting
  size.

The [viewer guide](../../host/viewer/README.md) has every option, the remotes of each platform
and the audio and video paths.

## VNC

With the `beanjar-vnc` package (or a checkout's host build), a VM's picture and sound are served
over VNC on its own address, port 5900, behind the VM's own password:

```sh
beanjar vnc start tv1
beanjar vnc password show tv1
beanjar vnc stop tv1
```

`vnc start tv1 --listen 0.0.0.0` (with `link=nat`) lets other machines connect; a `link=lan` VM
serves on its LAN address too. VNC and the viewer cannot run for the same VM at once. Clients
need RSA-AES authentication and may give any user name. See [beanjar-vnc](../dev/architecture/vnc.md).

## The web viewer

With the `beanjar-web` package (from a checkout, `host/web/bin/beanjar-web`), a browser shows the
VMs, their pictures (noVNC), their sound and a popup TV remote:

```sh
beanjar-web start      # a systemd user unit; prints the URL to open
beanjar-web status
beanjar-web stop
```

It listens on `127.0.0.1:8059` (a VM's Traffic Inspector on `8060`, an origin of its own, so
that the traffic it shows cannot act as you in beanjar-web), and the URL carries a token kept in
`~/.config/beanjar/web.token`. `--listen 0.0.0.0 --auth password` opens it to the LAN behind a
login page; `--certificate` and `--key` serve HTTPS; `beanjar-web enable` keeps it across reboots.
It starts VNC for a VM itself (so it needs `beanjar-vnc`), and reaches VMs on other hosts through
the manager's SSH. See [the web viewer](../dev/architecture/web-viewer.md).

## The remote from the CLI

The remote's buttons and the pointer go through the open front end, so scripts start one first
(`beanjar vnc start` on a host without a desktop); a screenshot needs none:

```sh
beanjar remote press tv1 HOME
beanjar remote press tv1 DOWN DOWN ENTER
beanjar remote click tv1 640 360
beanjar screenshot tv1 shot.png
```

`beanjar remote layout tv1` lists the key names of the VM's remote: the remote of its firmware,
the Japanese remote (with d, the broadcast types and 1-12) for a webOS VM on an ARIB (Japanese)
board. Coordinates are those of the
1280x720 screenshot. See [Commands](../../host/core/README.md#commands) in the manager guide.
