# Quick Start+ (instant on) and suspend

Decisions for making LG's Quick Start+ work in the emulator. They rest on facts gathered from the
firmwares and LG's GPL kernels. Each fact is to be
re-checked when its step is implemented.

## How the TVs do it

- **Quick Start+ is suspend-to-RAM.** It exists on W15M and every later firmware; WT1H has no
  Quick Start+.
  - With it on, power-off makes tvservice put the micom in warm standby (`25 02`) and turn the
    panel and audio off.
  - tvpowerd's `PowerStatesMgr::stateSuspendReady` then calls `ioctl(/dev/lgib, 0x6901)`
    (`LGIB_SUSPEND_TO_RAM`). LG's kernel answers that with `pm_suspend(PM_SUSPEND_MEM)`; the driver
    lives in `guest/kernel/power/user.c` under `CONFIG_LG_INSTANT_BOOT`.
  - The micom stays powered. Its power key, or a timer, wakes the SoC, and tvservice reads the wake
    reason from the micom.
- **Snapshot boot is a separate mechanism.** It is hibernation to a raw `hib` partition through
  `/dev/lgsnap`, signed by `libsb`, with `/sys/power/mode` set to `cold`, `making` or `resume`. The
  Quick Start+ setting also flips the bootloader's snapshot flag in the EEPROM.
- **The setting** is `quickStartMode` in the UI. ToolOption7's `eInstantBoot` decides whether a
  model offers it.
  On W22H (9.2.1, webOS 24) the settings service read `option/quickStartMode` "on" on that VM,
  `option/supportQuickStart` is no settings key (errorKey), and tvpower's `/power/getPowerState` and
  `/power2/getPowerState` report `Active` and `ACTIVE`/`active` while on (read 2026-10-26).

## What our stack offers

- **QEMU 10's arm64 `virt` machine** has no PSCI `SYSTEM_SUSPEND` and refuses `system_wakeup`, so a
  "deep" suspend of the whole VM is not possible without a QEMU patch. Under KVM the host kernel
  answers PSCI, and it offers `SYSTEM_SUSPEND` only when the VMM enables `KVM_CAP_ARM_SYSTEM_SUSPEND`,
  which QEMU 10.0 never does; TCG's PSCI has no `SYSTEM_SUSPEND` either. Under KVM on the Pi the
  guest's `/sys/power/mem_sleep` offers only `[s2idle]` (2026-11-26).
- **A stopped VM's clocks stop.** With `kvm-adjvtime` (on by default for `virt` 5.0 and later) the
  guest's virtual counter does not advance while the VM is stopped, so neither does
  `CLOCK_MONOTONIC`: a 420 s QMP `stop` of W22O on the Pi left it 47 s further on, the time it ran,
  with WebAppMgr unharmed. `CLOCK_REALTIME` stays behind by the pause. The x86-64 guest's clocksource
  is kvm-clock, which QEMU holds still across a stop as well.
- **q35 (the x86-64 guest kernel)** has ACPI S3 (`ACPI: PM: (supports S0 S3 S5)`, `mem_sleep`
  `[s2idle] deep`). QEMU's wakeup from S3 is a system reset back through the firmware, which resets
  the devices that patch 0004's virtio-pci no longer restores. Not tried here.
- **Neither guest kernel has a cpuidle driver** (`cpuidle/current_driver` `none`), and only a cpuidle
  driver's s2idle entry freezes the tick. So under s2idle `CLOCK_MONOTONIC` counts the time asleep,
  unlike on a TV, and every user-space watchdog sees a long suspend as a stall (W22O's WebAppMgr,
  docs/research/w22o-resume-crashes.md).
- **Our guest kernel** has `SUSPEND` and `PM_SLEEP`. Suspend-to-idle (s2idle) needs no firmware
  support: the kernel freezes tasks, suspends devices and idles the CPUs until a wakeup interrupt
  arrives.
- **Patch 0004** currently refuses every write to `/sys/power/state`. It was added because the guest
  had no wakeup source and a suspend would never come back.

## Decisions

### 1. Suspend-to-RAM is the guest kernel's s2idle

- **mem means s2idle.** "mem" maps to suspend-to-idle, through the kernel parameter
  `mem_sleep_default=s2idle`. Deep sleep doesn't exist on this machine, so this is what a "mem"
  suspend means here.
- **Patch 0004 is replaced.** `/sys/power/state` offers `freeze` and `mem` again, both suspending to
  idle.
- **No QEMU patch.** The guest's vCPUs idle in s2idle, and once it is down the host stops the VM
  (decision 6), as a TV powers its SoC down.

### 2. lgemu provides `/dev/lgib`, as LG's kernels do

- `LGIB_SUSPEND_TO_RAM` (`_IO('i',1)`) calls `pm_suspend()`.
- The device allows one open at a time, as LG's driver does (released on close; tvpowerd closes it
  after each ioctl).
- Only boards whose firmware opens `/dev/lgib` see a difference (W15M and later); WT1H keeps its own
  `suspend_action` path, which now also suspends to idle.
- The device belongs in lgemu, not in a SoC module, because the ABI is LG's generic one on every SoC.

### 3. The power key wakes the guest on a dedicated wake line

The first plan, making the IR port's virtio interrupt a wakeup source, failed in measurement. The
port's MSI-X vector is shared by all virtio-serial ports, and the PCI-MSI chip has no
`irq_set_wake`. Separately, virtio-pci reset every device on suspend, and 9p and the GPU have no
restore. Decided instead:

- **The wake line is QEMU virt's PL061 GPIO power button.** lgemu owns PL061 GPIO 3 and arms it as a
  wake IRQ. There is no gpio-keys driver, so webOS never sees a power-button input device.
- **The host wakes the guest by pulsing that line.** When the guest is in standby, the host's power
  key sends QMP `system_powerdown`, which pulses the line. On a TV, the always-on micom likewise wakes
  the SoC on a line of its own. On the x86-64 guest kernel (q35) the line is ACPI's fixed power
  button, which `system_powerdown` presses there; lgemu installs the fixed event's handler, and the
  kernel's ACPI button driver is configured out (`configs/x86_64.config`) so that it neither holds
  the event nor gives webOS a power-button input. The SCI is a wakeup source of s2idle, and a fixed
  event's status ends s2idle's loop.
  Only the IR relay (below) sends it: the GTK viewer, `beanjar remote press` and `remote pipe` (the
  web viewer's popup remote) send the keys to `ir.sock` in standby as when awake (2026-11-26; until
  then each sent `system_powerdown` itself), so the one process that stops the VM (decision 6) also
  wakes it.
- **ir.sock does it for every client**. The micom model in the guest sleeps with it, and the IR
  pump that feeds it is frozen, so a key written to the `webos.ir` port while the SoC is suspended
  waited in the port: it woke nothing and reached tvservice after the next wake (measured on W22O
  queue, 2026-11-09: a POWER and a local key sent by `tests/regress/ir-key.py` during a suspend, then
  `MICOM send key to IM` for both after a wake from `beanjar remote`). The micom's always-on part is
  therefore on the host: the launcher listens on `ir.sock` and relays it to QEMU's socket for the port
  (`ir-qemu.sock`) through `beanjar/ir_relay.py`. With QMP's standby chardev open, a key-down of the
  remote's POWER or of a local key there is `system_powerdown`; every key-down in standby, and the
  key-up of each, is dropped, as the micom ignores keys then. Awake, records pass unchanged.
- **A key during the way down is latched.** The micom model gets the warm standby command before the SoC
  suspends (milliseconds on a quiet host, seconds on a loaded one). A power key in that gap is latched, as a real
  micom does, and ends the suspend at its entry (a device's `suspend_noirq` raises a wakeup, so s2idle's loop
  leaves at once; the freezer would clear it earlier); the micom is then active.
- **The host's standby is the SoC's suspend, not the micom's warm standby** (2026-10-03). The
  webos.standby port opens at the kernel's suspend entry (lgemu's PM notifier,
  `PM_SUSPEND_PREPARE`, the last point at which the IR pump, which opens it, is not yet frozen; the
  freezer and the devices' suspend follow, then `suspend_noirq`, where a latched key is delivered,
  and s2idle) and closes at its exit (`PM_POST_SUSPEND`). It used to open at the warm standby
  command, before the set's own pre-suspend work: on W18R's slow path regress then measured "CPU
  in standby" over 9.3 s of that work at ~129 %, though the set did
  suspend and wake. `suspend_noirq` itself cannot open it: the pump is frozen by then.
- **After resume, the micom model reports the wake reason** that tvservice reads (micom read `0x9A`,
  remote key).
- **Patch 0004** makes virtio-pci leave its devices untouched across s2idle, so 9p, the GPU and the
  serial ports survive.

### 4. The micom models warm standby

- `25 02` and `25 03` move the micom model into warm standby and back. That state changes what the
  power key does: in warm standby it wakes the SoC instead of cutting power.
- While the SoC still runs in warm standby (Active Standby), the micom model reports the remote's
  keys on its 0xC0 poll as when active, and the firmware filters them by the power state it reads
  (0xB1 = 2): tvservice's `_MICOM_ProcessSingleKey` (W19P) hands POWER to the UI and drops most
  other keys itself, and W22O's `micomservice` passes POWER to uinput, which turns the set on. Key
  events still queued when the SoC suspends are dropped; a POWER or local key among them wakes it.
- A cold power-off (Quick Start+ off) keeps ending the PID namespace, as today.
- The host shows the standby state: the viewer goes black, and the manager labels the VM "standby".

### 5. Snapshot boot is out of scope for now

- Hibernation to a `hib` partition, `/dev/lgsnap` and the bootloader's snapshot flag come later.
  They need real partitions, `CONFIG_HIBERNATION` and the securitymanager check.
- Until then the EEPROM snapshot flag is left as the firmware writes it, and `/sys/power/mode` stays
  `cold` (patch 0003).

### 6. The host powers the SoC down: the VM is stopped in standby

A TV's SoC is powered down in Quick Start+'s standby, and its `CLOCK_MONOTONIC` does not count the
time. Under our s2idle it did (no cpuidle driver, above), and W22O's WebAppMgr aborted at the wake
of a long standby (docs/research/w22o-resume-crashes.md). Decided (2026-11-26): once the guest is
down, the host stops the VM with QMP `stop`, which holds the guest's clocks, and continues it at
the wake. No guest kernel change beyond one export (patch 0029), no QEMU patch.

- **The guest says when it is down.** lgemu's micom device opens a second port, `webos.suspended`
  (chardev `suspended`, a null chardev), from its noirq suspend callback, the last step before
  s2idle: user space is frozen and the other devices suspended (it is registered after them and
  suspends synchronously). Its noirq resume, the first step after the wake, closes it. The host sees
  the chardev's frontend-open in `query-chardev`. The IR pump cannot do it, as it is frozen by then;
  it hands lgemu the port at its start instead (`WEBOS_IR_SUSPENDED_PORT`, an `O_PATH`
  descriptor), so the kernel opens a kept `struct path` with `dentry_open`, with no path lookup in
  the suspend. The rest of the open and the close is safe there too: the kernel has no fanotify, so
  nothing waits on user space; virtio_console's open and release take spinlocks and send their
  control message by polling the control queue's used ring, not on its interrupt; the virtio-pci
  devices stay in D0 and keep running across s2idle (patch 0004). The close is `__fput_sync`: a
  plain `fput` from the suspending task (tvpowerd) would close it only on that task's return to
  user space, after the whole resume. A key latched on the way down (decision 3) skips the open.
- **The IR relay stops it.** `beanjar/ir_relay.py` (the host's always-on micom part, running for the
  VM's life) looks at QMP every 0.5 s while `micom-status` says standby (and never otherwise), and
  stops a running VM whose `suspended` chardev is open, once for each time the SoC is down: after
  a wake, which the guest takes a moment to show, it does not stop it again until the port has
  closed. A VM paused for another reason is never continued.
- **Not with vhost-user-gpu.** QEMU 10.0.2's vhost-user-gpu backend loses the display at a stop and
  cont (docs/dev/architecture/gpu-process.md), and on the Pi (W22O queue, 2026-11-26) it aborted at
  the third cont (`set_gpu_protocol_features: Assertion 'g->wait_in == 0' failed`); the guest's
  captureservice then hung in virtio-gpu (D state) and every later suspend failed to freeze it. So
  the relay never stops a VM whose QEMU has the `vgpu` chardev (the ARM hosts' default GPU), and
  logs `VM left running (its GPU is vhost-user-gpu)`: such a VM keeps the s2idle with running
  clocks of decisions 1 to 3, and W22O's WebAppMgr still aborts at the wake of a long standby there.
- **The relay alone wakes it**: the power key (decision 3) is `system_powerdown`, then `cont` if the
  relay stopped the VM. The edge is latched by QEMU's PL061 (and ACPI's PM1 status) while the VM is
  stopped, so the guest sees the wake as soon as it runs. Every front end sends the keys to
  `ir.sock` in standby, so no other process stops or continues it.
- **Run state.** A VM the relay stopped is "paused" to QMP; `poweroff.qmp_status` reads paused with
  both ports open as "standby", as for a running guest with `webos.standby` open, so `beanjar list`
  and the power-off refusal see standby either way. A VM paused otherwise stays "paused".
  `beanjar stop` of a stopped VM ends QEMU as before (the suspend synced the guest's disks).
- **The wall clock follows the RTC.** `CLOCK_MONOTONIC` leaves the stop out (kvm-adjvtime holds
  arm64's virtual counter, QEMU holds kvm-clock on x86-64). A TV's `CLOCK_REALTIME` and
  `CLOCK_BOOTTIME` include the standby, through its RTC read at the resume; the stock `rtc_resume`
  skips that under our s2idle (`timekeeping_rtc_skipresume`: `timekeeping_suspend` never ran).
  So lgemu reads rtc0 (PL031 on arm64 virt, the CMOS RTC on q35; both follow the host's clock while
  the VM is stopped) at its suspend and its resume, against `CLOCK_BOOTTIME`, and injects the
  difference with `timekeeping_inject_sleeptime64`, as `rtc_resume` would; patch 0029 exports it.
  The RTC counts whole seconds, so a difference under 2 s (an s2idle the host did not stop) is
  left alone rather than walked by a second at each standby; the clock is up to a second off after
  a stopped one. The host never pushes a time in.
- **x86-64** (q35, the PC's guest kernel) works the same way: kvm-clock, the CMOS RTC, and ACPI's
  fixed power button as the wake line (decision 3).
- **RTC alarms** cannot fire while the VM is stopped. A TV's power-on timers are the micom's, which
  lgemu does not model; lgemu logs an armed rtc0 alarm at each suspend
  (`rtc0 has an alarm armed`).

## Verification

- Stop and cont (decision 6), 2026-11-26, W22O queue (43.21.89.01): on the Pi (KVM, arm64 guest
  kernel, `gpu = "virtio-gpu-gl"`) five power off/on cycles, one of 519 s and four of about 97 s;
  each stopped about 66 s after POWER (W22O's own pre-suspend work) and continued at the wake. Over
  the long one, 581 s of host time, `CLOCK_MONOTONIC` moved 128.7 s and `CLOCK_BOOTTIME` and
  `CLOCK_REALTIME` 581 s (452.55 s injected); WebAppMgr kept its pids, no crash report, no
  watchdog line, Home after each wake, and no rtc0 alarm was armed at any suspend. `beanjar stop`
  of a stopped VM took 80 s (60 s of it the log save's wait, as before). On the PC (x86-64 guest
  kernel, q35, KVM) two cycles of about 190 s: the ACPI power button woke it, 254 s of host time
  moved `CLOCK_MONOTONIC` 133.7 s and `CLOCK_BOOTTIME` 255 s (121.5 s injected), no crash report.

- On W16R, W17H, W18R and W19P, with Quick Start+ on:
  - The power key gives warm standby, then `/dev/lgib`, then the guest in s2idle with its vCPUs idle.
  - Pressing the power key again resumes to the picture, much faster than a cold boot.
  - tvservice survives, and luna answers after resume.
- With Quick Start+ off: a cold power-off ends the VM, as today.
- WT1H: `echo mem` suspends to idle and resumes on the power key; the guest never hangs.
- A new regress step (standby, then resume) runs where the firmware offers Quick Start+.
