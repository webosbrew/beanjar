# The 32-bit userland under qemu-user (the compat bridge)

Every firmware's userland is 32-bit ARM, and the arm64 guest kernel normally runs it in AArch32
EL0. A host whose CPU has no AArch32 at EL0 (Apple silicon) cannot, so a VM there would be fully
emulated. With `userland = "qemu-user"` the arm64 guest kernel still runs under the hardware
hypervisor, and each 32-bit program runs under `qemu-arm` inside the guest (`"auto"`, the default,
does that by itself on such a host). qemu-arm translates
the instructions, but it does not convert the system calls: it hands them to the guest kernel's
own AArch32 compat syscall table.

## Why a bridge

Stock qemu-user converts every system call itself. It returns ENOTTY for any ioctl missing from
its table, which has almost no DRM and no evdev, uinput, V4L2, ALSA or LG driver entries, so
webOS's compositor crash-loops. A passthrough would not be enough either: evdev returns 24-byte
records to a 64-bit reader and 16-byte ones to a 32-bit one, and our modules
(`guest/kernel/modules/`) read 32-bit layouts through `compat_ptr()` and `in_compat_syscall()`.
The arm64 kernel already has the whole 32-bit ABI as plain C (the compat table and every
`compat_ioctl` converter), and none of it needs a CPU with AArch32. The bridge lets a 64-bit
process enter it.

## The pieces

- **Guest kernel, system call `0x7ff0`** (`guest/kernel/patches/0023-arm64-compat-bridge-syscall.patch`):
  it takes an AArch32 call number and r0 to r6, packed two per register, and runs
  `compat_sys_call_table[nr]` with TIF_32BIT set for that call only. The number lies above the
  native table, so only a 64-bit task reaches it (through `do_ni_syscall()`), and a task that never
  calls it sees no change. It refuses, with ENOSYS, the calls whose effect would outlive the flag
  or depend on the caller's registers: restart_syscall, exit, exit_group, fork, vfork, clone,
  clone3, execve, execveat, ptrace, sigreturn, rt_sigreturn, personality and sigaltstack. Seccomp
  and audit see the outer call.
- **Guest kernel, binfmt_misc flag `E`** (`0024-binfmt-misc-exe-flag.patch`; it implies `O`): the
  process's executable (`mm->exe_file`, `/proc/<pid>/exe`) is the binary rather than its
  interpreter. ls-hubd identifies a client by its executable and finds its role files by that
  path, so without `E` it would refuse every webOS service. An entry registered without `E`
  behaves as before. `CONFIG_BINFMT_MISC=y`.
- **qemu-arm** (beanjar-vendor's `qemu-user/`, QEMU 10.0.2 linux-user, one static aarch64 program):
  when guest addresses equal host addresses (`guest_base` 0) and the kernel answers the call, it
  forwards every system call through `0x7ff0` except those it must own. `bridge_keep()` in its
  `linux-user/compat-bridge.c.inc` is the only list of those calls:
  - tasks and programs, which are qemu's CPU state, threads and its own exec: exit, exit_group,
    fork, vfork, clone, clone3, execve, execveat, set_tid_address, restart_syscall, ptrace,
    personality;
  - signals, because qemu delivers them and its guest-to-host signal numbers are not the identity
    (real-time signals are shifted and SIGABRT is remapped): the sigaction, sigprocmask,
    sigsuspend, sigpending, sigtimedwait, sigqueueinfo, sigaltstack and sigreturn families,
    signalfd, kill, tkill, tgkill, pidfd_send_signal, pause, wait4, waitid, ppoll, pselect6,
    epoll_pwait, timer_create, mq_notify, prctl, and `fcntl` F_SETSIG and F_GETSIG;
  - the address space, which qemu's page flags and translated code follow: mmap2, munmap,
    mprotect, mremap, brk, shmat, shmdt, madvise, remap_file_pages;
  - its file-descriptor translators and faked `/proc` files: close, close_range, dup, dup2, dup3,
    open, openat, openat2, readlink, readlinkat, and read, write and the socket calls on a
    descriptor that has a translator;
  - what it fakes for the emulated CPU: uname (armv7l), ugetrlimit, setrlimit, prlimit64.

  `QEMU_BRIDGE=0` turns the bridge off, and `QEMU_BRIDGE=1` makes qemu say why it could not turn
  it on. qemu also copies what it logs as unimplemented to `/dev/kmsg`, once per message and
  process, so a system's gaps show in `dmesg` as `qemu-arm[<pid> <program>]: ...`
  (`QEMU_UNIMP_KMSG=0` turns that off).

## The setting

```toml
[hardware]
userland = "qemu-user"   # auto (default, not written), native or qemu-user
```

`beanjar config set NAME userland=qemu-user`; the launcher's variable is `USERLAND`. `auto` is
resolved at each start by `vmsettings.resolve_userland`, and the launcher logs the result and the
reason in one line (`userland: qemu-user (KVM on a CPU without AArch32 at EL0)`):

| Guest kernel and accelerator | userland |
|---|---|
| arm64 under TCG | native: TCG emulates AArch32 itself |
| arm64 under KVM, the CPU has AArch32 at EL0 (Raspberry Pi 5, RK3399) | native |
| arm64 under KVM, the CPU has no AArch32 at EL0 | qemu-user |
| arm64 under HVF (Apple silicon; not in the launcher yet) | qemu-user |
| x86-64 (`guest_kernel`, below) | qemu-user |

Whether the CPU has AArch32 at EL0 is the host kernel's own answer (`qemu.host_aarch32_el0`): on
arm64, `personality(PER_LINUX32)` fails with EINVAL unless `system_supports_32bit_el0()`, the
sanitised `ID_AA64PFR0_EL1.EL0` of all CPUs (`arch/arm64/kernel/sys.c`), which is also what KVM
offers its guests. It is asked in a child process. Then the firmware's own constraint applies
(below): NetCast and SimpleSmart need native, and where the host rule gives qemu-user they do not
start, with an error saying the firmware needs native 32-bit execution, which this host cannot
provide. An explicit `native` or `qemu-user` is taken as it is.

`qemu.plan` finds the interpreter with `paths.Tree.qemu_arm`: `QEMU_ARM=PATH` (a local build, for
development until beanjar-vendor publishes a release), else the vendored one, which the host build
publishes once `packaging/vendor.toml` pins `qemu-user` (`meson/vendor.py`): build/qemu-user, its
aarch64 program, for the arm64 guest kernel on every host, and on amd64 hosts also
build/qemu-user-amd64, its x86-64 one, for the x86-64 guest kernel (`guest = ["arm64", "amd64"]`;
`BEANJAR_VENDOR_QEMU_USER` and `BEANJAR_VENDOR_QEMU_USER_AMD64` take a CI artifact instead).
`beanjar-qemu` ships what the host build published: `/usr/share/beanjar/qemu-user/arm64/qemu-arm`,
and on amd64 `/usr/share/beanjar/qemu-user/amd64/qemu-arm`, where an installed launcher looks
for them. A launch without an interpreter fails. `share.assemble`
copies the interpreter to `/host/qemu-arm`, and boot.conf gets `USERLAND="qemu-user"`. The QEMU
command line changes in two places, its RAM and a swap disk.

Each 32-bit process under qemu-arm carries qemu-arm's own heap, its tracking of the guest
program's pages and its translated code on top of the program's own memory, so a qemu-user start
gives the guest a fixed `vmsettings.QEMU_USER_OVERHEAD_MIB` (1536 MiB) more than `mem` (`-m`:
`vmsettings.guest_ram_mib`) and logs `memory: 2048 MB for the TV + 1536 MB RAM and 1536 MB
host-RAM swap for qemu-user's overhead`. `mem` stays the TV's RAM. The host's memory admission and the unit's MemoryHigh
(`capacity`, `qemu.vm_guest_ram`, `VM.guest_ram`) use the larger size; `beanjar list` shows MEM as
`2048+1536` and `beanjar show` both (`tv_ram`, `guest_ram`). What webOS is told the set has is the
model's board parameters (board.cmdline's `hwopt=`), which do not follow `-m`; the balloon is never
inflated and the media pool has its own size, so neither changes.

Measured on WT1H at the Live TV "No Signal" screen 120 s after boot-done (x86-64 PC, `mem` 2048):
natively under TCG the processes held 232 MiB anonymous memory and the guest had 1546 MiB
available; under qemu-user (x86-64 guest kernel, `-m 3072`) 1514 MiB and 1106 MiB. The overhead
is about 1.3 GiB, of which about 930 MiB is qemu-arm's translated code (one 128 MiB code buffer
per process), so it follows the number of processes and how much code they ran rather than `mem`.
A newer firmware runs more of them: lm21ann-queue (webOS 26, 150 processes) had 2.1 to 2.2 GiB of
overhead at Home against 0.33 to 0.54 GiB of the programs' own, and with the earlier 50 % of `mem`
(768 MiB RAM and 768 MiB swap on its 1536 MiB board) both swaps filled during the first boot
([the study](../../research/qemu-user-memory.md)). Hence a fixed amount, not a share of `mem`.

`guest/initramfs/rootfs/init` also caps each qemu-arm's translation cache at 32 MiB
(`QEMU_TB_SIZE`, in PID 1's environment and systemd's `DefaultEnvironment`; every child inherits
it), down from QEMU's user-mode default of 128 MiB, which only WebAppMgr, the web apps and a few
large services reach. 16 MiB cost the first boot three times the CPU time in retranslation.

Most of that overhead is cold (only about 105 of the 1515 MiB anonymous memory was touched in 60 s
idle), so a qemu-user start also gives the guest a swap disk in host RAM
(`vmsettings.QEMU_USER_SWAP_MIB`, 1536 MiB; `host/core/beanjar/runtime/hostswap.py`): a sparse
memfd the process that runs QEMU creates just before it does (`hostswap.open_fds` in `launch.py`
and `netns.py`), passed with `-add-fd` at descriptors 200 (read-write) and 201 (read-only; QEMU
takes from the fdset a descriptor with the access mode it opens with, and opens both ways), and
attached as `-drive file=/dev/fdset/9,cache=unsafe,discard=unmap` on a virtio-blk disk with the
serial `beanjar-swap`. Host RAM goes only to pages the guest has swapped out, the host may swap
those in turn, and the memfd is gone with QEMU, crashed or not. `guest/initramfs/rootfs/init`
runs `mkswap` and `swapon -d` on the disk with that serial before webOS starts; native command
lines have no such disk. memchute counts free swap as usable (its job sets `zramswap 1`, so two
thirds of it), and its levels follow. A host without memfd (macOS) needs another backing in
`hostswap._backing`; none is built.

The memfd's pages are shmem charged to the cgroup of QEMU, which writes them, and reclaimable only
to the host's swap. The unit's MemoryHigh (KVM) therefore allows for the swap on top of RAM and
headroom; the admission does not count it, nor does a running VM's claim on the host
(`capacity.claim_mib`), since it is taken only as the guest swaps.

Measured on the same WT1H (x86-64 PC, KVM, `-m 3072`, 1024 MiB swap), with a tour of Home, the
browser on a page, the user guide, Settings, Homebrew Channel, Live TV, switches back and closing
the apps:

- At "No Signal" 120 s after boot-done nothing is swapped: the guest has 840 MiB free, the kernel
  does not swap without pressure, and MemAvailable is 1145 MiB, as without swap (1151). memchute's
  usable memory rises from 1062 to 1727 MiB (free swap counted).
- The tour leaves the guest at about 370 to 390 MiB available at its lowest; it swaps out only 4
  to 8 MiB. webOS killed nothing, with or without swap, and launch times did not change
  measurably.
- Under real pressure (the balloon squeezing the guest to 1700 MiB) 550 to 660 MiB of anonymous
  memory goes to swap, with 5 MiB read back while squeezed; once released, the guest has 1453 to
  1536 MiB available (native: 1546) and stays responsive (the tour again: the same launch times).
- Discard gives little back: page discards come only once a whole swap cluster is free, so the
  memfd stays near the high-water mark of swap use (617 MiB for a 590 MiB peak, with 470 MiB still
  in use after the tour). It is bounded by the swap's size, which MemoryHigh allows for.

`guest/initramfs/rootfs/init` reads boot.conf, and before it runs any firmware program it copies
the interpreter into the initramfs and registers it for 32-bit little-endian ARM ELF executables
with the flags `POFE`. `P` keeps argv[0], `O` opens the binary, `F` opens the interpreter at
registration so it serves the firmware's namespace and root, and `E` is described above. Every
32-bit program of the firmware then runs under qemu-arm, init included. Its `binfmt` function
registers any interpreter the launcher puts into `/host`. On the arm64 kernel the AArch64 programs
of newer firmware (`/usr/lib64` on queue and spacebuns) run natively, as binfmt claims only 32-bit
ELF; on the x86-64 kernel boot.conf's `USERLAND_AARCH64` has init register `/host/qemu-aarch64`
for 64-bit little-endian AArch64 ELF executables with the same flags (below).

To check that nothing runs natively, add `arm64.no32bit_el0` to the kernel command line
(`KERNEL_ARGS`). The kernel then reports no 32-bit EL0, so a native 32-bit exec fails with
ENOEXEC, and any program that escaped binfmt_misc would fail to start.

## Where it is refused

`vmsettings.NATIVE_ONLY`, firmware lines that need native 32-bit execution whatever the host can
do: an explicit `qemu-user` and `guest_kernel = "x86-64"` are refused by `beanjar config set` and at
launch, and `auto` fails the start where the host cannot run them natively:

- **NetCast** (LM14, NetCast 4.5): the stock application writes to MMIO windows
  (`/dev/miomap`: HWI2C, OTP, PQ), whose pages mstar-utopia keeps unmapped so that every access
  faults into lgemu's `store_mmio_fault`. That handler decodes the faulting AArch32 instruction,
  which a translated program does not have: the faulting task is qemu-arm (64-bit) and the
  instruction is qemu's AArch64 code, so the handler logs `lgemu: unsupported MMIO` and sends
  SIGBUS. qemu emits plain loads and stores for guest memory accesses, so supporting it would take
  an A64 LDR/STR decoder for 64-bit tasks in the same handler. No other module decodes user
  instructions, and webOS targets do not map these windows.
- **SimpleSmart**: not tried.

webOS and the test guest are allowed.

## Mappings a driver places itself

qemu-arm (64-bit host, 32-bit guest) reserves the whole 4 GiB guest address space at start with
private, anonymous, `PROT_NONE`, `MAP_NORESERVE` mappings, and puts the guest's own mappings over
them with `MAP_FIXED`; a guest unmap puts a reservation back. A mapping the guest makes with `mmap`
on a device file goes through qemu, which tracks it. To the kernel, though, the whole space below
4 GiB is taken, so a driver that picks an address and maps it from the kernel finds none free.

Only rtk does that: Realtek's `/dev/auth` MAP looks for a free PLI base from `pli_base` up, maps
the register window there and returns the base, and ALLOC maps blocks under it; KADP uses the
addresses as returned and never calls `mmap` for them. Without room MAP returned 0 and KADP read
its registers at `0 + pli_rbus` (W22H legacy-broadcast-arib in `pli_getPTS`, from about 50 s
uptime). In a translated process (the mm's `task_size` is above 4 GiB, which a native 32-bit task's
never is) rtk counts qemu's reservation as free: a mapping that is anonymous, private,
`PROT_NONE`, `VM_NORESERVE` and never faulted (no anon_vma), so never one the guest has read or
written. It maps over it with `MAP_FIXED` and unmaps by putting such a reservation back, so the
next MAP finds the same base again, as natively.

qemu does not learn of these mappings, which a guest `mmap` would need: KADP takes the address from
the ioctl, so only a change to KADP or a preload duplicating the driver's placement could route it
through qemu. What follows from that:
- the windows are missing from the guest's emulated `/proc/self/maps` (another process reading
  `/proc/<pid>/maps` sees them);
- a system call that qemu checks itself (`lock_user`) fails with EFAULT on a buffer in them; ioctl,
  read, write, readv and writev go to the kernel unchecked through the bridge;
- qemu could map the guest's own over them only by placing a mapping there: its own `mmap`
  placement starts at 0x40000000, above the bases MAP takes first, and its heap would have to grow
  to `pli_base`; natively the kernel would refuse such a mapping instead.

## Tests

`tests/integration/bridge` boots `build/Image` in qemu-system-aarch64 under full TCG, with a small
initramfs holding busybox, qemu-arm and `bridgetest.c` built static for 32-bit ARM. It runs
bridgetest natively, under qemu-arm with `QEMU_BRIDGE=0`, and through binfmt_misc `POFE`.
bridgetest checks uinput and evdev with 16-byte events, DRM and virtio-gpu ioctls, a robust mutex
whose owner died, signal restart and EINTR, a real-time signal, fork, exec and wait, and
`/proc/<pid>/exe`. It skips without qemu-arm (`QEMU_ARM`) or a static 32-bit ARM compiler
(`BRIDGE_CC`, the webOS SDK's or Debian's).

## The x86-64 guest kernel (experimental)

`guest_kernel = "x86-64"` (`GUEST_KERNEL`) runs the same idea on an x86-64 host: an x86-64 guest
kernel on QEMU's q35 machine, under KVM where `/dev/kvm` opens and TCG otherwise, with the whole
ARM userland under an x86-64 qemu-arm. It is refused on another host, with
`userland = "native"` and on NetCast and SimpleSmart, and a
start says it is experimental.

- **Kernel**: the same tree, patches and `webos_virt_defconfig` with
  `guest/kernel/configs/x86_64.config` over it, built with `ARCH=x86_64` by the build machine's
  compiler (`meson/kbuild.sh` with `GUEST_ARCH=x86_64`) on x86-64 build hosts (meson option
  `x86_64_kernel`, auto), with the out-of-tree modules, busybox and the initramfs programs:
  build/x86_64/bzImage and build/x86_64/initramfs.cpio.gz, from the same `initramfs.list` and
  `/init`. Patch 0025 adds system call `0x7ff0` to `do_syscall_64`: an i386 call number and five
  arguments, run by `ia32_sys_call()` with `TS_COMPAT` set for that call only, for ioctl, read,
  write, readv, writev, set_robust_list, get_robust_list, getdents, getdents64, lseek and _llseek
  (ext4 gives a 32-bit caller directory offsets that fit 32 bits; a 32-bit glibc's `readdir`
  fails with EOVERFLOW on the 64-bit ones, which left webOS 23's db8 an empty database directory);
  anything else is ENOSYS. Patch 0026
  registers the media pool's ivshmem driver at `device_initcall` on x86, where ACPI claims PCI
  resources later. lgemu's AArch32 MMIO decoder is arm64-only (hence NetCast's refusal).
- **qemu-arm** (beanjar-vendor `qemu-user/`, patch 0002, the amd64 tarball): the kernel's compat
  layer here is i386's, so qemu converts ordinary calls itself and hands the kernel only those
  whose meaning depends on a 32-bit caller. ioctls whose ARM layout equals x86-64's run natively;
  SNDRV_CTL ELEM_READ/WRITE and VIDIOC_DQEVENT are repacked to i386's layout. Patch 0003 (both
  architectures) points `/proc/<pid>/cmdline` and `environ` at the guest's argv and envp with
  `prctl(PR_SET_MM_MAP)`, which needs `CONFIG_CHECKPOINT_RESTORE` (in `webos_virt_defconfig`) and
  no capability: webOS's initctl finds the service it notifies systemd for by its command line,
  and every `Type=notify` service readied through it timed out while that read `qemu-arm ...`.
- **qemu-aarch64** (the same amd64 tarball, patch 0007): the 64-bit ARM programs, which an arm64
  kernel runs natively. `QEMU_AARCH64`, else the one beside the vendored x86-64 qemu-arm
  (build/qemu-user-amd64/bin/qemu-aarch64; installed, `/usr/share/beanjar/qemu-user/amd64/`), goes
  to `/host/qemu-aarch64` with `USERLAND_AARCH64="qemu-user"` in boot.conf (`share.assemble`); a
  start without one says so and starts anyway, as only queue and spacebuns ship AArch64 programs.
  The QEMU command line is the same either way. AArch64 and x86-64 are both LP64 with the same
  alignment and ioctl encoding, so no bridge is needed: with `guest_base` 0, which qemu picks for a
  64-bit guest when the address is free, an ioctl qemu has no table entry for goes to the kernel
  unchanged (stock qemu answers ENOTTY). The generic fixes (`/proc/<pid>/cmdline`, guest-only
  `/proc/self/status`, `SO_PEERGROUPS`, `kill()` of qemu's threads, a `/proc/self/task` without
  qemu's call_rcu thread) apply to it as to qemu-arm. Its
  guest lives in the x86-64 process's 47-bit address space, below where a 48-bit AArch64 kernel's
  `mmap` would put it; only a fixed mapping above 128 TiB would fail. The test guest's `tg-arm64`
  passes under it (`scripts/testguest-check --guest-kernel x86-64`, [test guest](test-guest.md)),
  its DRM version ioctl on the render node included.
- **Machine** (`qemu.plan`): `-M q35`, `-accel kvm -cpu host` (or `-cpu max -accel tcg`),
  `-vga none`, `console=ttyS0`, and the virt machine's devices, which are all PCI; q35's root bus
  has 30 free slots. Its QEMU is the vendored qemu-system-x86_64 (beanjar-vendor's amd64 QEMU
  tarball) or `QEMU_X86_64`, never one from PATH, which would lack the display patches.
- **Packages**: the amd64 `beanjar-qemu` carries qemu-system-x86_64 (`/usr/libexec/beanjar`, its
  firmware in the shared `/usr/share/beanjar/qemu`, `-L`) and the x86-64 qemu-arm;
  `beanjar-guest` the kernel and initramfs in `/usr/share/beanjar/guest/x86_64`
  ([distribution](distribution.md)).
