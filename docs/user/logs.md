# Logs and troubleshooting

## Where the logs are

Each VM keeps its logs in its own directory, `vms/NAME/logs/` of the library
(`~/.local/share/beanjar/vms/NAME/logs/` by default); the **Open** beside "Logs" in the manager's
Summary ("Files and runtime details") opens it.

| File | What it holds |
|---|---|
| `qemu.out` | The launcher's and QEMU's output, including why a start failed |
| `console.log` | The guest's serial console: the kernel and the initramfs |
| `webos.log` | webOS's `/dev/console` (a virtio console) |
| `netstub.log`, `netstub-mitm.log` | The traffic inspector (`mode=mitm`): TLS failures, mitmweb's startup and errors |
| `netns-*.log` | The network namespace's helpers: the uplink, the port relays, DHCP with `link=lan` |
| `viewer.log`, `vnc.log` | The viewer and `beanjar-vnc` |
| `saved/<time>-stop/` | The guest's logs, saved at each `beanjar stop`: `/var/log` with PmLog's messages, `dmesg`, the boot status, the jobs and processes |

The guest's `/var/log` is in memory, so it is only kept when the VM is stopped with
`beanjar stop` (or the window's Shut Down); `stop --unplug` saves nothing. A start clears the
previous run's `console.log`, `webos.log` and `netstub.log`.

Each VM runs as a systemd user service, `beanjar-vm-<library>-<name>`, in `beanjar.slice`, so its
processes and their output are also in the user journal:

```sh
systemctl --user status beanjar.slice
journalctl --user -u 'beanjar-vm-*-tv1'
```

`beanjar-web start` runs as the user unit `beanjar-web` (`journalctl --user -u beanjar-web`).

## When something goes wrong

- **A start is refused.** The message says why: not enough free memory for the VM's RAM and the
  headroom, `max_running` reached, a port another VM or program holds, a layer combination that
  `layer.toml` forbids, or a firmware whose target has no guest build. The window's Start shows the
  same reason as its tooltip.
- **The VM starts but shows nothing.** Look at `console.log` for the boot and at `qemu.out`; a VM
  made with `--display none` or `egl-headless` has no picture for the viewer or VNC.
- **The traffic inspector does not start.** `beanjar mitm show NAME` says why: no mitmweb, or one
  older than 12.0.1 (`pipx install mitmproxy`); otherwise see `netstub-mitm.log`, or set
  `mode=direct`. See
  [Inspect emulator traffic](inspect-emulator-traffic.md#troubleshooting).
- **A stop takes long.** `stop` waits up to 90 s for webOS to power the set off, then syncs the
  disks and stops QEMU itself. `beanjar stop NAME --unplug` cuts the power at once.
- **The TV behaves as if its region or settings were wrong** after a change of firmware or
  `broadcast`: the old EEPROM and settings are still on the state disks; `beanjar wipe NAME` gives
  a fresh TV.

`beanjar show NAME` shows a VM's state, settings, addresses and SSH command, which is a good first
thing to include in a bug report. Keep the library's logs private: they can contain the VM's
identity and, with a real model key, its secret.
