// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
/* rtk.ko's stand-in TAs, behind lgemu.ko's /dev/tee0 (lgemu/tee.c).
 *
 * sstore: LG's secure-store TA (UUID a1bc890f-21dc-f54b-9745-d6f6a0e768f6, the same in W19P's,
 * W20L's and W21P's libkadaptor: KADP_SStore_* and their x* helpers, each of which opens a session,
 * invokes one command and closes it). The sessions must open: each helper's failure path closes its
 * session whether or not it was opened, and TEEC_CloseSession of an unopened one faults (W21P's
 * browser network service in xMakeClearKey, W20L's webos-connman-adapter in xMakeKey, W19P's
 * airplay in xMFiVerifySymmetricKey).
 *
 * The commands are a secure store of its own, self-consistent, with the parameters libkadaptor's
 * helpers pass. Every key is
 * derived from the VM's hardware-unique key (tee_huk=, the launcher's identity/tee-huk, 32 random
 * bytes made for this VM): K(label) = HMAC-SHA256(HUK, label); a keystream is HMAC-SHA256(key,
 * nonce || le32 block) XORed over the data. tests/regress/lib/teeref.py is the same in Python (the
 * tee step checks the guest's answers against it). No key of any set or firmware is here, made or
 * answered.
 *
 *   0x200 CheckValid         success (a provisioned store)
 *   0x201 MakeKey (p0 data, p1 out), 0x203 MakeClearKey (p0 type, p1 data, p2 out)
 *                            a wrapped blob of len + 176 bytes, as LG's: "EMSS" le32(len) nonce[16]
 *                            ct zeros[120] tag[32];
 *                            an output of size 0 is a length probe (success, size set)
 *   0x202 GetSecureKey, 0x204 GetSocSstoreKey (p0 blob, p1 out); 0x205, 0x206 their lengths (p1.a)
 *                            the blob unwrapped; a blob not ours or altered: TEE_ERROR_MAC_INVALID
 *   0x2 / 0x1                encrypt / decrypt p2 into p3 (same length), the key from the header's
 *                            first two words (key id, mode) and p1
 *   0x20 / 0x40              the same as a container: p3 = nonce[16] tag[16] ct for p2's bytes from
 *                            32 on (the first 32 are the container's); 0x40 checks the tag
 *   0x8 / 0x10               the store's MAC (p0, the 0x40000-byte image) with its own slot, bytes
 *                            0x1c..0x3c, as zeros: into p1 / checked against that slot
 *   0x80 / 0x100             HMAC of p1 || p2 under the sub-op p0.a: into p3 / checked against p3
 *   0x4                      ReEncrypt at init: the store is ours already, p2 = p1
 *   0x800                    MakeNewSecureData: le32(len) and p1 encrypted (key from p0.a)
 *   others (0x400/0x401 RPMB, 0x1619 DRM key, 0x10000.. MFi, ...): TEEC_ERROR_NOT_SUPPORTED, which
 *                            the helpers turn into a clean failure
 *
 * Every other UUID is TEEC_ERROR_ITEM_NOT_FOUND, as before (no TEE at all), which their callers
 * (CI+, ACAS, OEMCrypto, PlayReady, SKB, ...) take cleanly. /proc/rtk/tee counts each (TA, command,
 * result); the first call of each command is logged with its parameters' types and sizes. */
#define pr_fmt(fmt) KBUILD_MODNAME ": tee: " fmt
#include <crypto/sha2.h>
#include <linux/hex.h>
#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/proc_fs.h>
#include <linux/random.h>
#include <linux/sched.h>
#include <linux/string.h>
#include <linux/tee.h>
#include <linux/unaligned.h>

#include "lgemu-drv.h"
#include "lgemu-tee.h"
#include "rtk-internal.h"

static char *tee_huk;
module_param(tee_huk, charp, 0);
MODULE_PARM_DESC(tee_huk,
                 "the TEE's hardware-unique key, 64 hex digits (the VM's own; random if none)");

#define TEE_ERROR_MAC_INVALID 0xFFFF3071u
#define OPEN                  0xffffffffu /* the "command" of a session's open, in the counts */

static u8 huk[32];

static int tee_show(char *buf, size_t size, const struct lgemu_count *e)
{
    if (e->a == OPEN)
        return scnprintf(buf, size, "%s open: %lu (%#x)\n", e->what, e->count, e->b);
    return scnprintf(buf, size, "%s command %#x: %lu (%#x)\n", e->what, e->a, e->count, e->b);
}

DEFINE_LGEMU_COUNTS(tee_counts, 64, 64, tee_show); /* (TA, command, result) */

/* the first call of each command is logged; later ones are only counted */
static u32 seen[48];
static unsigned int seen_count;

static bool first(u32 what)
{
    for (unsigned int i = 0; i < seen_count; i++)
        if (seen[i] == what)
            return false;
    if (seen_count < ARRAY_SIZE(seen))
        seen[seen_count++] = what;
    return true;
}

/* ---- the keys ---- */

static void key_of(const char *label, u8 out[32])
{
    hmac_sha256_usingrawkey(huk, sizeof(huk), label, strlen(label), out);
}

/* out[i] = in[i] ^ HMAC(key, nonce || le32(i / 32))[i % 32]; in and out may be the same */
static void stream(const u8 key[32], const u8 *nonce, size_t nonce_len, const u8 *in, u8 *out,
                   size_t len)
{
    struct hmac_sha256_key k;
    u8 block[32], ctr[4];

    hmac_sha256_preparekey(&k, key, 32);
    for (size_t off = 0; off < len; off += 32) {
        struct hmac_sha256_ctx c;

        put_unaligned_le32(off / 32, ctr);
        hmac_sha256_init(&c, &k);
        hmac_sha256_update(&c, nonce, nonce_len);
        hmac_sha256_update(&c, ctr, 4);
        hmac_sha256_final(&c, block);
        for (size_t i = 0; i < 32 && off + i < len; i++)
            out[off + i] = in[off + i] ^ block[i];
    }
    memzero_explicit(block, sizeof(block));
    memzero_explicit(&k, sizeof(k));
}

/* differ, in constant time */
static bool neq(const void *a, const void *b, size_t n)
{
    const u8 *x = a, *y = b;
    u8 d = 0;

    for (size_t i = 0; i < n; i++)
        d |= x[i] ^ y[i];
    return d;
}

/* ---- parameters ---- */

static u32 types_of(const struct lgemu_ta_param *p)
{
    return p[0].type | p[1].type << 4 | p[2].type << 8 | p[3].type << 12;
}

/* an output of need bytes: TEEC_SUCCESS with its size set if it fits, or if its size is 0 (the
 * helpers' length probe: KADP_SStore_SetKey's xMakeKey(..., NULL, &len)); else SHORT_BUFFER */
static u32 room(struct lgemu_ta_param *out, size_t need, bool *write)
{
    *write = out->size >= need && out->buf;
    if (out->size && !*write) {
        out->size = need;
        return TEEC_ERROR_SHORT_BUFFER;
    }
    out->size = need;
    return TEEC_SUCCESS;
}

/* ---- the wrapped blob of MakeKey / MakeClearKey ---- */

/* LG's blob is the data and 176 bytes (KADP_SStore_SetKey's and libsestore's buffers for 0x203 are
 * len + 176, and libsestore reads a stored one back whole, len + 176, for 0x202): ours too, as
 * "EMSS", le32 length, nonce[16], the data encrypted, zeros, tag[32] */
#define BLOB_HEAD  24
#define BLOB_TAG   32
#define BLOB_EXTRA 176

static void blob_tag(const u8 *blob, size_t len, u8 tag[32])
{
    u8 k[32];

    key_of("sstore wrap mac", k);
    hmac_sha256_usingrawkey(k, 32, blob, len, tag);
    memzero_explicit(k, sizeof(k));
}

static u32 wrap(const struct lgemu_ta_param *in, struct lgemu_ta_param *out)
{
    size_t need = in->size + BLOB_EXTRA;
    bool write;
    u32 ret = room(out, need, &write);
    u8 *b = out->buf, k[32];

    if (ret || !write)
        return ret;
    memmove(b + BLOB_HEAD, in->buf, in->size);
    memcpy(b, "EMSS", 4);
    put_unaligned_le32(in->size, b + 4);
    get_random_bytes(b + 8, 16);
    key_of("sstore wrap enc", k);
    stream(k, b + 8, 16, b + BLOB_HEAD, b + BLOB_HEAD, in->size);
    memzero_explicit(k, sizeof(k));
    memset(b + BLOB_HEAD + in->size, 0, BLOB_EXTRA - BLOB_HEAD - BLOB_TAG);
    blob_tag(b, need - BLOB_TAG, b + need - BLOB_TAG);
    return TEEC_SUCCESS;
}

/* the blob's data length, or -1 if it is not one of ours (unaltered) */
static long blob_len(const struct lgemu_ta_param *in)
{
    const u8 *b = in->buf;
    u8 tag[32];
    u32 len;

    if (!b || in->size < BLOB_EXTRA || memcmp(b, "EMSS", 4))
        return -1;
    len = get_unaligned_le32(b + 4);
    if (len != in->size - BLOB_EXTRA)
        return -1;
    blob_tag(b, in->size - BLOB_TAG, tag);
    if (neq(tag, b + in->size - BLOB_TAG, 32))
        return -1;
    return len;
}

static u32 unwrap(const struct lgemu_ta_param *in, struct lgemu_ta_param *out)
{
    long len = blob_len(in);
    bool write;
    u32 ret;
    u8 k[32];

    if (len < 0)
        return TEE_ERROR_MAC_INVALID;
    ret = room(out, len, &write);
    if (ret || !write)
        return ret;
    key_of("sstore wrap enc", k);
    stream(k, (const u8 *)in->buf + 8, 16, (const u8 *)in->buf + BLOB_HEAD, out->buf, len);
    memzero_explicit(k, sizeof(k));
    return TEEC_SUCCESS;
}

/* ---- Encryption / Decryption: p0 the 20-byte header, p1 the inputs, p2 -> p3 ---- */

/* the key of a header's key id and mode (its first two words) and the inputs p1 */
static void cipher_key(const struct lgemu_ta_param *p, u8 out[32])
{
    struct hmac_sha256_ctx c;
    u8 k[32];

    key_of("sstore cipher", k);
    hmac_sha256_init_usingrawkey(&c, k, 32);
    hmac_sha256_update(&c, p[0].buf, 8);
    if (p[1].size)
        hmac_sha256_update(&c, p[1].buf, p[1].size);
    hmac_sha256_final(&c, out);
    memzero_explicit(k, sizeof(k));
}

#define BOX_HEAD 32 /* the container's: nonce[16], tag[16] */

static void box_tag(const u8 key[32], const u8 *nonce, const u8 *ct, size_t len, u8 tag[32])
{
    struct hmac_sha256_ctx c;

    hmac_sha256_init_usingrawkey(&c, key, 32);
    hmac_sha256_update(&c, (const u8 *)"tag", 3);
    hmac_sha256_update(&c, nonce, 16);
    hmac_sha256_update(&c, ct, len);
    hmac_sha256_final(&c, tag);
}

static u32 cipher(u32 cmd, struct lgemu_ta_param *p)
{
    bool box = cmd == 0x20 || cmd == 0x40;
    u8 key[32], tag[32];
    const u8 *in = p[2].buf;
    u8 *out = p[3].buf;
    size_t len = p[2].size;
    bool write;
    u32 ret;

    if (p[0].size < 20 || (box && len < BOX_HEAD))
        return TEEC_ERROR_BAD_PARAMETERS;
    ret = room(&p[3], len, &write);
    if (ret || !write)
        return ret;
    cipher_key(p, key);
    if (!box) {
        stream(key, NULL, 0, in, out, len);
    } else if (cmd == 0x20) {
        u8 nonce[16];

        get_random_bytes(nonce, 16);
        stream(key, nonce, 16, in + BOX_HEAD, out + BOX_HEAD, len - BOX_HEAD);
        memcpy(out, nonce, 16);
        box_tag(key, nonce, out + BOX_HEAD, len - BOX_HEAD, tag);
        memcpy(out + 16, tag, 16);
    } else {
        box_tag(key, in, in + BOX_HEAD, len - BOX_HEAD, tag);
        if (neq(tag, in + 16, 16)) {
            ret = TEE_ERROR_MAC_INVALID;
        } else {
            memmove(out, in, BOX_HEAD);
            stream(key, out, 16, in + BOX_HEAD, out + BOX_HEAD, len - BOX_HEAD);
        }
    }
    memzero_explicit(key, sizeof(key));
    return ret;
}

/* ---- MACs ---- */

#define STORE_SLOT 0x1c /* xGenerateHMACValue copies the 32-byte MAC to the image's +0x1c */

static void store_mac(const struct lgemu_ta_param *img, u8 mac[32])
{
    static const u8 zeros[32];
    struct hmac_sha256_ctx c;
    u8 k[32];

    key_of("sstore store mac", k);
    hmac_sha256_init_usingrawkey(&c, k, 32);
    hmac_sha256_update(&c, img->buf, STORE_SLOT);
    hmac_sha256_update(&c, zeros, 32);
    hmac_sha256_update(&c, (const u8 *)img->buf + STORE_SLOT + 32, img->size - STORE_SLOT - 32);
    hmac_sha256_final(&c, mac);
    memzero_explicit(k, sizeof(k));
}

static void hmac_of(const struct lgemu_ta_param *p, u8 mac[32])
{
    struct hmac_sha256_ctx c;
    u8 k[32], sub[4];

    key_of("sstore hmac", k);
    put_unaligned_le32(p[0].a, sub);
    hmac_sha256_init_usingrawkey(&c, k, 32);
    hmac_sha256_update(&c, sub, 4);
    hmac_sha256_update(&c, p[1].buf, p[1].size);
    hmac_sha256_update(&c, p[2].buf, p[2].size);
    hmac_sha256_final(&c, mac);
    memzero_explicit(k, sizeof(k));
}

/* ---- the commands ---- */

#define T(a, b, c, d) ((a) | (b) << 4 | (c) << 8 | (d) << 12)
#define MI            TEE_IOCTL_PARAM_ATTR_TYPE_MEMREF_INPUT
#define MO            TEE_IOCTL_PARAM_ATTR_TYPE_MEMREF_OUTPUT
#define MIO           TEE_IOCTL_PARAM_ATTR_TYPE_MEMREF_INOUT
#define VI            TEE_IOCTL_PARAM_ATTR_TYPE_VALUE_INPUT
#define VIO           TEE_IOCTL_PARAM_ATTR_TYPE_VALUE_INOUT

static const struct {
    u32 cmd, types;
} sstore_types[] = {
    {0x200, 0},
    {0x201, T(MI, MO, 0, 0)},
    {0x202, T(MI, MO, 0, 0)},
    {0x204, T(MI, MO, 0, 0)},
    {0x203, T(MI, MI, MO, 0)},
    {0x205, T(MI, VIO, 0, 0)},
    {0x206, T(MI, VIO, 0, 0)},
    {0x1, T(MI, MI, MI, MO)},
    {0x2, T(MI, MI, MI, MO)},
    {0x20, T(MI, MI, MI, MO)},
    {0x40, T(MI, MI, MI, MO)},
    {0x8, T(MI, MO, 0, 0)},
    {0x10, T(MI, 0, 0, 0)},
    {0x80, T(VI, MI, MIO, MO)},
    {0x100, T(VI, MI, MI, MI)},
    {0x4, T(MI, MI, MO, 0)},
    {0x800, T(VI, MI, MO, 0)},
};

static u32 sstore_run(u32 cmd, struct lgemu_ta_param *p)
{
    u8 mac[32];
    bool write;
    long len;
    u32 ret;
    size_t i;

    for (i = 0; i < ARRAY_SIZE(sstore_types) && sstore_types[i].cmd != cmd; i++)
        ;
    if (i == ARRAY_SIZE(sstore_types))
        return TEEC_ERROR_NOT_SUPPORTED;
    if (types_of(p) != sstore_types[i].types)
        return TEEC_ERROR_BAD_PARAMETERS;
    switch (cmd) {
    case 0x200:
        return TEEC_SUCCESS;
    case 0x201:
        return wrap(&p[0], &p[1]);
    case 0x203:
        return wrap(&p[1], &p[2]);
    case 0x202:
    case 0x204:
        return unwrap(&p[0], &p[1]);
    case 0x205:
    case 0x206:
        len = blob_len(&p[0]);
        if (len < 0)
            return TEE_ERROR_MAC_INVALID;
        p[1].a = len;
        p[1].b = 0;
        return TEEC_SUCCESS;
    case 0x1:
    case 0x2:
    case 0x20:
    case 0x40:
        return cipher(cmd, p);
    case 0x8:
        if (p[0].size < STORE_SLOT + 32)
            return TEEC_ERROR_BAD_PARAMETERS;
        ret = room(&p[1], 32, &write);
        if (!ret && write) {
            store_mac(&p[0], mac);
            memcpy(p[1].buf, mac, 32);
        }
        return ret;
    case 0x10:
        if (p[0].size < STORE_SLOT + 32)
            return TEEC_ERROR_BAD_PARAMETERS;
        store_mac(&p[0], mac);
        return neq(mac, (const u8 *)p[0].buf + STORE_SLOT, 32) ? TEE_ERROR_MAC_INVALID
                                                               : TEEC_SUCCESS;
    case 0x80:
        ret = room(&p[3], 32, &write);
        if (!ret && write) {
            hmac_of(p, mac);
            memcpy(p[3].buf, mac, 32);
        }
        return ret;
    case 0x100:
        if (p[3].size != 32)
            return TEEC_ERROR_BAD_PARAMETERS;
        hmac_of(p, mac);
        return neq(mac, p[3].buf, 32) ? TEE_ERROR_MAC_INVALID : TEEC_SUCCESS;
    case 0x4:
        ret = room(&p[2], p[1].size, &write);
        if (!ret && write)
            memmove(p[2].buf, p[1].buf, p[1].size);
        return ret;
    case 0x800: {
        char label[24];

        ret = room(&p[2], p[1].size + 4, &write);
        if (ret || !write)
            return ret;
        snprintf(label, sizeof(label), "sstore new data %u", p[0].a);
        key_of(label, mac);
        memmove((u8 *)p[2].buf + 4, p[1].buf, p[1].size);
        put_unaligned_le32(p[1].size, p[2].buf);
        stream(mac, NULL, 0, (u8 *)p[2].buf + 4, (u8 *)p[2].buf + 4, p[1].size);
        return TEEC_SUCCESS;
    }
    }
    return TEEC_ERROR_NOT_SUPPORTED;
}

static u32 sstore_invoke(void *state, u32 cmd, struct lgemu_ta_param *p)
{
    u32 types = types_of(p), ret;
    size_t sizes[4];

    for (int i = 0; i < 4; i++)
        sizes[i] = p[i].size;
    ret = sstore_run(cmd, p);
    lgemu_count(&tee_counts, "sstore", cmd, ret);
    if (first(cmd))
        pr_info("sstore command %#x (%s): params %#x, sizes %zu %zu %zu %zu: %#x\n", cmd,
                current->comm, types, sizes[0], sizes[1], sizes[2], sizes[3], ret);
    return ret;
}

static u32 sstore_open(void **state, struct lgemu_ta_param *p)
{
    lgemu_count(&tee_counts, "sstore", OPEN, TEEC_SUCCESS);
    return TEEC_SUCCESS;
}

static const struct lgemu_ta tas[] = {
    {.name = "sstore",
     .uuid = {0xa1, 0xbc, 0x89, 0x0f, 0x21, 0xdc, 0xf5, 0x4b, 0x97, 0x45, 0xd6, 0xf6, 0xa0, 0xe7,
              0x68, 0xf6},
     .open = sstore_open,
     .invoke = sstore_invoke},
};

static int ta_setup(struct proc_dir_entry *dir)
{
    if (tee_huk && strlen(tee_huk) == 64 && !hex2bin(huk, tee_huk, 32)) {
        pr_info("the VM's hardware-unique key\n");
    } else {
        if (tee_huk)
            pr_warn("tee_huk is not 64 hex digits: a random key for this boot\n");
        else
            pr_info("no tee_huk: a random key for this boot\n");
        get_random_bytes(huk, sizeof(huk));
    }
    if (!proc_create_data("tee", 0444, dir, &lgemu_counts_proc_ops, &tee_counts))
        return -ENOMEM;
    return 0;
}

static const struct lgemu_ta *ta_find(const u8 *uuid)
{
    for (size_t i = 0; i < ARRAY_SIZE(tas); i++)
        if (!memcmp(tas[i].uuid, uuid, 16))
            return &tas[i];
    return NULL;
}

/* a UUID with no TA: counted under its first word, logged once a UUID (as far as the table goes) */
static void ta_unknown(const u8 *uuid)
{
    u32 word = get_unaligned_be32(uuid);

    lgemu_count(&tee_counts, "no TA", word, TEEC_ERROR_ITEM_NOT_FOUND);
    if (first(word | 1))
        pr_info("no TA %pUb (%s): item not found\n", uuid, current->comm);
}

static const struct lgemu_tee_tas rtk_tas = {.find = ta_find, .unknown = ta_unknown};
static bool added;

int rtk_tee_add(struct proc_dir_entry *dir)
{
    int err = ta_setup(dir);

    if (!err)
        err = lgemu_tee_add(&rtk_tas);
    added = !err;
    return err;
}

void rtk_tee_remove(void)
{
    if (added)
        lgemu_tee_remove(&rtk_tas);
    added = false;
}
