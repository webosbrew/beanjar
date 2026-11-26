// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* The board's I2C devices (i2c.c) on standard Linux I2C adapters, for the firmwares that reach
 * them through i2c-dev's /dev/i2c-N and I2C_RDWR instead of a vendor bus driver: webOS 4's
 * DILE_I2C (goldilocks on MStar m3, goldilocks2 on Realtek k5lp).
 *
 *   i2c_buses  the adapters, by number (i2c_add_numbered_adapter): W18R's DILE_I2C opens
 *              /dev/i2c-0 to -6 and falls back to the HAL if any of them does not open, so an
 *              adapter with no device on it is registered all the same.
 *   i2c_dev    the devices, device:bus:addr as lg115x.ko's i2c= but with the 7-bit address a
 *              Linux adapter sees (the 8-bit RELEASE.cfg address >> 1), bus one of i2c_buses.
 *              A device may sit at several addresses.
 *
 * A transfer goes to lgemu_i2c_xfer as the adapter gets it, each run of messages to one address
 * as one transfer. An address with no device does not acknowledge (-ENXIO, as a bus driver
 * reports an address NACK); a device's own NACK is its -EREMOTEIO. SMBus is the core's emulation
 * over I2C messages. With neither parameter nothing is registered. */
#define pr_fmt(fmt) KBUILD_MODNAME ": i2c_adapters: " fmt
#include <linux/i2c.h>
#include <linux/module.h>
#include <linux/slab.h>
#include "lgemu.h"
#include "lgemu-i2c.h"

#define MAX_BUSES 16
#define MAX_DEVS  16
#define ADDRS     128

static unsigned int i2c_buses[MAX_BUSES];
static int i2c_buses_count;
module_param_array(i2c_buses, uint, &i2c_buses_count, 0444);
MODULE_PARM_DESC(i2c_buses, "i2c_adapters: the Linux I2C adapters to register, by bus number "
                            "(/dev/i2c-N with i2c-dev), with or without devices");

static char *i2c_dev[MAX_DEVS];
static int i2c_dev_count;
module_param_array(i2c_dev, charp, &i2c_dev_count, 0444);
MODULE_PARM_DESC(i2c_dev,
                 "i2c_adapters: the board's I2C devices on them, device:bus:addr "
                 "(micom, nvm, amp, accel, light1, light2, tof, ledmicom, dlpc; bus one of "
                 "i2c_buses; the 7-bit address)");

struct lgemu_adapter {
    struct i2c_adapter adap;
    u8 map[ADDRS]; /* enum lgemu_i2c_device at each address */
    bool added;
};

static struct lgemu_adapter *adapters;

static int adapter_xfer(struct i2c_adapter *adap, struct i2c_msg *msgs, int num)
{
    struct lgemu_adapter *a = container_of(adap, struct lgemu_adapter, adap);
    int first, last, ret;

    for (first = 0; first < num; first = last) {
        u16 addr = msgs[first].addr;
        enum lgemu_i2c_device dev;

        if (msgs[first].flags & I2C_M_TEN)
            return -EOPNOTSUPP;
        for (last = first + 1;
             last < num && msgs[last].addr == addr && !(msgs[last].flags & I2C_M_TEN); last++)
            ;
        dev = addr < ADDRS ? a->map[addr] : LGEMU_I2C_NONE;
        if (dev == LGEMU_I2C_NONE)
            return -ENXIO;
        ret = lgemu_i2c_xfer(dev, msgs + first, last - first);
        if (ret < 0)
            return ret;
    }
    return num;
}

static u32 adapter_functionality(struct i2c_adapter *adap)
{
    return I2C_FUNC_I2C | I2C_FUNC_SMBUS_EMUL;
}

static const struct i2c_algorithm adapter_algo = {
    .xfer = adapter_xfer,
    .functionality = adapter_functionality,
};

static struct lgemu_adapter *find_bus(unsigned int bus)
{
    for (int i = 0; i < i2c_buses_count; i++)
        if (adapters[i].adap.nr == bus)
            return &adapters[i];
    return NULL;
}

static int parse(void)
{
    for (int i = 0; i < i2c_buses_count; i++) {
        if (i2c_buses[i] > INT_MAX) {
            pr_err("i2c_buses: %u is not a bus number\n", i2c_buses[i]);
            return -EINVAL;
        }
        for (int j = 0; j < i; j++)
            if (i2c_buses[j] == i2c_buses[i]) {
                pr_err("i2c_buses: %u twice\n", i2c_buses[i]);
                return -EINVAL;
            }
        adapters[i].adap.nr = i2c_buses[i];
    }
    for (int i = 0; i < i2c_dev_count; i++) {
        unsigned int at[2]; /* bus, address */
        enum lgemu_i2c_device dev;
        struct lgemu_adapter *a;

        if (lgemu_i2c_parse(i2c_dev[i], &dev, at, 2) || at[1] >= ADDRS) {
            pr_err("i2c_dev=%s: not device:bus:addr (7-bit)\n", i2c_dev[i]);
            return -EINVAL;
        }
        a = find_bus(at[0]);
        if (!a) {
            pr_err("i2c_dev=%s: bus %u is not in i2c_buses\n", i2c_dev[i], at[0]);
            return -EINVAL;
        }
        a->map[at[1]] = dev;
    }
    return 0;
}

int lgemu_i2c_adapters_init(void)
{
    int err;

    if (!i2c_buses_count) {
        if (i2c_dev_count) {
            pr_err("i2c_dev without i2c_buses\n");
            return -EINVAL;
        }
        return 0;
    }
    adapters = kcalloc(i2c_buses_count, sizeof(*adapters), GFP_KERNEL);
    if (!adapters)
        return -ENOMEM;
    err = parse();
    if (err)
        goto fail;
    for (int i = 0; i < i2c_buses_count; i++) {
        struct i2c_adapter *adap = &adapters[i].adap;

        adap->owner = THIS_MODULE;
        adap->algo = &adapter_algo;
        snprintf(adap->name, sizeof(adap->name), "lgemu i2c-%d", adap->nr);
        err = i2c_add_numbered_adapter(adap);
        if (err) {
            pr_err("i2c-%d: cannot register (%d)\n", adap->nr, err);
            goto fail;
        }
        adapters[i].added = true;
    }
    for (int i = 0; i < i2c_dev_count; i++)
        pr_info("i2c_dev: %s\n", i2c_dev[i]);
    pr_info("%d adapters\n", i2c_buses_count);
    return 0;
fail:
    lgemu_i2c_adapters_exit();
    return err;
}

void lgemu_i2c_adapters_exit(void)
{
    if (!adapters)
        return;
    for (int i = i2c_buses_count; i--;)
        if (adapters[i].added)
            i2c_del_adapter(&adapters[i].adap);
    kfree(adapters);
    adapters = NULL;
}
