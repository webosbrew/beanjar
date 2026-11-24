// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* Internal interfaces between the LG115x device families and module lifecycle. */
#ifndef LG115X_H
#define LG115X_H

#include <linux/device.h>
#include <linux/mutex.h>
#include <linux/types.h>

/* The commands a device has seen, each logged the first time with what it was answered. */
#define LX_SEEN_MAX 128

struct lx_seen {
    struct mutex lock;
    u32 cmd[LX_SEEN_MAX];
    unsigned int count;
};

#define LX_SEEN_INIT(name) {.lock = __MUTEX_INITIALIZER(name.lock)}

long lx_seen_log(struct lx_seen *s, const char *dev, unsigned int cmd, long ret);
extern const struct class lx_class;

int lx_i2c_add(void);
void lx_i2c_remove(void);

int lx_spi_add(void);
void lx_spi_remove(void);

int lx_ucom_add(void);
void lx_ucom_remove(void);

int lx_se_add(void);
void lx_se_remove(void);

int lx_logm_add(void);
void lx_logm_remove(void);

struct proc_dir_entry;
int lx_de_histogram_add(struct proc_dir_entry *parent);
void lx_de_histogram_remove(void);

int lx_de_add(void);
void lx_de_remove(void);

int lx_gpio_add(void);
void lx_gpio_remove(void);

int lx_sysfs_gpio_add(void);
void lx_sysfs_gpio_remove(void);

int lx_pe_add(void);
void lx_pe_remove(void);

int lx_gfx_add(void);
void lx_gfx_remove(void);
/* gop.c: the OSD's frame buffer node lx-gop (the parameter gop, NetCast 4) */
int lx_gop_add(void);
void lx_gop_remove(void);

int lx_aud_add(void);
void lx_aud_remove(void);

int lx_aud_modules_add(void);
void lx_aud_modules_remove(void);

int lx_kdevs_add(void);
void lx_kdevs_remove(void);

int lx_timer_add(void);
void lx_timer_remove(void);

#endif /* LG115X_H */
