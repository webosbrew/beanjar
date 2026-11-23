// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
#include "private.h"

/* ---- GPIO (Utopia module 42) in kernel mode: the pads and the software I2C buses on them ----
 *
 * W15U's KADP (libkadaptor.so.1) drives MStar's GPIO pads through the kernel-mode GPIO module
 * (kdrv_lm15u.ko's GPIO_adp_Ioctl): commands 1 set_high, 2 set_low, 3 set_input (the pad released)
 * and 5 get_level, each with an 8-byte {u32 pad, u32 result} (mdrv_gpio_get_level returns the
 * result word). Its software I2C (MApi_SWI2C) bit-bangs the start condition in user space with
 * those: SCL and SDA released, both read back high (each polled up to 3600 times, a stretched
 * clock), then SDA low and SCL low. The bytes go to the kernel: command 7 send_bus_data and 8
 * get_bus_data, {u16 len, u32 SCL pad at +4, u32 SDA pad at +8, u32 delay, u8 flag at +0x10,
 * u32 data at +0x14 (a user pointer), u16 at +0x18, u32 result at +0x1c (a user pointer to a byte
 * the caller sets to 1, the kernel's 0 a NACK)}, and command 9 the stop.
 *
 * Answering every read 0 made each start fail after 3 x 7200 polls: the amplifier's init
 * (hundreds of register writes on channel 4's bus) took most of an hour of CPU. Here a pad reads
 * what it was last driven to, and a released one its board level: high on the pads of the
 * devices' buses (gpio_i2c: their pull-ups) and where gpio_pad_level says so (the other buses'
 * pull-ups, inputs a board holds high), else low (the answer every pad had before). This is not
 * main.c's gpio_level, the lines of the gpiochip LM21's kernel GPIO exports. The buses' devices
 * (gpio_i2c: device:scl:sda:slave, the 8-bit slave) are lgemu.ko's; a byte to a slave not listed
 * is not acknowledged, as a board without that chip. Unlike swi2c.c (LM14's bus, bit-banged
 * through the pad registers), the bytes arrive whole here. A start is SDA driven low while SCL
 * is released: the next byte sent is the address. Written bytes are kept until the stop or the
 * next address, then passed to the device as one write; a read of the same device after a
 * repeated start passes them as its register address. */

#define GPIO_SET_HIGH  1
#define GPIO_SET_LOW   2
#define GPIO_SET_INPUT 3
#define GPIO_GET_LEVEL 5
#define GPIO_BUS_SEND  7
#define GPIO_BUS_GET   8
#define GPIO_BUS_STOP  9
#define GPIO_PADS      512
#define GPIO_I2C_MAX   4
#define GPIO_I2C_BUF   256

static bool utopia_gpio;
module_param(utopia_gpio, bool, 0444);
MODULE_PARM_DESC(utopia_gpio, "kernel-mode GPIO (Utopia module 42): pad levels and the software "
                              "I2C buses on them (W15U)");
static char *gpio_i2c[GPIO_I2C_MAX];
static int gpio_i2c_count;
module_param_array(gpio_i2c, charp, &gpio_i2c_count, 0444);
MODULE_PARM_DESC(gpio_i2c, "the devices on software I2C buses, device:scl_pad:sda_pad:slave (the "
                           "8-bit slave)");
static char *gpio_pad_level[16];
static int gpio_pad_level_count;
module_param_array(gpio_pad_level, charp, &gpio_pad_level_count, 0444);
MODULE_PARM_DESC(gpio_pad_level, "released pads whose board level is not 0, pad:level");

enum pad_state { PAD_RELEASED, PAD_LOW, PAD_HIGH };

static DEFINE_MUTEX(gpio_lock);
static u8 pad_state[GPIO_PADS];
static u8 pad_board[GPIO_PADS]; /* a released pad's level */

static struct gpio_i2c_dev {
    enum lgemu_i2c_device dev;
    u32 scl, sda, slave;
} devs[GPIO_I2C_MAX];

/* one bus's transaction, by its SDA pad */
static struct gpio_bus {
    u32 scl, sda;
    bool addressed;       /* the address byte was sent since the last start */
    int dev;              /* the device addressed, -1 none */
    bool read;            /* the address's R/W bit */
    u8 buf[GPIO_I2C_BUF]; /* written bytes not passed on yet */
    u32 len;
} buses[GPIO_I2C_MAX];
static int bus_count;

int gpio_parse(void)
{
    for (int i = 0; i < gpio_pad_level_count; i++) {
        u32 v[2];

        if (lgemu_parse_u32s(gpio_pad_level[i], 0, v, 2) || v[0] >= GPIO_PADS)
            return -EINVAL;
        pad_board[v[0]] = !!v[1];
    }
    for (int i = 0; i < gpio_i2c_count; i++) {
        unsigned int n[3];
        int b;

        if (lgemu_i2c_parse(gpio_i2c[i], &devs[i].dev, n, 3) || n[0] >= GPIO_PADS ||
            n[1] >= GPIO_PADS || n[2] > 0xff)
            return -EINVAL;
        devs[i].scl = n[0];
        devs[i].sda = n[1];
        devs[i].slave = n[2] & 0xfe;
        pad_board[n[0]] = pad_board[n[1]] = 1; /* the bus's pull-ups */
        for (b = 0; b < bus_count; b++)
            if (buses[b].sda == n[1])
                break;
        if (b == bus_count)
            buses[bus_count++] = (struct gpio_bus){.scl = n[0], .sda = n[1], .dev = -1};
    }
    return 0;
}

bool gpio_enabled(void)
{
    return utopia_gpio;
}

static struct gpio_bus *bus_of_sda(u32 sda)
{
    for (int i = 0; i < bus_count; i++)
        if (buses[i].sda == sda)
            return &buses[i];
    return NULL;
}

static int level(u32 pad)
{
    if (pad >= GPIO_PADS)
        return 0;
    return pad_state[pad] == PAD_RELEASED ? pad_board[pad] : pad_state[pad] == PAD_HIGH;
}

/* the written bytes to the device as one write (caller holds gpio_lock) */
static void bus_flush(struct gpio_bus *b)
{
    if (b->dev >= 0 && !b->read && b->len) {
        struct i2c_msg msg = {.addr = devs[b->dev].slave >> 1, .len = b->len, .buf = b->buf};

        lgemu_i2c_xfer(devs[b->dev].dev, &msg, 1);
    }
    b->len = 0;
}

/* a start: the next byte is an address; the bytes written so far wait for it (a read of the
 * same device after a repeated start takes them as its register address) */
static void bus_start(struct gpio_bus *b)
{
    b->addressed = false;
}

static void set_pad(u32 pad, enum pad_state s)
{
    struct gpio_bus *b;

    if (pad >= GPIO_PADS)
        return;
    b = bus_of_sda(pad);
    /* SDA falling while SCL is high: a start */
    if (b && s == PAD_LOW && level(pad) && level(b->scl))
        bus_start(b);
    pad_state[pad] = s;
}

struct gpio_bus_arg32 {
    u16 len, pad0;
    u32 scl, sda, delay;
    u8 flag, pad1[3];
    u32 data;
    u16 x, pad2;
    u32 result;
};
static_assert(sizeof(struct gpio_bus_arg32) == 0x20);

static long bus_ioctl(u32 cmd, u32 uarg)
{
    struct gpio_bus_arg32 a;
    struct gpio_bus *b;
    u8 ack = 1, data[GPIO_I2C_BUF];
    u32 n, i = 0;

    if (copy_from_user(&a, compat_ptr(uarg), sizeof(a)))
        return -EFAULT;
    b = bus_of_sda(a.sda);
    if (!b) { /* a bus with no device of ours: nothing acknowledges, a read gives 0xff */
        if (cmd == GPIO_BUS_GET) {
            n = min_t(u32, a.len, sizeof(data));
            memset(data, 0xff, n);
            if (copy_to_user(compat_ptr(a.data), data, n))
                return -EFAULT;
        }
        if (cmd != GPIO_BUS_STOP && a.result && put_user(0, (u8 __user *)compat_ptr(a.result)))
            return -EFAULT;
        return UTOPIA_STATUS_SUCCESS;
    }
    n = min_t(u32, a.len, sizeof(data));
    if (cmd == GPIO_BUS_STOP) {
        bus_flush(b);
        b->addressed = false;
        b->dev = -1;
        return UTOPIA_STATUS_SUCCESS;
    }
    if (cmd == GPIO_BUS_SEND) {
        if (copy_from_user(data, compat_ptr(a.data), n))
            return -EFAULT;
        if (!b->addressed && n) {
            int dev = -1;

            for (int d = 0; d < gpio_i2c_count; d++)
                if (devs[d].sda == b->sda && devs[d].slave == (data[0] & 0xfe))
                    dev = d;
            if (!((data[0] & 1) && dev == b->dev))
                bus_flush(b);
            b->addressed = true;
            b->read = data[0] & 1;
            b->dev = dev;
            i = 1;
        }
        if (b->dev < 0)
            ack = 0;
        for (; ack && i < n; i++)
            if (b->len < sizeof(b->buf))
                b->buf[b->len++] = data[i];
    } else if (cmd == GPIO_BUS_GET) {
        struct i2c_msg msgs[2] = {
            {.addr = 0, .len = b->len, .buf = b->buf},
            {.addr = 0, .flags = I2C_M_RD, .len = n, .buf = data},
        };
        int first = b->len ? 0 : 1;

        memset(data, 0xff, n);
        if (b->dev < 0 || !b->read)
            ack = 0;
        else {
            msgs[0].addr = msgs[1].addr = devs[b->dev].slave >> 1;
            if (lgemu_i2c_xfer(devs[b->dev].dev, msgs + first, 2 - first) < 0)
                ack = 0;
        }
        b->len = 0;
        if (copy_to_user(compat_ptr(a.data), data, n))
            return -EFAULT;
    } else {
        return UTOPIA_STATUS_SUCCESS;
    }
    if (!ack && a.result && put_user(ack, (u8 __user *)compat_ptr(a.result)))
        return -EFAULT;
    return UTOPIA_STATUS_SUCCESS;
}

/* Caller: utopia.c's kernel-mode module dispatch, for module 42 when utopia_gpio is set */
long gpio_ioctl(u32 cmd, u32 uarg)
{
    u32 a[2];
    long ret = UTOPIA_STATUS_SUCCESS;

    mutex_lock(&gpio_lock);
    switch (cmd) {
    case GPIO_SET_HIGH:
    case GPIO_SET_LOW:
    case GPIO_SET_INPUT:
    case GPIO_GET_LEVEL:
        if (copy_from_user(a, compat_ptr(uarg), sizeof(a))) {
            ret = -EFAULT;
            break;
        }
        if (cmd == GPIO_GET_LEVEL) {
            u32 v = level(a[0]);

            if (put_user(v, (u32 __user *)compat_ptr(uarg + 4)))
                ret = -EFAULT;
        } else {
            set_pad(a[0], cmd == GPIO_SET_LOW    ? PAD_LOW
                          : cmd == GPIO_SET_HIGH ? PAD_HIGH
                                                 : PAD_RELEASED);
        }
        break;
    case GPIO_BUS_SEND:
    case GPIO_BUS_GET:
    case GPIO_BUS_STOP:
        ret = bus_ioctl(cmd, uarg);
        break;
    }
    mutex_unlock(&gpio_lock);
    return ret;
}
