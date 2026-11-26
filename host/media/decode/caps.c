// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* GstCaps strings to codec parameters. */
#include "decode-priv.h"

#include <ctype.h>
#include <libavcodec/avcodec.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

/* ---- GstCaps strings ---- */

static void caps_clear(struct caps *c)
{
    for (unsigned i = 0; i < c->n; i++) {
        free(c->key[i]);
        free(c->type[i]);
        free(c->value[i]);
    }
    c->n = 0;
}

/* The first structure of a caps string: "name, key=(type)value, ...". */
static void caps_parse(struct caps *c, const char *s)
{
    memset(c, 0, sizeof *c);
    size_t n = strcspn(s, ",;");
    snprintf(c->name, sizeof c->name, "%.*s", (int)n, s);
    s += n;
    while (*s == ',' && c->n < 48) {
        s++;
        while (*s == ' ')
            s++;
        const char *eq = strchr(s, '=');
        if (!eq)
            break;
        const char *k = s, *ke = eq;
        while (ke > k && ke[-1] == ' ')
            ke--;
        s = eq + 1;
        while (*s == ' ')
            s++;
        const char *t = "", *te = t;
        if (*s == '(') {
            t = s + 1;
            te = strchr(t, ')');
            if (!te)
                break;
            s = te + 1;
        }
        const char *v = s;
        int depth = 0, quoted = 0;
        for (; *s; s++) {
            if (quoted) {
                if (*s == '\\' && s[1])
                    s++;
                else if (*s == '"')
                    quoted = 0;
                continue;
            }
            if (*s == '"')
                quoted = 1;
            else if (*s == '<' || *s == '{' || *s == '[')
                depth++;
            else if (*s == '>' || *s == '}' || *s == ']')
                depth--;
            else if (!depth && (*s == ',' || *s == ';'))
                break;
        }
        const char *ve = s;
        while (ve > v && ve[-1] == ' ')
            ve--;
        if (*v == '"' && ve > v + 1 && ve[-1] == '"') {
            v++;
            ve--;
        }
        c->key[c->n] = strndup(k, (size_t)(ke - k));
        c->type[c->n] = strndup(t, (size_t)(te - t));
        c->value[c->n] = strndup(v, (size_t)(ve - v));
        c->n++;
        if (*s == ';')
            break;
    }
}

static const char *caps_get(const struct caps *c, const char *key)
{
    for (unsigned i = 0; i < c->n; i++)
        if (!strcmp(c->key[i], key))
            return c->value[i];
    return NULL;
}

static int caps_int(const struct caps *c, const char *key, int def)
{
    const char *v = caps_get(c, key);
    return v ? atoi(v) : def;
}

static int caps_is(const struct caps *c, const char *key, const char *value)
{
    const char *v = caps_get(c, key);
    return v && !strcmp(v, value);
}

/* Hex digits to bytes, skipping anything else; returns the count. */
static size_t unhex(const char *s, const char *end, uint8_t *out)
{
    size_t n = 0;
    int hi = -1;
    for (; s < end && *s; s++) {
        int d = *s >= '0' && *s <= '9'   ? *s - '0'
                : *s >= 'a' && *s <= 'f' ? *s - 'a' + 10
                : *s >= 'A' && *s <= 'F' ? *s - 'A' + 10
                                         : -1;
        if (d < 0) {
            if (*s == ',')
                hi = -1;
            continue;
        }
        if (hi < 0)
            hi = d;
        else {
            if (out)
                out[n] = (uint8_t)(hi << 4 | d);
            n++;
            hi = -1;
        }
    }
    return n;
}

static uint8_t *extradata(const uint8_t *data, size_t size, int *out_size)
{
    uint8_t *e = av_mallocz(size + AV_INPUT_BUFFER_PADDING_SIZE);
    if (e && size)
        memcpy(e, data, size);
    *out_size = (int)size;
    return e;
}

/* codec_data=(buffer)hex as extradata */
static uint8_t *caps_buffer(const struct caps *c, const char *key, int *size)
{
    const char *v = caps_get(c, key);
    *size = 0;
    if (!v)
        return NULL;
    size_t n = unhex(v, v + strlen(v), NULL);
    uint8_t *tmp = malloc(n + 1);
    unhex(v, v + strlen(v), tmp);
    uint8_t *e = extradata(tmp, n, size);
    free(tmp);
    return e;
}

/* streamheader=(buffer)< h1, h2, h3 > as Xiph extradata: each header as a 16-bit big-endian
 * length and its bytes (what avpriv_split_xiph_headers reads when it starts 0x00 0x1e). */
static uint8_t *caps_xiph(const struct caps *c, int *size)
{
    const char *v = caps_get(c, "streamheader");
    *size = 0;
    if (!v)
        return NULL;
    size_t len = strlen(v);
    uint8_t *out = malloc(len + 16), *o = out;
    const char *p = v;
    while (*p) {
        while (*p && !isxdigit((unsigned char)*p))
            p++;
        const char *e = p;
        while (*e && isxdigit((unsigned char)*e))
            e++;
        if (e == p)
            break;
        size_t n = unhex(p, e, NULL);
        o[0] = (uint8_t)(n >> 8);
        o[1] = (uint8_t)n;
        unhex(p, e, o + 2);
        o += 2 + n;
        p = e;
    }
    uint8_t *x = extradata(out, (size_t)(o - out), size);
    free(out);
    return x;
}

void caps_codec(const char *string, struct codec_params *p)
{
    struct caps c;
    caps_parse(&c, string);
    memset(p, 0, sizeof *p);
    p->id = AV_CODEC_ID_NONE;
    p->width = caps_int(&c, "width", 0);
    p->height = caps_int(&c, "height", 0);
    p->rate = caps_int(&c, "rate", 0);
    p->channels = caps_int(&c, "channels", 0);
    p->block_align = caps_int(&c, "block_align", 0);
    p->bit_rate = caps_int(&c, "bitrate", 0);
    p->bits = caps_int(&c, "depth", 0);
    const char *n = c.name;
    int mpeg = caps_int(&c, "mpegversion", 0);
    p->split = caps_is(&c, "alignment", "nal") ||
               (caps_get(&c, "parsed") && caps_is(&c, "parsed", "false"));
    if (!strcmp(n, "video/x-h264"))
        p->id = AV_CODEC_ID_H264;
    else if (!strcmp(n, "video/x-h265") || !strcmp(n, "video/x-hevc"))
        p->id = AV_CODEC_ID_HEVC;
    else if (!strcmp(n, "video/x-vp8"))
        p->id = AV_CODEC_ID_VP8;
    else if (!strcmp(n, "video/x-vp9"))
        p->id = AV_CODEC_ID_VP9;
    else if (!strcmp(n, "video/x-av1"))
        p->id = AV_CODEC_ID_AV1;
    else if (!strcmp(n, "video/mpeg"))
        p->id = mpeg == 4   ? AV_CODEC_ID_MPEG4
                : mpeg == 1 ? AV_CODEC_ID_MPEG1VIDEO
                            : AV_CODEC_ID_MPEG2VIDEO;
    else if (!strcmp(n, "video/x-divx") || !strcmp(n, "video/x-xvid") || !strcmp(n, "video/x-3ivx"))
        p->id = AV_CODEC_ID_MPEG4;
    else if (!strcmp(n, "video/x-h263"))
        p->id = AV_CODEC_ID_H263;
    else if (!strcmp(n, "video/x-flash-video"))
        p->id = AV_CODEC_ID_FLV1;
    else if (!strcmp(n, "image/jpeg"))
        p->id = AV_CODEC_ID_MJPEG;
    else if (!strcmp(n, "video/x-wmv")) {
        const char *f = caps_get(&c, "format");
        p->id = f && !strcmp(f, "WVC1")              ? AV_CODEC_ID_VC1
                : caps_int(&c, "wmvversion", 3) == 3 ? AV_CODEC_ID_WMV3
                : caps_int(&c, "wmvversion", 3) == 2 ? AV_CODEC_ID_WMV2
                                                     : AV_CODEC_ID_WMV1;
    } else if (!strcmp(n, "video/x-msmpeg")) {
        int v = caps_int(&c, "msmpegversion", 43);
        p->id = v == 41   ? AV_CODEC_ID_MSMPEG4V1
                : v == 42 ? AV_CODEC_ID_MSMPEG4V2
                          : AV_CODEC_ID_MSMPEG4V3;
    } else if (!strcmp(n, "video/x-pn-realvideo"))
        p->id = caps_int(&c, "rmversion", 4) == 3 ? AV_CODEC_ID_RV30 : AV_CODEC_ID_RV40;
    else if (!strcmp(n, "audio/mpeg")) {
        if (mpeg == 1) {
            int layer = caps_int(&c, "layer", 3);
            p->id = layer == 1 ? AV_CODEC_ID_MP1 : layer == 2 ? AV_CODEC_ID_MP2 : AV_CODEC_ID_MP3;
        } else
            p->id = AV_CODEC_ID_AAC;
    } else if (!strcmp(n, "audio/x-ac3") || !strcmp(n, "audio/x-private1-ac3"))
        p->id = AV_CODEC_ID_AC3;
    else if (!strcmp(n, "audio/x-eac3"))
        p->id = AV_CODEC_ID_EAC3;
    else if (!strncmp(n, "audio/x-dts", 11) || !strcmp(n, "audio/x-private1-dts"))
        p->id = AV_CODEC_ID_DTS;
    else if (!strcmp(n, "audio/x-vorbis"))
        p->id = AV_CODEC_ID_VORBIS;
    else if (!strcmp(n, "audio/x-opus"))
        p->id = AV_CODEC_ID_OPUS;
    else if (!strcmp(n, "audio/x-flac"))
        p->id = AV_CODEC_ID_FLAC;
    else if (!strcmp(n, "audio/x-alaw"))
        p->id = AV_CODEC_ID_PCM_ALAW;
    else if (!strcmp(n, "audio/x-mulaw"))
        p->id = AV_CODEC_ID_PCM_MULAW;
    else if (!strcmp(n, "audio/AMR"))
        p->id = AV_CODEC_ID_AMR_NB;
    else if (!strcmp(n, "audio/AMR-WB"))
        p->id = AV_CODEC_ID_AMR_WB;
    else if (!strcmp(n, "audio/x-wma")) {
        int v = caps_int(&c, "wmaversion", 2);
        p->id = v == 1 ? AV_CODEC_ID_WMAV1 : v == 2 ? AV_CODEC_ID_WMAV2 : AV_CODEC_ID_WMAPRO;
    } else if (!strcmp(n, "audio/x-raw")) {
        const char *f = caps_get(&c, "format");
        static const struct {
            const char *name;
            enum AVCodecID id;
        } pcm[] = {
            {"S16LE", AV_CODEC_ID_PCM_S16LE}, {"S16BE", AV_CODEC_ID_PCM_S16BE},
            {"U8", AV_CODEC_ID_PCM_U8},       {"S8", AV_CODEC_ID_PCM_S8},
            {"S24LE", AV_CODEC_ID_PCM_S24LE}, {"S24BE", AV_CODEC_ID_PCM_S24BE},
            {"S32LE", AV_CODEC_ID_PCM_S32LE}, {"S32BE", AV_CODEC_ID_PCM_S32BE},
            {"F32LE", AV_CODEC_ID_PCM_F32LE}, {"F32BE", AV_CODEC_ID_PCM_F32BE},
            {"F64LE", AV_CODEC_ID_PCM_F64LE}, {"S24_32LE", AV_CODEC_ID_PCM_S24LE},
        };
        for (unsigned i = 0; f && i < sizeof pcm / sizeof pcm[0]; i++)
            if (!strcmp(f, pcm[i].name))
                p->id = pcm[i].id;
        if (!caps_is(&c, "layout", "interleaved") && caps_get(&c, "layout"))
            p->id = AV_CODEC_ID_NONE;
    }
    if (p->id == AV_CODEC_ID_VORBIS || p->id == AV_CODEC_ID_FLAC) {
        if (p->id == AV_CODEC_ID_VORBIS)
            p->extradata = caps_xiph(&c, &p->extradata_size);
        else {
            /* GStreamer's first FLAC header is 0x7f "FLAC" 1 0 n n, then "fLaC" and STREAMINFO */
            int size;
            uint8_t *x = caps_xiph(&c, &size);
            if (x && size > 2 + 9 + 4) {
                int first = x[0] << 8 | x[1];
                if (first > 9)
                    p->extradata = extradata(x + 2 + 9, (size_t)first - 9, &p->extradata_size);
            }
            av_free(x);
        }
    } else if (caps_get(&c, "codec_data"))
        p->extradata = caps_buffer(&c, "codec_data", &p->extradata_size);
    caps_clear(&c);
}

/* The codecs a software-only decoder takes (a VM's hwdec=none; docs/research/
 * media-helper-isolation.md, "The current build and a minimal one"): what broadcast, streaming
 * and common files use, and nothing else of caps_codec's table, so that a guest reaches fewer of
 * FFmpeg's decoders. Refused: WMV and VC-1, RealVideo, MSMPEG4, FLV, H.263, MJPEG, DTS, AMR, WMA,
 * A-law and mu-law. */
int caps_software_only(enum AVCodecID id)
{
    switch (id) {
    case AV_CODEC_ID_H264:
    case AV_CODEC_ID_HEVC:
    case AV_CODEC_ID_VP8:
    case AV_CODEC_ID_VP9:
    case AV_CODEC_ID_AV1: /* through libdav1d only */
    case AV_CODEC_ID_MPEG1VIDEO:
    case AV_CODEC_ID_MPEG2VIDEO:
    case AV_CODEC_ID_MPEG4:
    case AV_CODEC_ID_AAC:
    case AV_CODEC_ID_AC3:
    case AV_CODEC_ID_EAC3:
    case AV_CODEC_ID_MP1:
    case AV_CODEC_ID_MP2:
    case AV_CODEC_ID_MP3:
    case AV_CODEC_ID_OPUS:
    case AV_CODEC_ID_VORBIS:
    case AV_CODEC_ID_FLAC:
    case AV_CODEC_ID_PCM_S16LE:
    case AV_CODEC_ID_PCM_S16BE:
    case AV_CODEC_ID_PCM_U8:
    case AV_CODEC_ID_PCM_S8:
    case AV_CODEC_ID_PCM_S24LE:
    case AV_CODEC_ID_PCM_S24BE:
    case AV_CODEC_ID_PCM_S32LE:
    case AV_CODEC_ID_PCM_S32BE:
    case AV_CODEC_ID_PCM_F32LE:
    case AV_CODEC_ID_PCM_F32BE:
    case AV_CODEC_ID_PCM_F64LE:
        return 1;
    default:
        return 0;
    }
}
