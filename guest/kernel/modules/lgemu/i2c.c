// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* The I2C devices of an LG TV's board that its firmware needs answered, modelled once for every
 * SoC's bus driver (lgemu-i2c.h: lg115x.ko's /dev/lg/iic<n>, mstar-utopia.ko's HWI2C). A bus
 * module knows only which of its (bus, slave) pairs is which device, from its parameters (the
 * chip's table, guest/targets/soc/). This is the model the libhal_lg115x shim's hal-i2c.c was
 * before it, with the same behaviour:
 *
 *   The micom. A read is a command byte written and the reply read (the HAL's subAddrSize 1,
 *   subAddr = command, len = reply size); a write is the command and its arguments (the HAL's
 *   subAddrSize 0, data = command + arguments).
 *     read 0x16 (wake-on-voice)  1 byte, whether the set has the built-in far-field microphone
 *                                (mic.c's mic, the model's own fact): 1 with it, 0 without, as
 *                                the 2022 firmwares' hw-option-gen asks (supportVoiceLongDistance).
 *     read 0x67 (GPIO status)    7 bytes, one per GPIO pin, when the set has its built-in
 *                                microphone (mic.c): pin 6, its switch, 1 (on), the others 0.
 *     read 0x9A (power-on mode)  0x20 remoteKey. Never 0x28-0x2A: API_DDM_POWER_Init_PowerOnMode
 *                                shuts the set down on "last cold/cool/warm".
 *     read 0xC0 (MICOM_Task's 40 ms poll)  the micom's event queue, 3 bytes, 00 00 00 when empty.
 *                                The remote's keys arrive here, as on the set: the IR receiver
 *                                (below) queues 02 <code> <~code> for a button going down,
 *                                0A <code> <~code> for it coming up, and while it is held
 *                                03 <code> <~code> (a repeat) every IR_REPEAT_MS after the first
 *                                IR_FIRST_REPEAT_MS. tvservice checks code + ~code == 0xFF, maps
 the
 *                                code with its _gKeyTable and injects the Linux key on its own
 *                                uinput device, "LGE RCU" (MICOM_UINPUT_Init). A Japanese
 *                                remote's extended code (include/webos-ir.h, WEBOS_IR_EXT) is
 *                                <event> DB <N>, without the check byte. The set's own
                                keys arrive here too, as the micom's local key events (below,
                                "the local key"): 04/05/0E/10 <code> <~code>.
 *     read 0xB1 (power state)    3, the set on (2 in warm standby: after 26 07 or 25 02, until 25
 03
 *                                or the power key's wake). The first one marks "firmware ready" for
 *                                the host (struct webos_micom_status), as webOS's power stack
 *                                reads it as it starts. The IR receiver reports keys in either
 *                                state: the firmware filters them by it. In state 2 tvservice's
 *                                _MICOM_ProcessSingleKey (W19P) hands POWER to the UI ("1.Power
 *                                Key has been pushed") and a few other keys, and drops the rest;
 *                                micomservice's single-key tail (W22O) passes POWER to uinput.
 *     read 0x80 (RTC)            seven binary YMDHMS bytes from rtc0, independent of guest
 *                                system time; RTC failures are not answered with a fake date.
 *     write 0x24 (RTC)           unsupported: the ninth payload byte is not verified.
 *     other reads                fail, as tvservice tolerates (version, image type, ...).
 *     write 25 00 <reason>       power off (API_DDM_POWER_micomShutdownSystem), 200 ms later.
 *     write 44 <delay> <reason>  restart (DIL_MICOM_SetAutoPowerOffOn from
 *                                API_DDM_POWER_micomRebootSystem, tv.power/reboot), 200 ms later.
 *     write 27                   the power-off timeout (DIL_MICOM_SetPowerOffTimeOut, from
 *                                tv.power/setPowerOffTimeout): the power goes in 8 s, times
 *                                time_scale (tvpowerd's "Setting Try Micom PowerOff timer
 *                                8sec") unless ...
 *     write 52                   ... cancels it (DIL_MICOM_CancelPowerOff).
 *     write 26 07 <a> <b>, 25 02 warm standby (Quick Start+, below); 25 03 active.
 *     other writes               acknowledged (23 host ready, CEC, ...).
 *   The power switch ends webOS's PID namespace as reboot(2) called in it does (reboot_pid_ns):
 *   the namespace of the process that wrote the command, whose init then exits with SIGINT (power
 *   off) or SIGHUP (restart); guest/initramfs/rootfs/bin/webos-start turns that into the VM's
 power-off or
 *   reset, after a sync. A command from the initial namespace (a test from the debug shell) is
 *   logged and does nothing.
 *
 *   The IR receiver: the viewer's remote windows and PC keyboard send button edges, 4-byte
 *   records (include/webos-ir.h), on the webos.ir virtio-serial port. A virtio port cannot be
 *   read from the kernel (its file has no read_iter), so the initramfs's pump (guest/initramfs/src/
 *   virtio-ir.c) copies the port's stream to /dev/lgemu-ir and closes it when the host side goes
 *   away, which releases a held button. Buttons with a custom code other than LG's 0x04 are
 *   ignored, as by the micom. The other way, the pump reads the standby from /dev/lgemu-ir for the
 *   host (below, "standby and the power line").
 *
 *   The NVM EEPROM, 32 KiB, 2-byte big-endian address, wrapping at 32 KiB. DIL_NVM keeps offsets
 *   below 0x1C30 here, every init DB among them; the rest is the eMMC "nvram" partition
 *   (tvservice's private view, guest/layers/common/tvservice/libc-private.c). Backed by the block
 *   device nvm_disk (guest/initramfs/rootfs/init: the state disk boot.conf's NVM_EEPROM names),
 read whole at
 *   load and written through its page cache. What a factory programs into a new one (the
 *   ModelInfoDB, the ToolOptionDB) the launcher has written before the VM starts
 *   (host/core/beanjar/nvm.py).
 *
 *   The speaker amplifier. tvservice writes its registers at start-up, on every volume change
 *   and around turnOffAv/turnOnAv, and fails the Luna call when a write is not acknowledged.
 *   Writes are acknowledged and not kept: the volume the recording follows is the sound
 *   engine's (snd.c, the playback gain). A read is not acknowledged.
 *
 *   The tuner, a Silicon Labs Si21xx (W15U's Si2178B, the RF tuner its analog front end has on
 *   every board: TU_PAL_SI2178B_PLLInitialize initialises it unconditionally, and with no chip
 *   answering the probe it dereferences a context the probe never set up). Its command interface:
 *   a write is a command and its arguments, a read the reply, whose first byte is the status.
 *   Every write is acknowledged, every reply is CTS (0x80: clear to send, no error) and zeros: a
 *   chip that has done what it was told and has nothing to report (no signal). */
#define pr_fmt(fmt) KBUILD_MODNAME ": i2c: " fmt
#include <linux/bitops.h>
#include <linux/fs.h>
#include <linux/gpio/consumer.h>
#include <linux/interrupt.h>
#include <linux/jiffies.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/pid_namespace.h>
#include <linux/pm_domain.h>
#include <linux/pm_wakeup.h>
#include <linux/poll.h>
#include <linux/sched/signal.h>
#include <linux/sched/task.h>
#include <linux/slab.h>
#include <linux/spinlock.h>
#include <linux/string.h>
#include <linux/suspend.h>
#include <linux/timekeeping.h>
#include <linux/uaccess.h>
#include <linux/vmalloc.h>
#include <linux/workqueue.h>
#include "lgemu.h"
#include "lgemu-drv.h"
#include "lgemu-i2c.h"
#include "micom-rtc.h"
#include "../../../../include/webos-ir.h"

static char *nvm_disk;
module_param(nvm_disk, charp, 0444);
MODULE_PARM_DESC(nvm_disk, "i2c: the block device behind the NVM EEPROM (none: its accesses fail)");

/* ---- the micom's power switch ---- */

#define POWER_ACTION_MS     200 /* the write is acknowledged first, as by the micom */
#define POWER_OFF_TIMEOUT_S 8
/* The guest's slowdown against a set (webos.time_scale=N, the launcher's TCG factor; 1 under KVM),
 * which init passes on. The power-off timeout is the micom's own clock, a backstop for a shutdown
 * that a set finishes well within it; a guest N times slower gets N times as long. Without it a TCG
 * guest's restart (tvpowerd arms the timeout, then reboots) ends as a power-off. */
static unsigned int time_scale = 1;
module_param(time_scale, uint, 0444);
MODULE_PARM_DESC(time_scale, "the guest's slowdown: the power-off timeout times N");
#define POWER_ON_REMOTE_KEY 0x20
#define POWER_STATE_ON      3 /* read 0xB1: DIL_MICOM_GetPowerState's "active" */
#define POWER_STATE_WARM    2

/* The namespace a power command ends and its init. */
struct power_target {
    struct pid_namespace *ns;
    struct task_struct *reaper;
};

static DEFINE_MUTEX(power_lock);
static struct power_target action_target, timer_target;
static int action_sig;
static u8 power_state = POWER_STATE_ON;
/* webOS's power stack has started: its first read of the power state (0xB1), which the power stack
 * makes as it starts (tvservice on WT1H, tvpowerd on W22O), 0.3 to 0.8 s before tvpowerd answers.
 * Set once, from the load of this module (a cold boot); the status carries it to the host
 * (VM.power_off_refusal). */
static bool firmware_ready;

/* The caller's webOS namespace into t; false from the initial namespace. */
static bool power_target_get(struct power_target *t)
{
    struct pid_namespace *ns = task_active_pid_ns(current);

    if (ns == &init_pid_ns)
        return false;
    rcu_read_lock();
    t->reaper = READ_ONCE(ns->child_reaper);
    if (t->reaper)
        get_task_struct(t->reaper);
    rcu_read_unlock();
    if (!t->reaper)
        return false;
    t->ns = get_pid_ns(ns);
    return true;
}

static void power_target_put(struct power_target *t)
{
    if (t->reaper)
        put_task_struct(t->reaper);
    if (t->ns)
        put_pid_ns(t->ns);
    *t = (struct power_target){};
}

/* What reboot(2) does in a child PID namespace: its init exits with sig (SIGINT power off,
 * SIGHUP restart). */
static void cut_power(struct power_target *t, int sig)
{
    t->ns->reboot = sig;
    send_sig(SIGKILL, t->reaper, 1);
}

static void power_action(struct work_struct *work)
{
    mutex_lock(&power_lock);
    if (action_target.ns)
        cut_power(&action_target, action_sig);
    power_target_put(&action_target);
    mutex_unlock(&power_lock);
}
static DECLARE_DELAYED_WORK(action_work, power_action);

static void power_timer(struct work_struct *work)
{
    mutex_lock(&power_lock);
    if (timer_target.ns) {
        pr_info("micom: power-off timer expired, cutting the power\n");
        cut_power(&timer_target, SIGINT);
    }
    power_target_put(&timer_target);
    mutex_unlock(&power_lock);
}
static DECLARE_DELAYED_WORK(timer_work, power_timer);

static void cancel_timer(void)
{
    mutex_lock(&power_lock);
    pr_info("micom: power-off cancelled%s\n", timer_target.ns ? "" : " (no timer armed)");
    cancel_delayed_work(&timer_work);
    power_target_put(&timer_target);
    mutex_unlock(&power_lock);
}

static void schedule_power(int sig, const char *what)
{
    mutex_lock(&power_lock);
    power_target_put(&action_target);
    if (power_target_get(&action_target)) {
        pr_info("micom: %s\n", what);
        action_sig = sig;
        mod_delayed_work(system_wq, &action_work, msecs_to_jiffies(POWER_ACTION_MS));
    } else {
        pr_warn("micom: %s: not from webOS's namespace, ignored\n", what);
    }
    mutex_unlock(&power_lock);
}

/* Each arm restarts the timeout, as the micom's timer. */
static void arm_timer(void)
{
    mutex_lock(&power_lock);
    power_target_put(&timer_target);
    if (power_target_get(&timer_target)) {
        unsigned int s = POWER_OFF_TIMEOUT_S * max(time_scale, 1U);

        pr_info("micom: power-off timeout armed, the power goes in %u s unless cancelled\n", s);
        mod_delayed_work(system_wq, &timer_work, s * HZ);
    } else {
        pr_warn("micom: power-off timeout: not from webOS's namespace, ignored\n");
    }
    mutex_unlock(&power_lock);
}

/* ---- standby and the power line ----
 *
 * Quick Start+ (docs/dev/architecture/suspend.md): tvservice puts the micom in warm standby (26 07
 * <a> <b> from API_DDM_POWER_prepareSuspendMode on an external micom, or 25 02) and tvpowerd
 * suspends the SoC through /dev/lgib (lgib.c); the micom stays on, and its power key wakes the SoC.
 * The SoC's wake line here is QEMU virt's power button, PL061 GPIO 3 (the line its DT's
 * /gpio-keys/poweroff names, which QMP system_powerdown pulses for 100 ms): this part owns it (the
 * kernel has no gpio-keys driver, so webOS sees no power-button input) and makes its interrupt a
 * wakeup source, so a pulse ends suspend-to-idle and the micom is active again. A pulse while the
 * SoC runs does nothing, except in warm standby before the suspend: that key is latched and wakes
 * the SoC as it goes down. After the wake tvservice reads the power-on mode (0x9A, remote key;
 * _sysonoff_ prepareResume through API_DDM_POWER_LoadPowerOnMode), and may activate the micom (25
 * 03).
 *
 * The host learns of the standby from the IR pump (guest/initramfs/src/virtio-ir.c), which reads it
 * here: /dev/lgemu-ir is readable, one byte (WEBOS_IR_STATUS_*) each time it changes. Standby is
 * the time the SoC is suspended, from the kernel's suspend entry (a PM notifier's
 * PM_SUSPEND_PREPARE, the last point before the pump is frozen; then the freezer and the devices'
 * suspend, milliseconds, to s2idle) to its exit (PM_POST_SUSPEND): not the micom's warm standby,
 * which comes before the set's own pre-suspend work (seconds on a slow path, with the CPU busy).
 * WT1H's sleepd suspends without a warm standby. Before a suspend the notifier waits for the pump
 * to have passed it on (its next poll), as the pump is frozen after it. A power key latched in
 * warm standby still wakes the SoC at the suspend's noirq entry (wake_noirq). */
static DECLARE_WAIT_QUEUE_HEAD(status_wq);
static unsigned int status_seq;   /* changes of the standby state, with status_wq.lock */
static unsigned int status_acked; /* the last one the pump has passed on */
static bool status_reader;        /* the pump has read the state at least once */
static bool soc_suspended;
static bool wake_latched; /* a power key while going down, to wake the SoC as it is down */
static int power_line_irq = -1;
static struct gpio_desc *power_line;

static u8 standby_status(void)
{
    return READ_ONCE(soc_suspended) ? WEBOS_IR_STATUS_STANDBY : WEBOS_IR_STATUS_ON;
}

/* The standby state may have changed: wake the pump; the sequence number of the change. */
static unsigned int status_changed(void)
{
    unsigned int seq;

    spin_lock(&status_wq.lock);
    seq = ++status_seq;
    wake_up_locked_poll(&status_wq, EPOLLIN);
    spin_unlock(&status_wq.lock);
    return seq;
}

static void set_power_state(u8 state, const char *why);
static void keys_suspend(void);

static int pm_notify(struct notifier_block *nb, unsigned long action, void *data)
{
    unsigned int seq;

    switch (action) {
    case PM_SUSPEND_PREPARE:
        keys_suspend();
        WRITE_ONCE(soc_suspended, true);
        seq = status_changed();
        if (READ_ONCE(status_reader) &&
            !wait_event_timeout(status_wq, (int)(READ_ONCE(status_acked) - seq) >= 0, HZ))
            pr_warn("micom: the IR pump did not pass the standby on before the suspend\n");
        break;
    case PM_POST_SUSPEND:
        /* the latched key ended the suspend at its entry (wake_noirq): the micom is active now,
         * as after a key that woke the SoC (tvservice reads 0x9A, remote key, then) */
        if (xchg(&wake_latched, false))
            set_power_state(POWER_STATE_ON, "woken by the power key");
        WRITE_ONCE(soc_suspended, false);
        status_changed();
        break;
    }
    return NOTIFY_DONE;
}
static struct notifier_block pm_nb = {.notifier_call = pm_notify};

/* The micom's power state (read 0xB1); a change is the host's too. */
static void set_power_state(u8 state, const char *why)
{
    if (xchg(&power_state, state) != state) {
        pr_info("micom: %s: %s\n", why, state == POWER_STATE_WARM ? "warm standby" : "active");
        status_changed();
    }
}

/* A pulse that woke the SoC: its handler runs when the wakeup interrupt is replayed at the resume,
 * while soc_suspended still holds (the PM notifier clears it after the thaw). */
static bool power_line_woke;

static irqreturn_t power_line_isr(int irq, void *data)
{
    WRITE_ONCE(power_line_woke, READ_ONCE(soc_suspended));
    return IRQ_WAKE_THREAD;
}

/* The latched key's wake: with the devices in noirq suspend, after the PM notifier's
 * PM_SUSPEND_PREPARE and before s2idle's loop, which ends at once for a pending wakeup, so the
 * SoC is up again as it is down. (The freezer clears a wakeup, so it cannot be raised earlier.) */
static int wake_noirq(struct device *dev)
{
    if (READ_ONCE(wake_latched)) {
        pr_info("micom: the latched power key wakes the SoC as it suspends\n");
        pm_system_wakeup();
    }
    return 0;
}
static struct dev_pm_domain wake_pm_domain = {.ops = {.suspend_noirq = wake_noirq}};

/* In its own thread (status_wq's lock is not taken with interrupts off). Only a suspended SoC
 * wakes. A real micom latches a press while the set is going into warm standby and wakes it as
 * soon as it is down (the host sends the key when the standby shows, before the suspend, which a
 * loaded host stretches to seconds): the press is latched, and delivered at the suspend's entry
 * (wake_noirq). Without a warm standby it is lost, as the set ignores the key while it powers off.
 */
static irqreturn_t power_line_thread(int irq, void *data)
{
    if (!READ_ONCE(power_line_woke)) {
        if (READ_ONCE(power_state) == POWER_STATE_WARM) {
            WRITE_ONCE(wake_latched, true);
            pr_info("micom: power key on the wake line: the SoC is not suspended yet, latched\n");
        } else {
            pr_info(
                "micom: power key on the wake line: the SoC is not suspended, nothing to wake\n");
        }
        return IRQ_HANDLED;
    }
    WRITE_ONCE(wake_latched, true); /* also a key just before the s2idle's loop: it ends at once */
    pr_info("micom: power key on the wake line: the SoC wakes\n");
    set_power_state(POWER_STATE_ON, "woken by the power key");
    return IRQ_HANDLED;
}

static void power_line_init(void)
{
    struct device_node *np = of_find_node_by_path("/gpio-keys/poweroff");
    int err;

    if (!np) {
        pr_info("micom: no power button in the device tree: nothing wakes a suspended guest\n");
        return;
    }
    power_line = fwnode_gpiod_get_index(of_fwnode_handle(np), NULL, 0, GPIOD_IN, "micom wake");
    of_node_put(np);
    if (IS_ERR(power_line)) {
        pr_warn("micom: power button GPIO: %ld: nothing wakes a suspended guest\n",
                PTR_ERR(power_line));
        power_line = NULL;
        return;
    }
    power_line_irq = gpiod_to_irq(power_line);
    err = power_line_irq < 0
              ? power_line_irq
              : request_threaded_irq(power_line_irq, power_line_isr, power_line_thread,
                                     IRQF_TRIGGER_RISING | IRQF_ONESHOT, "lgemu-micom-wake", NULL);
    if (!err)
        err = enable_irq_wake(power_line_irq);
    if (err) {
        pr_warn("micom: power button wake line: %d: nothing wakes a suspended guest\n", err);
        if (power_line_irq >= 0)
            free_irq(power_line_irq, NULL);
        power_line_irq = -1;
        gpiod_put(power_line);
        power_line = NULL;
        return;
    }
    pr_info("micom: wake line is the power button (irq %d)\n", power_line_irq);
}

static void power_line_exit(void)
{
    if (power_line_irq >= 0) {
        disable_irq_wake(power_line_irq);
        free_irq(power_line_irq, NULL);
    }
    power_line_irq = -1;
    if (power_line)
        gpiod_put(power_line);
    power_line = NULL;
}

/* ---- the micom's IR receiver ---- */

#define IR_EVENTS 32
#define IR_FIRST_REPEAT_MS                                                                         \
    500                  /* a held button repeats after this: the micom firmware's own             \
                          * filter is not visible, and a tap of 100-200 ms must not */
#define IR_REPEAT_MS 108 /* then at the NEC repeat-frame rate */
static unsigned int ir_first_repeat_ms = IR_FIRST_REPEAT_MS;
static unsigned int ir_repeat_ms = IR_REPEAT_MS;
#define EV_REMOTE_KEY     0x02
#define EV_REMOTE_REPEAT  0x03
#define EV_REMOTE_RELEASE 0x0A
#define IR_CODE_POWER     0x08 /* LG's POWER, KEY_POWER (116) to the firmware */

static DEFINE_SPINLOCK(ir_lock);
static u8 ir_queue[IR_EVENTS][3]; /* the poll's reply: event and two bytes */
static unsigned int ir_head, ir_len;
static int ir_held = -1;   /* the code of the button down (ir_push), or -1 */
static u64 ir_next_repeat; /* ms, monotonic */

static u64 now_ms(void)
{
    return ktime_get_ns() / NSEC_PER_MSEC;
}

/* An event's two bytes: a data code and its complement (code + ~code == 0xFF, as tvservice
 * checks), or a Japanese remote's extended code WEBOS_IR_EXT(N) as 0xDB N, with no complement
 * (include/webos-ir.h). */
static void ir_bytes(u8 *b, int code)
{
    if (code > 0xFF) {
        b[0] = (u8)(code >> 8);
        b[1] = (u8)code;
    } else {
        b[0] = (u8)code;
        b[1] = (u8)~code;
    }
}

/* With ir_lock held. A full queue drops its oldest event. code: a data code, or WEBOS_IR_EXT(N). */
static void ir_push(u8 event, int code)
{
    unsigned int tail;

    if (ir_len == IR_EVENTS) {
        ir_head = (ir_head + 1) % IR_EVENTS;
        ir_len--;
    }
    tail = (ir_head + ir_len++) % IR_EVENTS;
    ir_queue[tail][0] = event;
    ir_bytes(&ir_queue[tail][1], code);
}

/* A button edge. A second button going down releases the first, as a remote sends one code. */
static void ir_edge(bool down, int code)
{
    spin_lock(&ir_lock);
    if (down) {
        if (ir_held >= 0)
            ir_push(EV_REMOTE_RELEASE, ir_held);
        ir_push(EV_REMOTE_KEY, code);
        ir_held = code;
        ir_next_repeat = now_ms() + ir_first_repeat_ms;
    } else if (ir_held == code) {
        ir_push(EV_REMOTE_RELEASE, code);
        ir_held = -1;
    }
    spin_unlock(&ir_lock);
}

/* ---- the local key ----
 *
 * The set's own buttons (a onekey set's one key, a joystick), which the micom reads itself and
 * reports on the same 0xC0 poll as its local key events (micomservice's MicomHandler): 04 <code>
 * <~code> a key down, 05 a repeat while held, 0E its release, 10 a single press with its release.
 * The host sends the button's edges (include/webos-ir.h, WEBOS_LOCAL_MAGIC). The press button
 * (WEBOS_LOCAL_PRESS, the onekey or a joystick's centre) is the micom's to time: released before
 * local_long_ms it is a tap, 10 EA; held that long, 04 EB once, and 0E EB at its release
 * (micomservice turns EA into IR_FRONT_ONEKEY_SHORT and EB into IR_FRONT_ONEKEY_LONG on a onekey
 * set). A joystick's direction is 04 <code> down, 05 repeats as the remote's, 0E up. As the
 * remote's keys they are reported in warm standby too (local_record). */
#define EV_LOCAL_KEY     0x04
#define EV_LOCAL_REPEAT  0x05
#define EV_LOCAL_RELEASE 0x0E
#define EV_LOCAL_TAP     0x10
#define LOCAL_LONG_MS    1000
static unsigned int local_long_ms = LOCAL_LONG_MS;
module_param(local_long_ms, uint, 0644);
MODULE_PARM_DESC(local_long_ms, "i2c: how long the local key is held for a long press (ms)");

static int local_held = -1; /* the local key code down, or -1 (with ir_lock) */
static bool local_long;     /* the press button's long press has been reported */
static u64 local_next;      /* ms: the press button's long press, a direction's repeat */

/* With ir_lock held. */
static void local_edge(bool down, u8 code)
{
    if (down) {
        if (local_held == code)
            return;
        if (local_held >= 0 && (local_held != WEBOS_LOCAL_PRESS || local_long))
            ir_push(EV_LOCAL_RELEASE,
                    local_held == WEBOS_LOCAL_PRESS ? WEBOS_LOCAL_LONG : (u8)local_held);
        local_held = code;
        local_long = false;
        if (code == WEBOS_LOCAL_PRESS) {
            local_next = now_ms() + local_long_ms;
        } else {
            ir_push(EV_LOCAL_KEY, code);
            local_next = now_ms() + ir_first_repeat_ms;
        }
    } else if (local_held == code) {
        if (code != WEBOS_LOCAL_PRESS)
            ir_push(EV_LOCAL_RELEASE, code);
        else if (local_long)
            ir_push(EV_LOCAL_RELEASE, WEBOS_LOCAL_LONG);
        else
            ir_push(EV_LOCAL_TAP, WEBOS_LOCAL_PRESS);
        local_held = -1;
    }
}

/* With ir_lock held: the held local key's long press or repeat, when due. */
static bool local_due(u8 *event, u8 *code)
{
    u64 now;

    if (local_held < 0 || (local_held == WEBOS_LOCAL_PRESS && local_long))
        return false;
    now = now_ms();
    if (now < local_next)
        return false;
    if (local_held == WEBOS_LOCAL_PRESS) {
        *event = EV_LOCAL_KEY;
        *code = WEBOS_LOCAL_LONG;
        local_long = true;
        return true;
    }
    *event = EV_LOCAL_REPEAT;
    *code = (u8)local_held;
    local_next += ir_repeat_ms;
    if (local_next <= now)
        local_next = now + ir_repeat_ms;
    return true;
}

/* The sender went away: a held button comes up. A press button not yet long is dropped: no tap
 * the user did not finish. */
static void ir_lost(void)
{
    int held, local;

    spin_lock(&ir_lock);
    held = ir_held;
    if (held >= 0) {
        ir_push(EV_REMOTE_RELEASE, held);
        ir_held = -1;
    }
    local = local_held;
    if (local >= 0 && (local != WEBOS_LOCAL_PRESS || local_long))
        ir_push(EV_LOCAL_RELEASE, local == WEBOS_LOCAL_PRESS ? WEBOS_LOCAL_LONG : (u8)local);
    local_held = -1;
    spin_unlock(&ir_lock);
    if (held >= 0)
        pr_info("micom: IR sender gone, 0x%02x released\n", held);
    if (local >= 0)
        pr_info("micom: IR sender gone, local key 0x%02x released\n", local);
}

/* The poll's reply: a queued event, else the held button's repeat when due, else none. */
static void ir_poll(u8 *buf, unsigned int len)
{
    u8 event = 0, code = 0, bytes[2] = {0, 0};

    spin_lock(&ir_lock);
    if (ir_len) {
        event = ir_queue[ir_head][0];
        bytes[0] = ir_queue[ir_head][1];
        bytes[1] = ir_queue[ir_head][2];
        ir_head = (ir_head + 1) % IR_EVENTS;
        ir_len--;
    } else if (local_due(&event, &code)) {
        ir_bytes(bytes, code);
    } else if (ir_held >= 0) {
        u64 now = now_ms();

        if (now >= ir_next_repeat) {
            event = EV_REMOTE_REPEAT;
            ir_bytes(bytes, ir_held);
            ir_next_repeat += ir_repeat_ms;
            if (ir_next_repeat <= now)
                ir_next_repeat = now + ir_repeat_ms;
        }
    }
    spin_unlock(&ir_lock);
    if (event == EV_LOCAL_KEY || event == EV_LOCAL_REPEAT || event == EV_LOCAL_RELEASE ||
        event == EV_LOCAL_TAP)
        pr_info_ratelimited("micom: local key event %02x %02x\n", event, bytes[0]);
    else if (event && bytes[0] == WEBOS_IR_EXT_PREFIX && event != EV_REMOTE_REPEAT)
        pr_info("micom: extended key event %02x %02x %02x\n", event, bytes[0], bytes[1]);
    memset(buf, 0, len);
    if (event && len >= 3) {
        buf[0] = event;
        buf[1] = bytes[0]; /* the code and its check byte (code + ~code == 0xFF), or 0xDB N */
        buf[2] = bytes[1];
    }
}

/* Direct MStar IR controllers configure their decoder's repeat delays through
 * MDRV_IR_SET_DELAYTIME; the transport still carries the same remote edges. */
void lgemu_ir_set_delay(unsigned int first_ms, unsigned int repeat_ms)
{
    spin_lock(&ir_lock);
    ir_first_repeat_ms = first_ms;
    ir_repeat_ms = repeat_ms;
    spin_unlock(&ir_lock);
}
EXPORT_SYMBOL_GPL(lgemu_ir_set_delay);

/* /dev/lgemu-ir: the webos.ir stream from the pump; each open file keeps its partial record. */
struct ir_file {
    u8 rec[sizeof(struct webos_ir_record)];
    unsigned int have;
    unsigned int seen; /* the standby change this reader has read */
};

static int ir_open(struct inode *inode, struct file *file)
{
    struct ir_file *f = kzalloc(sizeof(struct ir_file), GFP_KERNEL);

    if (!f)
        return -ENOMEM;
    spin_lock(&status_wq.lock);
    f->seen = status_seq - 1; /* the first read has the state at once */
    spin_unlock(&status_wq.lock);
    file->private_data = f;
    return 0;
}

static void micom_status(struct webos_micom_status *st);

/* The state (struct webos_micom_status, the standby byte first; up to n bytes of it) when it has
 * changed since this reader's last read. */
static ssize_t ir_read(struct file *file, char __user *ubuf, size_t n, loff_t *pos)
{
    struct ir_file *f = file->private_data;
    struct webos_micom_status status;
    unsigned int seq;
    int err;

    if (!n)
        return 0;
    spin_lock(&status_wq.lock);
    if (file->f_flags & O_NONBLOCK)
        err = f->seen != status_seq ? 0 : -EAGAIN;
    else
        err = wait_event_interruptible_locked(status_wq, f->seen != status_seq);
    seq = status_seq;
    spin_unlock(&status_wq.lock);
    if (err)
        return err;
    micom_status(&status);
    n = min(n, sizeof(status));
    if (copy_to_user(ubuf, &status, n))
        return -EFAULT;
    f->seen = seq;
    WRITE_ONCE(status_reader, true);
    return n;
}

/* Readable when the standby state has changed. A poll after a read tells a waiting suspend
 * (pm_notify) that the pump has passed that change on. */
static __poll_t ir_file_poll(struct file *file, poll_table *wait)
{
    struct ir_file *f = file->private_data;
    __poll_t mask = EPOLLOUT | EPOLLWRNORM;

    poll_wait(file, &status_wq, wait);
    spin_lock(&status_wq.lock);
    if (f->seen != status_seq)
        mask |= EPOLLIN | EPOLLRDNORM;
    if ((int)(f->seen - status_acked) > 0) {
        status_acked = f->seen;
        wake_up_locked(&status_wq);
    }
    spin_unlock(&status_wq.lock);
    return mask;
}

static int ir_release(struct inode *inode, struct file *file)
{
    kfree(file->private_data);
    ir_lost();
    return 0;
}

/* A local key edge from the host. The micom reports the set's own key in warm standby too, as the
 * remote's: micomservice takes a local key in the micom's power state 2 (its Active Standby, the
 * SoC running with the screen off) and turns the set on. One the firmware has not read when the
 * SoC suspends wakes it instead (keys_suspend). */
static void local_record(bool down, u8 code)
{
    pr_info("micom: local key 0x%02x %s%s\n", code, down ? "down" : "up",
            READ_ONCE(power_state) == POWER_STATE_WARM ? " (warm standby)" : "");
    spin_lock(&ir_lock);
    local_edge(down, code);
    spin_unlock(&ir_lock);
}

/* The SoC suspends (PM_SUSPEND_PREPARE): key events still queued are dropped, as a key read after
 * the wake would act a second time. A local key or the remote's POWER among them wakes the SoC as
 * it goes down, as the micom wakes it on those keys (the host's relay does the same once the
 * standby shows); the remote's other keys are lost, as the micom ignores them while the SoC is
 * suspended. */
static void keys_suspend(void)
{
    unsigned int i, kept = 0, wakes = 0, dropped = 0;

    spin_lock(&ir_lock);
    for (i = 0; i < ir_len; i++) {
        u8 *e = ir_queue[(ir_head + i) % IR_EVENTS];

        if (e[0] == EV_LOCAL_KEY || e[0] == EV_LOCAL_REPEAT || e[0] == EV_LOCAL_RELEASE ||
            e[0] == EV_LOCAL_TAP || (e[0] == EV_REMOTE_KEY && e[1] == IR_CODE_POWER)) {
            wakes++;
            continue;
        }
        if (e[0] == EV_REMOTE_KEY || e[0] == EV_REMOTE_REPEAT || e[0] == EV_REMOTE_RELEASE) {
            dropped++;
            continue;
        }
        memmove(ir_queue[(ir_head + kept) % IR_EVENTS], e, sizeof(ir_queue[0]));
        kept++;
    }
    ir_len = kept;
    if (local_held >= 0 || ir_held == IR_CODE_POWER)
        wakes++;
    local_held = -1;
    ir_held = -1;
    spin_unlock(&ir_lock);
    if (dropped)
        pr_info("micom: the SoC suspends with %u remote key events unread: dropped\n", dropped);
    if (wakes) {
        WRITE_ONCE(wake_latched, true);
        pr_info("micom: the SoC suspends with a power or local key unread: it wakes the SoC as it "
                "suspends\n");
    }
}

static ssize_t ir_write(struct file *file, const char __user *ubuf, size_t n, loff_t *pos)
{
    struct ir_file *f = file->private_data;
    u8 chunk[64];
    size_t done = 0;

    while (done < n) {
        size_t k = min(n - done, sizeof(chunk));

        if (copy_from_user(chunk, ubuf + done, k))
            return done ? done : -EFAULT;
        for (size_t i = 0; i < k; i++) {
            struct webos_ir_record *r = (struct webos_ir_record *)f->rec;

            if (!f->have && chunk[i] != WEBOS_IR_MAGIC && chunk[i] != WEBOS_IR_EXT_MAGIC &&
                chunk[i] != WEBOS_LOCAL_MAGIC)
                continue; /* out of step: find the next record */
            f->rec[f->have++] = chunk[i];
            if (f->have < sizeof(f->rec))
                continue;
            f->have = 0;
            if (r->magic == WEBOS_LOCAL_MAGIC)
                local_record(r->down != 0, r->code);
            else if (r->custom != WEBOS_IR_CUSTOM_LG)
                pr_info_ratelimited("micom: IR custom code 0x%02x ignored (LG's is 0x%02x)\n",
                                    r->custom, WEBOS_IR_CUSTOM_LG);
            else if (r->magic == WEBOS_IR_EXT_MAGIC)
                ir_edge(r->down != 0, WEBOS_IR_EXT(r->code));
            else
                ir_edge(r->down != 0, r->code);
        }
        done += k;
    }
    return n;
}

static const struct file_operations ir_fops = {
    .owner = THIS_MODULE,
    .open = ir_open,
    .release = ir_release,
    .read = ir_read,
    .write = ir_write,
    .poll = ir_file_poll,
    .llseek = noop_llseek,
};

static struct miscdevice ir_device = {
    .minor = MISC_DYNAMIC_MINOR,
    .name = "lgemu-ir",
    .fops = &ir_fops,
    .mode = 0600,
};
static bool ir_registered;

/* ---- the front LED ----
 *
 * The micom drives the set's front LED (the C2's power indicator) itself; webOS only writes its
 * settings, through libmicom_led. Each command's last payload is latched here and logged, and
 * the host reads it with the standby (/dev/lgemu-ir, struct webos_micom_status), its front LED:
 *   6E 01 <v> 01   Set_PowerIndicator(0, v): the standby light (micomservice's standByLight,
 *                  1 on, 0 off)          6F 02 <v> 01  Set_PowerIndicator(1, v)
 *   0E <v>         Set_CenterLEDControl   15 <v>  Set_RGBLEDControl   69 <v>  Set_PWMLEDControl
 *   14 <k> 03      Set_LEDBarControl, Set_RGBLEDTest                   68 <v>  Set_LEDColorSelect
 *   20 <v>         Set_LEDControl         DF      Set_WOVLEDControl */
static const u8 led_cmds[WEBOS_MICOM_LED_CMDS] = {0x6E, 0x6F, 0x0E, 0x15, 0x69,
                                                  0x14, 0x68, 0x20, 0xDF};
static struct webos_micom_led led_latch[WEBOS_MICOM_LED_CMDS];
static DEFINE_SPINLOCK(led_lock);

/* A write of an LED command: latched, logged; false for any other command. */
static bool led_write(const u8 *data, unsigned int len, const char *hex)
{
    unsigned int i, n = min(len - 1, (unsigned int)WEBOS_MICOM_LED_BYTES);
    struct webos_micom_led *l;
    bool changed;

    for (i = 0; i < WEBOS_MICOM_LED_CMDS && led_cmds[i] != data[0]; i++)
        ;
    if (i == WEBOS_MICOM_LED_CMDS)
        return false;
    l = &led_latch[i];
    spin_lock(&led_lock);
    changed = !l->writes || l->len != n || memcmp(l->data, data + 1, n);
    l->cmd = data[0];
    l->len = n;
    memset(l->data, 0, sizeof(l->data));
    memcpy(l->data, data + 1, n);
    if (!++l->writes)
        l->writes = 1;
    spin_unlock(&led_lock);
    pr_info_ratelimited("micom write %s: front LED%s\n", hex, changed ? "" : " (unchanged)");
    if (changed)
        status_changed();
    return true;
}

/* The state the host reads: the standby, the micom's power state, whether the firmware's power
 * stack has started, the front LED's latches. */
static void micom_status(struct webos_micom_status *st)
{
    unsigned int i;

    memset(st, 0, sizeof(*st));
    st->standby = standby_status();
    st->power = READ_ONCE(power_state);
    st->ready = READ_ONCE(firmware_ready);
    spin_lock(&led_lock);
    for (i = 0; i < WEBOS_MICOM_LED_CMDS; i++) {
        st->led[i] = led_latch[i];
        st->led[i].cmd = led_cmds[i];
    }
    spin_unlock(&led_lock);
}

/* ---- the micom ---- */

/* battery=1: the set has a battery behind its micom, the StanbyME's (N21D, LM21U). tvbatteryd's
 * libmicom_sensor asks it: micom_battery_Get_Status reads 0x0C (10 bytes on webOS 24, 8 on
 * webOS 6) and, on webOS 24, 0x09 (10 bytes); readMicomBatteryInfo reads 0x0B (1 byte, the
 * battery's I2C test, 1 = "OK") every fifth poll. 0x0C is byte 0 charging, 1 connected (AC),
 * 2 full_charged, 3 the level (0..99 shown as one more, 100 as 100), 4-5 and 6-7 two big-endian
 * temperatures in 0.1 K; of 0x09 only bytes 2-5 are kept, in fields no status reports.
 * getDiagnosisInfo (Settings' battery page, service-logger's cycle count) reads 0x0D and 0x0F,
 * 10 bytes each (micom_battery_Get_SelfDiagnostics). The fixed
 * state is a full battery on the adapter that is still charging: Home's status bar draws the
 * charging icon only for charging=1, and batteryPolicy.json's charging_off stops the charge
 * (0x0A 00) when connected, charging and full_charged are all 1, so full_charged stays 0.
 * Without a battery these reads are not answered. */
static bool battery;
module_param(battery, bool, 0444);
MODULE_PARM_DESC(battery, "answer the micom's battery reads: full, on AC, charging");

#define BATTERY_DECIKELVIN 2980 /* 25.0 C as tvbatteryd reads it (K - 273.0), inside 0..50 C */

/* 0x0D, five big-endian words in mV, which getDiagnosisInfo prints in decimal: total_voltage,
 * cell_1_voltage..cell_4_voltage; a four-cell pack at the end of its charge */
static const u16 battery_volts[] = {16718, 4180, 4178, 4181, 4179};
/* 0x0F, five big-endian words, printed in hex with "0x": cycle_count, protection_mode (0 none),
 * operation_mode (Settings' "BMS Operation Status", 0), fw_version (nothing checks it); the fifth
 * is not read */
static const u16 battery_bms[] = {12, 0, 0, 0x0110, 0};

static int battery_words(const u16 *words, unsigned int n, u8 *buf, unsigned int len)
{
    unsigned int i;

    for (i = 0; i < n && 2 * i + 1 < len; i++) {
        buf[2 * i] = words[i] >> 8;
        buf[2 * i + 1] = words[i] & 0xff;
    }
    return 0;
}

static int battery_read(u8 cmd, u8 *buf, unsigned int len)
{
    if (!battery || !len)
        return -EREMOTEIO;
    memset(buf, 0, len);
    switch (cmd) {
    case 0x0C: /* micom_battery_Get_Status */
        if (len < 8)
            return -EREMOTEIO;
        buf[0] = 1;   /* charging */
        buf[1] = 1;   /* connected */
        buf[2] = 0;   /* full_charged */
        buf[3] = 100; /* level */
        buf[4] = buf[6] = BATTERY_DECIKELVIN >> 8;
        buf[5] = buf[7] = BATTERY_DECIKELVIN & 0xff;
        return 0;
    case 0x09: /* micom_battery_Get_ChargingTimeInfo: no time left */
        return 0;
    case 0x0D: /* micom_battery_Get_SelfDiagnostics, first read: the voltages */
        return battery_words(battery_volts, ARRAY_SIZE(battery_volts), buf, len);
    case 0x0F: /* its second read: the BMS's state */
        return battery_words(battery_bms, ARRAY_SIZE(battery_bms), buf, len);
    case 0x0B: /* micom_battery_Get_BatteryCommunicationTest */
        buf[0] = 1;
        return 0;
    default:
        return -EREMOTEIO;
    }
}

/* micom_trace=1: every micom read and its answer in the kernel log (bring-up of a firmware
 * whose micom protocol is not known yet; the 40 ms event poll, 0xC0, only when it has an event) */
static bool micom_trace;
module_param(micom_trace, bool, 0644);
MODULE_PARM_DESC(micom_trace, "log every micom read and its answer");

static int micom_answer(const u8 *sub, unsigned int subsize, u8 *buf, unsigned int len);

static int micom_read(const u8 *sub, unsigned int subsize, u8 *buf, unsigned int len)
{
    int ret = micom_answer(sub, subsize, buf, len);

    if (micom_trace && (subsize != 1 || sub[0] != 0xC0 || (len && buf[0])))
        pr_info("micom read %02x (%u bytes): %s%*ph\n", subsize ? sub[0] : 0, len,
                ret ? "no answer" : "", ret ? 0 : (int)min(len, 16u), buf);
    return ret;
}

#define MIC_SWITCH_PIN 6

static int micom_answer(const u8 *sub, unsigned int subsize, u8 *buf, unsigned int len)
{
    if (subsize != 1)
        return -EREMOTEIO;
    switch (sub[0]) {
    case 0x80: /* CP_READ_RTC_YMDHMS: verified seven-byte binary calendar */
        return lgemu_micom_rtc_read(buf, len);
    case 0x16: /* micom_misc_Get_isSupportWOV: one byte, nonzero when the set has the far-field
                * (wake-on-voice) microphone. hw-option-gen's supportVoiceLongDistance is this
                * byte (W22O ponytail, W24G ombre), or it and nyxDeviceInfo's
                * support_voice_long_distance (queue). Unanswered, ponytail's KADP and rtk.ko's
                * MISC_UCOM_READ drop the error and the caller's 0xFF preset reads "has one",
                * while queue's KADP passes it on and reads "has none": so the micom answers
                * for every model, 1 or 0. */
        if (len != 1)
            return -EREMOTEIO;
        buf[0] = lgemu_mic_present();
        return 0;
    case 0x67: /* micom_misc_Get_GPIO_status: one byte per GPIO pin 0..6, the reply's byte N
                * pin N's level. Pin 6 is the built-in microphone's switch
                * (micom_misc_Get_MIC_Status): 1 is on, voiceinput_sound's micSwitchStatus
                * "high", which the microphone service needs; 0 is off ("low"). Answered on the
                * sets with the microphone (mic.c): the switch on, the other pins 0. */
        if (!lgemu_mic_present() || !len)
            return -EREMOTEIO;
        memset(buf, 0, len);
        if (len > MIC_SWITCH_PIN)
            buf[MIC_SWITCH_PIN] = 1;
        return 0;
    case 0x9A: /* DIL_MICOM_GetPowerOnMode */
        if (!len)
            return -EREMOTEIO;
        memset(buf, 0, len);
        buf[0] = POWER_ON_REMOTE_KEY;
        return 0;
    case 0xB1: /* DIL_MICOM_GetPowerState */
        if (!len)
            return -EREMOTEIO;
        memset(buf, 0, len);
        buf[0] = READ_ONCE(power_state);
        if (!xchg(&firmware_ready, true)) {
            pr_info("micom: firmware ready: %s (%s) read the power state\n",
                    current->group_leader->comm, current->comm);
            status_changed();
        }
        return 0;
    case 0xC0: /* MICOM_Task's poll: byte 0 is the event, 0 = none */
        ir_poll(buf, len);
        return 0;
    case 0x09:
    case 0x0B:
    case 0x0C:
    case 0x0D:
    case 0x0F:
        return battery_read(sub[0], buf, len);
    default: /* Unknown reads: no answer */
        return -EREMOTEIO;
    }
}

static DECLARE_BITMAP(micom_logged, 256);

static int micom_write(const u8 *data, unsigned int len)
{
    char hex[3 * 16 + 1] = "";
    unsigned int i;

    if (len < 1)
        return -EREMOTEIO;
    for (i = 0; i < len && i < 16; i++)
        snprintf(hex + 3 * i, 4, "%02x ", data[i]);
    hex[3 * i - 1] = 0;
    if (led_write(data, len, hex))
        return 0;
    switch (data[0]) {
    case 0x24: /* RTC write: the ninth payload byte is not verified. */
        return -EREMOTEIO;
    case 0x25: /* system power control: 25 <state> <arg> */
        if (len >= 2 && (data[1] == 2 || data[1] == 3)) { /* warm standby, activate */
            if (data[1] == 3)
                WRITE_ONCE(wake_latched, false);
            set_power_state(data[1] == 2 ? POWER_STATE_WARM : POWER_STATE_ON, hex);
            return 0;
        }
        if (len >= 2 && data[1] == 0) {
            pr_info("micom write %s: power off (reason 0x%02x)\n", hex, len > 2 ? data[2] : 0);
            cancel_timer();
            schedule_power(SIGINT, "shutdown system, cutting the power");
            return 0;
        }
        break;
    case 0x44: /* auto power off/on: 44 <delay> <reason>, the micom's restart */
        pr_info("micom write %s: restart\n", hex);
        cancel_timer();
        schedule_power(SIGHUP, "reboot system, cycling the power");
        return 0;
    case 0x27:
        pr_info("micom write %s: power-off timeout\n", hex);
        arm_timer();
        return 0;
    case 0x52:
        pr_info("micom write %s: cancel power-off\n", hex);
        cancel_timer();
        return 0;
    case 0x26: /* instant control: 26 07 <a> <b> (DIL_MICOM_SendSystemInstantControlCommand),
                * which API_DDM_POWER_prepareSuspendMode sends an external micom before the
                * suspend: Quick Start+'s standby */
        if (len >= 2 && data[1] == 7) {
            set_power_state(POWER_STATE_WARM, hex);
            return 0;
        }
        break;
    case 0x47: /* CEC transmit, every few seconds: not logged */
        return 0;
    }
    /* the remaining writes once per command */
    if (!test_and_set_bit(data[0], micom_logged))
        pr_info("micom write %s: acknowledged (further %02x writes are not logged)\n", hex,
                data[0]);
    return 0;
}

/* ---- the NVM EEPROM ---- */

#define NVM_SIZE 0x8000

static u8 *nvm; /* NULL: no backing, every access fails */
static struct file *nvm_file;
static DEFINE_MUTEX(nvm_lock);

static void nvm_open(void)
{
    loff_t pos = 0;
    ssize_t n;

    if (!nvm_disk || !*nvm_disk) {
        pr_info("no NVM EEPROM disk: its accesses fail\n");
        return;
    }
    nvm_file = filp_open(nvm_disk, O_RDWR | O_LARGEFILE, 0);
    if (IS_ERR(nvm_file)) {
        pr_warn("NVM EEPROM disk %s: %ld: its accesses fail\n", nvm_disk, PTR_ERR(nvm_file));
        nvm_file = NULL;
        return;
    }
    nvm = vzalloc(NVM_SIZE);
    n = nvm ? kernel_read(nvm_file, nvm, NVM_SIZE, &pos) : -ENOMEM;
    if (n != NVM_SIZE) {
        pr_warn("NVM EEPROM disk %s: short read (%zd): its accesses fail\n", nvm_disk, n);
        vfree(nvm);
        nvm = NULL;
        filp_close(nvm_file, NULL);
        nvm_file = NULL;
        return;
    }
    pr_info("NVM EEPROM from %s\n", nvm_disk);
}

static void nvm_close(void)
{
    if (nvm_file) {
        vfs_fsync(nvm_file, 0);
        filp_close(nvm_file, NULL);
    }
    vfree(nvm);
    nvm = NULL;
    nvm_file = NULL;
}

/* A 32 KiB (24C256-type) EEPROM ignores the top bit of its 16-bit address and wraps at the end
 * of the array. tvservice relies on that: after each write _NVM_WaitStable polls the chip with
 * a one-byte access at 0xFFFF until it acknowledges, for up to 200 ms; refusing that address made
 * every write take 200 ms, and a new state's first boot about 30 s longer. Writes complete at
 * once here, so the first poll is acknowledged. The chip's wrap within a page on a page write
 * is not modelled: DIL_NVM never writes across its 32-byte pages. */
static int nvm_access(bool write, const u8 *sub, unsigned int subsize, u8 *buf, unsigned int len)
{
    unsigned int addr, n, done;
    int ret = 0;

    if (!nvm || subsize != 2)
        return -EREMOTEIO;
    addr = ((unsigned int)sub[0] << 8 | sub[1]) % NVM_SIZE;
    mutex_lock(&nvm_lock);
    for (done = 0; done < len; done += n, addr = (addr + n) % NVM_SIZE) {
        loff_t pos = addr;

        n = min(len - done, NVM_SIZE - addr);
        if (!write) {
            memcpy(buf + done, nvm + addr, n);
            continue;
        }
        memcpy(nvm + addr, buf + done, n);
        if (kernel_write(nvm_file, buf + done, n, &pos) != n) {
            pr_warn_ratelimited("NVM EEPROM disk: write at 0x%04x failed\n", addr);
            ret = -EREMOTEIO;
        }
    }
    mutex_unlock(&nvm_lock);
    return ret;
}

/* ---- the interface of the bus modules ----
 *
 * A transfer as it goes on the wire (lgemu-i2c.h). What each device takes, the forms the
 * libhal_lg115x shim's HAL_I2C_Read/Write had (subAddr, then data):
 *   micom  a write of one command byte and a read of the reply (a command read), or one write
 *          of the command and its arguments (a command write)
 *   nvm    a write of the 2-byte address and a read, or one write of the address and the data
 *   amp    writes, acknowledged; a read is not
 *   accel  the BMA422's register pointer and registers (accel.c)
 *   light1, light2  the ambient light sensor's registers, as pqcontroller reads them (light.c)
 *   tof    the TMF8821 ToF motion sensor's registers, as msc reads them (tof.c) */

static bool is_read(const struct i2c_msg *m)
{
    return m->flags & I2C_M_RD;
}

static int micom_xfer(struct i2c_msg *msgs, int num)
{
    if (num == 2 && !is_read(&msgs[0]) && is_read(&msgs[1]))
        return micom_read(msgs[0].buf, msgs[0].len, msgs[1].buf, msgs[1].len);
    if (num == 1 && !is_read(&msgs[0]))
        return micom_write(msgs[0].buf, msgs[0].len);
    return -EREMOTEIO;
}

static int nvm_xfer(struct i2c_msg *msgs, int num)
{
    if (num == 2 && !is_read(&msgs[0]) && is_read(&msgs[1]))
        return nvm_access(false, msgs[0].buf, msgs[0].len, msgs[1].buf, msgs[1].len);
    if (num == 1 && !is_read(&msgs[0]) && msgs[0].len >= 2)
        return nvm_access(true, msgs[0].buf, 2, msgs[0].buf + 2, msgs[0].len - 2);
    return -EREMOTEIO;
}

static int amp_xfer(struct i2c_msg *msgs, int num)
{
    for (int i = 0; i < num; i++)
        if (is_read(&msgs[i]))
            return -EREMOTEIO;
    return 0;
}

static int tuner_xfer(struct i2c_msg *msgs, int num)
{
    for (int i = 0; i < num; i++)
        if (is_read(&msgs[i]) && msgs[i].len) {
            memset(msgs[i].buf, 0, msgs[i].len);
            msgs[i].buf[0] = 0x80; /* CTS */
        }
    return 0;
}

int lgemu_i2c_xfer(enum lgemu_i2c_device dev, struct i2c_msg *msgs, int num)
{
    int ret;

    switch (dev) {
    case LGEMU_I2C_MICOM:
        ret = micom_xfer(msgs, num);
        break;
    case LGEMU_I2C_NVM:
        ret = nvm_xfer(msgs, num);
        break;
    case LGEMU_I2C_AMP:
        ret = amp_xfer(msgs, num);
        break;
    case LGEMU_I2C_ACCEL:
        ret = lgemu_accel_xfer(msgs, num);
        break;
    case LGEMU_I2C_LIGHT1:
        ret = lgemu_light_xfer(1, msgs, num);
        break;
    case LGEMU_I2C_LIGHT2:
        ret = lgemu_light_xfer(2, msgs, num);
        break;
    case LGEMU_I2C_TOF:
        ret = lgemu_tof_xfer(msgs, num);
        break;
    case LGEMU_I2C_TUNER:
        ret = tuner_xfer(msgs, num);
        break;
    default:
        return -EINVAL;
    }
    return ret ? ret : num;
}
EXPORT_SYMBOL_GPL(lgemu_i2c_xfer);

int lgemu_i2c_user_xfer(enum lgemu_i2c_device dev, u16 addr, const u8 *sub, u16 sub_len,
                        void __user *data, u32 size, bool read)
{
    u8 *buf = kzalloc(sub_len + size + 1, GFP_KERNEL);
    int ret;

    if (!buf)
        return -ENOMEM;
    memcpy(buf, sub, sub_len);
    if (!read) {
        struct i2c_msg msg = {.addr = addr, .len = sub_len + size, .buf = buf};

        ret = copy_from_user(buf + sub_len, data, size) ? -EFAULT : lgemu_i2c_xfer(dev, &msg, 1);
    } else {
        struct i2c_msg msgs[2] = {
            {.addr = addr, .len = sub_len, .buf = buf},
            {.addr = addr, .flags = I2C_M_RD, .len = size, .buf = buf + sub_len},
        };
        int first = sub_len ? 0 : 1;

        ret = lgemu_i2c_xfer(dev, msgs + first, 2 - first);
        if (ret >= 0 && copy_to_user(data, buf + sub_len, size))
            ret = -EFAULT;
    }
    kfree(buf);
    return ret;
}
EXPORT_SYMBOL_GPL(lgemu_i2c_user_xfer);

int lgemu_i2c_parse(const char *spec, enum lgemu_i2c_device *dev, unsigned int *nums, int count)
{
    static const char *const names[] = {
        [LGEMU_I2C_MICOM] = "micom", [LGEMU_I2C_NVM] = "nvm",       [LGEMU_I2C_AMP] = "amp",
        [LGEMU_I2C_ACCEL] = "accel", [LGEMU_I2C_LIGHT1] = "light1", [LGEMU_I2C_LIGHT2] = "light2",
        [LGEMU_I2C_TOF] = "tof",     [LGEMU_I2C_TUNER] = "tuner",
    };
    char name[8];
    int i;

    if (lgemu_parse_named(spec, name, sizeof(name), 0, nums, count))
        return -EINVAL;
    *dev = LGEMU_I2C_NONE;
    for (i = 1; i < ARRAY_SIZE(names); i++)
        if (!strcmp(name, names[i]))
            *dev = i;
    return *dev == LGEMU_I2C_NONE ? -EINVAL : 0;
}
EXPORT_SYMBOL_GPL(lgemu_i2c_parse);

int lgemu_i2c_init(void)
{
    int err = misc_register(&ir_device);

    if (err)
        return err;
    ir_registered = true;
    dev_pm_domain_set(ir_device.this_device, &wake_pm_domain);
    nvm_open();
    power_line_init();
    register_pm_notifier(&pm_nb);
    return 0;
}

void lgemu_i2c_exit(void)
{
    if (ir_registered)
        misc_deregister(&ir_device);
    ir_registered = false;
    unregister_pm_notifier(&pm_nb);
    power_line_exit();
    cancel_delayed_work_sync(&action_work);
    cancel_delayed_work_sync(&timer_work);
    power_target_put(&action_target);
    power_target_put(&timer_target);
    nvm_close();
}
