# LM14 driver checks

For the W21A bring-up, `check-shmsize.c` queries the measured 10 MiB capacity and
`check-named-sync.c` checks the native named semaphore/RW-lock ABI. Build both as
static ARM or ARM64 executables with `-O2 -Wall -Wextra -Werror`, and run only in
a W21A test VM. `check-direct-ir.c` must be built as a dynamic ARM executable
with `-ldl` and run inside that VM's firmware root. It loads the stock KADP
library and injects a single NEC frame into the virtual receiver; do not run
it on a real TV. This is a driver check; it
does not start the native remote service.

`check-mailbox-layouts.c` is a static AArch32 VM check of Utopia 700's packed
mailbox structs and the older aligned variants. It uses a private message class,
guards ioctl output boundaries, preserves FRC information across round trips,
and checks enable, loopback, removal and cleanup. Compile with
`-static -O2 -Wall -Wextra -Werror`; run with a bounded timeout in the W21A VM.
`tests/regress/input/check-ir.py` separately checks native PM/micom remote delivery.

Run these only in an isolated test VM with disposable state. The ABI and register fields
come from the selected LM14 04.11.18 libraries and the matching TSP HAL sequence. They do not modify vendor files.

`check-mmap-table.py LIBMADP LIVE_DUMP` compares every extracted pair with a live dump from
stock `MAdp_SYS_GetMMAP`, then checks rejection of changed instructions, truncated ELF and
invalid MIU geometry. The live dump has `ID HEX_ADDRESS HEX_LENGTH` rows starting at ID zero.
Inputs and generated records belong under `build/`.

Build `check-semutex.c` as a static arm64 or AArch32 executable with `-O2 -Wall -Wextra
-Werror`. Run it before firmware in a fresh isolated init0 VM after loading `mstar-utopia`
with the selected imported LM14 memory geometry and `secure_shm_id=7 mbx_trace=1`.
It checks named mutexes across separate opens, timeout,
cross-file unlock, holder release, a holder that exits while a forked child keeps its file, deletion while held, and shared-memory aliases and
last-user release. The mailbox trace records the held-lock timeout.

For the ARM64 semutex check, build into the test output directory:

```sh
mkdir -p build/tests/mstar
aarch64-linux-gnu-gcc -static -O2 -Wall -Wextra -Werror \
  tests/regress/mstar/check-semutex.c -o build/tests/mstar/check-semutex
```

Copy `check-semutex`, `mstar-utopia.ko` and the selected derived `mstar-mmap.bin`
into the isolated VM's host share. Stop its automatic GOP presenter before
unloading or reloading the MStar module, since the presenter can hold a reference;
never force unload. The measured run used the full imported LM14 profile plus
test-only `secure_shm_id=7 mbx_trace=1`. This compact recipe shows the semutex
prerequisites rather than reproducing that full profile verbatim:

```sh
insmod /host/mstar-utopia.ko lm14=1 miu_interval=0x80000000 \
  miu_len=0x40000000 miu0_bus=0x20000000 miu1_bus=0xa0000000 \
  mmap_ids=77 lx_mem_id=0 lx_mem2_id=76 lx_mem2_bias=0x40000000 \
  mmap_file=/host/mstar-mmap.bin secure_shm_id=7 mbx_trace=1
[ -e /dev/semutex ] || mknod /dev/semutex c 181 0
/host/check-semutex
rmmod mstar_utopia
```

Keep the instance isolated from stock startup and stop it after the check.

Build `check-lm14-mpool.c` and `check-lm14-tsp.c` as static arm64 executables and put them in
the isolated host share. Load the LM14 module with the imported table, two 1 GiB MIUs at
interval `0x80000000`, and `tsp_riu=0x1f200000:0x1500`. Run the MPOOL check from the
initramfs shell. It verifies two MIU1 mappings share writes, rejects inconsistent SET
requests and checks acceptance of a final-page SET request.

Before starting firmware, run `check-lm14-tsp selftest`. It writes only its modeled test
registers and a disposable MIU buffer at the stock observed download address `0x1b4c0000`.
It checks source/length/START gating, cleared control, repeated requests and preservation
of other control bits. It also checks ALIVE gating and command 5/6 address configuration,
including pending/committed/reset state through `/proc/mstar-utopia/tsp`. The module logs CRC32 of copied QMEM. Compare its two destination
checksums with independently calculated CRC32 of these 16 KiB byte sequences:

```python
import zlib
for xor in (0, 0x55):
    print(f'{zlib.crc32(bytes((i & 255) ^ xor for i in range(16384))):08x}')
```

Unload/reload the module after these checks to discard the test backing before starting
stock firmware. Without `selftest`, the TSP executable only reads the bounded register
aperture. It does not issue a download.

Build `check-lm14-tsp-alive.c` with the selected firmware's ARM SDK and `-ldl`.
In the init0 guest, mount its stock vendor partition at `/webos/mnt/vendor`, then
run `chroot /webos /mnt/host/check-lm14-tsp-alive 100 phase` after the QMEM test.
It loads genuine MMIO/DMX providers and makes 100 independent HAL calls, recording
both return values and elapsed time. `phase` varies a small delay between calls;
there are no retries. A single false return fails the run. Use the explicit host
halt-poll helper for this LM14 test; normal shared-profile regressions keep defaults.

Build `check-lm14-idac.c` as a static arm64 helper and run it in a separate init0
profile with `lm14=1 adc_idac=1 lm14_unprovisioned=0`. Do not inherit the production
`lm14_unprovisioned=1` setting: its synchronous A32 transport is deliberately
unsupported for this ARM64 helper. This explicitly selects virtual zero CVBS
calibration, not shipping OTP data.
The check validates no-START, unsupported selector, all three published calibration bytes,
preserved unrelated control bits and repeated transactions. Reload the module before
stock startup to discard fixture register writes.

The synchronous MMIO transport has a separate synthetic test device, not a shipping
module or an LG register profile. Build the current kernel and `lgemu` first, then:

```sh
meson/kbuild.sh module "$PWD/subprojects/linux-6.18.53" \
  "$(echo "$PWD"/build/kernel/*/linux-obj)" aarch64-linux-gnu- \
  "$PWD/tests/regress/mstar" "$PWD/build/mmio-fixture-obj" "$PWD/build/mmio-fixture.ko" \
  "$(echo "$PWD"/build/kernel/*/lgemu/obj/Module.symvers)"
/opt/arm-webos-linux-gnueabi_sdk-buildroot/bin/arm-webos-linux-gnueabi-gcc \
  -marm -march=armv7-a -static -O2 -Wall -Wextra -Werror \
  tests/regress/mstar/check-mmio-fault.c -o build/check-mmio-fault
```

Put those two artifacts in the isolated init0 guest's host share. Load
`insmod /host/mmio-fixture.ko`, read its dynamic major from `/proc/devices`, and
create `/dev/mmio-fixture` with that major and minor 0. Run `/host/check-mmio-fault`.
It exercises scalar addressing, W1C and aliases, unsupported accesses without state
changes, and actual ptrace single-step completion. The helper kills/reaps its child on
failure. Unload `mmio_fixture` afterwards and stop the isolated VM. Its synthetic
register contents are test expectations, not firmware register values.

Build `check-lm14-otp.c` with the same ARM SDK options. Run it in a fresh isolated init0
guest with the imported selected-firmware memory table, `lm14=1 adc_idac=1
lm14_unprovisioned=1`, and `/webos/dev/miomap` available. It tests the observed read
selector ranges, explicitly blank hardware-owned storage, pending unsupported/write
requests, and inactive per-port status across aliases and byte/halfword/word lanes.
No real OTP data or authentication engine is involved. Reload the module or use a fresh
VM before stock startup. The opt-in model supports A32 little-endian scalar accesses;
an arm64 build of this test is deliberately unsupported.

Build `check-lm14-pq-y.c` with the same ARM SDK options. In a fresh isolated
init0 guest, supply the selected firmware's imported memory geometry and
`lm14=1 lm14_pq_y_sram=1`. The test exercises the selected four 128-entry,
ten-byte Y banks and four 64-entry, five-byte C banks, separate storage,
alias readback, publication before completion, hardware-owned output lanes,
and pending disabled, ambiguous, reserved, or out-of-bank requests.
`/proc/mstar-utopia/pq_y_sram` reports separate counters and CRCs for both RAMs.
Reload the module before stock startup so synthetic coefficients are discarded.
The same test validates the selected 691-entry gamma and 256-entry degamma
write controllers, including whole-storage CRCs calculated from its synthetic
payload, publication before completion, and pending disabled/reserved/overrun
requests. Their read-command dialect is not modeled because it has not been
established from the selected firmware.

Build `check-lm14-hwi2c.c` with the same ARM SDK options. In a fresh isolated init0 guest load
the module with the imported memory geometry and `lm14=1 lm14_hwi2c=1
i2c=micom:0x0052,amp:0x0254` (lgemu's micom on HWI2C port index 0, its amplifier on port index
2; a test placement, not the board's). It drives the selected libdrvHWI2C register sequence:
START, address, command, repeated START, receives with ACK and NACK, STOP, and checks the
interrupt flag, the state, acknowledgement by the placed devices only, a write delivered at its
STOP, an unanswered micom command and an absent slave not acknowledged, reset, register storage
outside the decoded bytes and rejection of the upper half of a register slot.
`/proc/mstar-utopia/hwi2c` counts each port's transfers and NACKs.

## Isolated refactor validation

The nine isolated checks passed with their documented profiles, including genuine
HAL TSP ALIVE returning true in all 100 calls. The initial ARM64 IDAC attempt
inherited `lm14_unprovisioned=1` and received SIGBUS; rerunning with the documented
separate `lm14_unprovisioned=0` profile passed. This corrected a harness setup
error and required no module behavior change. Final MStar and synthetic MMIO
fixture unloads both returned zero.
