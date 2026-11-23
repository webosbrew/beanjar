// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* What kadaptor-xc.c keeps of the window calls for kadaptor-capture.c: the last XC_SETWIN_INFO
 * per window (setwin_len[window] bytes; 0 before the first SetWindow), under setwin_lock. */
#ifndef KADAPTOR_XC_H
#define KADAPTOR_XC_H
#include <pthread.h>
#include <stdint.h>

#define SETWIN_MAX 128
extern pthread_mutex_t setwin_lock __attribute__((visibility("hidden")));
extern uint8_t setwin[2][SETWIN_MAX] __attribute__((visibility("hidden")));
extern uint32_t setwin_len[2] __attribute__((visibility("hidden")));

#endif
