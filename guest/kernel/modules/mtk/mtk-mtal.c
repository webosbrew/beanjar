// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* /dev/mtal: MediaTek's MTAL, one ioctl per MT* function of libmtal.so. Each function packs its
 * arguments as 32-bit words into an MTAL_IOCTL_<n>ARG_T on its stack and passes the command
 * _IOW(block, nr, that size) (MTVDO_Init: 0x40041100, no argument); the kdriver's mtal_ioctl
 * dispatches on the type byte to its block's handler (mtal_ioctl_mt<block>), which copies the
 * outputs to the pointers among the words. The answer is the MT_RESULT_T, MTR_OK 0, which
 * mtal_ioctl_result passes on; a negative one is printed and returned.
 *
 * Modelled: the SIF (I2C) block's transfers, on lgemu.ko's devices. Every other command answers
 * MTR_OK with its outputs untouched, and is counted in /proc/mtk/ioctl. */
#include <linux/compat.h>
#include <linux/module.h>
#include <linux/uaccess.h>

#include "lgemu-i2c.h"
#include "mtk.h"

/* ---- SIF: the board's I2C buses ---- */

/* MTSIF_WriteMultipleSubAddr and MTSIF_ReadMultipleSubAddr (W15L's libmtal.so 0x52b7c, 0x52c7c):
 * MTAL_IOCTL_8ARG_T of (channel, clock divider, device address (8-bit), the sub address (a user
 * pointer to its bytes, as they go on the wire), the sub address's length, buffer, byte count),
 * each a word. LG's HAL_I2C_Read/Write call them with RELEASE.cfg's [i2c] channel less one (the
 * NVM's 1 is SIF channel 0, the micom's 3 channel 2; seen on the emulator). */
#define SIF_WRITE 0x40200c0b
#define SIF_READ  0x40200c0c
#define SIF_MAX   4096

static char *i2c[8];
static int i2c_count;
module_param_array(i2c, charp, &i2c_count, 0444);
MODULE_PARM_DESC(i2c, "the board's I2C devices on MTAL's SIF buses, device:channel:address "
                      "(8-bit; RELEASE.cfg's [i2c] lines): micom, nvm, amp, tuner");

static bool sif_trace;
module_param(sif_trace, bool, 0644);
MODULE_PARM_DESC(sif_trace, "log every SIF transfer (debugging)");

static struct {
    enum lgemu_i2c_device dev;
    u32 channel, addr;
} i2c_map[ARRAY_SIZE(i2c)];

static enum lgemu_i2c_device sif_device(u32 channel, u32 addr)
{
    for (int i = 0; i < i2c_count; i++)
        if (i2c_map[i].channel == channel && i2c_map[i].addr == (addr & 0xfe))
            return i2c_map[i].dev;
    return LGEMU_I2C_NONE;
}

static long sif_xfer(unsigned int cmd, void __user *uarg)
{
    u32 a[7];
    u8 sub[4];
    enum lgemu_i2c_device dev;
    int ret;

    if (copy_from_user(a, uarg, sizeof(a)))
        return -EFAULT;
    if (sif_trace)
        pr_info("mtk: SIF %s channel %u device %#x, %u sub address bytes, %u bytes\n",
                cmd == SIF_READ ? "read" : "write", a[0], a[2], a[4], a[6]);
    if (a[4] > sizeof(sub) || a[6] > SIF_MAX)
        return -EINVAL;
    if (a[4] && copy_from_user(sub, compat_ptr(a[3]), a[4]))
        return -EFAULT;
    dev = sif_device(a[0], a[2]);
    if (dev == LGEMU_I2C_NONE) {
        pr_info_ratelimited("mtk: SIF %s channel %u device %#x: no device\n",
                            cmd == SIF_READ ? "read" : "write", a[0], a[2]);
        return -EIO;
    }
    ret = lgemu_i2c_user_xfer(dev, (a[2] & 0xff) >> 1, sub, a[4], compat_ptr(a[5]), a[6],
                              cmd == SIF_READ);
    /* MTR_OK; HAL_I2C_Read/Write retry on anything else */
    return ret < 0 ? (ret == -EREMOTEIO ? -EIO : ret) : 0;
}

long mtk_mtal_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    void __user *uarg = compat_ptr(arg);

    mtk_count_ioctl("mtal", cmd);
    switch (cmd) {
    case SIF_WRITE:
    case SIF_READ:
        return sif_xfer(cmd, uarg);
    }
    return mtk_mark_unhandled("mtal", cmd, 0);
}

int mtk_mtal_setup(void)
{
    for (int i = 0; i < i2c_count; i++) {
        unsigned int n[2];

        if (lgemu_i2c_parse(i2c[i], &i2c_map[i].dev, n, 2) || n[1] > 0xff)
            return -EINVAL;
        i2c_map[i].channel = n[0];
        i2c_map[i].addr = n[1] & 0xfe;
    }
    return 0;
}
