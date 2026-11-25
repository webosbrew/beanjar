/* SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors */
/* SPDX-License-Identifier: GPL-2.0-only */
/* Private interfaces between rtk.ko's source units. State and module parameters
 * stay with their implementation; these are not exported kernel symbols. */
#ifndef RTK_INTERNAL_H
#define RTK_INTERNAL_H

#include <linux/fs.h>
#include <linux/types.h>

#define NODE_NAME_LEN 32
#define RPC_CPUS_MAX  4 /* rtk-main.c's rpc_cpus: four minors each, below RPC_CTRL */
#define RPC_CTRL      100

struct proc_dir_entry;

/* Generic node registration owns the immutable node list and ioctl table. */
bool rtk_node_exists(const char *name);
void rtk_count_ioctl(const char *dev, unsigned int cmd);
long rtk_unhandled(const char *dev, unsigned int cmd, long ret);
long rtk_mark_unhandled(const char *dev, unsigned int cmd, long ret);

/* Memory's state remains private; VT/control use the same physical backing. */
int rtk_memory_params_check(void);
int rtk_dcu_setup(void);
int rtk_store_file_create(void);
void rtk_store_file_close(void);
void rtk_memory_cleanup(void);
void rtk_memory_log(int nodes_count);
u64 rtk_dcu_take(int i, unsigned long pages);
u32 *rtk_reg_ptr(u32 addr);
long rtk_node_memory_ioctl(int kind, unsigned long arg);
ssize_t rtk_zero_read(struct file *file, char __user *buf, size_t n, loff_t *pos);
extern const struct file_operations rtk_auth_fops, rtk_rtkmem_fops, rtk_md_fops;

int rtk_rpc_rules_parse(void);
bool rtk_rpc_proc_add(struct proc_dir_entry *dir);
void rtk_rpc_cleanup(void);
extern const struct file_operations rtk_rpc_fops;

int rtk_vsc_setup(void);
int rtk_vsc_index(const char *name);
const char *rtk_vsc_counter_name(int idx); /* module-lifetime canonical label */
bool rtk_vsc_legacy_node(const char *name);
int rtk_vsc_open(struct file *file, const char *name, int idx);
int rtk_vsc_release(struct inode *inode, struct file *file);
long rtk_vsc_ioctl(struct file *file, unsigned int cmd, unsigned long arg);
long rtk_vsc_legacy_ioctl(unsigned int cmd, unsigned long arg);

int rtk_vt_setup(void);
bool rtk_vt_node(const char *name);
long rtk_vt_ioctl(unsigned int cmd, unsigned long arg);

void rtk_clock_prepare(void);
void rtk_clock_start(void);
void rtk_clock_stop(void);
int rtk_registers_preset(void);
int rtk_boards_add(void);
void rtk_boards_remove(void);
int rtk_gpio_add(void);
void rtk_gpio_remove(void);
int rtk_pwms_make(void);
void rtk_pwms_remove(void);
bool rtk_ucom_command(const char *name, unsigned int cmd);
long rtk_ucom_ioctl(unsigned int cmd, unsigned long arg);

int rtk_fb_setup(void);
void rtk_fb_remove(void);
int rtk_gal_gfx_add(void);
void rtk_gal_gfx_remove(void);
/* tee=1: the stand-in TAs (tee-ta.c) behind lgemu.ko's /dev/tee0, which they add */
int rtk_tee_add(struct proc_dir_entry *dir);
void rtk_tee_remove(void);

#endif
