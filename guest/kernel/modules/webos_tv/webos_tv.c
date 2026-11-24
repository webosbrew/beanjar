// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* webos_tv.ko: LG's DVBv5 tuner driver ("WDVB", webOS 5 on "WFM") as its userland sees it: the
 * platform device with its frontend attributes, the DVB adapter with its frontends, and the analog
 * tuner. A frontend locks only on a frequency its signal source has (webos_tv.h: dtv_soc.ko, fed
 * from the host); without one, every tune is accepted and nothing locks. The stock split is the
 * same: the firmware's webos_tv.ko registers the adapter and the frontends, and the per-system
 * modules the demodulator.
 *
 * The users are bsppart's libdvbfrontend.so (HAL_DVBV5_*, webOS 4's tvservice) and, from webOS 5,
 * legacy-broadcast's libdvbv5 (and webOS 24's libdil_tu_plugin):
 * - /sys/devices/platform/<platform>/frontend/: frontend_type, tuner_nr, tuner_model_name,
 *   tuner_gpio_table and set_country ("country=%s,group=%s") are opened O_RDWR and written; a
 *   failed open or write fails the HAL call. DIL_TU_Probe needs frontend_type, and then the node.
 *   tuner_gpio_table is binary, the GPIO port numbers as 32-bit words, and its store answers 0 as
 *   the stock driver's does (memcpy into 128 bytes, return 0): HAL_DVBV5_SetGPIOTable fails when
 *   write() returns more than 0, and with it FE_SetFEConfig and the whole probe. debug_mode and
 *   debug_module_path are written when they exist; debug_level and atv_dtv_init are the stock
 *   driver's too, unused. webOS 5 adds adapter_number. The values are kept and shown.
 * - /dev/dvb/adapter0/frontend<n>: the kernel's DVB core (built in, as on LG's kernels) answers
 *   the standard ioctls and properties: FE_GET_INFO, DTV_ENUM_DELSYS (the frontend's delsys=),
 *   DTV_API_VERSION, DTV_DELIVERY_SYSTEM (which must be one of delsys=), DTV_FREQUENCY, DTV_TUNE
 *   and the rest. LG's own properties reach this driver through the core's lg_property (the local
 *   patch guest/kernel/patches/0016, as LG's dvb-core passes them on): a SET is accepted, a GET
 *   answered below ("LG's private properties"). They come in two forms:
 *   - numbered, above DTV_MAX_COMMAND (webOS 4 to 6, and webOS 22 on for the post job, monitor
 *     and version): the build's commands are parameters (*_cmd); the answer is in the union.
 *   - named (webOS 22 on): an ASCII EXT_* name at the start of the union, cmd 0 or garbage; the
 *     tuner library reads a GET's answer from the property's first word (where cmd is).
 *   The lock: webOS 4's HAL_DVBV5_CheckLockState (lock_cmd, W18R 0xae, W19P 0xaf) reads LG's
 *   HAL_DEMOD_LOCK_STATE_T, 0 HAL_DEMOD_LOCK_OK and 1 HAL_DEMOD_LOCK_FAIL (the Starfish SDK's
 *   hal_demod_common.h; with 0 everywhere a scan took every channel as locked and waited for its
 *   SI sections for good); its post job (postjob_cmd, W18R 0xb0, W19P 0xb1) four words: the lock
 *   state, the milliseconds before the FE task's next step (100, the FE interface's own default),
 *   the frequency offset and finished (all zeros had the FE task spin, ~2 million calls a second).
 *   From webOS 5 the numbers are LG's dvbfrontend's (KLP 1.0's GPL include/dvbfrontend_ext.h,
 *   DTV_EXT_* from DTV_MAX_COMMAND + 1, and its dvbv5_dtv_ops.c), with its answers: GET 0x81
 *   (status_cmd; GET_LOCK_STATUS) the demodulator's state, an fe_status, FE_HAS_LOCK (0x10)
 *   locked (the tuner library's lock test is ((v ^ 0x10) >> 4) & 1); 0x5d (freq_cmd;
 *   GET_TUNED_FREQUENCY) the tuner's frequency in kHz; 0x56 (specstore_cmd; STORE_SPECAILDATA)
 *   stores a word that 0x55 (speccheck_cmd; CHECK_SPECAILDATA) returns after the demodulator
 *   has read the signal's own: a TU_SPECDATA_VSB_T, which for an ATSC channel must be valid 8VSB
 *   (0x50000020; legacy-broadcast reports the channel's transmission system from it, and 1 is
 *   DVB-T); 0x71 (monitor_cmd; MONITOR) two bytes, the monitor's result and the state again,
 *   which must be 0x10; 0x80 (TUNEPOSTJOB) two bytes, the milliseconds to the FE thread's next
 *   step and finished (answers=0x80:6401: unanswered the thread spins and floods the log); 0x60
 *   and 0x61 (strength_cmd, quality_cmd) the signal's strength and quality.
 *   version_cmd is the LIB version (HAL_DVBV5_GetLIBVersion: W18R 0xc0, W19P 0xc1; 0x68 from
 *   webOS 5): webOS 4 wants 1.0.x, webOS 5 2.0.x, webOS 6 2.1.x, W23P 1.2.x, as `version`.
 * A tuner set with an analog tuner (W21P's PAL_SI2178B, PAL_SI2151; O22's NTSC_SI2178B) also has
 * LG's wfm's V4L2 capture device, /dev/video<atv> (atv=; below). An ARIB set's card device,
 * /dev/dvb/adapter0/acas0 (acas=; below), is LG's lxdvb_sci's, with a stub card that has the VM's
 * ID and no key.
 * /proc/webos_tv/props counts the commands by direction. */
#include <linux/compat.h>
#include <linux/ctype.h>
#include <linux/device.h>
#include <linux/hex.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/platform_device.h>
#include <linux/proc_fs.h>
#include <linux/string.h>
#include <linux/sysfs.h>
#include <linux/uaccess.h>
#include <linux/version.h>
#include <linux/videodev2.h>
#include <media/dvb_frontend.h>
#include <media/v4l2-dev.h>
#include <media/v4l2-device.h>
#include <media/v4l2-fh.h>
#include "lgemu-drv.h"
#include "webos_tv.h"

#define VALUE_MAX 512

static unsigned int frontends = 1;
module_param(frontends, uint, 0444);
MODULE_PARM_DESC(frontends, "frontend nodes, one per tuner (1 to 4)");
static char *platform = "wdvb";
module_param(platform, charp, 0444);
MODULE_PARM_DESC(platform, "the platform device's name: wdvb (webOS 4), wfm (webOS 5)");
static char *version = "1.0.15";
module_param(version, charp, 0444);
MODULE_PARM_DESC(version, "the LIB version version_cmd answers");
static char *answers = "";
module_param(answers, charp, 0444);
MODULE_PARM_DESC(answers, "other GET answers, 'cmd:hexbytes' (the union's bytes, at most 32), "
                          "',' between them");
static char *delsys = "";
module_param(delsys, charp, 0444);
MODULE_PARM_DESC(delsys, "each frontend's delivery systems (enum fe_delivery_system), '+' "
                         "between systems, ',' between frontends: DTV_ENUM_DELSYS's answer");
static unsigned int version_cmd;
module_param(version_cmd, uint, 0444);
MODULE_PARM_DESC(version_cmd, "the property command of HAL_DVBV5_GetLIBVersion (0: none)");
static unsigned int lock_cmd;
module_param(lock_cmd, uint, 0444);
MODULE_PARM_DESC(lock_cmd, "the property command of the HAL lock state, 0 OK, 1 FAIL (0: none)");
static unsigned int postjob_cmd;
module_param(postjob_cmd, uint, 0444);
MODULE_PARM_DESC(postjob_cmd, "the property command of HAL_DVBV5_TunePostJob's answer (0: none)");
static unsigned int freq_cmd;
module_param(freq_cmd, uint, 0444);
MODULE_PARM_DESC(freq_cmd, "the property command of the tuned frequency, Hz (0: none)");
static unsigned int status_cmd;
module_param(status_cmd, uint, 0444);
MODULE_PARM_DESC(status_cmd, "the property command of the lock's fe_status (0: none)");
static unsigned int monitor_cmd;
module_param(monitor_cmd, uint, 0444);
MODULE_PARM_DESC(monitor_cmd,
                 "the property command of the monitor: 0, then the fe_status (0: none)");
static unsigned int specstore_cmd;
module_param(specstore_cmd, uint, 0444);
MODULE_PARM_DESC(specstore_cmd, "the property command that stores the special data (0: none)");
static unsigned int speccheck_cmd;
module_param(speccheck_cmd, uint, 0444);
MODULE_PARM_DESC(speccheck_cmd, "the property command of the special data's check (0: none)");
static unsigned int strength_cmd;
module_param(strength_cmd, uint, 0444);
MODULE_PARM_DESC(strength_cmd, "the property command of the signal strength (0: none)");
static unsigned int quality_cmd;
module_param(quality_cmd, uint, 0444);
MODULE_PARM_DESC(quality_cmd, "the property command of the signal quality (0: none)");
static unsigned int atv;
module_param(atv, uint, 0444);
MODULE_PARM_DESC(atv, "the minor of the analog tuner's /dev/video<atv> (0: no analog tuner)");

/* ---- the platform device and its frontend attributes ---- */

static const char *const attr_names[] = {
    "frontend_type", "tuner_nr",          "tuner_model_name", "tuner_gpio_table", "set_country",
    "debug_mode",    "debug_module_path", "debug_level",      "atv_dtv_init",     "adapter_number",
};
#define ADAPTER_NUMBER 9  /* attr_names' adapter_number: "0", the one adapter */
#define GPIO_TABLE     3  /* attr_names' binary one */
#define GPIO_WORDS     32 /* the stock driver's 128 bytes */
#define ATTRS          ARRAY_SIZE(attr_names)

static DEFINE_MUTEX(values_lock);
static char values[ATTRS][VALUE_MAX];
static u32 gpio_table[GPIO_WORDS];
static size_t gpio_words;
static struct device_attribute dev_attrs[ATTRS];
static struct attribute *attrs[ATTRS + 1];
static const struct attribute_group frontend_group = {.name = "frontend", .attrs = attrs};
static struct platform_device *wdvb;

static ssize_t value_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    size_t i = attr - dev_attrs;
    ssize_t n;

    mutex_lock(&values_lock);
    if (i == GPIO_TABLE) {
        n = 0;
        for (size_t w = 0; w < gpio_words; w++)
            n += sysfs_emit_at(buf, n, "%s%u", w ? " " : "", gpio_table[w]);
        n += sysfs_emit_at(buf, n, "\n");
    } else {
        n = sysfs_emit(buf, "%s\n", values[i]);
    }
    mutex_unlock(&values_lock);
    return n;
}

static ssize_t value_store(struct device *dev, struct device_attribute *attr, const char *buf,
                           size_t count)
{
    size_t i = attr - dev_attrs, len = min(count, (size_t)VALUE_MAX - 1);

    if (i == GPIO_TABLE) {
        mutex_lock(&values_lock);
        gpio_words = min(count / 4, (size_t)GPIO_WORDS);
        memset(gpio_table, 0, sizeof(gpio_table));
        memcpy(gpio_table, buf, gpio_words * 4);
        mutex_unlock(&values_lock);
        pr_info_ratelimited("webos_tv: tuner_gpio_table: %zu ports, the first %u\n", gpio_words,
                            gpio_words ? gpio_table[0] : 0);
        return 0;
    }
    while (len && (buf[len - 1] == '\n' || buf[len - 1] == '\0'))
        len--;
    mutex_lock(&values_lock);
    memcpy(values[i], buf, len);
    values[i][len] = '\0';
    mutex_unlock(&values_lock);
    pr_info_ratelimited("webos_tv: %s = %.*s\n", attr_names[i], (int)min(len, (size_t)128), buf);
    return count;
}

static int props_show(char *buf, size_t size, const struct lgemu_count *e)
{
    return scnprintf(buf, size, "%s %#x %lu%s\n", e->what, e->a, e->count,
                     e->marked ? " not-emulated" : "");
}

/* ("set"/"get", property command, 0); (EXT_ name, 1 set or 0 get, 0) for the named form;
 * ("atv", ioctl, 0) */
DEFINE_LGEMU_COUNTS(props, 128, 64, props_show);

/* ---- LG's private properties ----
 * A property as both the 64-bit and the 32-bit process pass it (struct dtv_property and the
 * core's compat_dtv_property alike up to the union's buffer): cmd at 0, the union at 16, its
 * buffer.len at 48. A frontend's answers follow from its state: locked or not, and the frequency
 * it was tuned to. */
#define LGFE_UNION     16
#define LGFE_LEN       48
#define LGFE_CLEAR_END 64 /* the union's data, len and reserved1, in both layouts */
#define LGFE_NAME_MAX  32
#define LGFE_HAS_LOCK  0x10 /* FE_HAS_LOCK: demod_priv->state when locked */
#define LGFE_LOCK_OK   0    /* HAL_DEMOD_LOCK_OK */
#define LGFE_LOCK_FAIL 1    /* HAL_DEMOD_LOCK_FAIL */
#define LGFE_STRENGTH  80   /* a good signal, percent */
#define LGFE_QUALITY   90
#define LGFE_ANSWERS   16
/* TU_SPECDATA_VSB_T (tv_dil_tu.h): transSystem 0-4 (0 VSB), bValid 5, constellation 28-31 (5
 * TU_TPS_CONST_VSB_8); legacy-broadcast's VSB validator wants constellation 5 */
#define LGFE_SPEC_VALID 0x20
#define LGFE_SPEC_8VSB  0x50000020
#define LGFE_SYS_ATSC   11 /* SYS_ATSC */

struct lgfe_cmds {
    u32 version, lock, postjob, freq, status, monitor, specstore, speccheck, strength, quality;
    const char *version_text;
    unsigned int nanswers;
    struct {
        u32 cmd;
        u8 len;
        u8 bytes[32];
    } answer[LGFE_ANSWERS];
};

struct lgfe_state {
    bool locked;
    u32 freq, delsys; /* the last DTV_TUNE's: Hz (kHz for a satellite system) */
    u32 specdata;     /* the last STORE_SPECAILDATA's word */
};

/* the EXT_* names, for the counts: the name table (gDTV_EXT_CMD_NAME, DTV_EXT_ATTENUATOR 0x47 to
 * DTV_EXT_SET_EXT_PROPERTY 0x93) of the JP libdil_tu_plugin of W22O queue 43.21.89, spellings as
 * there */
static const char *const lgfe_names[] = {
    "EXT_ATTENUATOR",
    "EXT_GET_CELLID",
    "EXT_RELEASE_EWBS",
    "EXT_RF_PORT",
    "EXT_TS_OUTPUT",
    "EXT_TS_MODE",
    "EXT_TUNE_MODE",
    "EXT_MPLP_INFO",
    "EXT_CHANGE_PLP",
    "EXT_SET_PLP",
    "EXT_STORE_PLPID",
    "EXT_LNB_FAULT",
    "EXT_DRIVER_VERSION",
    "EXT_CHECK_SPECAILDATA",
    "EXT_STORE_SPECAILDATA",
    "EXT_BLINDSCANTP_TOTALCOUNT",
    "EXT_INIT_BLINDSCAN",
    "EXT_GET_BLINDSCAN_STATUS",
    "EXT_GET_TUNED_FREQUENCY",
    "EXT_STORE_DELIVERY_SYSTEM",
    "EXT_STORE_ATSC3_PLPDATA",
    "EXT_STAT_STRENGTH",
    "EXT_STAT_QUALITY",
    "EXT_STAT_PER",
    "EXT_STAT_SNR",
    "EXT_STAT_BER",
    "EXT_STAT_RSSI",
    "EXT_SUPPORT_ATSC3",
    "EXT_SUPPORT_ISDBS3",
    "EXT_GET_LNB_POWER",
    "EXT_GET_STACK_PACKETERR",
    "EXT_SET_RUN_PACKETERR",
    "EXT_SET_CLEAR_PACKETERR",
    "EXT_SET_RECORD_MODE",
    "EXT_GET_FREQUENCY_OFFSET",
    "EXT_BS_RESET_SCANDATA",
    "EXT_BS_SCIF_FLAG",
    "EXT_BS_MIN_FREQUENCY",
    "EXT_BS_MAX_FREQUENCY",
    "EXT_BS_SCIF_STATUS",
    "EXT_BS_RANGE",
    "EXT_BS_SCIFBS_BANK",
    "EXT_BS_SCIFBS_SLOT",
    "EXT_BS_SCIFBS_FUB",
    "EXT_BS_SCIFBS_PARAMS",
    "EXT_STORE_MOROR_ROTATE",
    "EXT_STORE_MOTOR_WAITTIME",
    "EXT_SET_PLL",
    "EXT_SET_DEMOD",
    "EXT_SET_DEMOD_ATSC3",
    "EXT_GET_LOCK_STATUS",
    "EXT_SET_FREQUENCY_ISDBS",
    "EXT_INIT_DEMOD",
    "EXT_SET_ISDBT_TSID",
    "EXT_GET_SIGNAL_STATE",
    "EXT_STAT_VALID",
    "EXT_STAT_HIERARCHICAL",
    "EXT_STAT_AGC",
    "EXT_BS_SCIFBS_UNICABLEVERTYPE",
    "EXT_BS_SCIFBS_PINPROTECTED",
    "EXT_BS_SCIFBS_SLOTPIN",
    "EXT_IS_BS",
    "EXT_GET_DVB_TS_CLK_RATE",
    "EXT_GET_SATELLITE_CHIP_ID",
    "EXT_SET_TRANSSYSTEM",
    "EXT_SET_RFGPIO",
    "EXT_SET_RESET",
    "EXT_SET_IS_EXT_PROPERTY",
    "EXT_SET_EXT_PROPERTY",
};

static u32 lgfe_get32(const u8 *p)
{
    return p[0] | p[1] << 8 | p[2] << 16 | (u32)p[3] << 24;
}

static void lgfe_put32(u8 *p, u32 v)
{
    p[0] = v;
    p[1] = v >> 8;
    p[2] = v >> 16;
    p[3] = v >> 24;
}

/* the property's EXT_* name (NUL-terminated in the union), or NULL: the named form */
static const char *lgfe_name(const u8 *p)
{
    const char *n = (const char *)p + LGFE_UNION;

    if (memcmp(n, "EXT_", 4) || strnlen(n, LGFE_NAME_MAX) == LGFE_NAME_MAX)
        return NULL;
    for (const char *c = n; *c; c++)
        if (!(*c == '_' || (*c >= 'A' && *c <= 'Z') || (*c >= '0' && *c <= '9')))
            return NULL;
    return n;
}

/* the demodulator's state, an fe_status: FE_HAS_LOCK while locked */
static u32 lgfe_status(const struct lgfe_state *st)
{
    return st->locked ? LGFE_HAS_LOCK : 0;
}

/* DTV_EXT_GET_TUNED_FREQUENCY: the tuner's freqtuned, kHz (a satellite system's are kHz already) */
static u32 lgfe_khz(const struct lgfe_state *st)
{
    bool sat = st->delsys == 4 || st->delsys == 5 || st->delsys == 6 || st->delsys == 9 ||
               st->delsys == 17;

    return sat ? st->freq : st->freq / 1000;
}

/* DTV_EXT_CHECK_SPECAILDATA: the demodulator's special data, which it reads from the signal
 * while locked (an ATSC channel: valid 8VSB), else the stored word */
static u32 lgfe_specdata(const struct lgfe_state *st)
{
    if (!st->locked)
        return st->specdata;
    return st->delsys == LGFE_SYS_ATSC ? LGFE_SPEC_8VSB : st->specdata | LGFE_SPEC_VALID;
}

/* A SET p: 1 taken (LG's: named, 0 or above DTV_MAX_COMMAND), 0 the core's (the standard ones).
 * A named SET's value is in the first word, as a named GET's answer is. */
static int lgfe_set(const struct lgfe_cmds *c, struct lgfe_state *st, const u8 *p)
{
    const char *name = lgfe_name(p);
    u32 cmd = lgfe_get32(p);

    if (name ? !strcmp(name, "EXT_STORE_SPECAILDATA") : c->specstore && cmd == c->specstore)
        st->specdata = lgfe_get32(name ? p : p + LGFE_UNION);
    return name || !cmd || cmd > DTV_MAX_COMMAND;
}

/* A GET p: 1 answered in p (LG's), 0 the core's (a standard command no answer names) */
static int lgfe_get(const struct lgfe_cmds *c, const struct lgfe_state *st, u8 *p)
{
    const char *name = lgfe_name(p);
    u32 cmd = lgfe_get32(p);
    u8 *u = p + LGFE_UNION;
    bool known = false;

    if (name) { /* the answer in the first word, where cmd is */
        u32 v = 0;

        /* DIL_TU_CheckSignalState (the JP plugin's named form): EXT_STAT_HIERARCHICAL set to
         * the layer, then GET_SIGNAL_STATE (its result only checked), STAT_VALID and the
         * statistics; legacy-broadcast-arib reads a layer of quality 0 as lost and mutes the
         * service (E203). PER, BER, AGC and RSSI stay 0. */
        if (!strcmp(name, "EXT_GET_LOCK_STATUS") || !strcmp(name, "EXT_GET_SIGNAL_STATE"))
            v = lgfe_status(st);
        else if (!strcmp(name, "EXT_STAT_VALID"))
            v = st->locked;
        else if (!strcmp(name, "EXT_STAT_STRENGTH"))
            v = st->locked ? LGFE_STRENGTH : 0;
        else if (!strcmp(name, "EXT_STAT_QUALITY"))
            v = st->locked ? LGFE_QUALITY : 0;
        else if (!strcmp(name, "EXT_GET_TUNED_FREQUENCY"))
            v = lgfe_khz(st);
        else if (!strcmp(name, "EXT_CHECK_SPECAILDATA"))
            v = lgfe_specdata(st);
        lgfe_put32(p, v);
        return 1;
    }
    if (!cmd)
        return 0;
    for (unsigned int a = 0; a < c->nanswers; a++)
        known |= c->answer[a].cmd == cmd;
    if (cmd <= DTV_MAX_COMMAND && !known && cmd != c->version && cmd != c->lock &&
        cmd != c->postjob && cmd != c->freq && cmd != c->status && cmd != c->monitor &&
        cmd != c->speccheck && cmd != c->strength && cmd != c->quality)
        return 0;
    memset(p + 4, 0, LGFE_CLEAR_END - 4);
    for (unsigned int a = 0; a < c->nanswers; a++)
        if (c->answer[a].cmd == cmd)
            memcpy(u, c->answer[a].bytes, c->answer[a].len);
    if (cmd == c->version && c->version_text) {
        size_t n = strnlen(c->version_text, 31);

        memcpy(u, c->version_text, n);
        lgfe_put32(p + LGFE_LEN, n);
    }
    if (cmd == c->lock)
        lgfe_put32(u, st->locked ? LGFE_LOCK_OK : LGFE_LOCK_FAIL);
    if (cmd == c->postjob) { /* lock, wait ms, offset, finished */
        lgfe_put32(u, st->locked ? LGFE_LOCK_OK : LGFE_LOCK_FAIL);
        lgfe_put32(u + 4, 100);
        lgfe_put32(u + 8, 0);
        lgfe_put32(u + 12, 1);
    }
    if (cmd == c->freq)
        lgfe_put32(u, lgfe_khz(st));
    if (cmd == c->status)
        lgfe_put32(u, lgfe_status(st));
    if (cmd == c->monitor) { /* the monitor op's result (0) and the demodulator's state */
        u[0] = 0;
        u[1] = lgfe_status(st);
        lgfe_put32(p + LGFE_LEN, 2);
    }
    if (cmd == c->speccheck)
        lgfe_put32(u, lgfe_specdata(st));
    if (cmd == c->strength)
        lgfe_put32(u, st->locked ? LGFE_STRENGTH : 0);
    if (cmd == c->quality)
        lgfe_put32(u, st->locked ? LGFE_QUALITY : 0);
    return 1;
}

/* answers=: 'cmd:hexbytes', ',' between them; 0, or -1 for a malformed one */
static int lgfe_parse_answers(struct lgfe_cmds *c, const char *s)
{
    while (*s) {
        u32 cmd = 0;
        unsigned int n = 0, digits = 0;
        int base = 10;

        if (s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) {
            base = 16;
            s += 2;
        }
        for (;; s++, digits++) {
            int d = *s >= '0' && *s <= '9'   ? *s - '0'
                    : *s >= 'a' && *s <= 'f' ? *s - 'a' + 10
                    : *s >= 'A' && *s <= 'F' ? *s - 'A' + 10
                                             : 99;
            if (d >= base)
                break;
            cmd = cmd * base + d;
        }
        if (!digits || *s != ':' || c->nanswers >= LGFE_ANSWERS)
            return -1;
        for (s++;; s += 2) {
            int hi = hex_to_bin(s[0]), lo = hi < 0 ? -1 : hex_to_bin(s[1]);

            if (hi < 0 || lo < 0)
                break;
            if (n >= sizeof(c->answer[0].bytes))
                return -1;
            c->answer[c->nanswers].bytes[n++] = hi << 4 | lo;
        }
        if (!n || (*s && *s != ','))
            return -1;
        c->answer[c->nanswers].cmd = cmd;
        c->answer[c->nanswers++].len = n;
        if (*s)
            s++;
    }
    return 0;
}
/* ---- end of LG's private properties ---- */

/* ---- the adapter and its frontends ---- */

#define FES_MAX 4
static struct lgfe_cmds cmds;
static struct dvb_adapter adapter;
static bool adapter_registered;
static struct fe {
    struct dvb_frontend fe;
    bool registered;
    u32 freq, delsys; /* the last DTV_TUNE's */
    u32 specdata;     /* the last STORE_SPECAILDATA's */
} fes[FES_MAX];
static DEFINE_MUTEX(source_lock);
static const struct webos_tv_source *source;

static unsigned int fe_index(struct dvb_frontend *fe)
{
    return container_of(fe, struct fe, fe) - fes;
}

static bool fe_locked(struct fe *f)
{
    bool locked;

    mutex_lock(&source_lock);
    locked = source && f->freq && source->signal(f->freq, f->delsys);
    mutex_unlock(&source_lock);
    return locked;
}

static int fe_lg_property(struct dvb_frontend *fe, struct dtv_property *tvp, bool set)
{
    struct fe *f = &fes[fe_index(fe)];
    const char *name = lgfe_name((const u8 *)tvp);
    const char *counted = "EXT_?";

    if (name) {
        for (size_t i = 0; i < ARRAY_SIZE(lgfe_names); i++)
            if (!strcmp(name, lgfe_names[i]))
                counted = lgfe_names[i];
        lgemu_count(&props, counted, set, 0);
    } else {
        lgemu_count(&props, set ? "set" : "get", tvp->cmd, 0);
    }
    if (set && !name && tvp->cmd == DTV_TUNE) { /* the core tunes; the source hears first */
        f->freq = fe->dtv_property_cache.frequency;
        f->delsys = fe->dtv_property_cache.delivery_system;
        mutex_lock(&source_lock);
        if (source)
            source->tuned(fe_index(fe), f->freq, f->delsys);
        mutex_unlock(&source_lock);
    }
    struct lgfe_state st = {.locked = !set && fe_locked(f),
                            .freq = f->freq,
                            .delsys = f->delsys,
                            .specdata = f->specdata};
    int r = set ? lgfe_set(&cmds, &st, (const u8 *)tvp) : lgfe_get(&cmds, &st, (u8 *)tvp);

    f->specdata = st.specdata;
    return r;
}

static int fe_read_status(struct dvb_frontend *fe, enum fe_status *status)
{
    *status = fe_locked(&fes[fe_index(fe)])
                  ? FE_HAS_SIGNAL | FE_HAS_CARRIER | FE_HAS_VITERBI | FE_HAS_SYNC | FE_HAS_LOCK
                  : 0;
    return 0;
}

static int fe_tune(struct dvb_frontend *fe, bool re_tune, unsigned int mode_flags,
                   unsigned int *delay, enum fe_status *status)
{
    *delay = HZ / 2;
    return fe_read_status(fe, status);
}

static enum dvbfe_algo fe_algo(struct dvb_frontend *fe)
{
    return DVBFE_ALGO_HW;
}

static int fe_read_signal_strength(struct dvb_frontend *fe, u16 *strength)
{
    *strength = fe_locked(&fes[fe_index(fe)]) ? 0xffff * LGFE_STRENGTH / 100 : 0;
    return 0;
}

static int fe_read_snr(struct dvb_frontend *fe, u16 *snr)
{
    *snr = fe_locked(&fes[fe_index(fe)]) ? 0xffff * LGFE_QUALITY / 100 : 0;
    return 0;
}

static int fe_set_voltage(struct dvb_frontend *fe, enum fe_sec_voltage voltage)
{
    return 0; /* LNB power, accepted */
}

static int fe_set_tone(struct dvb_frontend *fe, enum fe_sec_tone_mode tone)
{
    return 0;
}

static const struct dvb_frontend_ops fe_ops_template = {
    .info =
        {
            .frequency_min_hz = 1000, /* 1 kHz: a satellite system's limits are in kHz */
            .frequency_max_hz = U32_MAX,
            .frequency_stepsize_hz = 1,
            .symbol_rate_min = 0,
            .symbol_rate_max = U32_MAX,
            .caps = FE_CAN_INVERSION_AUTO | FE_CAN_FEC_AUTO | FE_CAN_QAM_AUTO |
                    FE_CAN_TRANSMISSION_MODE_AUTO | FE_CAN_GUARD_INTERVAL_AUTO |
                    FE_CAN_HIERARCHY_AUTO | FE_CAN_8VSB | FE_CAN_QAM_64 | FE_CAN_QAM_256,
        },
    .lg_property = fe_lg_property,
    .get_frontend_algo = fe_algo,
    .tune = fe_tune,
    .read_status = fe_read_status,
    .read_signal_strength = fe_read_signal_strength,
    .read_snr = fe_read_snr,
    .set_voltage = fe_set_voltage,
    .set_tone = fe_set_tone,
};

struct dvb_adapter *webos_tv_attach(const struct webos_tv_source *src)
{
    mutex_lock(&source_lock);
    if (source || !adapter_registered) {
        mutex_unlock(&source_lock);
        return NULL;
    }
    source = src;
    for (unsigned int i = 0; i < frontends; i++)
        src->tuned(i, fes[i].freq, fes[i].delsys);
    mutex_unlock(&source_lock);
    return &adapter;
}
EXPORT_SYMBOL_GPL(webos_tv_attach);

void webos_tv_detach(void)
{
    mutex_lock(&source_lock);
    source = NULL;
    mutex_unlock(&source_lock);
}
EXPORT_SYMBOL_GPL(webos_tv_detach);

static struct proc_dir_entry *proc_dir;

/* ---- the card's answers ----
 * An ARIB set's conditional-access card (B-CAS, or the A-CAS chip soldered on 4K boards) as its
 * userland sees it through LG's lxdvb_sci driver: the command APDUs legacy-broadcast-arib's
 * BCAS_EncodeAPDU sends and the answers BCAS_DecodeExtractInfoFromResponse takes (W22O queue
 * 43.21.89, docs/research/arib-data-broadcasting.md "Card protocol"). An answer is
 * 00 | N | IC instruction (2) | return code (2) | data | 90 00, N bytes after the first two
 * before the status word. This card is no real one: its ID is the VM's (card_id=), it holds no key
 * (the system key, the CBC value and any scrambling key are zeros) and has no contract, so it
 * descrambles nothing: an ECM gets "not contracted" (0xA103), the power-on control request
 * (0x80) "none" (0xA101). */
#define ACAS_CLA       0x90
#define ACAS_INT       0x30 /* initial setting conditions */
#define ACAS_IDI       0x32 /* card ID information */
#define ACAS_ECM       0x34
#define ACAS_EMM       0x36
#define ACAS_POC       0x80   /* power-on control information, one entry by number */
#define ACAS_NO_POC    0xA101 /* POC: none (the decoder's own no-entry case) */
#define ACAS_OK        0x2100 /* normal end */
#define ACAS_NO_KEY    0xA103 /* ECM: not contracted (BCAS_UNCONTRACTED_CHANNEL) */
#define ACAS_CA_SYSTEM 0x0005 /* B-CAS/A-CAS: INT's CA system ID must be this */
#define ACAS_TYPE      0x01   /* INT's card type */
#define ACAS_PARTITION 0x50   /* INT's message partition length */
#define ACAS_MAKER     'B'    /* IDI's maker letter and version: "B001" in the settings */
#define ACAS_VERSION   1
#define ACAS_RX_MAX    0xfe /* the driver's receive limit */

struct acas_card {
    u8 id[6]; /* the 48-bit card ID, big-endian */
};

/* CRC-16/CCITT (0x1021, from 0xFFFF) over the card ID: the IDI check code. The firmware shows it
 * beside the number (%05d) and does not check it; a real card's check code is the issuer's. */
static u16 acas_check_code(const struct acas_card *card)
{
    u16 crc = 0xffff;

    for (int i = 0; i < 6; i++) {
        crc ^= (u16)card->id[i] << 8;
        for (int b = 0; b < 8; b++)
            crc = crc & 0x8000 ? (crc << 1) ^ 0x1021 : crc << 1;
    }
    return crc;
}

/* The answer to the command tx (tx_len bytes) into rx (cap bytes): its length, 0 if it does not
 * fit. A command that is no B-CAS APDU gets the status word alone: 6E 00 (class not supported) or
 * 6D 00 (instruction not supported), which the firmware takes as a failed transfer. */
static size_t acas_answer(const struct acas_card *card, const u8 *tx, size_t tx_len, u8 *rx,
                          size_t cap)
{
    u8 data[64];
    size_t n = 0, len;
    u16 rc = ACAS_OK, check;

    /* the APDU in a T=1 block, as an ACAS build's BCAS_EncodeBlock sends it: NAD 0, PCB, LEN, the
     * APDU, LRC (W22O JP: 00 00 05 90 30 00 02 00 LRC). The answer comes back bare, as LG's driver
     * gives it, which BCAS_DecodeExtractInfoFromResponse reads from byte 0. */
    if (tx_len >= 5 && tx[0] == 0x00 && tx[2] + 4u == tx_len) {
        tx += 3;
        tx_len -= 4;
    }
    if (tx_len < 4 || tx[0] != ACAS_CLA) {
        if (cap < 2)
            return 0;
        rx[0] = 0x6e;
        rx[1] = 0x00;
        return 2;
    }
    memset(data, 0, sizeof(data));
    switch (tx[1]) {
    case ACAS_INT: /* CA system ID, card ID, type, partition length, system key 32, CBC 8, 1 */
        data[0] = ACAS_CA_SYSTEM >> 8;
        data[1] = ACAS_CA_SYSTEM & 0xff;
        memcpy(data + 2, card->id, 6);
        data[8] = ACAS_TYPE;
        data[9] = ACAS_PARTITION;
        n = 51;
        break;
    case ACAS_IDI: /* one card ID: maker letter, version, ID (6), check code (2) */
        check = acas_check_code(card);
        data[0] = 1;
        data[1] = ACAS_MAKER;
        data[2] = ACAS_VERSION;
        memcpy(data + 3, card->id, 6);
        data[9] = check >> 8;
        data[10] = check & 0xff;
        n = 11;
        break;
    case ACAS_ECM:
        rc = ACAS_NO_KEY;
        break;
    case ACAS_POC: /* BCAS_POC_Refresh asks for entry 1, 2, ... until one fails */
        rc = ACAS_NO_POC;
        break;
    case ACAS_EMM:
    case 0x38:
    case 0x3a:
    case 0x3c:
        break; /* taken, nothing for this card */
    default:
        if (cap < 2)
            return 0;
        rx[0] = 0x6d;
        rx[1] = 0x00;
        return 2;
    }
    len = 2 + 4 + n + 2;
    if (len > cap)
        return 0;
    rx[0] = 0x00;
    rx[1] = 4 + n;
    rx[2] = 0x00; /* IC instruction */
    rx[3] = 0x00;
    rx[4] = rc >> 8;
    rx[5] = rc & 0xff;
    memcpy(rx + 6, data, n);
    rx[6 + n] = 0x90;
    rx[7 + n] = 0x00;
    return len;
}

/* card_id=: 12 hex digits into card; -1 if it is not that */
static int acas_parse_id(struct acas_card *card, const char *s)
{
    for (int i = 0; i < 6; i++) {
        int hi = hex_to_bin(s[2 * i]), lo = hi < 0 ? -1 : hex_to_bin(s[2 * i + 1]);

        if (hi < 0 || lo < 0)
            return -1;
        card->id[i] = hi << 4 | lo;
    }
    return s[12] ? -1 : 0;
}
/* ---- end of the card's answers ---- */

/* ---- the card's device (acas=) ----
 * /dev/dvb/adapter0/acas0, as LG's lxdvb_sci registers it on an ARIB set (the A-CAS node; exactly
 * one of adapter0's and adapter1's must exist). Opened O_RDWR|O_NONBLOCK; only ioctls, each a
 * 16-byte {u32 id; u32 size; u64 value} (nyxAcas.module, the same in a 32-bit process):
 * 0x40106F00 with id 0 init (a no-op in LG's driver too), 1 reset (the card activates: no ATR
 * reaches userland), 2 transfer (size 0x20, value a pointer to {u32 tx_len; u32 timeout; u64 tx;
 * u64 rx; u64 rx_len}, *rx_len a u32: in the capacity, out the answer's length, 0 a failure); and
 * 0xC0106F01 with id 3, the reset wait in seconds into value (0: no wait). */
#define ACAS_IOC_CONTROL _IOW('o', 0, struct acas_ctl)
#define ACAS_IOC_GET     _IOWR('o', 1, struct acas_ctl)
#define ACAS_TX_MAX      0x102 /* the driver's transmit limit */

struct acas_ctl {
    u32 id;
    u32 size;
    u64 value;
};

struct acas_apdu {
    u32 tx_len;
    u32 timeout;
    u64 tx;
    u64 rx;
    u64 rx_len;
};

static unsigned int acas;
module_param(acas, uint, 0444);
MODULE_PARM_DESC(acas, "1: the ARIB set's card device, /dev/dvb/adapter0/acas0 (0: none)");
static char *card_id = "000000000000";
module_param(card_id, charp, 0444);
MODULE_PARM_DESC(card_id, "the card's 48-bit ID, 12 hex digits (the VM's own, from beanjar)");
static struct acas_card acas_card;
static DEFINE_MUTEX(acas_lock);
static bool acas_made;

static long acas_transfer(u64 value)
{
    struct acas_apdu a;
    u8 tx[ACAS_TX_MAX], rx[ACAS_RX_MAX];
    u32 cap;
    size_t n;

    if (copy_from_user(&a, u64_to_user_ptr(value), sizeof(a)))
        return -EFAULT;
    if (!a.tx_len || a.tx_len > sizeof(tx) || !a.rx || !a.rx_len)
        return -EINVAL;
    if (copy_from_user(tx, u64_to_user_ptr(a.tx), a.tx_len) ||
        get_user(cap, (u32 __user *)u64_to_user_ptr(a.rx_len)))
        return -EFAULT;
    n = acas_answer(&acas_card, tx, a.tx_len, rx, min_t(u32, cap, sizeof(rx)));
    pr_info_ratelimited("webos_tv: acas0: %*ph (%u bytes) -> %zu bytes, %*ph\n",
                        (int)min_t(u32, a.tx_len, 8), tx, a.tx_len, n, (int)min_t(size_t, n, 8),
                        rx);
    /* the instruction, in a T=1 block or bare */
    lgemu_count(&props, "acas", a.tx_len >= 5 && !tx[0] ? tx[4] : a.tx_len >= 2 ? tx[1] : 0, 0);
    if (n && copy_to_user(u64_to_user_ptr(a.rx), rx, n))
        return -EFAULT;
    return put_user((u32)n, (u32 __user *)u64_to_user_ptr(a.rx_len));
}

static long acas_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    struct acas_ctl c;
    long ret;

    if (cmd != ACAS_IOC_CONTROL && cmd != ACAS_IOC_GET)
        return lgemu_ioctl_unhandled(&props, "acas", cmd, -ENOTTY);
    if (copy_from_user(&c, (void __user *)arg, sizeof(c)))
        return -EFAULT;
    mutex_lock(&acas_lock);
    if (cmd == ACAS_IOC_GET) {
        ret = -EINVAL;
        if (c.id == 3) { /* getResetTime */
            c.value = 0;
            ret = copy_to_user((void __user *)arg, &c, sizeof(c)) ? -EFAULT : 0;
        }
    } else if (c.id == 0 || c.id == 1) { /* init, reset */
        pr_info("webos_tv: acas0: %s\n", c.id ? "reset" : "init");
        ret = 0;
    } else if (c.id == 2) {
        ret = acas_transfer(c.value);
    } else {
        ret = -EINVAL;
    }
    mutex_unlock(&acas_lock);
    lgemu_count(&props, cmd == ACAS_IOC_GET ? "acas get" : "acas control", c.id, 0);
    return ret;
}

static const struct file_operations acas_fops = {
    .owner = THIS_MODULE,
    .open = nonseekable_open,
    .unlocked_ioctl = acas_ioctl,
    .compat_ioctl = compat_ptr_ioctl,
};

static struct miscdevice acas_dev = {
    .minor = MISC_DYNAMIC_MINOR,
    .name = "acas0",
    .nodename = "dvb/adapter0/acas0",
    .fops = &acas_fops,
};

/* ---- the analog tuner (atv=) ----
 *
 * LG's wfm for webOS 6 (KLP 1.0's GPL dvbfrontend/v4l2) registers the analog tuner as a V4L2
 * capture device, "webos_vdev_video" at video minor 2 (v4l2_atv_core.c's v4l2_register:
 * webos_vdev_register(dev, &dev->atv_vdev, "video", 2)), once the board's tuner type names an
 * ATV tuner (PAL_SI2178B: si2178b_atv); here it is made at load. legacy-broadcast opens it in its
 * tuner 0's FE_ATV_Initialize (v4l2_open O_RDWR, then VIDIOC_G_TUNER) and fails the tuner's
 * whole FE init without it (W21P's legacy-broadcast-dvb 0x3568fa). It is a video device of the
 * kernel's V4L2 core, as LG's is, whose ioctls all come here as they are (this driver uses no
 * video_ioctl2; the core passes a 32-bit process's 'V' ioctls on with compat_ptr). The answers are
 * LG's driver's with no signal (the vidioc_* of v4l2_atv_video.c and the si2178b_atv subdev's
 * tuner_g_tuner/g_frequency/core g_ctrl), and the V4L2 core's -ENOTTY for an ioctl the driver
 * has no op for (video_ioctl_ops: only the webos_vidioc_* and the vb2 ones; ENUM_FMT, ENUM_INPUT,
 * G_STD and the rest are under #if 0). QUERYCAP webos_vidioc_querycap's capabilities (the core
 * adds EXT_PIX_FORMAT and the version); G_FMT of the capture 720x480 YUYV interlaced, SMPTE 170M
 * (webos_vidioc_g_fmt_vid_cap); G_PARM 4 read buffers, 1/25 s a frame (webos_vidioc_g_parm);
 * G_TUNER an analog TV tuner of 40 to 1002 MHz (Si2178B_ATV_FREQ_MIN/MAX) with signal 0;
 * S_FREQUENCY kept, G_FREQUENCY it / 1000 (tuner_g_frequency's). The controls are the ATV
 * handler's (v4l2_atv_ctrl.c's init_ctrl; it is the device's, so the V4L2 core's handler path
 * answers QUERYCTRL, G_CTRL and S_CTRL before webos_vidioc_s_ctrl): the mute (default 1), user
 * ctrl and init ctrl (0-255, default 5), kept as set (a boolean as 0/1, an integer clamped), and
 * v4l2-ext-frontend.h's read-only volatile ones, which the si2178b_atv subdev reads with no
 * signal: the tuner lock 0 (DIL_TU_ATVTunerLockState takes 1 as locked), the signal status 0,
 * the demodulator firmware 0 and the tune post job's "found" 0; setting one of these -EACCES, an
 * unknown control -EINVAL. S_TUNER, the event subscriptions, the priority and the vb2 queue's
 * calls are 0 (nothing streams); VIDIOC_DQEVENT waits for an event that never comes
 * (O_NONBLOCK: none). */
#define ATV_FREQ_MIN         40000000   /* Si2178B_ATV_FREQ_MIN */
#define ATV_FREQ_MAX         1002000000 /* Si2178B_ATV_FREQ_MAX */
#define MAX_WDVB_FE          5          /* wfm.h: webos_vidioc_g_tuner's index limit */
#define CID_FRONTEND_BASE    (V4L2_CID_USER_BASE + 0x9000) /* V4L2_CID_EXT_USER_FRONTEND_BASE */
#define CID_TUNERLOCK        (CID_FRONTEND_BASE + 0)
#define CID_DEMOD_FW_VERSION (CID_FRONTEND_BASE + 1)
#define CID_SIGNALSTATUS     (CID_FRONTEND_BASE + 2)
#define CID_ATVTUNEPOSTJOB   (CID_FRONTEND_BASE + 3)
#define CID_ATVINIT          (CID_FRONTEND_BASE + 4)
#define CID_PRIVATE_CTRL_1   (V4L2_CID_USER_BASE + 0x1000) /* V4L2_CID_USER_SI2178B_BASE + 0 */
#define VIDIOC_NR_DQEVENT    89

static DECLARE_WAIT_QUEUE_HEAD(atv_never);
/* init_ctrl's handler, by id: name, type, min, max, default, read-only volatile; the value */
static struct atv_ctrl {
    u32 id;
    const char *name;
    u32 type;
    s32 min, max, def;
    bool ro;
    s32 val;
} atv_ctrls[] = {
    {V4L2_CID_AUDIO_MUTE, "Mute", V4L2_CTRL_TYPE_BOOLEAN, 0, 1, 1, false, 1},
    {CID_PRIVATE_CTRL_1, "user ctrl", V4L2_CTRL_TYPE_INTEGER, 0, 255, 5, false, 5},
    {CID_TUNERLOCK, "tuner lock ctrl", V4L2_CTRL_TYPE_BOOLEAN, 0, 1, 0, true, 0},
    {CID_DEMOD_FW_VERSION, "demod fw version ctrl", V4L2_CTRL_TYPE_INTEGER, 0, 0xffff, 0, true, 0},
    {CID_SIGNALSTATUS, "tuner singal status ctrl", V4L2_CTRL_TYPE_INTEGER, 0, (s32)0xffffffff, 0,
     true, 0},
    {CID_ATVTUNEPOSTJOB, "atv_tune_postjob", V4L2_CTRL_TYPE_BOOLEAN, 0, 1, 0, true, 0},
    {CID_ATVINIT, "init ctrl", V4L2_CTRL_TYPE_INTEGER, 0, 255, 5, false, 5},
};

static struct atv_ctrl *atv_ctrl(u32 id, bool next)
{
    struct atv_ctrl *best = NULL;

    for (size_t i = 0; i < ARRAY_SIZE(atv_ctrls); i++)
        if (next ? atv_ctrls[i].id > id && (!best || atv_ctrls[i].id < best->id)
                 : atv_ctrls[i].id == id)
            best = &atv_ctrls[i];
    return best;
}
static bool atv_v4l2_registered, atv_made;

/* the 32-bit process's numbers of the two whose structure has a pointer in the 64-bit layout */
#define VIDIOC_G_FMT_32 _IOWR('V', 4, u8[204])
#define FMT_PIX         4 /* struct v4l2_format's pix in the 32-bit layout */
static u32 atv_frequency;

static long atv_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    void __user *up = (void __user *)arg;

    lgemu_count(&props, "atv", cmd, 0);
    switch (cmd) {
    case VIDIOC_QUERYCAP: {
        struct v4l2_capability cap = {
            .version = KERNEL_VERSION(4, 4, 84),
            .device_caps = V4L2_CAP_READWRITE | V4L2_CAP_VIDEO_CAPTURE | V4L2_CAP_STREAMING |
                           V4L2_CAP_EXT_PIX_FORMAT,
        };

        strscpy(cap.card, "webos_vdev_video", sizeof(cap.card));
        cap.capabilities = cap.device_caps | V4L2_CAP_DEVICE_CAPS;
        return copy_to_user(up, &cap, sizeof(cap)) ? -EFAULT : 0;
    }
    case VIDIOC_G_FMT_32: {
        u32 type,
            pix[12] = {720,     480,           V4L2_PIX_FMT_YUYV,         V4L2_FIELD_INTERLACED,
                       720 * 2, 720 * 2 * 480, V4L2_COLORSPACE_SMPTE170M, V4L2_PIX_FMT_PRIV_MAGIC};

        if (get_user(type, (u32 __user *)up))
            return -EFAULT;
        if (type != V4L2_BUF_TYPE_VIDEO_CAPTURE)
            return -EINVAL;
        return copy_to_user(up + FMT_PIX, pix, sizeof(pix)) ? -EFAULT : 0;
    }
    case VIDIOC_G_PARM: {
        struct v4l2_streamparm p;

        if (copy_from_user(&p, up, sizeof(p)))
            return -EFAULT;
        p.parm.capture.readbuffers = 4;
        p.parm.capture.timeperframe.numerator = 1;
        p.parm.capture.timeperframe.denominator = 25;
        return copy_to_user(up, &p, sizeof(p)) ? -EFAULT : 0;
    }
    case VIDIOC_G_TUNER: {
        struct v4l2_tuner t;

        if (copy_from_user(&t, up, sizeof(t)))
            return -EFAULT;
        if (t.index > MAX_WDVB_FE)
            return -EINVAL;
        t.type = V4L2_TUNER_ANALOG_TV;
        t.capability |= V4L2_TUNER_CAP_NORM | V4L2_TUNER_CAP_FREQ_BANDS;
        t.rangelow = ATV_FREQ_MIN;
        t.rangehigh = ATV_FREQ_MAX;
        t.signal = 0;
        return copy_to_user(up, &t, sizeof(t)) ? -EFAULT : 0;
    }
    case VIDIOC_S_TUNER:
        return 0;
    case VIDIOC_S_FREQUENCY:
    case VIDIOC_G_FREQUENCY: {
        struct v4l2_frequency f;

        if (copy_from_user(&f, up, sizeof(f)))
            return -EFAULT;
        if (cmd == VIDIOC_S_FREQUENCY) {
            atv_frequency = f.frequency;
            return 0;
        }
        f.type = V4L2_TUNER_ANALOG_TV;
        f.frequency = atv_frequency / 1000;
        return copy_to_user(up, &f, sizeof(f)) ? -EFAULT : 0;
    }
    case VIDIOC_QUERYCTRL: {
        struct v4l2_queryctrl q;
        struct atv_ctrl *c;

        if (copy_from_user(&q, up, sizeof(q)))
            return -EFAULT;
        c = atv_ctrl(q.id & ~(V4L2_CTRL_FLAG_NEXT_CTRL | V4L2_CTRL_FLAG_NEXT_COMPOUND),
                     q.id & V4L2_CTRL_FLAG_NEXT_CTRL);
        if (!c)
            return -EINVAL;
        memset(&q, 0, sizeof(q));
        q.id = c->id;
        q.type = c->type;
        strscpy(q.name, c->name, sizeof(q.name));
        q.minimum = c->min;
        q.maximum = c->max;
        q.step = 1;
        q.default_value = c->def;
        q.flags = c->ro ? V4L2_CTRL_FLAG_VOLATILE | V4L2_CTRL_FLAG_READ_ONLY : 0;
        return copy_to_user(up, &q, sizeof(q)) ? -EFAULT : 0;
    }
    case VIDIOC_G_CTRL:
    case VIDIOC_S_CTRL: {
        struct v4l2_control v;
        struct atv_ctrl *c;

        if (copy_from_user(&v, up, sizeof(v)))
            return -EFAULT;
        c = atv_ctrl(v.id, false);
        if (!c)
            return -EINVAL;
        if (cmd == VIDIOC_G_CTRL) {
            v.value = c->val;
            return copy_to_user(up, &v, sizeof(v)) ? -EFAULT : 0;
        }
        if (c->ro)
            return -EACCES;
        c->val = c->type == V4L2_CTRL_TYPE_BOOLEAN ? !!v.value : clamp(v.value, c->min, c->max);
        return 0;
    }
    case VIDIOC_SUBSCRIBE_EVENT:
    case VIDIOC_UNSUBSCRIBE_EVENT:
    case VIDIOC_G_PRIORITY:
    case VIDIOC_S_PRIORITY:
        return 0;
    }
    if (_IOC_TYPE(cmd) == 'V' && _IOC_NR(cmd) == VIDIOC_NR_DQEVENT) {
        if (file->f_flags & O_NONBLOCK)
            return -ENOENT;
        return wait_event_interruptible(atv_never, false);
    }
    if (_IOC_TYPE(cmd) == 'V' &&
        (_IOC_NR(cmd) == _IOC_NR(VIDIOC_REQBUFS) || _IOC_NR(cmd) == _IOC_NR(VIDIOC_QUERYBUF) ||
         _IOC_NR(cmd) == _IOC_NR(VIDIOC_QBUF) || _IOC_NR(cmd) == _IOC_NR(VIDIOC_DQBUF) ||
         _IOC_NR(cmd) == _IOC_NR(VIDIOC_EXPBUF) || _IOC_NR(cmd) == _IOC_NR(VIDIOC_STREAMON)))
        return 0;
    return lgemu_ioctl_unhandled(&props, "atv", cmd, -ENOTTY);
}

static const struct v4l2_file_operations atv_fops = {
    .owner = THIS_MODULE,
    .open = v4l2_fh_open, /* the V4L2 core's must-haves: a file handle per open */
    .release = v4l2_fh_release,
    .unlocked_ioctl = atv_ioctl,
};
static struct v4l2_device atv_v4l2;
static struct video_device atv_vdev;

static void webos_tv_cleanup(void)
{
    if (acas_made)
        misc_deregister(&acas_dev);
    acas_made = false;
    if (atv_made)
        video_unregister_device(&atv_vdev);
    atv_made = false;
    if (atv_v4l2_registered)
        v4l2_device_unregister(&atv_v4l2);
    atv_v4l2_registered = false;
    for (unsigned int i = 0; i < FES_MAX; i++)
        if (fes[i].registered) {
            dvb_unregister_frontend(&fes[i].fe);
            fes[i].registered = false;
        }
    if (adapter_registered)
        dvb_unregister_adapter(&adapter);
    adapter_registered = false;
    proc_remove(proc_dir);
    if (wdvb) {
        sysfs_remove_group(&wdvb->dev.kobj, &frontend_group);
        platform_device_unregister(wdvb);
    }
}

static int __init webos_tv_init(void)
{
    static short adapter_nr[DVB_MAX_ADAPTERS] = {[0] = 0, [1 ... DVB_MAX_ADAPTERS - 1] = DVB_UNSET};
    unsigned int fe = 0, n = 0;
    int err;

    if (!frontends || frontends > FES_MAX) {
        pr_err("webos_tv: frontends=%u: 1 to %u\n", frontends, FES_MAX);
        return -EINVAL;
    }
    for (const char *c = delsys; *c;) { /* "9+8,8" */
        unsigned int v;
        char *end;

        v = simple_strtoul(c, &end, 0);
        if (end == c || fe >= FES_MAX || n >= MAX_DELSYS - 1 || !v || v > 255 ||
            (*end && *end != '+' && *end != ',')) {
            pr_err("webos_tv: delsys=%s: systems '+', frontends ','\n", delsys);
            return -EINVAL;
        }
        fes[fe].fe.ops.delsys[n++] = v;
        if (*end == ',') {
            fe++;
            n = 0;
        }
        c = *end ? end + 1 : end;
    }
    cmds = (struct lgfe_cmds){
        .version = version_cmd,
        .lock = lock_cmd,
        .postjob = postjob_cmd,
        .freq = freq_cmd,
        .status = status_cmd,
        .monitor = monitor_cmd,
        .specstore = specstore_cmd,
        .speccheck = speccheck_cmd,
        .strength = strength_cmd,
        .quality = quality_cmd,
        .version_text = version,
    };
    if (lgfe_parse_answers(&cmds, answers)) {
        pr_err("webos_tv: answers=%s: 'cmd:hexbytes', ',' (at most %u)\n", answers, LGFE_ANSWERS);
        return -EINVAL;
    }
    strscpy(values[ADAPTER_NUMBER], "0", VALUE_MAX);
    for (size_t i = 0; i < ATTRS; i++) {
        sysfs_attr_init(&dev_attrs[i].attr);
        dev_attrs[i].attr.name = attr_names[i];
        dev_attrs[i].attr.mode = 0664;
        dev_attrs[i].show = value_show;
        dev_attrs[i].store = value_store;
        attrs[i] = &dev_attrs[i].attr;
    }
    wdvb = platform_device_register_simple(platform, PLATFORM_DEVID_NONE, NULL, 0);
    if (IS_ERR(wdvb)) {
        err = PTR_ERR(wdvb);
        wdvb = NULL;
        return err;
    }
    err = sysfs_create_group(&wdvb->dev.kobj, &frontend_group);
    if (err) {
        platform_device_unregister(wdvb);
        wdvb = NULL;
        return err;
    }
    proc_dir = proc_mkdir("webos_tv", NULL);
    if (!proc_dir || !proc_create_data("props", 0444, proc_dir, &lgemu_counts_proc_ops, &props)) {
        err = -ENOMEM;
        goto fail;
    }
    /* adapter 0, the one LG's userland opens (/dev/dvb/adapter0) */
    err = dvb_register_adapter(&adapter, "webos_tv", THIS_MODULE, &wdvb->dev, adapter_nr);
    if (err < 0)
        goto fail;
    adapter_registered = true;
    for (unsigned int i = 0; i < frontends; i++) {
        u8 systems[MAX_DELSYS];

        memcpy(systems, fes[i].fe.ops.delsys, sizeof(systems));
        fes[i].fe.ops = fe_ops_template;
        memcpy(fes[i].fe.ops.delsys, systems, sizeof(systems));
        if (!systems[0])
            fes[i].fe.ops.delsys[0] = SYS_DVBT; /* the core needs one */
        snprintf(fes[i].fe.ops.info.name, sizeof(fes[i].fe.ops.info.name), "webos_tv frontend%u",
                 i);
        err = dvb_register_frontend(&adapter, &fes[i].fe);
        if (err)
            goto fail;
        fes[i].registered = true;
    }
    if (acas) {
        if (acas_parse_id(&acas_card, card_id)) {
            pr_err("webos_tv: card_id=%s: 12 hex digits\n", card_id);
            err = -EINVAL;
            goto fail;
        }
        err = misc_register(&acas_dev);
        if (err)
            goto fail;
        acas_made = true;
    }
    if (atv) {
        strscpy(atv_v4l2.name, "wfm", sizeof(atv_v4l2.name)); /* the device has no driver */
        err = v4l2_device_register(&wdvb->dev, &atv_v4l2);
        if (err)
            goto fail;
        atv_v4l2_registered = true;
        strscpy(atv_vdev.name, "webos_vdev_video", sizeof(atv_vdev.name));
        atv_vdev.v4l2_dev = &atv_v4l2;
        atv_vdev.fops = &atv_fops;
        atv_vdev.release = video_device_release_empty;
        atv_vdev.vfl_dir = VFL_DIR_RX;
        atv_vdev.device_caps = V4L2_CAP_READWRITE | V4L2_CAP_VIDEO_CAPTURE | V4L2_CAP_STREAMING;
        err = video_register_device(&atv_vdev, VFL_TYPE_VIDEO, atv);
        if (err)
            goto fail;
        atv_made = true;
        if (atv_vdev.num != atv)
            pr_warn("webos_tv: the analog tuner is video%d, not video%u\n", atv_vdev.num, atv);
    }
    pr_info("webos_tv: %s, adapter %d, %u frontend(s); commands: LIB version %#x (%s), lock "
            "%#x, post job %#x, frequency %#x, status %#x, monitor %#x, special data %#x/%#x, "
            "strength %#x, quality %#x; "
            "analog tuner %s%u; card %s\n",
            platform, adapter.num, frontends, version_cmd, version, lock_cmd, postjob_cmd, freq_cmd,
            status_cmd, monitor_cmd, specstore_cmd, speccheck_cmd, strength_cmd, quality_cmd,
            atv ? "video" : "none ", atv, acas ? "acas0" : "none");
    return 0;
fail:
    webos_tv_cleanup();
    return err;
}

static void __exit webos_tv_exit(void)
{
    webos_tv_cleanup();
}

module_init(webos_tv_init);
module_exit(webos_tv_exit);
MODULE_LICENSE("GPL");
MODULE_VERSION("emulator");
MODULE_DESCRIPTION("LG's WDVB DVBv5 tuner driver, locking where its source has a signal "
                   "(beanjar)");
