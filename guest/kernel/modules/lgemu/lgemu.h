/* SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors */
/* SPDX-License-Identifier: GPL-2.0-only */
/* The parts of lgemu.ko (core.c loads them in order, unloads them in reverse). */
#ifndef LGEMU_H
#define LGEMU_H

int lgemu_partinfo_init(void);
void lgemu_partinfo_exit(void);
int lgemu_low_mem_notify_init(void);
void lgemu_low_mem_notify_exit(void);
int lgemu_i2c_init(void);
void lgemu_i2c_exit(void);
int lgemu_lgib_init(void);
void lgemu_lgib_exit(void);
int lgemu_sestore_init(void);
void lgemu_sestore_exit(void);
int lgemu_gfx_init(void);
void lgemu_gfx_exit(void);
int lgemu_snd_init(void);
void lgemu_snd_exit(void);
int lgemu_capture_init(void);
void lgemu_capture_exit(void);
/* video60's histogram controls (capture-histogram.c), for v4l2-capture.c: started is the open
 * file's own flag; stop also undoes a file's start when it is closed. */
#define LGEMU_HIST_START  0x00982a14
#define LGEMU_HIST_PRESET 0x00982a15
#define LGEMU_HIST_DATA   0x00982a16
#define LGEMU_HIST_STOP   0x00982a17
int lgemu_capture_histogram(bool *started, bool get, u32 id, u32 size, u32 value);
void lgemu_capture_histogram_stop(bool *started);
void lgemu_capture_histogram_exit(void);
int lgemu_mic_init(void);
void lgemu_mic_exit(void);
bool lgemu_mic_present(void);
int lgemu_video_init(void);
void lgemu_video_exit(void);
int lgemu_vsc_init(void);
void lgemu_vsc_exit(void);
int lgemu_hdmi_init(void);
void lgemu_hdmi_exit(void);
int lgemu_thermal_init(void);
void lgemu_thermal_exit(void);
#if IS_ENABLED(CONFIG_I2C)
int lgemu_i2c_adapters_init(void);
void lgemu_i2c_adapters_exit(void);
#else
static inline int lgemu_i2c_adapters_init(void)
{
    return 0;
}
static inline void lgemu_i2c_adapters_exit(void)
{
}
#endif
#if IS_ENABLED(CONFIG_CEC_CORE)
int lgemu_cec_init(void);
void lgemu_cec_exit(void);
#else
static inline int lgemu_cec_init(void)
{
    return 0;
}
static inline void lgemu_cec_exit(void)
{
}
#endif

#endif
