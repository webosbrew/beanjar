# The GPU in its own process (vhost-user-gpu)

A VM's virgl GPU can run in QEMU's own vhost-user-gpu backend (`contrib/vhost-user-gpu` of the
same QEMU source, in beanjar-vendor's prebuilt QEMU, published as `build/qemu/libexec` and
shipped in `beanjar-qemu` as `/usr/libexec/beanjar/vhost-user-gpu`), not on QEMU's main loop. This is meant for ARM hosts
with a GPU's render node (the Raspberry Pi 5, the ROCK Pi 4 boards). The guest sees the same
virtio-gpu with the same resolution, outputs and id. The measurements behind it: with virgl on the main loop, every frame the guest UI renders holds up usb-redir, the
xHCI, the chardevs and the display, and a USB camera lost a fifth of its frames.

It is the default on ARM hosts (`gpu = "auto"`; "Measured on the Pi: camera" below gives the
reason), and `gpu = "virtio-gpu-gl"` keeps a VM on the old path. QEMU 10.0.2's backend loses the display for
good when the device is reset or the VM paused, so beanjar does neither to a running VM: a guest
restart ends QEMU and the VM starts again cold, and a full host disk makes QEMU quit instead of
pausing ("Guest restarts and a full disk" below). See "Limits" below.

The PC under WSL keeps `virtio-gpu-gl-pci`: the backend renders through its own EGL with a GBM
device and hands 3D scanouts to QEMU only as dma-bufs, which Mesa's d3d12 driver cannot export,
and our surfaceless and read-back patches are in QEMU's `ui/`, not in
the backend.

## The setting

`vm.toml`:

```toml
[display]
gpu = "virtio-gpu-gl"   # auto (default, not written), vhost-user-gpu or virtio-gpu-gl
```

or `beanjar config set NAME gpu=virtio-gpu-gl`; the launcher's variable is `GPU_DEVICE`.
`auto`, the default, is vhost-user-gpu on an aarch64 host whose render node is a GPU's (not
vgem's) when the QEMU is ours (it brings the backend), guests with two outputs (queue:
`max_outputs=2`) included; else virtio-gpu-gl, with a note for a system QEMU. The PC (x86, WSL)
is unchanged. `vhost-user-gpu` asks for it anywhere and fails the start when there is no render
node or no backend (`VHOST_USER_GPU` names another backend binary). `virtio-gpu-gl` is the per-VM way
back to virgl in QEMU.

## The command line

`qemu.plan` swaps `virtio-gpu-gl-pci,...` for `vhost-user-gpu-pci,chardev=vgpu,...` with the same
properties (`xres`, `yres`, `id` on touch models, `max_outputs=2` for the second output of
queue), adds `-chardev socket,id=vgpu,path=<RuntimeDirectory>/vhost-user-gpu.sock`, and
puts the guest RAM on `-object memory-backend-memfd,id=ram,size=<MEM>M,share=on` with
`-M virt,memory-backend=ram`: the backend maps the guest's RAM to read its buffers. The display
(`-display dbus,gl=on` or `egl-headless`) is unchanged; the backend passes 3D scanouts to QEMU
as dma-bufs and cursor updates as messages, so the viewer, beanjar-vnc and screendump see what
they saw before. Only that mode changes the RAM; virtio-gpu-gl keeps QEMU's anonymous RAM.

## The process and its lifetime

The launcher (`launch.run`) binds and listens on the socket before it starts anything, then
starts `python3 -m beanjar.display.gpu_backend PID FD vhost-user-gpu --virgl --render-node=NODE`, and
execs QEMU. QEMU's connect at realize lands in the listen backlog, so it never races the
backend. `beanjar.display.gpu_backend` accepts that one connection and execs the backend with it
(`--fd=N`); it gives up if PID (the launcher's, then QEMU's) ends first. The backend writes to
`run/gpu-backend.out`.

- Normal stop and power-off: QEMU closes the connection and the backend exits.
- Guest restart: QEMU exits (below), the backend with it, and the next QEMU gets a new one.
- QEMU crash: the kernel closes the socket and the backend exits the same way; the unit
  (`KillMode=control-group`) ends whatever is left of the VM's processes.
- Forced stop: systemd kills the unit's control group, the backend included.
- The setsid fallback (no user systemd): the launcher's session, the backend included.

The backend is in the VM's unit, so `beanjar show` and the viewer's GPU meter (`gpu.py`, the
DRM fdinfo of the unit's processes) count its GPU use.

## Guest restarts and a full disk

Every VM, whatever its GPU, runs this way under its unit (`beanjar/runtime/lifecycle.py`, `unit.run`):

- **A guest restart is a cold start.** QEMU runs with `-action reboot=shutdown`, so a reset
  request (webOS's restart, which `webos-start` turns into `reboot -f`; PSCI; QMP `system_reset`)
  ends it. Its exit status is 0 as for a power-off; what tells them apart is the `SHUTDOWN` event's
  reason (`guest-reset` or `host-qmp-system-reset`, against `guest-shutdown`), which the unit reads
  on a QMP monitor of its own: `-chardev socket,id=lifecycle,path=<RuntimeDirectory>/lifecycle.sock`
  and `-mon chardev=lifecycle,mode=control`, QEMU the client of a socket the unit listens on
  before the launcher starts. After a restart the unit waits for the last QEMU's helpers (it is
  their child subreaper; the media helper, the tuner, the GPU backend, at most 10 s, then
  SIGKILL), keeps `console.log` and `webos.log` as `*.prev.log`, and runs the launcher again in the
  same unit: the same private bus, `RuntimeDirectory` (`reason.json`) and `qemu.out`.
  `qemu.out` says `beanjar: the guest restarted (guest-reset): starting the VM again, cold` and
  `beanjar: the new QEMU runs N s after the guest's restart`; `run/restarts` counts them. A
  power-off, a crash or a stop ends the unit as before. A foreground `vm-launcher run` has no
  monitor and resets in place.
- **What comes back by itself.** The launcher makes again what a start makes: the media helper and
  pool, the tuner, the GPU backend, the second output's setup, the network namespace with slirp
  and the inspector (in `RuntimeDirectory/netns`, which the network wrapper removes when QEMU
  ends). From the `SHUTDOWN` until the new QEMU's monitor is up, `run/restarting` exists: the
  front ends' core (`host/viewer/core`: the viewer, beanjar-vnc and the web viewer through it) then
  keeps its window and waits for a new `org.qemu` on the bus (up to 180 s) instead of ending, and
  registers its display and audio with it. Without the marker a lost display ends the front end
  2 s later, as when a VM stops. USB devices that were attached (`io usb attach`, `--image` sticks,
  `io usb attach`'s devices) are recorded in `RuntimeDirectory/usb.json` and put back on the new
  QEMU before its guest boots, at the same sockets; `io usb attach` starts its client again for them.
- **A full host disk quits QEMU.** The drives keep QEMU's default `werror=enospc`, which pauses the
  VM when a write hits ENOSPC. The same monitor sees the `BLOCK_IO_ERROR` (`nospace`, action
  `stop`), says `beanjar: the host's disk is full: writing DRIVE failed (...); QEMU quits instead
  of pausing the VM`, and sends `quit`; the unit then ends as failed, and is not started again.
  `werror=report` was not taken: the guest would get I/O errors on its state disks and run on
  half broken. Before beanjar-vendor's QEMU 10.0.2+beanjar3, a paused VM with vhost-user-gpu would
  never have shown a picture again (below).

## virgl_fence_set_fd: failed err=-9

The backend logs this now and then. It is virglrenderer 1.1.0's, harmless, and does not occur
with virgl in QEMU. The backend initialises virglrenderer with `VIRGL_RENDERER_THREAD_SYNC` (QEMU's
in-process virgl does not), so a sync thread waits on each fence. `vrend_renderer_create_fence`
then exports the new fence's sync file into virglrenderer's fence table
(`vrend_renderer_export_ctx0_fence`). That lookup searches `fence_list` and `fence_wait_list` but
not the fence the sync thread has just taken off the wait list to wait on
(`vrend_state.fence_waiting`). When that is the new fence and an older signalled fence is still
in `fence_list`, the lookup reports success without a file descriptor, and `virgl_fence_set_fd(-1)`
fails with EBADF (-9). Only that fence id is missing from the table, which only
`virgl_renderer_export_fence` and in-fences of DRM native contexts read; neither the backend nor
QEMU calls them. The guest's fences complete through the `write_fence` callback as always.
Upstream fixed the race in virglrenderer d19ba2be (2026-09-10, MR 1681: export the fence
before handing it to the sync thread); it is in no release yet, 1.3.0 included.

## Limits of QEMU 10.0.2's backend

Debian's 10.0.13 has the same code for all three (checked 2026-11-07): the stable releases
since 10.0.2 changed `contrib/vhost-user-gpu` only for hardening (size and overflow checks in
`vugbm.c`, `submit_3d` and the frontend's message payloads), Debian's patches do not touch it, and
upstream master has fixed neither the `sock_fd == -1` refusal in `vg_process_msg`
(`VHOST_USER_GPU_SET_SOCKET`) nor the per-output `vg_wait_ok` in `virgl_cmd_resource_flush`.

All three are in `contrib/vhost-user-gpu`. beanjar-vendor's QEMU patch 0005 (10.0.2+beanjar3)
fixes the first two, the display socket after a stop or a reset; the third stays.

- **A QMP `stop` and `cont` lost the display, then the backend (fixed by patch 0005).** What
  QEMU does, from the 10.0.2 source: the VM's run state change calls the device's `set_status`
  with the status it has. On `stop` (`vm_running` false) `vhost_user_gpu_set_status` stops
  watching the display socket, deletes its chardev, which closes QEMU's end, and stops the vhost
  device (`vhost_user_backend_stop`: `GET_VRING_BASE` per queue, after which libvhost-user's
  queues are stopped and their kick and call fds closed). On `cont` (`DRIVER_OK` and running) it
  makes a new socket pair, sends the backend one end with `VHOST_USER_GPU_SET_SOCKET`, and only
  then starts the vhost device again (`SET_VRING_BASE`, `SET_VRING_KICK`). Nothing else changes:
  the frontend keeps its consoles and the dma-bufs of their scanouts. The unpatched backend:
  1. It refuses the new socket while it still holds the old one (`vg_process_msg: assertion
     'g->sock_fd == -1' failed`); it does not watch the old one, so it has not seen it close.
  2. Its next writes fail on the closed socket (`vg_sock_fd_write: runtime check failed`,
     twice), and a failed write closes the socket (`sock_fd = -1`). Nothing reaches the display
     again.
  3. The flush that wrote then waits for its answer with a `GSource` on fd -1, which never fires.
     The control queue is served only while no wait is open, so from then on the guest's GPU
     commands are not answered: on the Pi, captureservice hung in D state in `virtio_gpu`, and
     every later guest suspend failed.
  4. At the next `cont` the socket is -1, so `SET_SOCKET` is taken, and asking for the protocol
     features asserts that no wait is open: `set_gpu_protocol_features: Assertion 'g->wait_in ==
     0' failed`, and the backend is gone. On the PC (2D backend, below) that was the second
     `cont`; on the Pi (W22O queue, 2026-11-26) the third.

  Patch 0005 makes `SET_SOCKET` replace the old socket, and dropping a socket drops the wait on
  it: a flush's `DMABUF_UPDATE` counts as answered, a `GET_DISPLAY_INFO` or `GET_EDID` is asked
  again on the new socket. A short read is the frontend closing its end, so a wait sees that
  instead of reading garbage; a failed write keeps the socket (and raises no SIGPIPE) for the wait
  or the next `SET_SOCKET` to settle. Commands wait in the ring while there is no socket or the
  queue is stopped, since the frontend sets the queue's base again before it starts it and a
  command taken in between would be taken twice; the queue is served once when it starts. QEMU's
  frontend is unchanged. Measured below ("Reproducing them"). The `cont` after `-S` at start
  (display_setup, the link-down of `link = "off"`) comes before the guest's driver starts the
  device and was always safe, and Quick Start+'s standby and resume do not reset the device
  (guest kernel patch 0004, docs/dev/architecture/suspend.md).
- **A device reset kills the display until QEMU ends, without patch 0005.** webOS's restart
  (Settings, developer mode, a reset, an update; `webos-start` turns it into `reboot -f`) and QMP
  `system_reset` reset the device. The guest's driver then starts it again, QEMU hands the backend
  a new display socket, and the unpatched backend refuses it as above. Measured on the Pi with
  `system_reset`: the same `vg_process_msg: assertion 'g->sock_fd == -1' failed`. The reset goes
  through the same `set_status` path as a stop, so patch 0005 should cover it too (inferred, not
  measured). Under a unit no reset reaches the device any more: a restart is a new QEMU with a new
  backend ("Guest restarts and a full disk").
- **One resource on two outputs aborts the backend.** `virgl_cmd_resource_flush` sends one
  update per output that shows the resource and waits for an answer after each. The second wait
  trips `vg_wait_ok: Assertion 'g->wait_in == 0' failed`. It is not a deadlock: `vg_wait_ok`
  allows only one outstanding wait, and QEMU's frontend answers one update at a time (one
  `backend_blocked` flag), so the updates cannot be sent together either. The backend has no
  option against it. In a guest, only the kernel's fbdev emulation puts one resource on both
  outputs, and only when the last DRM file closes. That happens at shutdown and at a restart,
  and with the cold restart the next boot has a new backend anyway. See "Two outputs (queue, and
  spacebuns until 2026-11-23)" below. So `auto` does not leave two-output guests out.
- What pauses or resets a VM in beanjar (audited 2026-11-07): webOS's own restart, above, is a
  normal workflow, and a cold start now; no pause is. The `stop` and `cont` targets are the IR
  relay's in standby (2026-11-26) and the media pool's guest test (`guest/kernel/modules/media-pool/tests/run-tests.py`,
  step 3, "the doorbell across a pause"; `--skip-pause` leaves it out). Both stopped only VMs with
  virtio-gpu-gl until the backend took a new display socket (patch 0005, above). The other `cont`s resume a VM started with `-S` before its
  guest runs: `secondary.py` (the second output; `--keep-paused` with `QEMU_EXTRA=-S` leaves
  the `cont` to the user, still before the guest), `netns.off` (`link = "off"`'s link-down) and
  `tests/integration/viewer/check-qemu-listener-close.py`. The viewer,
  beanjar-vnc, `beanjar screenshot`, the web UI and `tests/regress/audio/capture.py` (HMP `wavcapture`)
  do not pause. Beanjar takes no snapshots and migrates nothing. QEMU itself pauses a VM when a
  writable disk hits ENOSPC on the host (`-drive` `werror=enospc`, the default); the unit makes it
  quit instead (above).
- `screendump` over QMP answers "no surface" with either device, because both scanouts are GL.
  `beanjar screenshot`, the viewer and beanjar-vnc read the D-Bus display and see the picture.

### Reproducing them

All need a VM on an ARM host with a render node (the Pi or a ROCK Pi 4), our QEMU and its
backend; the backend's messages are in the VM's `run/gpu-backend.out`.

The display after `stop` and `cont`:

1. Start a VM with `gpu = "vhost-user-gpu"` and let it reach Home, e.g. W22O queue. A backend
   other than the build's is `VHOST_USER_GPU=PATH` in the environment of `beanjar start`.
2. `host/scripts/vm-launcher qmp --dir VM-DIR '{"execute":"stop"}'`, wait 5 s, then
   `host/scripts/vm-launcher qmp --dir VM-DIR '{"execute":"cont"}'` (`query-status` says
   running), or let the IR relay do it: Quick Start+ on (`quickStartMode`), POWER, and POWER
   again to wake.
3. Press a remote key (`beanjar remote press NAME HOME`) and take `beanjar screenshot NAME`.

Without patch 0005: the picture is the one from before the stop and never changes again, and
`gpu-backend.out` has `vg_process_msg: assertion 'g->sock_fd == -1' failed`, followed by two
`vg_sock_fd_write: runtime check failed: (ret == buflen)`; a later `cont` aborts the backend
(`set_gpu_protocol_features: Assertion 'g->wait_in == 0' failed`). The same VM with
`gpu = "virtio-gpu-gl"` keeps its display across the same steps. Measured:

- Pi (k8hp-mullet, before 2026-11-26): the picture grabbed with `tests/regress/virgl/dbus-grab.py`
  before the stop, after it with a remote key, and after a HOME: all three the same with
  vhost-user-gpu, changed with virtio-gpu-gl. Pi (W22O queue, the relay's standby, 2026-11-26): the
  abort at the third `cont`, captureservice hung in `virtio_gpu`, later suspends failed.
- PC (2026-11-26, W22O queue, x86-64 guest kernel): the PC's only render node is vgem, which
  virgl cannot export scanouts from (`virgl_cmd_set_scanout: Assertion 'fd >= 0' failed`), so the
  backend ran without `--virgl` (2D, a wrapper as `VHOST_USER_GPU`) on vgem's primary node; the
  protocol over the display socket is the same. 10.0.2+beanjar2's backend: the refusal and the two
  failed writes at the first `cont`, the abort at the second.

With patch 0005, measured 2026-11-26:

- PC, the same 2D setup: 5 `stop`/`cont` cycles, no warning or assertion from the backend, which
  ran on.
- Pi (W22O queue, the default display, so vhost-user-gpu, 10.0.2+beanjar3's arm64 backend): 6
  Quick Start+ standby and wake cycles through the IR relay with its vhost-user-gpu guard off,
  each paused 40 to 60 s. The relay stopped and continued the VM each time; after each wake the
  picture followed HOME and RIGHT (Home's focus moved), the backend logged no warning or
  assertion and ran on, captureservice slept in `do_sys_poll` with no task in D state, and
  `getForegroundAppInfo` answered `com.webos.app.home`.

The display after a guest restart (only without the unit's monitor now, e.g. a foreground
`vm-launcher run` with `GPU_DEVICE=vhost-user-gpu`):

1. As above, at Home.
2. `host/scripts/vm-launcher qmp --dir VM-DIR '{"execute":"system_reset"}'` (or restart the
   set from webOS), and wait for the boot.
3. The picture stays the one from before the reset, and `gpu-backend.out` has the same
   `vg_process_msg: assertion 'g->sock_fd == -1' failed`.

The shared resource on two outputs:

Measured with W24G spacebuns while it had two outputs (until 2026-11-23); W22O queue is now the
two-output guest, and the same steps with it are expected to show the same, not measured.

1. Create a VM on W24G spacebuns (02.68.80.01) with layers `normal,gl` and the dbus display, which
   gave it `max_outputs=2`, and start it with `gpu = "vhost-user-gpu"`.
2. On a fresh state, spacebuns restarts once, about 85 s after the start (it reaches Home on its second boot). During that shutdown the backend exits:
   `gpu-backend.out` ends with `vg_wait_ok: Assertion 'g->wait_in == 0' failed`. Without the
   unit's cold restart (a foreground run), `qemu.out` then has `Failed to set vhost-user-gpu
   socket` and the picture stays black; under the unit the next QEMU has a new backend and the
   VM reaches Home (measured below).
3. Start the same VM again (its state now past the first boot): it reaches Home with
   vhost-user-gpu. To trigger the abort in a running guest, stop `surface-manager-daemon` and then
   `webapp-mgr`, the last process with a DRM file open.

### Two outputs (queue, and spacebuns until 2026-11-23)

Spacebuns has one output since 2026-11-23, as a set: one scanout whose connector is a TV
connector with an overlay plane (guest kernel patches 0020 and 0028, the words in the gl layer's
`native-tv-rootfs`). LG's drm-backend copies that connector into a second head, TV-2, and puts the
768x2160 popup output on the overlay plane, as it does on a set's lg1kdrm (one CRTC with a
primary, an overlay and a cursor plane). That output is what gives LSMW's popup window its
controllers; without it the compositor faulted in `getFocusedWindowInfo`, which is why a second
scanout was added before (docs/research/graphics/compositors.md, "Spacebuns outputs on a set").
The fbdev abort below needs two CRTCs, so it no longer happens on spacebuns. Queue still has two
scanouts.

What follows was measured on rk-1 on 2026-11-07, with spacebuns' two scanouts then. The backend
was a scratch build with log lines in `virgl_cmd_set_scanout` and `virgl_cmd_resource_flush`.

- Why two outputs then: spacebuns' stock Weston configuration (`product.env`, configd) has two
  displays, the 1920x1080 TV and a 768x2160 popup. With one output and no overlay plane its
  compositor faulted on the second window's missing controllers. Queue's setup is the same and
  keeps its second scanout.
- What shares the resource: our guest kernel's fbdev emulation (`CONFIG_DRM_FBDEV_EMULATION`,
  guest/kernel/configs/webos_virt_defconfig). At boot, before the compositor runs, it creates
  `fb0` as one 1920x2160 buffer covering both modes (`/sys/class/graphics/fb0/virtual_size`;
  resource 2 in the backend). While any process has a DRM file open, it stays off the screen.
  When the last one closes, the kernel restores fbdev's mode, and fbdev, as always in DRM, scans
  one framebuffer out on every CRTC: `set_scanout 0 res=2`, then `set_scanout 1 res=2`, and that
  plane's flush of resource 2 aborts the backend. Neither LG's compositor nor our Weston settings
  mirror anything. While the compositor runs, both scanouts show its own buffers. Stopping the
  compositor alone disables both scanouts and nothing more, because WebAppMgr still holds the
  render node. In normal use, the last close comes only at shutdown or a restart.
- So spacebuns' black screen on a fresh VM was its first-boot restart, the restart limit above.
  The two-output abort happens in that same shutdown. With the cold restart the abort is harmless:
  the old backend dies with the old QEMU, and the next boot gets a new one. The only guest-side lever would be to turn
  off fbdev emulation for the target (`drm_kms_helper.fbdev_emulation=0`). That removes the
  `/dev/fb0` that LG's eplmanager, captureservice and pqcontroller open, and it would not bring
  the display back after a restart. It is not done.
- An abort during the guest's shutdown does not cut the shutdown short. With `beanjar stop`
  (webOS's power-off), the backend aborted in the shutdown, and the guest still synced and
  remounted read-only, reached `reboot: Power down`, and QEMU exited 5 s after the call, as
  with virtio-gpu-gl. With webOS's restart (`reboot -f` in webOS, which `webos-start` turns
  into a reset), it aborted when webOS's processes ended, the guest reached
  `reboot: Restarting system`, and QEMU ran on into the next boot (without a GPU: `[drm] KMS
  disabled`, the restart limit). The first-boot restart behaves the same way. The guest only
  queues commands that nothing reads any more and does not wait on them.
- A backend fix, sized and not applied, if one is ever taken with the restart fix, would update one scanout at a time:
  keep the flush's resource, rectangle and next scanout in `VuGpu`, and send the next update from
  `source_wait_cb` after QEMU's answer before resuming the queue (about 25 lines in `virgl.c`,
  `vhost-user-gpu.c` and `vugpu.h`). The backend also leaves `scanout[].resource_id` set when the
  resource is unreferenced. A guest that reuses that id for another output's buffer would hit the
  same assertion. That was not seen here: no unreference of a resource still on a scanout.

## Measured (2026-11-07)

On the Raspberry Pi 5 (KVM), `scripts/regress` booted k7lp-kisscurl (W21P, webOS 6),
k8hp-mullet (W22H) and o22-queue (W22O, webOS 26, two outputs) to Home with vhost-user-gpu. It
also booted k5lp-goldilocks2 and m3-goldilocks, which went through Quick Start+ standby and
resume (0% QEMU CPU in standby, Home after resume). The decode regression (`--tv --tv-steps decode
--decode-cases file.h264,mse.h264`) passed on k7lp-kisscurl, k8hp-mullet and lm21u-kisscurl with
493 to 496 pictures shown of 500 to 502 decoded, 28 or 29 of 37 captures with a picture. That is
the same as virtio-gpu-gl the same day and as the earlier reference run. The contact sheets look
the same.

On rk-2 (KVM, RK3399), with LG's camera app (`tests/regress/virtcam/run-camera.py --app`, 40 s),
the alt 12 camera, usb-redir `debug=4`, and the home app left running:

| Firmware | GPU | Pictures/s | Overflows per run | Main loop (cores) | Backend (cores) |
| --- | --- | --- | --- | --- | --- |
| N21D, webOS 6, 1536 MiB | virtio-gpu-gl | 22.7-23.1 | 59-71 | 0.91-0.93 | - |
| N21D | vhost-user-gpu | 26.9-27.8 | 20-27 | 0.85-0.90 | 0.54-0.56 |
| W22H, webOS 24, 2048 MiB | virtio-gpu-gl | 26.0-26.4 | 28-35 | 0.85 | - |
| W22H | vhost-user-gpu | 29.2-29.3 | 1-3 | 0.89 | 0.35-0.37 |

The main loop is as
busy as before (usb-redir and the xHCI), but no longer stalls behind virgl. One N21D run of four
never fed its decoder; its numbers are left out. On rk-1, W22O queue, W24G spacebuns
(virtio-gpu-gl, see above) and WT1H booted to their UI. beanjar-vnc served the picture. After a
`kill -9` of QEMU the backend was gone at once, and the unit was cleaned up.

Idle at Home on the Pi (k8hp-mullet, 30 s, the picture still): QEMU's main loop 0.001 core with
either device, the backend 0.000, the vCPUs 1.04 cores with vhost-user-gpu and 1.33 with
virtio-gpu-gl.

### Restarts, a power-off and a full disk (2026-11-07, rk-2, KVM)

With vhost-user-gpu, beanjar-vnc open, `mode = "mitm"`:

- N21D (webOS 6), webOS's own restart (`reboot` as root in webOS): `qemu.out` had `the guest
  restarted (guest-reset)` and `the new QEMU runs 8.1 s after the guest's restart` (18 s from the
  command, webOS's shutdown included). beanjar-vnc kept its pid and said `display: connected to the
  new QEMU`; its picture and `beanjar screenshot` (taken by it) showed Home, the clock moving. The
  virtual camera, plugged by `tests/regress/virtcam/run-camera.py --keep` from the PC, was attached and
  connected again (`USB devices put back: 2`, with a stick image), and after the boot LG's camera
  app (`run-camera.py --app`) passed: 561 of 561 inputs shown, 28.05 pictures/s. The stick is on
  the guest's bus at boot (`sda`), but this firmware's PDM does not mount a stick present at boot
 .
- What the 8.1 s are, against a reset in place (which costs nothing here): the last QEMU's helpers
  end in 0.6 to 1.0 s, then the launcher's preparation and the network namespace with its
  inspector (mitmweb ready about 6 s after its start), then QEMU's start. The guest's own boot is
  the same either way: a reset reloads the kernel too. Spacebuns: 9.0 s.
- W24G spacebuns (two outputs), fresh state: its first-boot restart came with the backend's
  `vg_wait_ok` abort in the shutdown, as above; the unit started QEMU again (9.0 s), and the VM
  reached Home with the new backend, beanjar-vnc reattached.
- `beanjar stop` (webOS's power-off) on both: QEMU exited 6 to 10 s after the call, the unit
  ended, no restart, and no QEMU, backend, helper, beanjar-vnc or runtime directory was left.
- A full disk: a fresh N21D VM with its `state/` on a 48 MiB tmpfs. About 30 s after the start
  QEMU's write to `nvram` hit ENOSPC; `qemu.out` said `beanjar: the host's disk is full: writing
  nvram failed (No space left on device); QEMU quits instead of pausing the VM`, the unit ended
  failed (status 1) and was not started again, and nothing of the VM was left running.

## Measured on the Pi: camera (why it is the ARM default)

Raspberry Pi 5 under KVM, LG's camera app with the virtual camera at MJPEG 1280x720, 30 fps,
alt 12, 60 s runs; the guest's pictures per second and the usb-redir overflows (input QEMU's
usb-redir dropped because its queue was full) per run:

| VM   | virtio-gpu-gl                                 | vhost-user-gpu                                |
| ---- | --------------------------------------------- | --------------------------------------------- |
| N21D | 22.1 to 22.5 pictures/s, 117 to 155 overflows | 26.8 to 27.5 pictures/s, 66 to 81 overflows   |
| W22H | 25.9 pictures/s, 106 overflows                | 28.6 pictures/s, 29 overflows                 |

N21D had three runs of each mode, W22H one. The camera sent 29.4 fps with nothing skipped, so
the loss is in QEMU's usb-redir queue, not at the source. QEMU's main thread used the same CPU in
both (about 0.6 core); the vCPUs used about 0.2 core less with the backend, which itself used
about 0.1 core; PSS was about 10 MB higher. Under a light UI load the two modes measured equal,
so the default costs nothing there. Hence `auto` picks the backend on ARM hosts.
