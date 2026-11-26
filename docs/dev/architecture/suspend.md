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
- **No QEMU patch.** The VM keeps running; its vCPUs simply idle in the guest.

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
  the SoC on a line of its own.
  The GTK viewer does that itself. From the CLI, VNC and the web, the manager does: `beanjar remote
  press` and `remote pipe` (which the web viewer's popup remote uses; also for HOST/NAME, as they run on
  the VM's host) turn a POWER key-down (IR 0x08) into `system_powerdown` when QMP shows the standby
  chardev open, and drop its key-up. Awake, or for any other key, they send IR as before.
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

## Verification

- On W16R, W17H, W18R and W19P, with Quick Start+ on:
  - The power key gives warm standby, then `/dev/lgib`, then the guest in s2idle with its vCPUs idle.
  - Pressing the power key again resumes to the picture, much faster than a cold boot.
  - tvservice survives, and luna answers after resume.
- With Quick Start+ off: a cold power-off ends the VM, as today.
- WT1H: `echo mem` suspends to idle and resumes on the power key; the guest never hangs.
- A new regress step (standby, then resume) runs where the firmware offers Quick Start+.
