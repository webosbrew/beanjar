// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
#include "private.h"

/* Registers the userland depends on beyond reading back what it wrote (MStar's register interface,
 * RIU: 16-bit registers, register r of a window at base + 2 r; both given as "base:reg:..."):
 * reg_init, a power-on value ("base:reg:value"); reg_autoclear, bits the hardware clears when the
 * operation they start is done ("base:reg:mask"; the module clears them within a few ms). */
static char *reg_init[32], *reg_autoclear[32];
static int reg_init_count, reg_autoclear_count;
module_param_array(reg_init, charp, &reg_init_count, 0444);
MODULE_PARM_DESC(reg_init, "registers' power-on values, base:reg:value (16 bits)");
module_param_array(reg_autoclear, charp, &reg_autoclear_count, 0444);
MODULE_PARM_DESC(reg_autoclear, "self-clearing register bits, base:reg:mask (16 bits)");

/* ---- the backing store: pages by address, allocated on first touch (lgemu-drv.h) ---- */

static struct lgemu_store registers = LGEMU_STORE_INIT(registers); /* by bus address */

static struct autoclear {
    u16 *reg;
    u16 mask;
} autoclears[ARRAY_SIZE(reg_autoclear)];
static struct delayed_work autoclear_work;

/* LM14 HAL_TSP_CPU_SetBase downloads 0x1000 words from MIU memory into
 * the transport processor's QMEM. Its 16-bit register slots are four bytes
 * apart (LG's regTSP.h). This models the DMA, not execution of TSP firmware. */
static char *tsp_riu;
module_param(tsp_riu, charp, 0444);
MODULE_PARM_DESC(tsp_riu, "TSP download controller RIU base:flat bank index (LM14)");
static u16 *tsp_cpu_lo, *tsp_cpu_hi, *tsp_src_lo, *tsp_words, *tsp_src_hi, *tsp_ctrl;
static u16 *tsp_cmd_lo, *tsp_cmd_hi, *tsp_data_lo, *tsp_data_hi;
static u8 tsp_qmem[0x4000];
static bool tsp_loaded;
static DEFINE_SPINLOCK(tsp_service_lock);
static bool tsp_mem_high_valid, tsp_mem_configured;
static u16 tsp_mem_high;
static u32 tsp_mem_base;
static struct hrtimer tsp_mailbox_timer;
#define TSP_MAILBOX_NS 200000

int tsp_registers(void)
{
    u32 v[2];
    u64 base;

    if (!tsp_riu)
        return 0;
    if (!lm14 || lgemu_parse_u32s(tsp_riu, 16, v, 2))
        return -EINVAL;
    base = v[0] + 2ULL * v[1];
    tsp_cpu_lo = lgemu_store_ptr(&registers, base + 0x180);
    tsp_cpu_hi = lgemu_store_ptr(&registers, base + 0x184);
    tsp_src_lo = lgemu_store_ptr(&registers, base + 0x1e0);
    tsp_words = lgemu_store_ptr(&registers, base + 0x1e4);
    tsp_src_hi = lgemu_store_ptr(&registers, base + 0x228);
    tsp_ctrl = lgemu_store_ptr(&registers, base + 0x1e8);
    tsp_cmd_lo = lgemu_store_ptr(&registers, base + 0x100);
    tsp_cmd_hi = lgemu_store_ptr(&registers, base + 0x104);
    tsp_data_lo = lgemu_store_ptr(&registers, base + 0x200);
    tsp_data_hi = lgemu_store_ptr(&registers, base + 0x204);
    return tsp_cpu_lo && tsp_cpu_hi && tsp_src_lo && tsp_words && tsp_src_hi && tsp_ctrl &&
                   tsp_cmd_lo && tsp_cmd_hi && tsp_data_lo && tsp_data_hi
               ? 0
               : -ENOMEM;
}

/* No allocation or sleeping: callable from the fast register timer. The
 * stock HAL has a finite spin budget shorter than a jiffy. */
static void tsp_mailbox_tick(void)
{
    unsigned long flags;
    u16 ctrl, op, payload;
    bool ack = false;

    if (!tsp_ctrl)
        return;
    spin_lock_irqsave(&tsp_service_lock, flags);
    ctrl = READ_ONCE(*tsp_ctrl);
    if (!READ_ONCE(tsp_loaded) || (ctrl & 3) != 3 || (ctrl & 4)) {
        tsp_mem_high_valid = tsp_mem_configured = false;
        goto out;
    }
    op = READ_ONCE(*tsp_cmd_hi);
    payload = READ_ONCE(*tsp_cmd_lo);
    if (op == 0x100 && !payload) {
        WRITE_ONCE(*tsp_data_lo, 0);
        WRITE_ONCE(*tsp_data_hi, 0x100);
        ack = true;
    } else if (op == 0x500) {
        tsp_mem_high = payload;
        tsp_mem_high_valid = true;
        ack = true;
    } else if (op == 0x600 && tsp_mem_high_valid) {
        u32 base = ((u32)tsp_mem_high << 16) | payload;

        /* These commands carry an address, not a buffer length. */
        for (unsigned int id = 0; id < memory_record_count(); id++) {
            u64 pa = record_pa(id);

            if (base >= pa && (u64)base < pa + memory_record_length(id)) {
                tsp_mem_base = base;
                tsp_mem_configured = true;
                tsp_mem_high_valid = false;
                pr_info_ratelimited("TSP firmware DRAM base=%08x\n", tsp_mem_base);
                ack = true;
                break;
            }
        }
    }
    /* Unknown or invalid requests retain their latch. Publish the state/data
     * first, then clear the command high half as the completion commit. */
    if (ack) {
        if (op != 0x100)
            WRITE_ONCE(*tsp_cmd_lo, 0);
        smp_wmb();
        WRITE_ONCE(*tsp_cmd_hi, 0);
    }
out:
    spin_unlock_irqrestore(&tsp_service_lock, flags);
}

static enum hrtimer_restart tsp_mailbox_run(struct hrtimer *timer)
{
    if (!READ_ONCE(tsp_loaded))
        return HRTIMER_NORESTART;
    tsp_mailbox_tick();
    hrtimer_forward_now(timer, ns_to_ktime(TSP_MAILBOX_NS));
    return HRTIMER_RESTART;
}

static void tsp_download_tick(void)
{
    u16 ctrl;
    u64 source;
    bool covered = false;

    if (!tsp_ctrl)
        return;
    ctrl = READ_ONCE(*tsp_ctrl);
    if (!(ctrl & 4) || (ctrl & 8))
        return;
    {
        unsigned long flags;

        spin_lock_irqsave(&tsp_service_lock, flags);
        WRITE_ONCE(tsp_loaded, false);
        tsp_mem_high_valid = tsp_mem_configured = false;
        spin_unlock_irqrestore(&tsp_service_lock, flags);
    }
    /* The observed DRAM-only QMEM transaction; unsupported configurations
     * leave DONE clear rather than claim a transfer that did not happen. */
    if (READ_ONCE(*tsp_cpu_lo) || READ_ONCE(*tsp_cpu_hi) ||
        READ_ONCE(*tsp_words) != sizeof(tsp_qmem) / 4)
        return;
    source = ((u64)(READ_ONCE(*tsp_src_hi) & 0xff) << 24) | ((u64)READ_ONCE(*tsp_src_lo) << 8);
    for (unsigned int id = 0; id < memory_record_count(); id++) {
        u64 base = record_pa(id);

        if (source >= base && source + sizeof(tsp_qmem) <= base + memory_record_length(id)) {
            covered = true;
            break;
        }
    }
    if (!covered)
        return;
    for (size_t off = 0; off < sizeof(tsp_qmem);) {
        u64 addr = source + off;
        size_t n = min_t(size_t, PAGE_SIZE - offset_in_page(addr), sizeof(tsp_qmem) - off);
        void *p = lgemu_store_ptr(memory_store(), addr);

        if (!p)
            return;
        memcpy(tsp_qmem + off, p, n);
        off += n;
    }
    /* Publish completion only after the requested bytes reached QMEM. */
    if (READ_ONCE(*tsp_ctrl) == ctrl) {
        smp_wmb();
        WRITE_ONCE(tsp_loaded, true);
        WRITE_ONCE(*tsp_ctrl, ctrl | 8);
        pr_info_ratelimited("TSP QMEM DMA: source=%llx bytes=%zu crc32=%08x\n", source,
                            sizeof(tsp_qmem), crc32_le(~0, tsp_qmem, sizeof(tsp_qmem)) ^ ~0);
    }
}

/* Explicit virtual CVBS calibration, not physical OTP contents. Selected LM14
 * byte indices 204c/204e and 2081/2082/2083 expand to these PM offsets.
 * No analog CVBS output is modeled; the virtual calibration storage is zero. */
static bool adc_idac;
module_param(adc_idac, bool, 0444);
MODULE_PARM_DESC(adc_idac, "LM14 virtual zero CVBS IDAC trim read controller");
static u8 *adc_start, *adc_selector, *adc_result[3];
static const u8 adc_calibration[3] = {0, 0, 0};
static bool lm14_unprovisioned;
module_param(lm14_unprovisioned, bool, 0444);
MODULE_PARM_DESC(lm14_unprovisioned,
                 "LM14 synchronous blank virtual OTP and inactive HDCP input status");
static bool lm14_pq_y_sram;
module_param(lm14_pq_y_sram, bool, 0444);
MODULE_PARM_DESC(lm14_pq_y_sram, "LM14 synchronous Y/C coefficient SRAM read/write controller");
static DEFINE_SPINLOCK(lm14_mmio_lock);
static const u64 lm14_trap_pages[] = {0x1f004000, 0x1f221000, 0x1f226000, 0x1f264000};
static u8 *lm14_trap_bytes[ARRAY_SIZE(lm14_trap_pages)];
static const u64 lm14_status[] = {0x1f221584, 0x1f226784, 0x1f226f84, 0x1f226b84};
static const u16 pq_y_input[] = {0x70c, 0x710, 0x714, 0x724, 0x728};
static const u16 pq_y_output[] = {0x718, 0x71c, 0x720, 0x72c, 0x730};
static u8 pq_y_records[512][10];
static u32 pq_y_writes, pq_y_reads;
static u8 pq_c_records[256][5];
static u32 pq_c_writes, pq_c_reads;
static u16 pq_gamma_records[0x2b3], pq_degamma_records[256];
static u32 pq_gamma_writes, pq_degamma_writes;

/* Selected Hal_PQ_set_sram_xvycc_gamma_table: 0x68 starts a write to
 * address/data registers following each control word. No read command is
 * established for this interface. Caller owns lm14_mmio_lock. */
static void lm14_pq_gamma_transaction(u8 *page, unsigned int off)
{
    u16 ctrl, index, value;
    u16 *records = off == 0xbd4 ? pq_gamma_records : pq_degamma_records;
    unsigned int count =
        off == 0xbd4 ? ARRAY_SIZE(pq_gamma_records) : ARRAY_SIZE(pq_degamma_records);

    memcpy(&ctrl, page + off, 2);
    memcpy(&index, page + off + 4, 2);
    memcpy(&value, page + off + 8, 2);
    if (ctrl != 0x68 || index >= count)
        return;
    records[index] = value;
    if (off == 0xbd4)
        pq_gamma_writes++;
    else
        pq_degamma_writes++;
    ctrl &= ~8;
    memcpy(page + off, &ctrl, 2);
}

/* Selected Hal_PQ_{set,get}_{y,c}_sram_table: distinct coefficient RAMs.
 * Caller owns the MMIO lock; completion follows actual copying. */
static void lm14_pq_y_transaction(u8 *page)
{
    u16 ctrl, index, op, mode;

    memcpy(&ctrl, page + 0x704, sizeof(ctrl));
    memcpy(&index, page + 0x708, sizeof(index));
    op = ctrl & 0x300;
    mode = ctrl & 3;
    if ((mode != 1 && mode != 2) || (ctrl & ~0x303) || index >= ARRAY_SIZE(pq_y_records) ||
        (op != 0x100 && op != 0x200))
        return;
    if (mode == 2) {
        /* Selected C banks: 64 five-byte entries at addresses 0, 0x40,
         * 0x180, 0x1c0. The gap is not a bank; Y and C are separate RAMs. */
        if (index >= 0x80 && index < 0x180)
            return;
        index = index < 0x80 ? index : index - 0x100;
        for (unsigned int i = 0; i < 3; i++) {
            unsigned int bytes = i == 2 ? 1 : 2;
            if (op == 0x100)
                memcpy(pq_c_records[index] + i * 2, page + pq_y_input[i], bytes);
            else
                memcpy(page + pq_y_output[i], pq_c_records[index] + i * 2, bytes);
        }
        if (op == 0x100)
            pq_c_writes++;
        else
            pq_c_reads++;
        goto completed;
    }
    for (unsigned int i = 0; i < ARRAY_SIZE(pq_y_input); i++) {
        if (op == 0x100)
            memcpy(pq_y_records[index] + i * 2, page + pq_y_input[i], 2);
        else
            memcpy(page + pq_y_output[i], pq_y_records[index] + i * 2, 2);
    }
    if (op == 0x100)
        pq_y_writes++;
    else
        pq_y_reads++;
completed:
    ctrl &= ~op;
    memcpy(page + 0x704, &ctrl, sizeof(ctrl));
}

#define LM14_HWI2C_PAGE0 0x1f223000ULL
#define LM14_HWI2C_PAGE1 0x1f243000ULL

static bool lm14_trap_page(u64 page)
{
    if (hwi2c_enabled() && (page == LM14_HWI2C_PAGE0 || page == LM14_HWI2C_PAGE1))
        return true;
    if (swi2c_enabled() && page == swi2c_page())
        return true;
    for (unsigned int i = 0; i < ARRAY_SIZE(lm14_trap_pages); i++)
        if (page == lm14_trap_pages[i] && (i < 3 ? lm14_unprovisioned : lm14_pq_y_sram))
            return true;
    return false;
}

/* Caller owns lm14_mmio_lock. The explicitly blank virtual storage is never
 * copied from real OTP. Only selected read transactions are completed; write
 * mode and unknown selectors remain pending. Transport completion is not key
 * validity, provisioning, or authentication. */
static void lm14_otp_start(u8 *pm, unsigned int ctrl, unsigned int selector)
{
    unsigned int sel = pm[selector];

    /* Selected code clears selector bit8 (write mode) and replaces low7.
     * Unexplained reserved bits are not accepted as read transactions. */
    if (!(pm[ctrl] & 1) || pm[selector + 1] || (sel & 0x80))
        return;
    if (ctrl == 0 && sel >= 16 && sel <= 55) {
        memset(pm + 0xc0, 0, 2);
        memset(pm + 0xc4, 0, 2);
        memset(pm + 0xc8, 0, 2);
        memset(pm + 0xcc, 0, 2);
    } else if (ctrl == 0x98 && sel < 36) {
        memset(pm + 0x100, 0, 2);
        memset(pm + 0x104, 0, 2);
        memset(pm + 0x108, 0, 2);
        memset(pm + 0x10c, 0, 2);
    } else if (ctrl == 0x98 && sel == 36 && adc_idac) {
        pm[0x101] = adc_calibration[0];
        pm[0x104] = adc_calibration[1];
        pm[0x105] = adc_calibration[2];
    } else {
        return;
    }
    /* Synchronous publication occurs before the busy-bit clear. */
    pm[ctrl] &= ~1;
}

static int lm14_mmio_access(u64 addr, unsigned int width, bool write, u32 *value)
{
    u8 *page = NULL;
    unsigned long flags;
    unsigned int off = offset_in_page(addr);

    if ((addr & PAGE_MASK) == LM14_HWI2C_PAGE0 || (addr & PAGE_MASK) == LM14_HWI2C_PAGE1)
        return lm14_hwi2c_access(addr, width, write, value);
    if (swi2c_enabled() && (addr & PAGE_MASK) == swi2c_page())
        return swi2c_access(addr, width, write, value);
    for (unsigned int i = 0; i < ARRAY_SIZE(lm14_trap_pages); i++)
        if ((addr & PAGE_MASK) == lm14_trap_pages[i])
            page = lm14_trap_bytes[i];
    if (!page || (width != 1 && width != 2 && width != 4) || off + width > PAGE_SIZE)
        return -EINVAL;
    spin_lock_irqsave(&lm14_mmio_lock, flags);
    if (!write) {
        *value = 0;
        memcpy(value, page + off, width);
    } else {
        /* Status is hardware-owned with no HDMI input source. Upper status
         * clear writes cannot manufacture events; lower ENC_En/AKsv_Rcv cannot
         * be asserted by a CPU write. Preserve neighboring register lanes. */
        for (unsigned int i = 0; i < width; i++) {
            bool status_byte = false;
            for (unsigned int j = 0; j < ARRAY_SIZE(lm14_status); j++)
                if (addr + i >= lm14_status[j] && addr + i < lm14_status[j] + 2)
                    status_byte = true;
            /* Read-result lanes are hardware-owned blank/calibration storage,
             * not a way for user writes to provision this virtual device. */
            if ((addr & PAGE_MASK) == lm14_trap_pages[0]) {
                unsigned int lane = off + i;
                if ((lane >= 0xc0 && lane < 0xce && (lane & 2) == 0) ||
                    (lane >= 0x100 && lane < 0x10e && (lane & 2) == 0))
                    status_byte = true;
            }
            if ((addr & PAGE_MASK) == lm14_trap_pages[3]) {
                for (unsigned int j = 0; j < ARRAY_SIZE(pq_y_output); j++)
                    if (off + i >= pq_y_output[j] && off + i < pq_y_output[j] + 2)
                        status_byte = true;
            }
            if (!status_byte)
                page[off + i] = *value >> (i * 8);
        }
        if ((addr & PAGE_MASK) == lm14_trap_pages[0]) {
            if (off == 0)
                lm14_otp_start(page, 0, 4);
            if (off == 0x98)
                lm14_otp_start(page, 0x98, 0x9c);
        }
        if ((addr & PAGE_MASK) == lm14_trap_pages[3] && off < 0x706 && off + width > 0x704)
            lm14_pq_y_transaction(page);
        if ((addr & PAGE_MASK) == lm14_trap_pages[3]) {
            if (off < 0xbd6 && off + width > 0xbd4)
                lm14_pq_gamma_transaction(page, 0xbd4);
            if (off < 0xbe2 && off + width > 0xbe0)
                lm14_pq_gamma_transaction(page, 0xbe0);
        }
    }
    spin_unlock_irqrestore(&lm14_mmio_lock, flags);
    return 0;
}

int lm14_mmio_registers(void)
{
    if (!lm14_unprovisioned && !lm14_pq_y_sram && !hwi2c_enabled() && !swi2c_enabled())
        return 0;
    if (!lm14)
        return -EINVAL;
    for (unsigned int i = 0; i < ARRAY_SIZE(lm14_trap_pages); i++) {
        if (!lm14_trap_page(lm14_trap_pages[i]))
            continue;
        lm14_trap_bytes[i] = lgemu_store_ptr(&registers, lm14_trap_pages[i]);
        if (!lm14_trap_bytes[i])
            return -ENOMEM;
    }
    if (hwi2c_enabled() && (!lgemu_store_ptr(&registers, LM14_HWI2C_PAGE0) ||
                            !lgemu_store_ptr(&registers, LM14_HWI2C_PAGE1)))
        return -ENOMEM;
    if (swi2c_enabled()) {
        u8 *page = lgemu_store_ptr(&registers, swi2c_page());

        if (!page)
            return -ENOMEM;
        swi2c_reset_pads(page);
    }
    registers.trap_page = lm14_trap_page;
    registers.access = lm14_mmio_access;
    return 0;
}

int adc_idac_registers(void)
{
    static const u32 offsets[] = {0x4101, 0x4104, 0x4105};

    if (!adc_idac)
        return 0;
    if (!lm14)
        return -EINVAL;
    adc_start = lgemu_store_ptr(&registers, 0x1f004098);
    adc_selector = lgemu_store_ptr(&registers, 0x1f00409c);
    for (unsigned int i = 0; i < ARRAY_SIZE(offsets); i++) {
        adc_result[i] = lgemu_store_ptr(&registers, 0x1f000000 + offsets[i]);
        if (!adc_result[i])
            return -ENOMEM;
    }
    return adc_start && adc_selector ? 0 : -ENOMEM;
}

static void adc_idac_tick(void)
{
    u8 start;

    if (!adc_start || lm14_unprovisioned)
        return;
    start = READ_ONCE(*adc_start);
    if (!(start & 1) || READ_ONCE(*adc_selector) != 0x24)
        return;
    for (unsigned int i = 0; i < ARRAY_SIZE(adc_result); i++)
        WRITE_ONCE(*adc_result[i], adc_calibration[i]);
    smp_wmb();
    if (READ_ONCE(*adc_start) == start && READ_ONCE(*adc_selector) == 0x24)
        WRITE_ONCE(*adc_start, start & ~1);
}

/* the register "base:reg:x" names (hexadecimal), in the register store, and x */
static u16 *reg_parse(const char *spec, unsigned int *x)
{
    u32 v[3];

    if (lgemu_parse_u32s(spec, 16, v, 3) || v[2] > 0xffff)
        return NULL;
    *x = v[2];
    return lgemu_store_ptr(&registers, v[0] + 2ULL * v[1]);
}

/* Every jiffy: the userland sets a bit with a plain store into its mapping of the register page,
 * which the module cannot see, and then spins on it with a loop count, not a time (MStar's
 * HAL_SYS_ReadEfuseHDCPKey's MAX_TIMEOUT_COUNT; XC's PQ SRAM, once per table entry), so a slower
 * or backed-off poll would make each wait longer or time it out. The work is freezable: in a
 * suspend (Quick Start+'s s2idle) the userland is frozen, nothing can set a bit, and the guest
 * idles; it runs again at the resume.
 * Once a bit is seen set, a timer clears them every AUTOCLEAR_FAST_NS for AUTOCLEAR_BURST more:
 * a burst of operations each waiting for its bit (a PQ SRAM table of hundreds of entries, which
 * W16R's user-mode XC writes when a window is set up, with a sleep between the polls) took a
 * jiffy per entry, two seconds a table, where the hardware takes microseconds. */
#define AUTOCLEAR_FAST_NS 50000
#define AUTOCLEAR_BURST   (HZ / 10)
static struct hrtimer autoclear_fast;
static unsigned long autoclear_until;

static bool autoclear_clear(void)
{
    bool set = false;

    for (int i = 0; i < reg_autoclear_count; i++) {
        u16 v = READ_ONCE(*autoclears[i].reg);

        if (v & autoclears[i].mask) {
            WRITE_ONCE(*autoclears[i].reg, v & ~autoclears[i].mask);
            set = true;
        }
    }
    if (set)
        WRITE_ONCE(autoclear_until, jiffies + AUTOCLEAR_BURST);
    return set;
}

static enum hrtimer_restart autoclear_fast_run(struct hrtimer *t)
{
    autoclear_clear();
    if (time_after(jiffies, READ_ONCE(autoclear_until)))
        return HRTIMER_NORESTART;
    hrtimer_forward_now(t, ns_to_ktime(AUTOCLEAR_FAST_NS));
    return HRTIMER_RESTART;
}

static void autoclear_run(struct work_struct *work)
{
    if (autoclear_clear() && !hrtimer_active(&autoclear_fast))
        hrtimer_start(&autoclear_fast, ns_to_ktime(AUTOCLEAR_FAST_NS), HRTIMER_MODE_REL_SOFT);
    xc_riu_tick();
    adc_idac_tick();
    tsp_download_tick();
    if (READ_ONCE(tsp_loaded) && !hrtimer_active(&tsp_mailbox_timer))
        hrtimer_start(&tsp_mailbox_timer, ns_to_ktime(TSP_MAILBOX_NS), HRTIMER_MODE_REL_SOFT);
    queue_delayed_work(system_freezable_wq, &autoclear_work, 1);
}

int registers_preset(void)
{
    unsigned int x;

    if (lm14_unprovisioned || lm14_pq_y_sram || hwi2c_enabled() || swi2c_enabled()) {
        u32 fields[3];
        /* The synchronous device owns these pages. Do not permit a second
         * polling writer selected through the generic XC/profile knobs. */
        if (utopia_has_xc_riu())
            return -EINVAL;
        if (tsp_riu) {
            u32 base[2];
            u64 addr;
            if (lgemu_parse_u32s(tsp_riu, 16, base, 2))
                return -EINVAL;
            addr = (u64)base[0] + (u64)base[1] * 2;
            for (u64 page = addr & PAGE_MASK; page <= ((addr + 0x229) & PAGE_MASK);
                 page += PAGE_SIZE)
                if (lm14_trap_page(page))
                    return -EINVAL;
        }
        for (int i = 0; i < reg_init_count + reg_autoclear_count; i++) {
            const char *spec = i < reg_init_count ? reg_init[i] : reg_autoclear[i - reg_init_count];
            u64 addr;
            if (lgemu_parse_u32s(spec, 16, fields, 3))
                return -EINVAL;
            addr = (u64)fields[0] + (u64)fields[1] * 2;
            if (i >= reg_init_count && lm14_trap_page(addr & PAGE_MASK))
                return -EINVAL;
            if (i < reg_init_count) {
                for (unsigned int j = 0; j < ARRAY_SIZE(lm14_status); j++)
                    if (addr < lm14_status[j] + 2 && addr + 2 > lm14_status[j])
                        return -EINVAL;
                if ((addr & PAGE_MASK) == lm14_trap_pages[0])
                    return -EINVAL;
                if (lm14_pq_y_sram && (addr & PAGE_MASK) == lm14_trap_pages[3])
                    return -EINVAL;
                if (hwi2c_enabled() && ((addr & PAGE_MASK) == LM14_HWI2C_PAGE0 ||
                                        (addr & PAGE_MASK) == LM14_HWI2C_PAGE1))
                    return -EINVAL;
                if (swi2c_enabled() && (addr & PAGE_MASK) == swi2c_page())
                    return -EINVAL;
            }
        }
    }

    for (int i = 0; i < reg_init_count; i++) {
        u16 *reg = reg_parse(reg_init[i], &x);

        if (!reg)
            return -EINVAL;
        *reg = x;
    }
    for (int i = 0; i < reg_autoclear_count; i++) {
        autoclears[i].reg = reg_parse(reg_autoclear[i], &x);
        if (!autoclears[i].reg)
            return -EINVAL;
        /* The explicit trim controller owns its busy bit; a generic clear
         * would acknowledge unsupported selectors without publishing data. */
        if (adc_idac && (void *)autoclears[i].reg == (void *)adc_start && (x & 1))
            return -EINVAL;
        autoclears[i].mask = x;
    }
    if (reg_autoclear_count || utopia_has_xc_riu() || tsp_riu || adc_idac)
        queue_delayed_work(system_freezable_wq, &autoclear_work, 1);
    return 0;
}

/* ---- /dev/miomap (176): register windows ---- */

#define MIOMAP_IOC_CHIPINFO    _IOWR('I', 0x00, struct miomap_chipinfo)
#define MIOMAP_IOC_SET_MAP     _IOW('I', 0x01, struct miomap_info32)
#define MIOMAP_IOC_FLUSHDCACHE _IOR('I', 0x02, struct miomap_info32)
/* W18A's KADP (MDrv_MMIO_Init): the same request with 64-bit fields */
#define MIOMAP_IOC_SET_MAP64 _IOW('I', 0x01, struct miomap_info64)

struct miomap_info32 { /* DrvMIOMap_Info_t without MP_NEW_UTOPIA_32BIT */
    u32 addr, size;
};
struct miomap_info64 {
    u64 addr, size;
};
static_assert(MIOMAP_IOC_SET_MAP64 == 0x40104901);
struct miomap_chipinfo {
    u16 chiptype, chipversion;
};

struct miomap_file {
    struct mutex lock;
    u64 addr;
};

static int miomap_open(struct inode *inode, struct file *file)
{
    struct miomap_file *f = kzalloc(sizeof(*f), GFP_KERNEL);

    if (!f)
        return -ENOMEM;
    mutex_init(&f->lock);
    file->private_data = f;
    return 0;
}

static long miomap_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    struct miomap_file *f = file->private_data;
    void __user *uarg = (void __user *)arg;

    ioctl_count("miomap", cmd);

    switch (cmd) {
    case MIOMAP_IOC_SET_MAP: {
        struct miomap_info32 i;

        if (copy_from_user(&i, uarg, sizeof(i)))
            return -EFAULT;
        mutex_lock(&f->lock);
        f->addr = i.addr;
        mutex_unlock(&f->lock);
        return 0;
    }
    case MIOMAP_IOC_SET_MAP64: {
        struct miomap_info64 i;

        if (copy_from_user(&i, uarg, sizeof(i)))
            return -EFAULT;
        mutex_lock(&f->lock);
        f->addr = i.addr;
        mutex_unlock(&f->lock);
        return 0;
    }
    case MIOMAP_IOC_FLUSHDCACHE:
        return 0;
    }
    return unhandled("miomap", cmd, 0);
}

static int miomap_mmap(struct file *file, struct vm_area_struct *vma)
{
    struct miomap_file *f = file->private_data;
    u64 addr;

    mutex_lock(&f->lock);
    addr = f->addr;
    mutex_unlock(&f->lock);
    return lgemu_store_mmap(&registers, vma, addr);
}

static ssize_t tsp_state_read(struct file *file, char __user *buf, size_t n, loff_t *pos)
{
    unsigned long flags;
    char state[160];
    int len;
    u16 ctrl;
    bool loaded;

    spin_lock_irqsave(&tsp_service_lock, flags);
    ctrl = READ_ONCE(*tsp_ctrl);
    loaded = READ_ONCE(tsp_loaded);
    len = scnprintf(state, sizeof(state),
                    "loaded=%u running=%u pending=%u high=%04x configured=%u base=%08x\n", loaded,
                    loaded && (ctrl & 7) == 3, tsp_mem_high_valid,
                    tsp_mem_high_valid ? tsp_mem_high : 0, tsp_mem_configured,
                    tsp_mem_configured ? tsp_mem_base : 0);
    spin_unlock_irqrestore(&tsp_service_lock, flags);
    return simple_read_from_buffer(buf, n, pos, state, len);
}

const struct proc_ops tsp_state_proc_ops = {
    .proc_read = tsp_state_read,
    .proc_lseek = default_llseek,
};

static ssize_t pq_y_state_read(struct file *file, char __user *buf, size_t n, loff_t *pos)
{
    unsigned long flags;
    char state[256];
    int len;

    spin_lock_irqsave(&lm14_mmio_lock, flags);
    len = scnprintf(
        state, sizeof(state),
        "writes=%u reads=%u crc32=%08x\nc_writes=%u c_reads=%u c_crc32=%08x\ngamma_writes=%u "
        "gamma_crc32=%08x degamma_writes=%u degamma_crc32=%08x\n",
        pq_y_writes, pq_y_reads, crc32_le(~0, (u8 *)pq_y_records, sizeof(pq_y_records)) ^ ~0,
        pq_c_writes, pq_c_reads, crc32_le(~0, (u8 *)pq_c_records, sizeof(pq_c_records)) ^ ~0,
        pq_gamma_writes, crc32_le(~0, (u8 *)pq_gamma_records, sizeof(pq_gamma_records)) ^ ~0,
        pq_degamma_writes, crc32_le(~0, (u8 *)pq_degamma_records, sizeof(pq_degamma_records)) ^ ~0);
    spin_unlock_irqrestore(&lm14_mmio_lock, flags);
    return simple_read_from_buffer(buf, n, pos, state, len);
}
const struct proc_ops pq_y_state_proc_ops = {
    .proc_read = pq_y_state_read,
    .proc_lseek = default_llseek,
};

static int private_release(struct inode *inode, struct file *file)
{
    kfree(file->private_data);
    return 0;
}

const struct file_operations miomap_fops = {
    .owner = THIS_MODULE,
    .open = miomap_open,
    .release = private_release,
    .mmap = miomap_mmap,
    .unlocked_ioctl = miomap_ioctl,
    .compat_ioctl = compat_ptr_ioctl,
};
struct lgemu_store *mmio_store(void)
{
    return &registers;
}
bool mmio_has_tsp(void)
{
    return tsp_riu != NULL;
}
bool mmio_has_pq(void)
{
    return lm14_pq_y_sram;
}
void mmio_work_init(void)
{
    INIT_DELAYED_WORK(&autoclear_work, autoclear_run);
    hrtimer_setup(&autoclear_fast, autoclear_fast_run, CLOCK_MONOTONIC, HRTIMER_MODE_REL_SOFT);
    hrtimer_setup(&tsp_mailbox_timer, tsp_mailbox_run, CLOCK_MONOTONIC, HRTIMER_MODE_REL_SOFT);
}
void mmio_work_cancel(void)
{
    cancel_delayed_work_sync(&autoclear_work);
    hrtimer_cancel(&autoclear_fast);
    hrtimer_cancel(&tsp_mailbox_timer);
}
void mmio_free(void)
{
    lgemu_store_free(&registers);
}
