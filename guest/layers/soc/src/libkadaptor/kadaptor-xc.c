// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* libkadaptor for the emulator on M2 (W16R) and LM15U (W15U): the XC scaler's window calls, also to
 * the kernel.
 *
 * The viewer draws the media at the scaler's windows, which on MStar chips mstar-utopia.ko decodes
 * from the kernel-mode Utopia XC commands that carry them (guest/kernel/modules/mstar-utopia,
 * "XC's windows"). W18R's KADP issues those in kernel mode. W16R's and W15U's do not: their
 * MApi_XC_Init (W16R's libkadaptor 0x3e7858, W15U's 0x389980) opens two XC instances, a
 * kernel-mode one (UtopiaOpen(0x80000008)) that only Init and SkipWaitVsync use, and a user-mode
 * one (UtopiaOpen(8)), which every window call uses (W16R's MApi_XC_SetWindow 0x3f0a40,
 * GenerateBlackVideo 0x3e8bec, ...; W15U's 0x392760, 0x38ad98, ...): Utopia's XC driver runs
 * inside the calling process and programs the scaler's registers itself (MLoad queues). So here
 * the window calls KADP makes (through its PLT, from mbrg_outputsource_*; libmsil's too) run the
 * stock function, then pass the same command with the same arguments to a kernel-mode XC
 * instance of their own, as W18R's KADP would, and the one decoder in mstar-utopia.ko reports
 * these windows as it reports M3's. The command numbers are the release's Utopia numbering, read
 * from the kernel module's xc_window parameter (the chip's soc table: W16R's EnableMainWindow,
 * DisableSubWindow and EnableWindow are 245, 247 and 193, W15U's 239, 241 and 190).
 *
 * With Dynamic Scaling on (adaptive streams: MSE), W16R's HAL moves the main window with KADP's
 * mbrg_outputsource_scaler_SetDSDispWindow(source, x, y, w, h) instead, which queues the window
 * in DS command memory for the scaler to apply frame by frame and issues no window call: that
 * one is passed on as the main window's SetWindow, the last one's capture and crop with the new
 * source and display window. W15U's KADP has no such function.
 *
 * W16R's tv.capture answers are in kadaptor-capture.c, which uses the last SetWindow kept here
 * (kadaptor-xc.h). Every other function is the stock one: this library takes the name its users
 * NEED (W16R's libkadaptor.so, no SONAME; W15U's libkadaptor.so.1, with libkadaptor.so linked to
 * it) and NEEDs the stock file (W16R's libkadaptor.so.0.1.0, W15U's libkadaptor.so.1.0.0). */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <pthread.h>
#include <stdint.h>
#include <string.h>
#include <syslog.h>

#include "kadaptor-xc.h"

#define XC_MODULE_KERNEL 0x80000008u /* UtopiaOpen: MODULE_XC (8) in kernel mode */
#define XC_WINDOW_PARAM  "/sys/module/mstar_utopia/parameters/xc_window"
/* xc_window's order: SetWindow, GenerateBlackVideo, ..ForBothWin, EnableMainWindow,
 * DisableSubWindow, EnableWindow */
enum {
    CMD_SET_WINDOW,
    CMD_BLACK,
    CMD_BLACK_BOTH,
    CMD_ENABLE_MAIN,
    CMD_DISABLE_SUB,
    CMD_ENABLE_WINDOW,
    N_CMDS
};
static uint32_t cmds[N_CMDS];

static void *stock(const char *name)
{
    void *f = dlsym(RTLD_NEXT, name);
    if (!f)
        syslog(LOG_ERR, "libkadaptor (emulator): stock %s not found", name);
    return f;
}

static pthread_once_t once = PTHREAD_ONCE_INIT;
static uint32_t (*utopia_ioctl)(void *instance, uint32_t cmd, void *arg);
static void *kernel_xc;

static void open_kernel_xc(void)
{
    uint32_t (*utopia_open)(uint32_t module, void **instance, uint32_t version, void *attribute) =
        stock("UtopiaOpen");
    char line[128] = "";
    FILE *f = fopen(XC_WINDOW_PARAM, "re");
    unsigned n = 0;
    if (f) {
        if (fgets(line, sizeof line, f)) {
            char *p = line, *end;
            while (n < N_CMDS) {
                unsigned long v = strtoul(p, &end, 0);
                if (end == p)
                    break;
                cmds[n++] = (uint32_t)v;
                p = *end == ',' ? end + 1 : end;
            }
        }
        fclose(f);
    }
    utopia_ioctl = stock("UtopiaIoctl");
    if (n < N_CMDS || !utopia_open || !utopia_ioctl ||
        utopia_open(XC_MODULE_KERNEL, &kernel_xc, 0, NULL) != 0) {
        kernel_xc = NULL;
        syslog(LOG_WARNING,
               "libkadaptor (emulator): no kernel-mode XC instance or no xc_window (%u of %u "
               "commands): windows not reported",
               n, N_CMDS);
    }
}

/* The command to the kernel, after the stock call (its answer is the stock one). */
static void to_kernel(unsigned cmd, void *arg)
{
    pthread_once(&once, open_kernel_xc);
    if (kernel_xc && cmds[cmd])
        utopia_ioctl(kernel_xc, cmds[cmd], arg);
}

/* The last XC_SETWIN_INFO per window (124 bytes on W16R and W15U), for the DS window moves and
 * kadaptor-capture.c */
pthread_mutex_t setwin_lock = PTHREAD_MUTEX_INITIALIZER;
uint8_t setwin[2][SETWIN_MAX];
uint32_t setwin_len[2];

/* MS_BOOL MApi_XC_SetWindow(XC_SETWIN_INFO *, MS_U32 u32InitDataLen, SCALER_WIN) */
uint8_t MApi_XC_SetWindow(void *info, uint32_t len, uint32_t window)
{
    static uint8_t (*f)(void *, uint32_t, uint32_t);
    struct {
        uint32_t info, len, window;
        uint8_t ret, pad[3];
    } a;
    uint8_t ret;
    if (!f)
        f = stock(__func__);
    ret = f ? f(info, len, window) : 0;
    memset(&a, 0, sizeof a);
    a.info = (uint32_t)(uintptr_t)info;
    a.len = len;
    a.window = window;
    to_kernel(CMD_SET_WINDOW, &a);
    if (info && window < 2 && len >= 20 && len <= SETWIN_MAX) {
        pthread_mutex_lock(&setwin_lock);
        memcpy(setwin[window], info, len);
        setwin_len[window] = len;
        pthread_mutex_unlock(&setwin_lock);
    }
    return ret;
}

/* MS_BOOL mbrg_outputsource_scaler_SetDSDispWindow(INPUT_SOURCE_TYPE_t, MS_U16 x, y, w, h) */
uint32_t mbrg_outputsource_scaler_SetDSDispWindow(uint32_t source, uint32_t x, uint32_t y,
                                                  uint32_t w, uint32_t h)
{
    static uint32_t (*f)(uint32_t, uint32_t, uint32_t, uint32_t, uint32_t);
    uint8_t info[SETWIN_MAX];
    struct {
        uint32_t info, len, window;
        uint8_t ret, pad[3];
    } a;
    uint16_t disp[4] = {(uint16_t)x, (uint16_t)y, (uint16_t)w, (uint16_t)h};
    uint32_t ret, len;
    if (!f)
        f = stock(__func__);
    ret = f ? f(source, x, y, w, h) : 0;
    pthread_mutex_lock(&setwin_lock);
    len = setwin_len[0];
    memcpy(info, setwin[0], len);
    pthread_mutex_unlock(&setwin_lock);
    if (!len)
        return ret;
    memcpy(info, &source, 4);   /* enInputSourceType */
    memcpy(info + 12, disp, 8); /* stDispWin */
    memset(&a, 0, sizeof a);
    a.info = (uint32_t)(uintptr_t)info;
    a.len = len;
    to_kernel(CMD_SET_WINDOW, &a);
    return ret;
}

/* E_APIXC_ReturnValue MApi_XC_GenerateBlackVideo(MS_BOOL, SCALER_WIN) */
uint32_t MApi_XC_GenerateBlackVideo(uint8_t enable, uint32_t window)
{
    static uint32_t (*f)(uint8_t, uint32_t);
    struct {
        uint8_t enable, pad[3];
        uint32_t window, ret;
    } a = {enable, {0}, window, 0};
    uint32_t ret;
    if (!f)
        f = stock(__func__);
    ret = f ? f(enable, window) : 0;
    to_kernel(CMD_BLACK, &a);
    return ret;
}

/* E_APIXC_ReturnValue MApi_XC_GenerateBlackVideoForBothWin(MS_BOOL) */
uint32_t MApi_XC_GenerateBlackVideoForBothWin(uint8_t enable)
{
    static uint32_t (*f)(uint8_t);
    struct {
        uint8_t enable, pad[3];
        uint32_t ret;
    } a = {enable, {0}, 0};
    uint32_t ret;
    if (!f)
        f = stock(__func__);
    ret = f ? f(enable) : 0;
    to_kernel(CMD_BLACK_BOTH, &a);
    return ret;
}

/* void MApi_XC_EnableMainWindow(MS_BOOL) */
void MApi_XC_EnableMainWindow(uint8_t enable)
{
    static void (*f)(uint8_t);
    struct {
        uint8_t enable, pad[3];
    } a = {enable, {0}};
    if (!f)
        f = stock(__func__);
    if (f)
        f(enable);
    to_kernel(CMD_ENABLE_MAIN, &a);
}

/* void MApi_XC_DisableSubWindow(void) */
void MApi_XC_DisableSubWindow(void)
{
    static void (*f)(void);
    uint32_t a = 0;
    if (!f)
        f = stock(__func__);
    if (f)
        f();
    to_kernel(CMD_DISABLE_SUB, &a);
}

/* void MApi_XC_EnableWindow(MS_BOOL, SCALER_WIN) */
void MApi_XC_EnableWindow(uint8_t enable, uint32_t window)
{
    static void (*f)(uint8_t, uint32_t);
    struct {
        uint8_t enable, pad[3];
        uint32_t window;
    } a = {enable, {0}, window};
    if (!f)
        f = stock(__func__);
    if (f)
        f(enable, window);
    to_kernel(CMD_ENABLE_WINDOW, &a);
}
