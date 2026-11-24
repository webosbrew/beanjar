// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
#include "private.h"

static unsigned long long miu_interval, miu_len;
static char *mmap_file;
static bool mmap_u64;
module_param(mmap_u64, bool, 0444);
MODULE_PARM_DESC(mmap_u64, "export mmapger records/info with 64-bit fields (LM21AN)");
module_param(mmap_file, charp, 0444);
MODULE_PARM_DESC(mmap_file, "firmware-derived mmapger records (binary mmap.bin layout)");
static unsigned int miu0_bus, miu1_bus, mmap_ids, mmap_slot, lx_mem_id, lx_mem2_id;
module_param(miu_interval, ullong, 0444);
MODULE_PARM_DESC(miu_interval, "MIU1's physical address (MIU_INTERVAL)");
module_param(miu_len, ullong, 0444);
MODULE_PARM_DESC(miu_len, "each MIU's size in bytes (MIU_DRAM_LEN0, _LEN1)");
module_param(miu0_bus, uint, 0444);
MODULE_PARM_DESC(miu0_bus, "MIU0's ARM bus address (a PA plus this)");
module_param(miu1_bus, uint, 0444);
MODULE_PARM_DESC(miu1_bus, "MIU1's ARM bus address (an MIU1 offset plus this)");
module_param(mmap_ids, uint, 0444);
MODULE_PARM_DESC(mmap_ids, "the number of mmapger records (MStar's E_MMAP_ID_*)");
module_param(mmap_slot, uint, 0444);
MODULE_PARM_DESC(mmap_slot, "the bytes each mmapger record gets");
static char *mmap_len[16];
static int mmap_len_count;
module_param_array(mmap_len, charp, &mmap_len_count, 0444);
MODULE_PARM_DESC(mmap_len, "records whose length is not mmap_slot, id:bytes (the firmware's own)");
/* Records that lie inside another, as on the board (SimpleSmart's TSP_BUF, VQ_BUF, VBI_1/2,
 * VD_3DCOMB and TTX_BUF in POOL0, which its MAdp_MPool_Init maps; KADP's MAdp_MPool_PA2KSEG1 finds
 * only addresses inside a mapped record): "child:parent:offset", the child at the parent's address
 * plus offset, in its MIU, taking no slot of its own; it must end within the parent. */
static char *mmap_in[16];
static int mmap_in_count;
module_param_array(mmap_in, charp, &mmap_in_count, 0444);
MODULE_PARM_DESC(mmap_in, "records inside another, child:parent:offset (the board's nesting)");
module_param(lx_mem_id, uint, 0444);
MODULE_PARM_DESC(lx_mem_id, "the mmapger record of LX_MEM (the kernel's memory)");
module_param(lx_mem2_id, uint, 0444);
MODULE_PARM_DESC(lx_mem2_id, "the mmapger record of LX_MEM2 (the kernel's memory in MIU1)");

/* KADP (MAdp_MPool_Init) checks SYS_INFO's LX_MEM2_ADDR + this against mmapger's LX_MEM2 + MIU0's
 * bus address; SYS_INFO's MIU1 address is then the MIU1 bus address less it. A KADP convention
 * that changed between builds: W16R's (M2) adds 0x40000000, W18R's (M3) nothing. */
static unsigned int lx_mem2_bias;
module_param(lx_mem2_bias, uint, 0444);
MODULE_PARM_DESC(lx_mem2_bias, "what KADP adds to SYS_INFO's LX_MEM2_ADDR before its check");

/* The older Utopia user side of an MStar webOS build (W15U, Monaco): the system and memory pool
 * drivers' structures with 32-bit fields where W16R's (CONFIG_MP_NEW_UTOPIA_32BIT) have u64 ones:
 * SYS_INFO's IO_Sys_Info_t (0x48 bytes, 0xc0485399), MPOOL_SET's DrvMPool_Info_t (16 bytes,
 * 0x80104d04, the address an offset in the MIU it names), KERNELDETECT's DrvMPool_Kernel_Info_t
 * (16 bytes, 0xc0104d05) and the flushes' DrvMPool_Flush_Info_t (12 bytes, 0x800c4d07 and
 * 0x800c4d09), as W15U's libkadaptor.so.1 issues them (MDrv_SYS_Info, MsOS_MPool_*). LM14's
 * NetCast KADP has the same SYS_INFO and MPOOL_SET sizes, its MPOOL_SET address a PA (lm14). */
static bool utopia32;
module_param(utopia32, bool, 0444);
MODULE_PARM_DESC(utopia32, "32-bit-field SYS_INFO and memory pool structures (W15U)");

/* MIU_INTERVAL2 of /proc/mmapger/info.bin, where a third MIU would start: W15U's MAdp_MPool_Init
 * maps a record at or above it as MIU 2, so on a two-MIU chip it is above every record; 0 (the
 * default) where the KADP has no MIU 2 branch. */
static unsigned int miu_interval2;
module_param(miu_interval2, uint, 0444);
MODULE_PARM_DESC(miu_interval2, "MIU_INTERVAL2 in mmapger's info.bin (0: none)");

/* The mmapger record of SECURE_SHM, the memory MStar's TEE client shares with the secure world.
 * Set on a chip whose KADP starts that client at init (M3: W18R's MAdp_SYS_Init runs a "TEE INIT"
 * task): it brings LG's /proc/mmapger/mmap.ini, MStar's mailbox /dev/msmailbox and the secure
 * world's side of the mailbox (below, "the secure world"). Unset (M2), none of them exist. */
static int secure_shm_id = -1;
module_param(secure_shm_id, int, 0444);
MODULE_PARM_DESC(secure_shm_id, "the mmapger record of SECURE_SHM (the TEE's shared memory); "
                                "unset: no TEE, no mailbox");

/* The mmapger record of GOP_FRAMEBUFFER, the pool of the GOP node /dev/mstar-gop-fb ("the GOP
 * frame buffer" below): set for SimpleSmart, whose /dev/fb0 it is. Unset: no such node. */
static int gop_fb = -1;
module_param(gop_fb, int, 0444);
MODULE_PARM_DESC(gop_fb, "the mmapger record of GOP_FRAMEBUFFER, the pool of /dev/mstar-gop-fb "
                         "(SimpleSmart's /dev/fb0); unset: no such node");

#define MEMTYPE_MIU1    0x1
#define MEMTYPE_UNCACHE 0x4

/* /proc/mmapger/mmap.bin: one record a mmap id (LG's layout of MStar's mmap.ini entries). */
struct mmapger_record {
    u32 available, addr, gap_chk, len, memory_type;
};

/* /proc/mmapger/info.bin: MIU_DRAM_LEN, _LEN0, _LEN1, _LEN2, (unused by KADP), MIU_INTERVAL,
 * MIU_INTERVAL2 */
struct mmapger_info {
    u32 dram_len, len0, len1, len2, unused, interval, interval2;
};

/* W22A's ARM32 KADP uses the widened kernel ABI: five/seven u64 fields,
 * 40/56 bytes. Internal sparse allocations and derived-file records stay u32. */
struct mmapger_record64 {
    u64 available, addr, gap_chk, len, memory_type;
};
struct mmapger_info64 {
    u64 dram_len, len0, len1, len2, unused, interval, interval2;
};
static_assert(sizeof(struct mmapger_record64) == 40);
static_assert(sizeof(struct mmapger_info64) == 56);
static struct mmapger_record64 *records64;
static struct mmapger_record *records;
static struct mmapger_info miu_info;

/* LG's names of the mmapger records, in id order, as its kernel's mmapger.c prints them in
 * /proc/mmapger/mmap.ini (MMAPGerTag[] of W18R's kernel, built with CONFIG_CMA: 95 names) */
static const char *const mmapger_names_m3[] = {
    "DUMMY_MEM",
    "LINUX_MEM",
    "EMAC_MEM",
    "XIP_MEM",
    "NFDRM_NUTTX_MEM",
    "NFDRM_HW_AES_BUF",
    "HW_SECURE_BUFFER",
    "SECURE_SHM",
    "SECURE_UPDATE_AREA",
    "SECURE_TSP",
    "BIN_MEM",
    "MAD_BASE_BUFFER",
    "VD_3DCOMB",
    "G3D_TEXTURE0_BUF",
    "LINUX_LOW_MEM_RESERVE",
    "VE_FRAMEBUFFER",
    "TTX_BUF",
    "PVR_DOWNLOAD_BUFFER",
    "PVR_UPLOAD_BUFFER",
    "TSP_BUF",
    "VQ_BUF",
    "MAILBOX",
    "DIP",
    "MPOOL",
    "PNG_OUTPUT_BUF",
    "PNG_INPUT_BUF",
    "VDEC_GN_IAP",
    "GOP_FRAMEBUFFER",
    "GOP_REGDMA",
    "GFX_VQ_BUFFER",
    "VBI_1_BUFFER",
    "VBI_2_BUFFER",
    "LD_BUFFER",
    "PIU_BUF",
    "DUMMY",
    "DUAL_STREAM_DUMY",
    "VDEC_CPU",
    "SVD_CPU",
    "VDEC_BITSTREAM",
    "VDEC_SUB_BITSTREAM",
    "VDEC_N1_BITSTREAM",
    "VDEC_N2_BITSTREAM",
    "VDEC_FRAMEBUFFER",
    "VDEC_SUB_FRAMEBUFFER",
    "NFDRM_VDEC_SHARE_MEM",
    "JPD_THUMBNAIL",
    "POSD0",
    "POSD1",
    "AUDIO_CLIP",
    "PVR_THUMBNAIL_DECODE_BUFFER",
    "LD_BUF",
    "BB",
    "DEBUG",
    "BT_POOL",
    "APVR_BUF",
    "VIDEO_CONF",
    "G3D_CMD_Q_BUF",
    "G3D_VERTEX_BUF",
    "G3D_COLOR_BUF",
    "G3D_TEXTURE1_BUF",
    "GPD_ES_BUF",
    "GPD_OUTPUT_BUF",
    "ISDBT_TDI",
    "VE_DIPW_BUF",
    "MFE_BUF",
    "MFE_OUT_BUF",
    "VIDEO_IN_CAPTURE_TMP",
    "LX_MEM1",
    "DTMB_TDI",
    "SCALER_DNR_BUF",
    "SCALER_DNR_W_BARRIER",
    "SCALER_PIP_BUF",
    "SCALER_MAIN_FRCM_BUFFER",
    "SC_SUB_FRCM",
    "SCALER_MCDI_ME1_BUFFER",
    "SCALER_MCDI_ME2_BUFFER",
    "SC_OD",
    "SC_BUF",
    "SCALER_MLOAD_BUFFER",
    "SCALER_DYNAMIC_XC_BUFFER",
    "TWOTO3D_DEPTH_DETECT_BUF",
    "TWOTO3D_DEPTH_RENDER_BUF",
    "T3D_DMA_CLIENT9_BUF",
    "SC2_MAIN_FB",
    "VDEC_DUMMY",
    "JPD_FRAMEBUFFER",
    "VDEC_SW_DETILE_BUF",
    "JPG_OUTPUT_BUF",
    "HWI2C_DMA",
    "E_MMAP_ID_XC_DOLBY_HDR",
    "E_MMAP_ID_CFD_MLOAD",
    "E_MMAP_ID_XC_AUTODOWNLOAD",
    "E_MMAP_ID_XC_AUTODOWNLOAD_XVYCC",
    "E_MMAP_ID_XC_VIRTUAL_REGISTER",
    "CIPLUS_BUFFER",
};

/* MMAPGerTag[] of W18A's kernel (4.4.84-636.glacier.26, LM18A: 102 names): W18R's first 86 (to
 * JPD_FRAMEBUFFER, SECURE_SHM still 7), then its own XC and FRC records */
static const char *const mmapger_names_lm18a_tail[] = {
    "E_MMAP_ID_VDEC_FRAMEBUFFER_BALANCE_BW",
    "HWI2C_DMA",
    "E_MMAP_ID_FRC_R2",
    "E_MMAP_ID_XC_DOLBY_HDR",
    "E_MMAP_ID_CFD_MLOAD",
    "E_MMAP_ID_XC_MLOAD2",
    "E_MMAP_ID_XC_DMA",
    "E_MMAP_ID_XC_FRC_R",
    "E_MMAP_ID_XC_FRC_L",
    "E_MMAP_ID_XC_FRC_PQ",
    "E_MMAP_ID_XC_AUTODOWNLOAD",
    "E_MMAP_ID_XC_AUTODOWNLOAD_XVYCC",
    "E_MMAP_ID_XC_AUTODOWNLOAD_ODGAMMA",
    "E_MMAP_ID_XC_VIRTUAL_REGISTER",
    "E_MMAP_ID_XC_DUMMY",
    "CIPLUS_BUFFER",
};

static char *mmap_names = "m3";
module_param(mmap_names, charp, 0444);
MODULE_PARM_DESC(mmap_names, "whose MMAPGerTag[] names the records in mmap.ini: m3 (W18R's 95, the "
                             "default) or lm18a (W18A's 102)");

/* the name of record id under mmap_names, or NULL past the table's end */
static const char *mmapger_name(u32 id)
{
    if (!strcmp(mmap_names, "lm18a")) {
        if (id < 86)
            return mmapger_names_m3[id];
        id -= 86;
        return id < ARRAY_SIZE(mmapger_names_lm18a_tail) ? mmapger_names_lm18a_tail[id] : NULL;
    }
    return id < ARRAY_SIZE(mmapger_names_m3) ? mmapger_names_m3[id] : NULL;
}

static int mmapger_file_layout(void)
{
    struct file *file;
    loff_t pos = 0;
    size_t bytes = array_size(mmap_ids, sizeof(*records));
    ssize_t n;

    file = filp_open(mmap_file, O_RDONLY, 0);
    if (IS_ERR(file))
        return PTR_ERR(file);
    if (i_size_read(file_inode(file)) != bytes) {
        filp_close(file, NULL);
        return -EINVAL;
    }
    n = kernel_read(file, records, bytes, &pos);
    filp_close(file, NULL);
    if (n < 0)
        return n;
    if (n != bytes)
        return -EIO;
    for (u32 id = 0; id < mmap_ids; id++) {
        const struct mmapger_record *r = &records[id];

        if (r->memory_type & ~(MEMTYPE_MIU1 | MEMTYPE_UNCACHE) || r->available > r->addr ||
            r->gap_chk > r->addr - r->available || (u64)r->addr + r->len > miu_len)
            return -EINVAL;
    }
    /* Overlapping and nested firmware regions are aliases in the same sparse
     * physical store, never separately allocated buffers. */
    return 0;
}

static int export_mmapger_layout(void)
{
    if (!mmap_u64)
        return 0;
    records64 = kcalloc(mmap_ids, sizeof(*records64), GFP_KERNEL);
    if (!records64)
        return -ENOMEM;
    for (u32 id = 0; id < mmap_ids; id++) {
        const struct mmapger_record *r = &records[id];
        records64[id] = (struct mmapger_record64){
            .available = r->available,
            .addr = r->addr,
            .gap_chk = r->gap_chk,
            .len = r->len,
            .memory_type = r->memory_type,
        };
    }
    return 0;
}

int mmapger_layout(void)
{
    u64 off[2] = {0, 0};
    u32 *nested __free(kfree) = NULL, *nest_off __free(kfree) = NULL;

    if (!miu_interval || !miu_len || miu_len > miu_interval || !miu0_bus || !miu1_bus ||
        !mmap_ids || (!mmap_file && !mmap_slot) || mmap_slot % PAGE_SIZE || lx_mem_id >= mmap_ids ||
        lx_mem2_id >= mmap_ids || gop_fb >= (int)mmap_ids)
        return -EINVAL;
    if (mmap_file && (mmap_slot || mmap_len_count || mmap_in_count))
        return -EINVAL;
    /* with a TEE, mmap.ini names the records: SECURE_SHM must be the one LG's names say */
    if (strcmp(mmap_names, "m3") && strcmp(mmap_names, "lm18a"))
        return -EINVAL;
    if (secure_shm_id >= 0 && (!mmapger_name(mmap_ids - 1) || secure_shm_id >= mmap_ids ||
                               strcmp(mmapger_name(secure_shm_id), "SECURE_SHM")))
        return -EINVAL;
    records = kcalloc(mmap_ids, sizeof(*records), GFP_KERNEL);
    nested = kcalloc(mmap_ids, sizeof(*nested), GFP_KERNEL); /* parent + 1, 0: none */
    nest_off = kcalloc(mmap_ids, sizeof(*nest_off), GFP_KERNEL);
    if (!records || !nested || !nest_off)
        return -ENOMEM;
    if (mmap_file) {
        int err = mmapger_file_layout();

        if (err)
            return err;
        miu_info = (struct mmapger_info){
            .dram_len = 2 * miu_len,
            .len0 = miu_len,
            .len1 = miu_len,
            .interval = miu_interval,
            .interval2 = miu_interval2,
        };
        return export_mmapger_layout();
    }
    for (u32 id = 0; id < mmap_ids; id++)
        records[id].len = mmap_slot;
    for (int i = 0; i < mmap_len_count; i++) {
        u32 v[2];

        if (lgemu_parse_u32s(mmap_len[i], 0, v, 2) || v[0] >= mmap_ids || !v[1] ||
            v[1] % PAGE_SIZE) {
            pr_err("mmap_len=%s: not id:bytes (pages)\n", mmap_len[i]);
            return -EINVAL;
        }
        records[v[0]].len = v[1];
    }
    for (int i = 0; i < mmap_in_count; i++) {
        u32 v[3];

        if (lgemu_parse_u32s(mmap_in[i], 0, v, 3) || v[0] >= mmap_ids || v[1] >= mmap_ids ||
            v[0] == v[1] || v[2] % PAGE_SIZE) {
            pr_err("mmap_in=%s: not child:parent:offset (pages)\n", mmap_in[i]);
            return -EINVAL;
        }
        nested[v[0]] = v[1] + 1;
        nest_off[v[0]] = v[2];
    }
    for (u32 id = 0, miu1 = 0; id < mmap_ids; id++) {
        if (nested[id])
            continue;
        miu1 |= off[0] + records[id].len > miu_len;

        if (off[miu1] + records[id].len > miu_len)
            return -EINVAL;
        records[id].available = records[id].addr = off[miu1];
        records[id].memory_type = (miu1 ? MEMTYPE_MIU1 : 0) | MEMTYPE_UNCACHE;
        off[miu1] += records[id].len;
    }
    for (u32 id = 0; id < mmap_ids; id++) {
        u32 parent = nested[id] - 1;

        if (!nested[id])
            continue;
        if (nested[parent] || nest_off[id] + (u64)records[id].len > records[parent].len) {
            pr_err("mmap_in: record %u does not fit in record %u\n", id, parent);
            return -EINVAL;
        }
        records[id].available = records[id].addr = records[parent].addr + nest_off[id];
        records[id].memory_type = records[parent].memory_type;
    }
    miu_info = (struct mmapger_info){
        .dram_len = 2 * miu_len,
        .len0 = miu_len,
        .len1 = miu_len,
        .interval = miu_interval,
        .interval2 = miu_interval2,
    };
    return export_mmapger_layout();
}

u64 record_pa(u32 id)
{
    return records[id].addr + ((records[id].memory_type & MEMTYPE_MIU1) ? miu_interval : 0);
}

/* the ARM bus address of a record, as SYS_INFO reports it */
static u32 record_bus(u32 id)
{
    return records[id].addr + ((records[id].memory_type & MEMTYPE_MIU1) ? miu1_bus : miu0_bus);
}

static struct lgemu_store phys_mem = LGEMU_STORE_INIT(phys_mem); /* by PA */

/* ---- /dev/system (148): MStar's system driver ---- */

#define IOCTL_SYS_FLUSH_MEMORY _IO('S', 0x50)
#define IOCTL_SYS_READ_MEMORY  _IO('S', 0x51)
#define IOCTL_SYS_INFO         _IOWR('S', 0x99, struct sys_info)

/* IO_Sys_Info_t with CONFIG_MP_NEW_UTOPIA_32BIT: u64 fields, 0x90 bytes */
struct sys_info {
    u64 lx_mem_addr, lx_mem_length, lx_mem2_addr, lx_mem2_length;
    u64 emac_addr, emac_length, dram_addr, dram_length, bb_addr, bb_length;
    u64 mpool_mem_addr, mpool_mem_length, g3d_mem0_addr, g3d_mem0_length;
    u64 g3d_mem1_addr, g3d_mem1_length, g3d_cmdq_addr, g3d_cmdq_length;
};
static_assert(sizeof(struct sys_info) == 0x90);

/* The older IO_Sys_Info_t (0xc0485399: LM14, and W15U with utopia32): the same eighteen fields
 * as SYS_INFO, but 32-bit addresses and lengths rather than u64. */
struct sys_info32 {
    u32 lx_mem_addr, lx_mem_length, lx_mem2_addr, lx_mem2_length;
    u32 emac_addr, emac_length, dram_addr, dram_length, bb_addr, bb_length;
    u32 mpool_mem_addr, mpool_mem_length, g3d_mem0_addr, g3d_mem0_length;
    u32 g3d_mem1_addr, g3d_mem1_length, g3d_cmdq_addr, g3d_cmdq_length;
};
static_assert(sizeof(struct sys_info32) == 0x48);
#define IOCTL_SYS_INFO32 _IOWR('S', 0x99, struct sys_info32)

static long system_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    ioctl_count("system", cmd);
    /* MsOS_FlushMemory / MsOS_ReadMemory: the MIU's write-buffer and read-pipe flushes
     * (Chip_Flush_Memory, Chip_Read_Memory). They change nothing a caller can see, and the
     * emulated memory is coherent. */
    if (cmd == IOCTL_SYS_FLUSH_MEMORY || cmd == IOCTL_SYS_READ_MEMORY)
        return 0;
    if (cmd == IOCTL_SYS_INFO || ((lm14 || utopia32) && cmd == IOCTL_SYS_INFO32)) {
        struct sys_info info = {
            .lx_mem_addr = record_bus(lx_mem_id),
            .lx_mem_length = records[lx_mem_id].len,
            .lx_mem2_addr = (u32)(record_bus(lx_mem2_id) - lx_mem2_bias),
            .lx_mem2_length = records[lx_mem2_id].len,
            .dram_addr = miu0_bus,
            .dram_length = miu_info.dram_len,
        };
        if (cmd == IOCTL_SYS_INFO32) {
            struct sys_info32 legacy = {
                .lx_mem_addr = info.lx_mem_addr,
                .lx_mem_length = info.lx_mem_length,
                .lx_mem2_addr = info.lx_mem2_addr,
                .lx_mem2_length = info.lx_mem2_length,
                .dram_addr = info.dram_addr,
                .dram_length = info.dram_length,
            };
            return copy_to_user((void __user *)arg, &legacy, sizeof(legacy)) ? -EFAULT : 0;
        }
        return copy_to_user((void __user *)arg, &info, sizeof(info)) ? -EFAULT : 0;
    }
    return unhandled("system", cmd, 0);
}

/* ---- /dev/malloc and /dev/mpool (158): MStar's memory pool driver ---- */

#define MALLOC_IOC_MPOOL_INFO         _IOWR('M', 0x00, struct mpool_info)
#define MALLOC_IOC_FLUSHDCACHE        _IOR('M', 0x01, struct mpool_info)
#define MALLOC_IOC_MPOOL_CACHE        _IOR('M', 0x03, unsigned int)
#define MALLOC_IOC_MPOOL_SET          _IOR('M', 0x04, struct mpool_info)
#define MALLOC_IOC_MPOOL_KERNELDETECT _IOWR('M', 0x05, struct mpool_kernel_info)
#define MALLOC_IOC_FLUSHDCACHE_PAVA   _IOR('M', 0x07, struct mpool_flush_info32)
#define MALLOC_IOC_VERSION            _IOWR('M', 0x08, unsigned int)
#define MALLOC_IOC_FLUSHDCACHE_ALL    _IOR('M', 0x09, struct mpool_flush_info32)

struct mpool_info { /* DrvMPool_Info_t, u64 fields: the same in compat */
    u64 addr, size, interval;
    u8 miu_sel;
};
struct mpool_kernel_info { /* DrvMPool_Kernel_Info_t */
    u64 lx_addr, lx_size, lx2_addr, lx2_size;
};
struct mpool_flush_info32 { /* DrvMPool_Flush_Info_t from a 32-bit process */
    u32 virt;
    u64 phys, size;
};
/* the older structures, 32-bit fields (LM14; W15U with utopia32) */
struct mpool_info_u32 {
    u32 addr, size, interval;
    u8 miu_sel;
    u8 pad[3];
};
static_assert(sizeof(struct mpool_info_u32) == 16);
struct mpool_kernel_info_u32 {
    u32 lx_addr, lx_size, lx2_addr, lx2_size;
};
struct mpool_flush_info_u32 {
    u32 virt, phys, size;
};
#define MALLOC_IOC_MPOOL_SET_U32          _IOR('M', 0x04, struct mpool_info_u32)
#define MALLOC_IOC_MPOOL_KERNELDETECT_U32 _IOWR('M', 0x05, struct mpool_kernel_info_u32)
#define MALLOC_IOC_FLUSHDCACHE_PAVA_U32   _IOR('M', 0x07, struct mpool_flush_info_u32)
#define MALLOC_IOC_FLUSHDCACHE_ALL_U32    _IOR('M', 0x09, struct mpool_flush_info_u32)

struct mpool_file {
    struct mutex lock;
    bool set;
    u64 pa, size;
};

static int mpool_open(struct inode *inode, struct file *file)
{
    struct mpool_file *f = kzalloc(sizeof(*f), GFP_KERNEL);

    if (!f)
        return -ENOMEM;
    mutex_init(&f->lock);
    file->private_data = f;
    return 0;
}

static int mpool_release(struct inode *inode, struct file *file)
{
    kfree(file->private_data);
    return 0;
}

static long mpool_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    struct mpool_file *f = file->private_data;
    void __user *uarg = (void __user *)arg;

    ioctl_count("malloc", cmd);

    switch (cmd) {
    case MALLOC_IOC_MPOOL_INFO: { /* the whole of MIU0 */
        struct mpool_info i = {.size = miu_len, .interval = miu_interval};
        return copy_to_user(uarg, &i, sizeof(i)) ? -EFAULT : 0;
    }
    case MALLOC_IOC_MPOOL_SET:
    case MALLOC_IOC_MPOOL_SET_U32: { /* the next mmap: MIU miu_sel, offset addr */
        struct mpool_info i;

        if (cmd == MALLOC_IOC_MPOOL_SET_U32 && utopia32 && !lm14) {
            struct mpool_info_u32 legacy;

            /* W15U's MsOS_MPool_Mapping passes the offset in the MIU, as W16R's */
            if (copy_from_user(&legacy, uarg, sizeof(legacy)))
                return -EFAULT;
            i = (struct mpool_info){
                .addr = legacy.addr,
                .size = legacy.size,
                .interval = legacy.interval,
                .miu_sel = legacy.miu_sel,
            };
        } else if (cmd == MALLOC_IOC_MPOOL_SET_U32) {
            struct mpool_info_u32 legacy;

            if (!lm14)
                return unhandled("malloc", cmd, 0);
            if (copy_from_user(&legacy, uarg, sizeof(legacy)))
                return -EFAULT;
            /* LM14 libMsOS adds the MIU interval before this ioctl. */
            if (legacy.miu_sel > 1 || legacy.interval != miu_interval ||
                legacy.addr < legacy.miu_sel * miu_interval ||
                (u64)legacy.addr - legacy.miu_sel * miu_interval > miu_len ||
                legacy.size > miu_len - ((u64)legacy.addr - legacy.miu_sel * miu_interval))
                return -EINVAL;
            i = (struct mpool_info){
                .addr = legacy.addr - legacy.miu_sel * miu_interval,
                .size = legacy.size,
                .interval = legacy.interval,
                .miu_sel = legacy.miu_sel,
            };
        } else if (copy_from_user(&i, uarg, sizeof(i))) {
            return -EFAULT;
        }
        if (i.miu_sel > 1)
            return -EINVAL;
        mutex_lock(&f->lock);
        f->set = true;
        f->pa = i.addr + i.miu_sel * miu_interval;
        f->size = i.size;
        mutex_unlock(&f->lock);
        return 0;
    }
    case MALLOC_IOC_MPOOL_KERNELDETECT: {
        struct mpool_kernel_info i = {
            .lx_addr = record_pa(lx_mem_id),
            .lx_size = records[lx_mem_id].len,
            .lx2_addr = record_pa(lx_mem2_id),
            .lx2_size = records[lx_mem2_id].len,
        };
        return copy_to_user(uarg, &i, sizeof(i)) ? -EFAULT : 0;
    }
    case MALLOC_IOC_MPOOL_KERNELDETECT_U32: {
        struct mpool_kernel_info_u32 i = {
            .lx_addr = record_pa(lx_mem_id),
            .lx_size = records[lx_mem_id].len,
            .lx2_addr = record_pa(lx_mem2_id),
            .lx2_size = records[lx_mem2_id].len,
        };

        if (!utopia32)
            break;
        return copy_to_user(uarg, &i, sizeof(i)) ? -EFAULT : 0;
    }
    case MALLOC_IOC_FLUSHDCACHE_PAVA_U32:
    case MALLOC_IOC_FLUSHDCACHE_ALL_U32:
        if (!utopia32)
            break;
        return 0;
    case MALLOC_IOC_MPOOL_CACHE: /* cached or not: all of it is plain RAM here */
    case MALLOC_IOC_FLUSHDCACHE:
    case MALLOC_IOC_FLUSHDCACHE_PAVA:
    case MALLOC_IOC_FLUSHDCACHE_ALL:
        return 0;
    }
    return unhandled("malloc", cmd, 0);
}

static int mpool_mmap(struct file *file, struct vm_area_struct *vma)
{
    struct mpool_file *f = file->private_data;
    u64 pa;

    mutex_lock(&f->lock);
    pa = f->set ? f->pa : 0; /* without MPOOL_SET: the pool, from PA 0 */
    mutex_unlock(&f->lock);
    return lgemu_store_mmap(&phys_mem, vma, pa);
}

/* ---- /proc/mmapger: LG's memory manager's view of the layout ---- */

static ssize_t info_read(struct file *file, char __user *buf, size_t n, loff_t *pos)
{
    if (mmap_u64) {
        const struct mmapger_info64 info = {
            .dram_len = miu_info.dram_len,
            .len0 = miu_info.len0,
            .len1 = miu_info.len1,
            .len2 = miu_info.len2,
            .unused = miu_info.unused,
            .interval = miu_info.interval,
            .interval2 = miu_info.interval2,
        };
        return simple_read_from_buffer(buf, n, pos, &info, sizeof(info));
    }
    return simple_read_from_buffer(buf, n, pos, &miu_info, sizeof(miu_info));
}

static ssize_t mmap_read(struct file *file, char __user *buf, size_t n, loff_t *pos)
{
    if (mmap_u64)
        return simple_read_from_buffer(buf, n, pos, records64, mmap_ids * sizeof(*records64));
    return simple_read_from_buffer(buf, n, pos, records, mmap_ids * sizeof(*records));
}

/* mmap.ini: the records as LG's kernel prints them (mmapger.c ini_dump: its own MEMORY_TYPE
 * legend, bit 1 UNCACHE, bit 2 REMAPPING_TO_USER_SPACE). The TEE client (MmapParser) takes
 * SECURE_SHM's _ADR, _LEN and MIU0/MIU1 from it. */
static int mmap_ini_show(struct seq_file *m, void *v)
{
    seq_puts(m, "#define MIU0                    (0x000000)\n"
                "#define MIU1                    (0x000001)\n"
                "#define TYPE_NONE               (0x000000)\n"
                "#define UNCACHE                 (0x000002)\n"
                "#define REMAPPING_TO_USER_SPACE (0x000004)\n"
                "#define CACHE                   (0x000008)\n"
                "#define NONCACHE_BUFFERABLE     (0x000010)\n"
                "#define MIU2                    (0x000020)\n\n");
    for (u32 id = 0; id < mmap_ids; id++) {
        const struct mmapger_record *r = &records[id];
        const char *name = mmapger_name(id);
        u32 t = r->memory_type;

        seq_printf(m,
                   "#define %s_AVAILABLE 0x%08X\n#define %s_ADR 0x%08X\n"
                   "#define %s_GAP_CHK 0x%08X\n#define %s_LEN 0x%08X\n#define %s_MEMORY_TYPE %s",
                   name, r->available, name, r->addr, name, r->gap_chk, name, r->len, name,
                   t & 0x20  ? "(MIU2 | TYPE_NONE"
                   : t & 0x1 ? "(MIU1 | TYPE_NONE"
                             : "(MIU0 | TYPE_NONE");
        seq_printf(m, "%s%s%s%s)\n\n", t & 0x2 ? " | UNCACHE" : "",
                   t & 0x4 ? " | REMAPPING_TO_USER_SPACE" : "", t & 0x8 ? " | CACHE" : "",
                   t & 0x10 ? " | NONCACHE_BUFFERABLE" : "");
    }
    return 0;
}

static int mmap_ini_open(struct inode *inode, struct file *file)
{
    return single_open(file, mmap_ini_show, NULL);
}

const struct proc_ops mmap_ini_proc_ops = {
    .proc_open = mmap_ini_open,
    .proc_read = seq_read,
    .proc_lseek = seq_lseek,
    .proc_release = single_release,
};

const struct file_operations system_fops = {
    .owner = THIS_MODULE,
    .open = plain_open,
    .unlocked_ioctl = system_ioctl,
    .compat_ioctl = compat_ptr_ioctl,
};
const struct file_operations mpool_fops = {
    .owner = THIS_MODULE,
    .open = mpool_open,
    .release = mpool_release,
    .mmap = mpool_mmap,
    .unlocked_ioctl = mpool_ioctl,
    .compat_ioctl = compat_ptr_ioctl,
};
const struct proc_ops info_proc_ops = {.proc_read = info_read, .proc_lseek = default_llseek};
const struct proc_ops mmap_proc_ops = {.proc_read = mmap_read, .proc_lseek = default_llseek};
struct lgemu_store *memory_store(void)
{
    return &phys_mem;
}
u32 memory_record_count(void)
{
    return mmap_ids;
}
u32 memory_record_length(u32 id)
{
    return records[id].len;
}
int memory_gop_id(void)
{
    return gop_fb;
}
bool memory_has_tee(void)
{
    return secure_shm_id >= 0;
}
void memory_store_free(void)
{
    lgemu_store_free(&phys_mem);
}
void memory_layout_free(void)
{
    kfree(records64);
    records64 = NULL;
    kfree(records);
    records = NULL;
}
void memory_log_layout(void)
{
    pr_info("%u mmapger records of %u KiB (%d other lengths), MIU0 and MIU1 %llu MiB each\n",
            mmap_ids, mmap_slot >> 10, mmap_len_count, miu_len >> 20);
}
