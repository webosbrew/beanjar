// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
#ifndef HOST_MEDIA_DECODE_H
#define HOST_MEDIA_DECODE_H
#include "ipc.h"
struct decoder;
/* hwdec: the streams' first preference (auto, none or a device type); software_only (hwdec none
 * only): locked to software, refusing a viewer's other preference (HM_PREFERENCE_LOCKED), and
 * decoding only the codecs caps_software_only allows. */
struct decoder *decoder_create(unsigned ports, const char *hwdec, int software_only);
/* Nonblocking ingress. -1 means overload: caller must reconnect guest and start
 * a new session, not continue after dropping a compressed reference packet. */
int decoder_submit(struct decoder *, unsigned port, const struct webos_media_header *, const void *,
                   int64_t received);
void decoder_disconnect(struct decoder *, unsigned port);
void decoder_attach(struct decoder *, unsigned port, int fd);
void decoder_detach(struct decoder *, unsigned port);
/* Receives viewer preferences/releases and retires due frames even with no
 * viewer. Returns -1 when caller must close the viewer connection. */
int decoder_tick(struct decoder *, unsigned port);
void decoder_destroy(struct decoder *);
/* Messages for the guest (PICTURE answers of a DECODE session, include/webos-media.h): fn takes
 * each whole message (malloc'd, len bytes) and frees it; called from the decoder's threads, in
 * order per port. Set once, before the first submit. */
typedef void decoder_reply_fn(void *ctx, unsigned port, unsigned char *message, size_t len);
void decoder_set_reply(struct decoder *, decoder_reply_fn *fn, void *ctx);
/* The pool (include/webos-media.h, "The pool"), and how to ring the guest (any thread): DECODE
 * sessions that ask for shared pictures get them there. Without it pictures come inline. Set
 * once, before the first submit. */
struct pool;
void decoder_set_pool(struct decoder *, struct pool *, int (*ring_guest)(void *ctx), void *ctx);
/* The guest rang the host: releases taken, ended claims retired, free extents reaped. */
void decoder_doorbell(struct decoder *);
#endif
