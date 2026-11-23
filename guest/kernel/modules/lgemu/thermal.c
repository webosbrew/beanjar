// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* /proc/thermalinfo: the SoC's temperature, and on webOS 23 and later its IDDQ currents, as the
 * stock kernels' power-management drivers give them. fancontroller's thermal report
 * (ThermalMonitorThread, about once a second: getSoCCurrentTemperature) opens
 * /proc/thermalinfo/soc_temperature read-write, reads up to 32 bytes and takes atoi of them as
 * degrees Celsius, averaged over three samples; from webOS 23 on it reads iddq_core and iddq_scpu
 * the same way (getIDDQCoreCurrentValue, getIDDQCpuCurrentValue). Without the files it logs a
 * warning each second and reports 65535.
 *
 * On a set the directory is built into the kernel, next to the SoC's PM/AVS driver ("pm/tune",
 * "pm/debug", "thermalinfo", "check tempdir"; O22's and LM21's kernel images), with no module of
 * its own to follow; so it is a part of lgemu.ko here, one for every SoC, which the board turns on
 * with the parameters (guest/targets/soc/<chip>.toml, thermal):
 *   thermalinfo  1: /proc/thermalinfo/soc_temperature
 *   iddq         "core,scpu": iddq_core and iddq_scpu too, those values (mA)
 *   soc_temp     the temperature, °C (set at runtime: `beanjar sensor set temperature`)
 * Each file reads as the number and a newline. Writes are refused, as on a file without a write
 * handler. */
#define pr_fmt(fmt) KBUILD_MODNAME ": thermal: " fmt
#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/moduleparam.h>
#include <linux/proc_fs.h>
#include <linux/seq_file.h>
#include "lgemu.h"

/* ---- the values (tests/unit/kernel/test_lgemu_thermal.py compiles this part on the host) ---- */

#define THERMAL_MIN (-40)
#define THERMAL_MAX 200 /* past any shutdown threshold (fancontroller's junction 145) */

/* An integer within [min, max], a newline allowed at the end; -EINVAL otherwise. */
static int thermal_parse_int(const char *text, int min, int max, int *out)
{
    int v, len = strlen(text), used = -1;

    if (len && text[len - 1] == '\n')
        len--;
    if (sscanf(text, "%d%n", &v, &used) != 1 || used != len || v < min || v > max)
        return -EINVAL;
    *out = v;
    return 0;
}

/* "core,scpu" (mA, each 0 to 65534) into out[2]; -EINVAL otherwise. */
static int thermal_parse_iddq(const char *text, int *out)
{
    int v[2], len = strlen(text), used = -1;

    if (len && text[len - 1] == '\n')
        len--;
    if (sscanf(text, "%d,%d%n", &v[0], &v[1], &used) != 2 || used != len)
        return -EINVAL;
    for (int i = 0; i < 2; i++)
        if (v[i] < 0 || v[i] > 65534)
            return -EINVAL;
    out[0] = v[0];
    out[1] = v[1];
    return 0;
}

/* ---- end of the values ---- */

static bool thermalinfo;
module_param(thermalinfo, bool, 0444);
MODULE_PARM_DESC(thermalinfo, "thermal: /proc/thermalinfo/soc_temperature (fancontroller's SoC "
                              "temperature)");

static int soc_temp = 45; /* idle, in a room */
static int iddq_values[2];
static bool iddq_given;

static int soc_temp_set(const char *val, const struct kernel_param *kp)
{
    int v, err = thermal_parse_int(val, THERMAL_MIN, THERMAL_MAX, &v);

    if (err)
        return err;
    WRITE_ONCE(soc_temp, v);
    if (thermalinfo)
        pr_info("SoC %d C\n", v);
    return 0;
}

static int soc_temp_get(char *buf, const struct kernel_param *kp)
{
    return sprintf(buf, "%d\n", READ_ONCE(soc_temp));
}

static const struct kernel_param_ops soc_temp_ops = {.set = soc_temp_set, .get = soc_temp_get};
module_param_cb(soc_temp, &soc_temp_ops, NULL, 0644);
MODULE_PARM_DESC(soc_temp, "thermal: the SoC's temperature in degrees Celsius, -40 to 200 "
                           "(default 45)");

static int iddq_set(const char *val, const struct kernel_param *kp)
{
    int v[2], err = thermal_parse_iddq(val, v);

    if (err)
        return err;
    WRITE_ONCE(iddq_values[0], v[0]);
    WRITE_ONCE(iddq_values[1], v[1]);
    iddq_given = true;
    return 0;
}

static int iddq_get(char *buf, const struct kernel_param *kp)
{
    return sprintf(buf, "%d,%d\n", READ_ONCE(iddq_values[0]), READ_ONCE(iddq_values[1]));
}

static const struct kernel_param_ops iddq_ops = {.set = iddq_set, .get = iddq_get};
module_param_cb(iddq, &iddq_ops, NULL, 0644);
MODULE_PARM_DESC(iddq, "thermal: /proc/thermalinfo/iddq_core and iddq_scpu (webOS 23 on), "
                       "core,scpu in mA");

static struct proc_dir_entry *dir;

static int soc_temperature_show(struct seq_file *m, void *v)
{
    seq_printf(m, "%d\n", READ_ONCE(soc_temp));
    return 0;
}

static int iddq_show(struct seq_file *m, void *v)
{
    seq_printf(m, "%d\n", READ_ONCE(*(int *)m->private));
    return 0;
}

int lgemu_thermal_init(void)
{
    if (!thermalinfo) {
        if (iddq_given)
            pr_warn("iddq without thermalinfo: no /proc/thermalinfo\n");
        return 0;
    }
    dir = proc_mkdir("thermalinfo", NULL);
    if (!dir)
        return -ENOMEM;
    if (!proc_create_single("soc_temperature", 0644, dir, soc_temperature_show))
        goto fail;
    if (iddq_given &&
        (!proc_create_single_data("iddq_core", 0444, dir, iddq_show, &iddq_values[0]) ||
         !proc_create_single_data("iddq_scpu", 0444, dir, iddq_show, &iddq_values[1])))
        goto fail;
    pr_info("/proc/thermalinfo: SoC %d C%s\n", soc_temp, iddq_given ? ", IDDQ" : "");
    return 0;
fail:
    proc_remove(dir);
    dir = NULL;
    return -ENOMEM;
}

void lgemu_thermal_exit(void)
{
    proc_remove(dir);
    dir = NULL;
}
