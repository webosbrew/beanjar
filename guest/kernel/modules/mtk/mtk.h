/* SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors */
/* SPDX-License-Identifier: GPL-2.0-only */
/* mtk.ko: MediaTek's kernel drivers (kdrv_mtk5882.ko on W15L) as its MTAL userland, libmtal.so,
 * uses them: what the parts of the module share. */
#ifndef MTK_H
#define MTK_H
#include <linux/fs.h>
#include <linux/types.h>

#include "lgemu-drv.h"

/* an ioctl of a node, counted in /proc/mtk/ioctl */
void mtk_count_ioctl(const char *dev, unsigned int cmd);
/* an ioctl a node does not model: counted, and logged the first time, with its answer ret */
long mtk_unhandled(const char *dev, unsigned int cmd, long ret);
/* the same for an ioctl already counted */
long mtk_mark_unhandled(const char *dev, unsigned int cmd, long ret);

/* mtk-mtal.c: /dev/mtal's ioctls (one per MT* function of libmtal) */
long mtk_mtal_ioctl(struct file *file, unsigned int cmd, unsigned long arg);
int mtk_mtal_setup(void);

/* mtk-cb.c: /dev/cb, the callback channel libmtal's callback thread waits on */
extern const struct file_operations mtk_cb_fops;

#endif
