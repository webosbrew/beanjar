// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* A software-only decoder (decoder_create's software_only, a VM's hwdec=none): the short codec
 * list over the caps a guest can send (caps.c, caps_software_only), and the lock against a
 * viewer's hwdec preference (decoder.c: an ACK with HM_PREFERENCE_LOCKED, the preference kept at
 * none). Prints TAP; exit status 0 only if every check passed. */
#include "../../../host/media/decode/decode-priv.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

static int n, failed;
static void ok(int cond, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    printf("%s %d - ", cond ? "ok" : "not ok", ++n);
    vprintf(fmt, ap);
    printf("\n");
    va_end(ap);
    failed += !cond;
}

static const char *const taken[] = {
    "video/x-h264, stream-format=(string)byte-stream",
    "video/x-h265, stream-format=(string)hvc1",
    "video/x-hevc",
    "video/x-vp8",
    "video/x-vp9",
    "video/x-av1, stream-format=(string)obu-stream",
    "video/mpeg, mpegversion=(int)1",
    "video/mpeg, mpegversion=(int)2",
    "video/mpeg, mpegversion=(int)4",
    "video/x-divx, divxversion=(int)5",
    "video/x-xvid",
    "audio/mpeg, mpegversion=(int)4",
    "audio/mpeg, mpegversion=(int)2",
    "audio/mpeg, mpegversion=(int)1, layer=(int)1",
    "audio/mpeg, mpegversion=(int)1, layer=(int)2",
    "audio/mpeg, mpegversion=(int)1, layer=(int)3",
    "audio/x-ac3",
    "audio/x-private1-ac3",
    "audio/x-eac3",
    "audio/x-opus",
    "audio/x-vorbis",
    "audio/x-flac",
    "audio/x-raw, format=(string)S16LE, layout=(string)interleaved",
    "audio/x-raw, format=(string)S16BE",
    "audio/x-raw, format=(string)U8",
    "audio/x-raw, format=(string)S8",
    "audio/x-raw, format=(string)S24LE",
    "audio/x-raw, format=(string)S24BE",
    "audio/x-raw, format=(string)S32LE",
    "audio/x-raw, format=(string)S32BE",
    "audio/x-raw, format=(string)F32LE",
    "audio/x-raw, format=(string)F32BE",
    "audio/x-raw, format=(string)F64LE",
    "audio/x-raw, format=(string)S24_32LE",
};

static const char *const refused[] = {
    "video/x-wmv, wmvversion=(int)1",
    "video/x-wmv, wmvversion=(int)2",
    "video/x-wmv, wmvversion=(int)3",
    "video/x-wmv, wmvversion=(int)3, format=(string)WVC1",
    "video/x-pn-realvideo, rmversion=(int)3",
    "video/x-pn-realvideo, rmversion=(int)4",
    "video/x-msmpeg, msmpegversion=(int)41",
    "video/x-msmpeg, msmpegversion=(int)42",
    "video/x-msmpeg, msmpegversion=(int)43",
    "video/x-flash-video",
    "video/x-h263",
    "image/jpeg",
    "audio/x-dts",
    "audio/x-private1-dts",
    "audio/AMR",
    "audio/AMR-WB",
    "audio/x-wma, wmaversion=(int)1",
    "audio/x-wma, wmaversion=(int)2",
    "audio/x-wma, wmaversion=(int)3",
    "audio/x-alaw",
    "audio/x-mulaw",
};

static int codec_of(const char *caps)
{
    struct codec_params p;
    caps_codec(caps, &p);
    av_free(p.extradata);
    return p.id;
}

/* The ACK a viewer gets for a HELLO with preference */
static struct host_media_message hello(struct decoder *d, int viewer, const char *preference)
{
    struct host_media_message m = {
        .magic = HOST_MEDIA_MAGIC, .version = HOST_MEDIA_VERSION, .type = HM_HELLO};
    snprintf(m.preference, sizeof m.preference, "%s", preference);
    hm_send(viewer, &m, NULL, 0);
    decoder_tick(d, 0);
    struct host_media_message ack = {0};
    int fds[HOST_MEDIA_OBJECTS];
    unsigned nfds = 0;
    for (;;) {
        if (hm_receive(viewer, &ack, fds, &nfds) != 1)
            break;
        hm_close_fds(fds, nfds);
        if (ack.type == HM_ACK)
            break;
    }
    ack.preference[sizeof ack.preference - 1] = 0;
    return ack;
}

int main(void)
{
    for (size_t i = 0; i < sizeof taken / sizeof *taken; i++) {
        int id = codec_of(taken[i]);
        ok(id != AV_CODEC_ID_NONE && caps_software_only(id), "taken: %s (%s)", taken[i],
           avcodec_get_name(id));
    }
    for (size_t i = 0; i < sizeof refused / sizeof *refused; i++) {
        int id = codec_of(refused[i]);
        ok(id != AV_CODEC_ID_NONE && !caps_software_only(id), "refused: %s (%s)", refused[i],
           avcodec_get_name(id));
    }
    ok(!caps_software_only(AV_CODEC_ID_NONE), "no codec is not taken");

    struct decoder *d = decoder_create(1, "vaapi", 1);
    ok(!d, "software only needs hwdec none");
    if (d)
        decoder_destroy(d);
    d = decoder_create(1, "none", 1);
    ok(d != NULL, "software only with hwdec none");
    if (!d)
        return 1;
    int sv[2];
    if (socketpair(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0, sv) < 0)
        return 1;
    decoder_attach(d, 0, sv[0]);
    static const char *const others[] = {"auto", "vaapi", "cuda", "vulkan", "no-such-device"};
    for (size_t i = 0; i < sizeof others / sizeof *others; i++) {
        struct host_media_message ack = hello(d, sv[1], others[i]);
        ok(ack.type == HM_ACK && ack.status == HM_PREFERENCE_LOCKED &&
               !strcmp(ack.preference, "none"),
           "locked: a viewer's %s is refused (status %u, kept %s)", others[i], ack.status,
           ack.preference);
    }
    struct host_media_message ack = hello(d, sv[1], "none");
    ok(ack.status == HM_PREFERENCE_APPLIED && !strcmp(ack.preference, "none"),
       "locked: a viewer's none is applied (status %u)", ack.status);
    ack = hello(d, sv[1], "");
    ok(ack.status == HM_PREFERENCE_APPLIED && !strcmp(ack.preference, "none"),
       "locked: no preference keeps none (status %u)", ack.status);
    decoder_detach(d, 0);
    decoder_destroy(d);
    close(sv[0]);
    close(sv[1]);

    /* not locked: a viewer's device type is taken, as before */
    d = decoder_create(1, "none", 0);
    if (!d || socketpair(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0, sv) < 0)
        return 1;
    decoder_attach(d, 0, sv[0]);
    ack = hello(d, sv[1], "auto");
    ok(ack.status == HM_PREFERENCE_APPLIED && !strcmp(ack.preference, "auto"),
       "not locked: a viewer's auto is applied (status %u, %s)", ack.status, ack.preference);
    decoder_detach(d, 0);
    decoder_destroy(d);
    close(sv[0]);
    close(sv[1]);

    printf("1..%d\n", n);
    return failed ? 1 : 0;
}
