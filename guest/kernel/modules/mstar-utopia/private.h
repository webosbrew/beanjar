// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
#ifndef MSTAR_UTOPIA_PRIVATE_H
#define MSTAR_UTOPIA_PRIVATE_H

#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt
#include <linux/compat.h>
#include <linux/crc32.h>
#include <linux/delay.h>
#include <linux/hrtimer.h>
#include <linux/device.h>
#include <linux/fs.h>
#include <linux/mm.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/proc_fs.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <linux/unaligned.h>
#include <linux/semaphore.h>
#include <linux/seq_file.h>
#include <linux/string.h>
#include <linux/sysfs.h>
#include <linux/wait.h>
#include <linux/vmalloc.h>
#include <linux/workqueue.h>
#include "lgemu-drv.h"
#include "lgemu-i2c.h"
#include "../../../../include/mstar-gop.h"
#include "../../../../include/webos-video.h"

/* Module-internal contracts. Each subsystem owns its parameters and mutable state. */
extern bool lm14;
extern bool direct_ir;
int plain_open(struct inode *inode, struct file *file);
long system_lg_ioctl(struct file *file, unsigned int cmd, unsigned long arg);
void ioctl_count(const char *dev, unsigned int cmd);
long unhandled(const char *dev, unsigned int cmd, long ret);
void diagnostics_init(void);
struct lgemu_trace *diagnostics_utopia_trace(void);
bool diagnostics_proc_create(struct proc_dir_entry *dir);

int mmapger_layout(void);
u64 record_pa(u32 id);
u32 memory_record_count(void);
u32 memory_record_length(u32 id);
int memory_gop_id(void);
bool memory_has_tee(void);
struct lgemu_store *memory_store(void);
void memory_store_free(void);
void memory_layout_free(void);
void memory_log_layout(void);

int tsp_registers(void);
int adc_idac_registers(void);
int lm14_mmio_registers(void);
int registers_preset(void);
struct lgemu_store *mmio_store(void);
bool mmio_has_tsp(void);
bool mmio_has_pq(void);
void mmio_work_init(void);
void mmio_work_cancel(void);
void mmio_free(void);

void semutex_init(void);
void semutex_new_init(void);
long semutex_new_ioctl(unsigned int cmd, void __user *uarg);
enum { SEM_OP_WAIT = 32, SEM_OP_LOCK, SEM_OP_TRY, SEM_OP_TIMEOUT, SEM_OP_UNLOCK, SEM_OP_RELEASE };
void mailbox_sem_event(u8 op, u32 index, int result, s32 aux, pid_t tgid, pid_t pid,
                       const char *name);
void mailbox_init(void);
bool mailbox_enabled(void);
int mailbox_proc_create(struct proc_dir_entry *dir);
void mailbox_free(void);
void gop_exit(void);

#define UTOPIA_STATUS_SUCCESS          0
#define UTOPIA_STATUS_FAIL             1
#define UTOPIA_MODULE_HWI2C            31
#define MDRV_CMD_HWI2C_WRITEBYTES_CUST 9
#define MDRV_CMD_HWI2C_READBYTES_CUST  12
int hwi2c_parse(void);
long hwi2c_customer(u32 cmd, u32 uarg);
int lm14_hwi2c_access(u64 addr, unsigned int width, bool write, u32 *value);
bool hwi2c_enabled(void);
int swi2c_parse(void);
bool swi2c_enabled(void);
u64 swi2c_page(void);
void swi2c_reset_pads(u8 *page);
int swi2c_access(u64 addr, unsigned int width, bool write, u32 *value);
int gpio_parse(void);
bool gpio_enabled(void);
long gpio_ioctl(u32 cmd, u32 uarg);
int audio_info_parse(void);
int adv_fn009_parse(void);
bool utopia_has_pmlog(void);
bool utopia_has_xc_riu(void);
void xc_riu_tick(void);
void utopia_counts_init(void);
void utopia_screen_init(void);
extern const struct file_operations system_fops;
extern const struct file_operations mpool_fops;
extern const struct file_operations miomap_fops;
extern const struct file_operations semutex_fops;
extern const struct file_operations mbx_fops;
extern const struct file_operations gop_fops;
extern const struct file_operations system_lg_fops;
extern const struct file_operations ir_fops;
extern const struct proc_ops info_proc_ops;
extern const struct proc_ops mmap_proc_ops;
extern const struct proc_ops mmap_ini_proc_ops;
extern const struct proc_ops tsp_state_proc_ops;
extern const struct proc_ops pq_y_state_proc_ops;
extern const struct proc_ops hwi2c_state_proc_ops;
extern const struct proc_ops swi2c_state_proc_ops;
extern const struct proc_ops utopia_proc_ops;
extern const struct proc_ops pmlog_proc_ops;

#endif
