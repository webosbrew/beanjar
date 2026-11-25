// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* lgemu: interfaces of LG's webOS TV kernel that the stock firmware uses and the emulator's
 * mainline kernel does not have, as one out-of-tree module (guest/kernel/modules/lgemu,
 * docs/dev/guides/build.md "Kernel module lgemu"). guest/initramfs/rootfs/init loads it before
 * webOS starts.
 *
 * Each interface is a part in its own file with an init and an exit function; this file only
 * runs them: in order at load, and in reverse at unload or when a later part fails to start. */
#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt
#include <linux/kernel.h>
#include <linux/module.h>
#include "lgemu.h"

static const struct {
    const char *name;
    int (*init)(void);
    void (*exit)(void);
} parts[] = {
    {"partinfo", lgemu_partinfo_init, lgemu_partinfo_exit},
    {"emmc", lgemu_emmc_init, lgemu_emmc_exit},
    {"low_mem_notify", lgemu_low_mem_notify_init, lgemu_low_mem_notify_exit},
    {"i2c", lgemu_i2c_init, lgemu_i2c_exit},
    {"lgib", lgemu_lgib_init, lgemu_lgib_exit},
    {"sestore", lgemu_sestore_init, lgemu_sestore_exit},
    {"gfx", lgemu_gfx_init, lgemu_gfx_exit},
    {"i2c_adapters", lgemu_i2c_adapters_init, lgemu_i2c_adapters_exit},
    {"snd", lgemu_snd_init, lgemu_snd_exit},
    {"mic", lgemu_mic_init, lgemu_mic_exit},
    {"video", lgemu_video_init, lgemu_video_exit},
    {"v4l2-vsc", lgemu_vsc_init, lgemu_vsc_exit},
    {"earc", lgemu_earc_init, lgemu_earc_exit},
    {"v4l2-hdmi", lgemu_hdmi_init, lgemu_hdmi_exit},
    {"capture", lgemu_capture_init, lgemu_capture_exit},
    {"cec", lgemu_cec_init, lgemu_cec_exit},
    {"thermal", lgemu_thermal_init, lgemu_thermal_exit},
    {"fb", lgemu_fb_init, lgemu_fb_exit},
};

static int __init lgemu_init(void)
{
    for (size_t i = 0; i < ARRAY_SIZE(parts); i++) {
        int err = parts[i].init();
        if (err) {
            pr_err("%s: cannot start (%d)\n", parts[i].name, err);
            while (i--)
                parts[i].exit();
            return err;
        }
    }
    return 0;
}

static void __exit lgemu_exit(void)
{
    for (size_t i = ARRAY_SIZE(parts); i--;)
        parts[i].exit();
}

module_init(lgemu_init);
module_exit(lgemu_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("LG webOS TV kernel interfaces for the webOS emulator");
