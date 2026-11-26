/* SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors */
/* SPDX-License-Identifier: GPL-2.0-only */
/* The I2C devices of an LG TV's board, modelled once in lgemu.ko (i2c.c) for every SoC's bus
 * driver: LG's own (lg115x.ko, /dev/lg/iic<n>) and MStar's (mstar-utopia.ko, Utopia's HWI2C). A
 * bus module maps its (bus, slave) pairs to these devices from its own parameters (the chip's
 * table) and hands each transfer over as the I2C messages that go on the wire, as an I2C
 * adapter's master_xfer gets them: the register or command bytes are the start of the first
 * write, and a read follows with a repeated start. So a standard Linux adapter (i2c-dev's
 * /dev/i2c-N, as i2c-stub is one) can expose the same devices later without a change to them.
 */
#ifndef LGEMU_I2C_H
#define LGEMU_I2C_H
#include <linux/i2c.h>
#include <linux/types.h>

enum lgemu_i2c_device {
    LGEMU_I2C_NONE,
    LGEMU_I2C_MICOM,    /* the micom: power, the IR receiver */
    LGEMU_I2C_NVM,      /* the NVM EEPROM, 32 KiB, 2-byte big-endian address */
    LGEMU_I2C_AMP,      /* the speaker amplifier: writes acknowledged */
    LGEMU_I2C_ACCEL,    /* the accelerometer, a BMA422 (accel.c): the StanbyME's orientation */
    LGEMU_I2C_LIGHT1,   /* the ambient light sensor, libdile_i2c's LIGHT_SENSOR_1 (light.c) */
    LGEMU_I2C_LIGHT2,   /* the ambient light sensor, LIGHT_SENSOR_2 (light.c) */
    LGEMU_I2C_TOF,      /* the ToF motion sensor, a TMF8821: TMF882X_SENSOR_N (tof.c) */
    LGEMU_I2C_TUNER,    /* a Silicon Labs Si21xx tuner's command interface: idle, always ready */
    LGEMU_I2C_LEDMICOM, /* the OLED Flex's gaming LED micom: GAMING_LED_MICOM (ledmicom.c) */
    LGEMU_I2C_DLPC,     /* a projector's DLP controller, a TI DLPC6540: DPP6540 (dlpc.c) */
};

/* One transfer to dev: num messages (msgs[i].addr is not read: the caller chose dev by it), each
 * a write, or with I2C_M_RD a read, of len bytes at buf (kernel memory). num when the device
 * acknowledged, -EREMOTEIO when it did not (what a bus reports as a NACK), -EINVAL for no
 * device. May sleep. */
int lgemu_i2c_xfer(enum lgemu_i2c_device dev, struct i2c_msg *msgs, int num);
void lgemu_ir_set_delay(unsigned int first_ms, unsigned int repeat_ms);

/* One access as the SoC drivers' ioctls describe it, the data in user memory: a write is sub (the
 * register or command bytes, kernel memory) and size bytes from data in one message; a read writes
 * sub (when sub_len is not 0) and reads size bytes into data after a repeated start. addr is the
 * 7-bit slave. lgemu_i2c_xfer's result, -EFAULT or -ENOMEM; data is written only on success. */
int lgemu_i2c_user_xfer(enum lgemu_i2c_device dev, u16 addr, const u8 *sub, u16 sub_len,
                        void __user *data, u32 size, bool read);

/* A bus module's parameter "device:n1[:n2...]" (device: micom, nvm, amp, accel, light1, light2,
 * tof, tuner, ledmicom or dlpc):
 * the device and the count numbers after it (kstrtouint, base 0), or -EINVAL. */
int lgemu_i2c_parse(const char *spec, enum lgemu_i2c_device *dev, unsigned int *nums, int count);

/* accel.c: one transfer to the accelerometer, as lgemu_i2c_xfer's; 0 (it acknowledges all). */
int lgemu_accel_xfer(struct i2c_msg *msgs, int num);
/* light.c: one transfer to the light sensor of type 1 or 2, as lgemu_i2c_xfer's; 0. */
int lgemu_light_xfer(int type, struct i2c_msg *msgs, int num);
/* tof.c: one transfer to the ToF motion sensor, as lgemu_i2c_xfer's; 0. */
int lgemu_tof_xfer(struct i2c_msg *msgs, int num);
/* ledmicom.c: one transfer to the gaming LED micom, as lgemu_i2c_xfer's; 0. */
int lgemu_ledmicom_xfer(struct i2c_msg *msgs, int num);
/* dlpc.c: one transfer to the DLP controller, as lgemu_i2c_xfer's; 0. */
int lgemu_dlpc_xfer(struct i2c_msg *msgs, int num);

#endif
