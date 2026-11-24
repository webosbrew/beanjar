// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* lg115x.ko: LG's own kernel drivers for its LG115x SoCs (H13, M14, M16P/LG1313), the "kdrivers"
 * built into LG's kernels (drivers/lg_kdriver), as far as KADP (libkadaptor) needs them. The
 * interface is LG's, the same across these chips, so the chip's values are parameters from the chip
 * table (guest/targets/soc/, through boot.conf's SOC_MODULES), not compiled in.
 *
 * So far:
 *
 * - /proc/lg/base/{chip_rev,ace_rev,platform,graphic_res}: the base driver's chip identity, which
 *   KADP_OSA_Initialize reads with fscanf(" %x") into lx_chip. The stock kernel's
 *   BASE_ProbeChipRev derives it from the bootloader's chip= (and ace=) and hwopt=; each file
 *   reads as the stock proc handler prints it, "0x%x\n".
 * - /proc/lg/base/board_opt, only when the parameter board_opt is given: M16P's kernel (W17H,
 *   4.4.3) adds BOARD_OPT to lx_chip (BASE_PLT_Probe: DDR, FRC and panel bits from hwopt= and
 *   modelopt=), which its KADP_OSA_Initialize reads as it reads the others; the H13 and M14
 *   kernels have no such file.
 * - /dev/lg/iic<n> and /dev/lg/iicGPIO<n>: the I2C driver (LG's kdriver/core/i2c, i2c_drv.c), major
 *   154, the hardware buses at minors 0-15 and the GPIO ones at 64-67 (the firmware's
 *   /etc/device_table, whose nodes makedevs creates). KADP_I2C_Open(n) opens iic<n>, which the
 *   HAL's channel n + 1 is; KADP_I2C_Read/Write fill LX_I2C_RW_DATA_T and expect the byte count
 *   back (i2c_core.c's _I2C_ReadCmd returns bufSize). The devices on the buses are lgemu.ko's
 *   (lgemu-i2c.h), placed by the parameter i2c from the chip's table; any other slave does not
 *   acknowledge, as a board without that chip.
 * - /dev/spidev0.<n>: the SoC's SPI master under Linux's spidev (major 153, the firmware's
 *   /etc/device_table), which KADP_SPI_* use with spidev's ABI (read, write, the mode, word size
 *   and speed ioctls, SPI_IOC_MESSAGE). The parameter spi_cs is the number of chip selects. No
 *   chip on it is modelled (a UHD H13 board's D14 and U14): a transfer completes and every byte
 *   read is 0.
 * - /dev/lg/se0, only when the parameter se_mem is given: the security engine driver (LG's
 *   kdriver/core/se, se_drv.c and se_kapi.h, major 159), KADP's gateway to the TrustZone firmware.
 *   Its memory (GET_MEM_INFO: the SE region and the TZ firmware's, se_mem and se_tzfw) is the
 *   module's own zeroed pages, which mmap of the SE region at its physical address gives every
 *   process alike; a TZ operation (RUN_TZ_OP, the eight words the stock driver hands to SMC 0)
 *   returns TZ_SUCCESS with the words unchanged and nothing written: no secure world is
 *   modelled. The first time each (type, command) pair is seen it is logged.
 * - /dev/lg/logm, only when the parameter logm_obj is given: the log manager (LG's
 * kdriver/core/logm, logm_drv.c, logm_cfg.c and logm_kapi.h, major 1810), where KADP and liblogs
 * register their modules and write their lines. Its object table (200 entries: name, masks, level,
 * references, counts) is mapped read-only into every client, which writes a line only for a level
 * the object's mask enables; a new object has ERROR and WARNING, as the stock LOGM_FORCE_ERR_WARN.
 *   The entry's size differs by generation (logm_obj: 76 in WT1H's KADP, 80 in W15M's with a user
 *   mask, 84 in W17H's with a hidden mask too). A user line goes to printk as the stock path
 *   control's default ("force printk") prints it, "<name> <level> (<pid>:<tid>) <line>", unless
 *   hidden; there is no ring to read (the stock one drops what it printed). Unlike the stock, a
 *   line an object repeats is printed once and then counted (a poll of a device no driver here
 *   answers would print the same error for ever).
 * - /dev/lg/de0-3, only when the parameter de_mem is given: the display engine (LG's
 *   kdriver/core/de, de_drv.c, de_io.c and de_kapi.h, major 145), as far as HAL_VT's video-to-
 *   memory (VTM) capture needs it: the capacity (de_capacity, LX_DE_CAPACITY_T's values; M17's
 *   DE_REG_M17_GetCapacity on m16p), the system status, the output frame rate, the source status
 *   (none), the VTM block state, the sub-window mode and source sizes (kept as set, as LG's
 *   resource_mgr.c), and SET_VTM's shared page: KADP maps it at the address SET_VTM
 *   returns (de_mem's base), and reads each window's frame there, which this driver writes at
 *   once (the stock driver's DE firmware fills it): three 4:2:0 semi-planar buffers at addresses
 *   after the page, at the requested size. Nothing is behind those addresses (tvservice maps them
 *   through its own /dev/mem). Every other setting is accepted and every other query refused,
 *   each logged once.
 * - /dev/lg/gpio0, only when the parameter gpio_pins is given: the GPIO driver (LG's
 * kdriver/core/gpio, gpio_drv.c and gpio_kapi.h, major 152), KADP_GPIO_*'s pin mux, mode and value
 * of the SoC's pins (gpio_pins, gpio_core.c's max_num) and the ACE's 16 (the EX commands).
 * LX_GPIO_PARAM_T is 24 bytes in H13's KADP and 36 in M14's and M16P's; H13's query is an _IOW,
 * M16P's an _IOWR. A pin reads what was set on it (nothing on the board drives one); no pin
 * interrupt fires. Each pin a process reads is logged once.
 * - /sys/class/gpio's lines 0 to gpio_lines - 1, only when the parameter gpio_lines is given:
 *   lgemu's GPIO chip (lgemu-drv.h), where O22's libdile_gpio exports its pins by number; an input
 *   reads its gpio_level, 0 unless named (the board with nothing connected).
 * - /dev/lg/pe0: the picture enhancement driver (LG's kdriver/core/pe, pe_drv.c, pe_io.c and
 *   pe_kapi.h, major 156), whose whole interface is one packet ioctl (rw, module and function
 *   types, a size, a buffer). Inits and settings are accepted; the TNR's level query reads 0 (no
 *   picture goes through it); on M17, with pe_hdr, the HDR LUT map's description and 3D LUT info,
 *   and the map itself (mmap). Any other query is refused; each packet type is logged once.
 * - /dev/lg/amst0, only when the parameter amst is given: the audio master (LG's
 * kdriver/core/audio, audio_drv_master.c and audio_kapi_master.h, major 143 minor 4). Its ioctl
 * numbers moved between the 2014 and 2016 kdrivers, so the handle's and the event query's are the
 * parameter's, with the master's device and the settings to keep for their queries. Every setting
 * is accepted (no DSP: the sound is PulseAudio's), no event comes, a DSP register reads 0; other
 * queries are refused, each command logged once.
 * - /dev/lg/adec0, aren0 and aad0, and on M16P aaad0, with amst: the audio driver's decoders and
 *   renderers (143:0 and 143:3; a file is one of DEC0-1 or REN0-11 once SET_INDEX_CTX or the
 *   handle query allocates it, as the 2016 and 2014 drivers do) and the analog audio decoder (LG's
 *   core/aad at 140:0; the 2016 audio driver's copy at 143:5, whose node the driver makes). No
 *   stream is decoded: a decoder answers its open's state, the analog decoder as with no carrier
 *   on the SIF; settings are accepted.
 * - /dev/lg/sys0, be0, afe0 and ovi0: the register driver (a SoC register reads what was written
 *   to it, 0 before), the back end (be: its firmware-info and register queries, by the chip's
 *   numbers; be_led: M17's LED register block), the analog front end and the output interface
 *   (settings accepted). Any other query is refused; each command is logged once.
 * - /dev/mem's page at 0xfd100000, only when the parameter timer_hz is given: the SoC's SP804
 *   timers, whose timer 2 counts down from boot at timer_hz (bootd's hardware time).
 * - /dev/lg/gfx0: the gfx driver (LG's kdriver/core/gfx, major 151; the firmware's device_table
 *   makes the node, its mknod job links /dev/gfx to it), as the graphics memory the soc layer's
 *   DILE_GAL maps (lgemu.ko's gfx.c); none of LG's gfx ioctls (KADP's 2D engine, which the shim
 *   replaces) is answered.
 * - /dev/lg/lx-gop (parameter gop, NetCast 4): the OSD's frame buffer over the same memory, the
 *   emulator's GOP contract (include/mstar-gop.h, gop.c); init binds it over the firmware's fb0. */
#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt
#include <linux/compat.h>
#include <linux/devmem_window.h>
#include <linux/ctype.h>
#include <linux/fs.h>
#include <linux/jhash.h>
#include <linux/module.h>
#include <linux/proc_fs.h>
#include <linux/seq_file.h>
#include <linux/slab.h>
#include <linux/spi/spidev.h>
#include <linux/uaccess.h>
#include <linux/vmalloc.h>
#include <linux/mutex.h>
#include <linux/math64.h>
#include <linux/mm.h>
#include <linux/timekeeping.h>
#include "lgemu-drv.h"
#include "lgemu-i2c.h"

#include "lg115x.h"

/* ---- the chip: parameters, all required ---- */

static unsigned int chip_rev, ace_rev, platform, graphic_res, board_opt;
module_param(chip_rev, uint, 0444);
MODULE_PARM_DESC(chip_rev, "LX_CHIP_REV: the chip's id << 8 | its revision (M14 B0: 0x14b0)");
module_param(ace_rev, uint, 0444);
MODULE_PARM_DESC(ace_rev, "the ACE's revision, as chip_rev (the chip's own without ace=)");
module_param(platform, uint, 0444);
MODULE_PARM_DESC(platform, "CHIP_PLT, the platform word");
module_param(graphic_res, uint, 0444);
MODULE_PARM_DESC(graphic_res, "GFX_RES, the graphic resolution, width << 16 | height");
module_param(board_opt, uint, 0444);
MODULE_PARM_DESC(board_opt, "BOARD_OPT (M16P's kernels), /proc/lg/base/board_opt; 0: no such file");
static bool v4l2_capture;
module_param(v4l2_capture, bool, 0444);
MODULE_PARM_DESC(v4l2_capture, "/dev/video60, the TV picture from the viewer (webos.capture)");

static int capture_add(void)
{
    return v4l2_capture ? lgemu_v4l2_capture_add() : 0;
}

/* ---- shared: commands logged once, the device class ---- */

/* Logs cmd of dev the first time, as answered (a query), accepted (a setting) or not answered
 * (ret nonzero); returns ret. */
long lx_seen_log(struct lx_seen *s, const char *dev, unsigned int cmd, long ret)
{
    unsigned int i;

    mutex_lock(&s->lock);
    for (i = 0; i < s->count && s->cmd[i] != cmd; i++)
        ;
    if (i == s->count && i < LX_SEEN_MAX) {
        s->cmd[s->count++] = cmd;
        pr_info("%s: ioctl %#x (nr %u, %u bytes) %s (%s)\n", dev, cmd, _IOC_NR(cmd), _IOC_SIZE(cmd),
                ret                         ? "not answered"
                : _IOC_DIR(cmd) & _IOC_READ ? "answered"
                                            : "accepted",
                current->comm);
    }
    mutex_unlock(&s->lock);
    return ret;
}

/* LG's device class (os_util.c's kdrv_dev_class): nodes as /dev/lg/<name>, for the devices a
 * stock driver makes itself rather than the firmware's device table */
static char *lx_devnode(const struct device *dev, umode_t *mode)
{
    if (mode)
        *mode = 0666;
    return kasprintf(GFP_KERNEL, "lg/%s", dev_name(dev));
}

const struct class lx_class = {.name = "lg", .devnode = lx_devnode};

static void lx_class_remove(void)
{
    class_unregister(&lx_class);
}

static int lx_class_add(void)
{
    return class_register(&lx_class);
}

/* ---- /proc/lg/base ---- */

static int base_show(struct seq_file *m, void *v)
{
    seq_printf(m, "0x%x\n", *(unsigned int *)m->private);
    return 0;
}

static int base_open(struct inode *inode, struct file *file)
{
    return single_open(file, base_show, pde_data(inode));
}

static const struct proc_ops base_proc_ops = {
    .proc_open = base_open,
    .proc_read = seq_read,
    .proc_lseek = seq_lseek,
    .proc_release = single_release,
};

static const struct {
    const char *name;
    unsigned int *value;
} base_files[] = {
    {"chip_rev", &chip_rev},
    {"ace_rev", &ace_rev},
    {"platform", &platform},
    {"graphic_res", &graphic_res},
};

static struct proc_dir_entry *lg_dir;

/* the devices, added in this order and removed in the reverse */
static const struct {
    const char *name;
    int (*add)(void);
    void (*remove)(void);
} lx_devices[] = {
    {"class", lx_class_add, lx_class_remove},
    {"i2c", lx_i2c_add, lx_i2c_remove},
    {"spi", lx_spi_add, lx_spi_remove},
    {"ucom", lx_ucom_add, lx_ucom_remove},
    {"se", lx_se_add, lx_se_remove},
    {"logm", lx_logm_add, lx_logm_remove},
    {"de", lx_de_add, lx_de_remove},
    {"v4l2-capture", capture_add, lgemu_v4l2_capture_remove},
    {"gpio", lx_gpio_add, lx_gpio_remove},
    {"sysfs-gpio", lx_sysfs_gpio_add, lx_sysfs_gpio_remove},
    {"pe", lx_pe_add, lx_pe_remove},
    {"gfx", lx_gfx_add, lx_gfx_remove},
    {"gop", lx_gop_add, lx_gop_remove},
    {"amst", lx_aud_add, lx_aud_remove},
    {"audio", lx_aud_modules_add, lx_aud_modules_remove},
    {"kdevs", lx_kdevs_add, lx_kdevs_remove},
    {"timer", lx_timer_add, lx_timer_remove},
};

static void lx_devices_remove(size_t n)
{
    while (n--)
        lx_devices[n].remove();
}

static int __init lg115x_init(void)
{
    struct proc_dir_entry *base;
    size_t i;
    int err;

    if (!chip_rev || !ace_rev || !platform || !graphic_res) {
        pr_err("chip_rev, ace_rev, platform and graphic_res are required\n");
        return -EINVAL;
    }
    lg_dir = proc_mkdir("lg", NULL);
    base = lg_dir ? proc_mkdir("base", lg_dir) : NULL;
    if (!base)
        goto fail;
    for (i = 0; i < ARRAY_SIZE(base_files); i++)
        if (!proc_create_data(base_files[i].name, 0444, base, &base_proc_ops, base_files[i].value))
            goto fail;
    if (board_opt && !proc_create_data("board_opt", 0444, base, &base_proc_ops, &board_opt))
        goto fail;
    pr_info("chip_rev 0x%x ace_rev 0x%x platform 0x%x graphic_res 0x%08x board_opt 0x%x\n",
            chip_rev, ace_rev, platform, graphic_res, board_opt);
    for (i = 0; i < ARRAY_SIZE(lx_devices); i++) {
        err = lx_devices[i].add();
        if (err) {
            pr_err("%s: cannot add the device (%d)\n", lx_devices[i].name, err);
            lx_devices_remove(i);
            proc_remove(lg_dir);
            return err;
        }
    }
    err = lx_de_histogram_add(lg_dir);
    if (err) {
        lx_devices_remove(ARRAY_SIZE(lx_devices));
        proc_remove(lg_dir);
        return err;
    }
    return 0;
fail:
    proc_remove(lg_dir);
    return -ENOMEM;
}

static void __exit lg115x_exit(void)
{
    lx_de_histogram_remove();
    lx_devices_remove(ARRAY_SIZE(lx_devices));
    proc_remove(lg_dir);
}

module_init(lg115x_init);
module_exit(lg115x_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("LG's LG115x kernel drivers for KADP, for the webOS emulator");
