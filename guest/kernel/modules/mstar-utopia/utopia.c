// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-only
#include "private.h"

/* ---- /proc/utopia: Utopia's kernel side ----
 *
 * MsOS_MPool_Mapping_Dynamic reports each mapping (the kernel maps nothing for itself here). And
 * UtopiaOpen(module | 0x80000000) runs a module in the kernel: SetMODULE on a /proc/utopia file of
 * its own, then IoctlMODULE (cmd, pArg) for each call and CloseMODULE. MStar's kernel runs the
 * module's code there (its *_adp layer copies pArg in and out per command). None of those modules
 * is emulated yet: a module opens, and each of its commands answers UTOPIA_STATUS_SUCCESS without
 * touching pArg, counted by module and command (reading /proc/utopia lists them), so a boot shows
 * which ones matter. */

#define UTOPIA_IOCTL_SetMODULE   _IOWR('U', 1, int)
#define UTOPIA_IOCTL_IoctlMODULE _IOWR('U', 3, int)
#define UTOPIA_IOCTL_CloseMODULE _IOWR('U', 4, int)
/* W21A corrects _IOC_SIZE to the actual AArch32 argument structures. The
 * older builds encode sizeof(int) while passing the same fields. */
#define UTOPIA_IOCTL_SetMODULE_12           _IOWR('U', 1, u32[3])
#define UTOPIA_IOCTL_IoctlMODULE_8          _IOWR('U', 3, u32[2])
#define UTOPIA_IOCTL_MODULE_DYNAMIC_MAPPING _IOWR('U', 5, struct kdrv_mpool_info32)

/* Utopia's module ids (the order of its module names, MODULE_UTOPIA = 0 ... MODULE_AUDIO = 45 in
 * W16R's libkadaptor; W18R's adds HDMIRX 43) and commands (UTPA2's apiDMX_v2.h) of the calls that
 * block in MStar's kernel */
#define UTOPIA_MODULE_DMX         29
#define MAPI_CMD_DMX_WAITTSPCBEVT 190 /* waits for a TSP interrupt */
#define MDRV_CMD_HDCP22_WAITEVENT 23  /* waits for an HDCP 2.2 interrupt */
/* the HDMI receiver's module: W18R's 43 (new on m3); W18A's 70-name table puts it at 49 */
static unsigned int hdmirx_module = 43;
module_param(hdmirx_module, uint, 0444);
MODULE_PARM_DESC(hdmirx_module, "Utopia's HDMIRX module id, whose HDCP 2.2 wait (command 23) "
                                "times out (W18R 43, W18A 49)");

/* XC (module 8), the scaler: W18R's KADP asks it two things in kernel mode that answer more than
 * a status (W18R's libkadaptor). MApi_XC_S_Disp_Ctrl (0x48c184) passes its command (0x158 on W18R)
 * an stXC_CMD_S_DISP_CTRL { u32 cmd; u32 pbuf; u32 size; MS_BOOL bReturn (u8 at +12) } and
 * returns bReturn; its sub-command 0x21 (MApi_SC_get_disp_de_window, 0x248aa8) reads the display's
 * DE window, an MS_WINDOW_TYPE { u16 x, y, width, height } of 8 bytes, which libhal_vt's
 * HAL_VT_GetVideoFrameOutputDeviceOutputInfo divides by (width, height: a zero window raised
 * SIGFPE in every tv.capture). MApi_XC_HDR_Control (0x48284c) passes its command (0x142)
 * { u32 ctrl; u32 param; u32 ret } and returns ret, E_APIXC_RET_OK 1 on success; its control 9
 * (CFD) is what Monitor_taskHdrSeamless calls every 16 ms, printing two lines when it fails.
 * The command numbers are the Utopia build's (W16R's 0x142 is MApi_XC_Set_FRC_InputTiming with
 * an 8-byte argument, its 0x158 MApi_XC_Offline_ADC_SetMode), hence parameters, 0 for none. */
/* AUDIO, the sound engine's OSD (master) volume: HAL_AUDIO_LGSE_SetFN009's variables
 * {level, volume} (level: LG's volume curve, 0x7F00 at volume 100, 0 silence; the user mute is
 * volume 0) go through MApi_AUDIO_ADVSOUND_SetParam2 as a kernel-mode command whose pArg is an
 * AUDIO_ST_ADVSOUND_PARAM2 (MStar's apiAUDIO.h) with a user pointer to them. W16R's (AUDIO module
 * 45, command 90): bSet (u8), the parameter's id (u32 at +4; LGSEFN009_VARIABLES 186), index (+8),
 * u32value1 (+0xc), u16value2 the count (+0x10). W18R's (module 48, command 91) has the parameter
 * as a string: bSet, index (+4), the count (u16 at +8), the parameter (a user pointer to
 * "LGSE_FN_009_VARIABLES", +0xc), u32value1 (+0x10). The level goes to lgemu's playback gain
 * (lgemu snd.c), the stand-in for the DSP's volume after the PCM. */
static char *adv_fn009;
module_param(adv_fn009, charp, 0444);
MODULE_PARM_DESC(adv_fn009,
                 "AUDIO's ADVSOUND SetParam2 of the OSD volume, module:command:parameter "
                 "(W16R 45:90:186, a number: the id layout; W18R 48:91:LGSE_FN_009_VARIABLES, a "
                 "string: its layout); unset: not decoded");
static u32 adv_module = ~0u, adv_cmd, adv_param_id;
static char adv_param_name[32];

int adv_fn009_parse(void)
{
    char rest[sizeof(adv_param_name)];

    if (!adv_fn009)
        return 0;
    if (sscanf(adv_fn009, "%u:%u:%31s", &adv_module, &adv_cmd, rest) != 3)
        return -EINVAL;
    if (kstrtou32(rest, 0, &adv_param_id))
        strscpy(adv_param_name, rest, sizeof(adv_param_name));
    return 0;
}

static void adv_fn009_decode(u32 parg)
{
    u8 st[20];
    u32 params, count, level;
    char name[sizeof(adv_param_name)];

    if (copy_from_user(st, compat_ptr(parg), sizeof(st)))
        return;
    if (adv_param_name[0]) {
        u32 pname = get_unaligned_le32(st + 0xc);

        if (strncpy_from_user(name, compat_ptr(pname), sizeof(name)) <= 0 ||
            strncmp(name, adv_param_name, sizeof(name)))
            return;
        count = get_unaligned_le16(st + 8);
        params = get_unaligned_le32(st + 0x10);
    } else {
        if (get_unaligned_le32(st + 4) != adv_param_id)
            return;
        params = get_unaligned_le32(st + 0xc);
        count = get_unaligned_le16(st + 0x10);
    }
    if (count >= 1 && !get_user(level, (u32 __user *)compat_ptr(params)))
        lgemu_snd_osd_level(level & 0xffff);
}

/* The digital sound outputs: what libhal's HAL_AUDIO_SPDIF_SetOutputType, ARC_SetOutputType and
 * HDMI_SetPortAudioReturnChannel send in kernel mode, kept in lgemu's /proc/lgemu/sound_out (lgemu
 * snd.c). The numbers are the Utopia build's, hence parameters (W18R's libkadaptor and
 * libhal_msd94bcg; W16R's: AUDIO 45, the same SetMode, SetAudioParam2 159, no SYSTEM_Control,
 * ARC by AUDIO's ARC_HWEN_BY_PORT 164 {u32 port, u8 enable}):
 *   - snd_spdif_mode, MApi_AUDIO_SPDIF_SetMode (W18R 48:70): AUDIO_ST_SPDIF_OP {u32 eType, u8
 *     bHWEN, u8 bMute, u8 mode at +6}, eType 2 the set; mode 0 PCM, 3 transcode (auto AAC),
 *     4 non-PCM (auto, half auto, forced AC-3). The S/PDIF and the ARC calls both send it, so it
 *     is the last of the two; "SPDIF SetMode";
 *   - snd_system_control, MApi_AUDIO_SYSTEM_Control (W18R 48:191): {u32 length, u32 return, a
 *     user pointer to "KEY=VALUE"}: SetSpdifOutputType and SetHdmiArcOutputType, kept under
 *     their key with the value 0 NONE, 1 AC3, 2 AC3P, 3 AAC (-1 another);
 *   - snd_param2, MApi_AUDIO_SetAudioParam2 (W18R 48:160:160,161): {u32 ret, u32 decoder, u32
 *     type at +8, u32 value at +12}, only the types listed (160 the optical half auto, 161 LGSE
 *     no processing with ARC); "SetAudioParam2 TYPE";
 *   - snd_arc, ARC on or off, module:command:offset of its enable byte (W18R XC's
 *     MDrv_HDMI_ARC_PINControl 8:490:4, {u32 port 0x50, u8 enable, u8 driving high}; W16R
 *     45:164:4); "ARC Enable". */
static char *snd_spdif_mode, *snd_system_control, *snd_param2, *snd_arc;
module_param(snd_spdif_mode, charp, 0444);
MODULE_PARM_DESC(snd_spdif_mode, "AUDIO's SPDIF_SetMode, module:command (W18R 48:70)");
module_param(snd_system_control, charp, 0444);
MODULE_PARM_DESC(snd_system_control, "AUDIO's SYSTEM_Control, module:command (W18R 48:191)");
module_param(snd_param2, charp, 0444);
MODULE_PARM_DESC(snd_param2,
                 "AUDIO's SetAudioParam2, module:command:type,... (W18R 48:160:160,161)");
module_param(snd_arc, charp, 0444);
MODULE_PARM_DESC(snd_arc, "the ARC enable, module:command:offset (W18R 8:490:4, W16R 45:164:4)");
#define SND_PARAM2_TYPES 4
static u32 spdif_mode_mc[2] = {~0u}, system_control_mc[2] = {~0u}, param2_mc[2] = {~0u},
           arc_mc[3] = {~0u}, param2_types[SND_PARAM2_TYPES];
static int param2_type_count;

int snd_out_parse(void)
{
    char rest[48], *p, *tok;
    int n;

    if ((snd_spdif_mode && lgemu_parse_u32s(snd_spdif_mode, 0, spdif_mode_mc, 2)) ||
        (snd_system_control && lgemu_parse_u32s(snd_system_control, 0, system_control_mc, 2)) ||
        (snd_arc && lgemu_parse_u32s(snd_arc, 0, arc_mc, 3)))
        return -EINVAL;
    if (!snd_param2)
        return 0;
    if (sscanf(snd_param2, "%u:%u:%n", &param2_mc[0], &param2_mc[1], &n) != 2 ||
        strscpy(rest, snd_param2 + n, sizeof(rest)) < 0)
        return -EINVAL;
    p = rest;
    while ((tok = strsep(&p, ",")) && *tok) {
        if (param2_type_count == SND_PARAM2_TYPES ||
            kstrtou32(tok, 0, &param2_types[param2_type_count++]))
            return -EINVAL;
    }
    return 0;
}

static void snd_out_decode(u32 module, u32 cmd, u32 parg)
{
    void __user *u = compat_ptr(parg);
    u8 st[16];
    long v;

    if (module == spdif_mode_mc[0] && cmd == spdif_mode_mc[1]) {
        if (!copy_from_user(st, u, 8) && get_unaligned_le32(st) == 2) {
            v = st[6];
            lgemu_snd_out_set("SPDIF SetMode", &v, 1);
        }
    } else if (module == system_control_mc[0] && cmd == system_control_mc[1]) {
        static const char *const values[] = {"NONE", "AC3", "AC3P", "AAC"};
        char text[48], *eq;

        if (copy_from_user(st, u, 12) ||
            strncpy_from_user(text, compat_ptr(get_unaligned_le32(st + 8)), sizeof(text)) <= 0)
            return;
        text[sizeof(text) - 1] = 0;
        eq = strchr(text, '=');
        if (!eq)
            return;
        *eq++ = 0;
        v = -1;
        for (size_t i = 0; i < ARRAY_SIZE(values); i++)
            if (!strcmp(eq, values[i]))
                v = i;
        lgemu_snd_out_set(text, &v, 1);
    } else if (module == param2_mc[0] && cmd == param2_mc[1]) {
        char name[32];
        u32 type;

        if (copy_from_user(st, u, 16))
            return;
        type = get_unaligned_le32(st + 8);
        for (int i = 0; i < param2_type_count; i++) {
            if (param2_types[i] != type)
                continue;
            v = get_unaligned_le32(st + 12);
            snprintf(name, sizeof(name), "SetAudioParam2 %u", type);
            lgemu_snd_out_set(name, &v, 1);
        }
    } else if (module == arc_mc[0] && cmd == arc_mc[1] && arc_mc[2] < sizeof(st)) {
        if (!copy_from_user(st, u, arc_mc[2] + 1)) {
            v = st[arc_mc[2]];
            lgemu_snd_out_set("ARC Enable", &v, 1);
        }
    }
}

/* AUDIO's DSP memory, where MStar's kernel-mode AUDIO answers from what it was given (W15U's
 * kdrv_lm15u.ko): MApi_AUDIO_SetDspBaseAddr (command 2: {u8 dsp, u32 0 at +4, u32 PA at +8}; KADP's
 * MAdp_MAD_InitializeModule passes DSP 2 and mmapger record 11's PA) sets HAL_AUDIO's base, and
 * MApi_AUDIO_GetCommAudioInfo (command 101: {u64 result, u32 info type at +8}) answers some info
 * types from it: HAL_MAD_GetCommInfo's 62, the DSP's common buffer, is the 16-byte aligned base +
 * 0x900000 (GetDspMadBaseAddr) + 0x160000, and 63, its size, the constant 0x20000. AU_BufInit
 * passes both to MAdp_MPool_PA2KSEG1, which asserts on an address outside every mapped record.
 * audio_info: "module:setbase:dsp:getinfo:" then the info types, comma-separated, each
 * "type+offset" (the base plus offset) or "type=value"; the others answer 0 as before. */
static char *audio_info;
module_param(audio_info, charp, 0444);
MODULE_PARM_DESC(audio_info, "AUDIO's DSP base and common info, module:setbase:dsp:getinfo:"
                             "type+offset,type=value,... (W15U 45:2:2:101:62+0xa60000,63=0x20000)");
#define AUDIO_INFO_MAX 8
static u32 ai_module = ~0u, ai_setbase, ai_dsp, ai_getinfo, ai_count;
static struct {
    u32 type, value;
    bool relative;
} ai_types[AUDIO_INFO_MAX];
static u32 ai_base;
static bool ai_base_set;

int audio_info_parse(void)
{
    char *s, *p, *tok;
    int n;

    if (!audio_info)
        return 0;
    if (sscanf(audio_info, "%u:%u:%u:%u:%n", &ai_module, &ai_setbase, &ai_dsp, &ai_getinfo, &n) !=
        4)
        return -EINVAL;
    s = kstrdup(audio_info + n, GFP_KERNEL);
    if (!s)
        return -ENOMEM;
    p = s;
    while ((tok = strsep(&p, ",")) && *tok) {
        char *op = strpbrk(tok, "+=");

        if (!op || ai_count == AUDIO_INFO_MAX)
            goto bad;
        ai_types[ai_count].relative = *op == '+';
        *op = 0;
        if (kstrtou32(tok, 0, &ai_types[ai_count].type) ||
            kstrtou32(op + 1, 0, &ai_types[ai_count].value))
            goto bad;
        ai_count++;
    }
    kfree(s);
    return 0;
bad:
    kfree(s);
    return -EINVAL;
}

static long audio_info_ioctl(u32 cmd, u32 uarg)
{
    u8 st[12];

    if (cmd == ai_setbase) {
        if (copy_from_user(st, compat_ptr(uarg), sizeof(st)))
            return -EFAULT;
        if (st[0] == ai_dsp) {
            ai_base = get_unaligned_le32(st + 8) & ~0xfu;
            ai_base_set = true;
        }
    } else if (cmd == ai_getinfo) {
        u32 type;

        if (get_user(type, (u32 __user *)compat_ptr(uarg + 8)))
            return -EFAULT;
        for (u32 i = 0; i < ai_count; i++) {
            u64 v = ai_types[i].value;

            if (ai_types[i].type != type)
                continue;
            if (ai_types[i].relative)
                v += ai_base_set ? ai_base : 0;
            if (copy_to_user(compat_ptr(uarg), &v, sizeof(v)))
                return -EFAULT;
            break;
        }
    }
    return UTOPIA_STATUS_SUCCESS;
}

static unsigned int xc_disp_ctrl;
module_param(xc_disp_ctrl, uint, 0444);
MODULE_PARM_DESC(xc_disp_ctrl, "XC's MApi_XC_S_Disp_Ctrl command (0: none)");
/* MApi_SC_get_disp_de_window's sub-command: W18R's 0x21, W18A's 0x22 */
static unsigned int xc_de_window = 0x21;
module_param(xc_de_window, uint, 0444);
MODULE_PARM_DESC(xc_de_window, "S_Disp_Ctrl's sub-command that reads the DE window (W18R 0x21, "
                               "W18A 0x22)");
static unsigned int xc_hdr_control;
module_param(xc_hdr_control, uint, 0444);
MODULE_PARM_DESC(xc_hdr_control, "XC's MApi_XC_HDR_Control command (0: none)");

#define UTOPIA_MODULE_XC        8
#define UTOPIA_MODULE_GPIO      42 /* gpio.c */
#define XC_HDR_CTRL_CFD_CONTROL 9
#define XC_PANEL_WIDTH          1920 /* the m2 and m3 boards' FHD panels */
#define XC_PANEL_HEIGHT         1080

struct xc_disp_ctrl32 {
    u32 cmd;
    u32 pbuf; /* a user pointer */
    u32 size;
    u8 ret; /* MS_BOOL bReturn */
    u8 pad[3];
};
static_assert(sizeof(struct xc_disp_ctrl32) == 16);

struct xc_hdr_control32 {
    u32 ctrl;
    u32 param; /* a user pointer */
    u32 ret;
};
static_assert(sizeof(struct xc_hdr_control32) == 12);

/* ---- XC's windows: the video plane under the OSD, for the host viewer ----
 *
 * The stock vendor HAL places tvservice's scaler windows (HAL_VSC_*: tv.display/setDisplayWindow,
 * the input region of setMediaVideoData) through KADP's scaler bridge, which ends in kernel-mode
 * XC commands. Decoded here, they are the windows lgemu.ko sends the viewer (lgemu_video_window,
 * include/webos-video.h), which draws the media stream there:
 *   MApi_XC_SetWindow(XC_SETWIN_INFO *, u32 InitDataLen, SCALER_WIN) for every input and output
 *       region: stXC_SET_WINDOW {u32 pInfo; u32 len; u32 eWindow; MS_BOOL bReturn (u8 at +12)},
 *       and pInfo's first 28 bytes are { INPUT_SOURCE_TYPE_t enInputSourceType; MS_WINDOW_TYPE
 *       stCapWin, stDispWin, stCropWin } with MS_WINDOW_TYPE { u16 x, y, width, height } (both
 *       builds; the struct is 124 bytes on W16R and 60, packed, on W18R). The display window is
 *       in the panel's pixels, the capture window the input region (the stream's size for media).
 *       The source the HAL connected (HAL_VSC_Connect: VDEC -> STORAGE or DTV, the second VDEC
 *       STORAGE2 or DTV2) comes with it. It answers TRUE, as MStar's XC does.
 *   MApi_XC_GenerateBlackVideo(MS_BOOL, SCALER_WIN) {u8 bEnable; u32 eWindow} and
 *       ..._ForBothWin(MS_BOOL) {u8 bEnable}: the window's video mute (HAL_VSC_SetWinBlank)
 *   MApi_XC_EnableMainWindow(MS_BOOL) {u8 bEnable}, MApi_XC_DisableSubWindow(),
 *       MApi_XC_EnableWindow(MS_BOOL, SCALER_WIN) {u8 bEnable; u32 eWindow}: a window closed
 *       (HAL_VSC_Close)
 * The command numbers are the Utopia build's (W16R 19, 52, 50, 245, 247, 193; W18R 20, 50, 48,
 * 236, 238, 185): xc_window, 0 for none. A window is shown with the stream of a VDEC source; a
 * window on any other input (the tuner's AVD, HDMI) has no picture here and is reported muted. */
enum {
    XCW_SET_WINDOW,
    XCW_BLACK,
    XCW_BLACK_BOTH,
    XCW_ENABLE_MAIN,
    XCW_DISABLE_SUB,
    XCW_ENABLE_WINDOW,
    XCW_COMMANDS
};
static unsigned int xc_window[XCW_COMMANDS];
static int xc_window_count;
module_param_array(xc_window, uint, &xc_window_count, 0444);
MODULE_PARM_DESC(xc_window,
                 "XC's window commands: SetWindow, GenerateBlackVideo, "
                 "GenerateBlackVideoForBothWin, EnableMainWindow, DisableSubWindow, EnableWindow "
                 "(0: none)");

/* MStar's INPUT_SOURCE_TYPE_t (apiXC.h): the MVOP sources, which a VDEC feeds */
#define XC_SOURCE_DTV      28
#define XC_SOURCE_STORAGE  34
#define XC_SOURCE_DTV2     37
#define XC_SOURCE_STORAGE2 38
#define XC_SOURCE_DTV3     39
#define VIDEO_INPUT_VDEC   3 /* include/webos-video.h's */

struct xc_set_window32 {
    u32 info; /* a user pointer to XC_SETWIN_INFO */
    u32 len;
    u32 window; /* SCALER_WIN: 0 MAIN, 1 SUB */
    u8 ret;     /* MS_BOOL bReturnValue */
    u8 pad[3];
};
static_assert(sizeof(struct xc_set_window32) == 16);

struct xc_win16 {
    u16 x, y, w, h;
};
struct xc_setwin_info32 { /* the first fields, the same in both builds */
    u32 source;
    struct xc_win16 cap, disp, crop;
};
static_assert(sizeof(struct xc_setwin_info32) == 28);

static DEFINE_MUTEX(xc_windows_lock);
static struct lgemu_video_window xc_windows[2];

static void xc_riu_update(void);

static void xc_window_update(unsigned int win, const struct lgemu_video_window *w, u32 event)
{
    xc_windows[win] = *w;
    lgemu_video_window(win, w, event);
    xc_riu_update();
}

static long xc_set_window(u32 uarg)
{
    struct xc_set_window32 a;
    struct xc_setwin_info32 info;
    struct lgemu_video_window w;
    u32 port = 0, event;
    bool vdec = true;
    u8 ok = 1;

    if (copy_from_user(&a, compat_ptr(uarg), sizeof(a)))
        return -EFAULT;
    if (a.window > 1 || a.len < sizeof(info) ||
        copy_from_user(&info, compat_ptr(a.info), sizeof(info)))
        return UTOPIA_STATUS_SUCCESS;
    switch (info.source) {
    case XC_SOURCE_DTV:
    case XC_SOURCE_STORAGE:
        port = 0;
        break;
    case XC_SOURCE_DTV2:
    case XC_SOURCE_STORAGE2:
        port = 1;
        break;
    case XC_SOURCE_DTV3:
        port = 2;
        break;
    default:
        vdec = false;
        break;
    }
    mutex_lock(&xc_windows_lock);
    w = xc_windows[a.window];
    if (!w.open || w.x != (s16)info.disp.x || w.y != (s16)info.disp.y || w.w != info.disp.w ||
        w.h != info.disp.h || w.width != info.cap.w || w.height != info.cap.h)
        pr_info("XC SetWindow %u: source %u cap %u,%u %ux%u disp %d,%d %ux%u crop %u,%u %ux%u\n",
                a.window, info.source, info.cap.x, info.cap.y, info.cap.w, info.cap.h,
                (s16)info.disp.x, (s16)info.disp.y, info.disp.w, info.disp.h, info.crop.x,
                info.crop.y, info.crop.w, info.crop.h);
    event = !w.open                                ? WEBOS_VIDEO_REGISTER
            : !w.connected || w.input_port != port ? WEBOS_VIDEO_CONNECT
                                                   : WEBOS_VIDEO_RECT;
    w.open = w.connected = w.has_rect = true;
    /* the HAL's input type: VDEC for the MVOP's sources; the others have no picture here */
    w.input = vdec ? VIDEO_INPUT_VDEC : 0;
    w.input_port = vdec ? port : info.source;
    w.x = info.disp.x;
    w.y = info.disp.y;
    w.w = info.disp.w;
    w.h = info.disp.h;
    w.width = info.cap.w;
    w.height = info.cap.h;
    xc_window_update(a.window, &w, event);
    mutex_unlock(&xc_windows_lock);
    if (copy_to_user(compat_ptr(uarg + offsetof(struct xc_set_window32, ret)), &ok, 1))
        return -EFAULT;
    return UTOPIA_STATUS_SUCCESS;
}

/* XC's register read, MApi_XC_R2BYTEMSK {u32 reg; u16 mask; u16 ret}, of the one register the
 * window path reads back: the VOP's screen control (REG_SC_BK10_19, flat 0x1032), whose bits 1 and
 * 5 are the main and sub windows' black screen that GenerateBlackVideo sets. KADP's unblank
 * (mbrg_XC_IsBlackVideoEnable in Monitor_taskMute's worker) goes on only while the window reads
 * back as black: with 0 it never lifted the video mute. xc_r2bytemsk: the command (W18R 202),
 * 0 for none. */
static unsigned int xc_r2bytemsk;
module_param(xc_r2bytemsk, uint, 0444);
MODULE_PARM_DESC(xc_r2bytemsk, "XC's MApi_XC_R2BYTEMSK command (0: none)");
#define XC_REG_VOP_SCREEN_CONTROL 0x1032
#define XC_BLACK_SCREEN_MAIN      0x0002
#define XC_BLACK_SCREEN_SUB       0x0020

static long xc_read_register(u32 uarg)
{
    struct {
        u32 reg;
        u16 mask, ret;
    } a;
    u16 v;

    if (copy_from_user(&a, compat_ptr(uarg), sizeof(a)))
        return -EFAULT;
    if (a.reg != XC_REG_VOP_SCREEN_CONTROL)
        return UTOPIA_STATUS_SUCCESS;
    mutex_lock(&xc_windows_lock);
    v = (xc_windows[0].mute ? XC_BLACK_SCREEN_MAIN : 0) |
        (xc_windows[1].mute ? XC_BLACK_SCREEN_SUB : 0);
    mutex_unlock(&xc_windows_lock);
    v &= a.mask;
    if (copy_to_user(compat_ptr(uarg + offsetof(typeof(a), ret)), &v, sizeof(v)))
        return -EFAULT;
    return UTOPIA_STATUS_SUCCESS;
}

/* XC's registers where a user-mode XC instance reads them (W16R's KADP: its window calls run
 * user-mode and reach the module only through the libkadaptor shim,
 * guest/layers/soc/src/libkadaptor), in the register store, kept as the scaler would have them for
 * the windows decoded here: REG_SC_BK10_19 (0x1032), the VOP screen control: the black screen, bits
 * 1 (main) and 5 (sub); REG_SC_BK02_3F and _BK04_3F (0x027e, 0x047e), the main and sub windows' VSD
 * line counters, which count the input's lines while the window has a source (here its capture
 * height): KADP's unblank waits in MDrv_PQ_UpdateBuffer until the main one passes 19, and at 0 it
 *       timed out twice, 4 s each;
 *   REG_SC_BK01_1E and _BK03_1E (0x013c, 0x033c), the main and sub IP sync flags: the input
 *       vsync (bit 2) and the output vsync (bit 0, low while it lasts) change every tick, as the
 *       scaler's frames go by (MDrv_XC_wait_input_vsync and _output_vsync count their edges, for
 *       up to 200 ms a call, around every window enable, freeze and double-buffer switch).
 * (The menu load engine's busy bit, REG_SC_BK1F_02 bit 15, which MDrv_XC_MLoad_Fire sets and
 * then waits on, sleeping 1 ms a poll for up to 600 ms, clears itself: reg_autoclear.)
 * xc_riu: the RIU base and XC's flat register base ("0x1f000000:0x130000"; register r at
 * base + 2 (flat + r)), unset for none. */
static char *xc_riu;
module_param(xc_riu, charp, 0444);
MODULE_PARM_DESC(xc_riu,
                 "XC's registers in the register store, RIU base:XC flat base (unset: none)");
#define XC_REG_VSD_LINES_MAIN 0x027e
#define XC_REG_VSD_LINES_SUB  0x047e
#define XC_REG_SYNC_FLAG_MAIN 0x013c
#define XC_REG_SYNC_FLAG_SUB  0x033c
#define XC_SYNC_OUTPUT_VSYNC  0x0001
#define XC_SYNC_INPUT_VSYNC   0x0004

static u16 *xc_riu_reg(u32 reg)
{
    static u32 v[2];
    static int parsed; /* 1 ok, -1 bad */

    if (!xc_riu)
        return NULL;
    if (!parsed)
        parsed = lgemu_parse_u32s(xc_riu, 16, v, 2) ? -1 : 1;
    return parsed > 0 ? lgemu_store_ptr(mmio_store(), v[0] + 2ULL * (v[1] + reg)) : NULL;
}

/* every tick (autoclear_run): the vsync flags change */
void xc_riu_tick(void)
{
    static bool phase;
    const u32 flags[2] = {XC_REG_SYNC_FLAG_MAIN, XC_REG_SYNC_FLAG_SUB};

    if (!xc_riu)
        return;
    phase = !phase;
    for (int i = 0; i < 2; i++) {
        u16 *f = xc_riu_reg(flags[i]);

        if (f)
            WRITE_ONCE(*f, (READ_ONCE(*f) & ~(XC_SYNC_OUTPUT_VSYNC | XC_SYNC_INPUT_VSYNC)) |
                               (phase ? XC_SYNC_INPUT_VSYNC : XC_SYNC_OUTPUT_VSYNC));
    }
}

/* the windows' state into XC's registers (xc_windows_lock held) */
static void xc_riu_update(void)
{
    u16 *screen = xc_riu_reg(XC_REG_VOP_SCREEN_CONTROL);
    const u32 vsd[2] = {XC_REG_VSD_LINES_MAIN, XC_REG_VSD_LINES_SUB};

    if (!screen)
        return;
    WRITE_ONCE(*screen, (READ_ONCE(*screen) & ~(XC_BLACK_SCREEN_MAIN | XC_BLACK_SCREEN_SUB)) |
                            (xc_windows[0].mute ? XC_BLACK_SCREEN_MAIN : 0) |
                            (xc_windows[1].mute ? XC_BLACK_SCREEN_SUB : 0));
    for (int i = 0; i < 2; i++) {
        u16 *lines = xc_riu_reg(vsd[i]);

        if (lines)
            WRITE_ONCE(*lines, xc_windows[i].open && xc_windows[i].connected
                                   ? (u16)min_t(u32, xc_windows[i].height, 0x7ff)
                                   : 0);
    }
}

/* GenerateBlackVideo (window: 0, 1; 2 both), EnableMainWindow, DisableSubWindow, EnableWindow */
static long xc_window_state(int which, u32 uarg)
{
    u8 a[8] = {0};
    u32 window;

    if (which != XCW_DISABLE_SUB &&
        copy_from_user(a, compat_ptr(uarg),
                       which == XCW_BLACK || which == XCW_ENABLE_WINDOW ? 8 : 1))
        return -EFAULT;
    window = which == XCW_BLACK || which == XCW_ENABLE_WINDOW ? get_unaligned_le32(a + 4)
             : which == XCW_BLACK_BOTH                        ? 2
             : which == XCW_DISABLE_SUB                       ? 1
                                                              : 0;
    mutex_lock(&xc_windows_lock);
    for (unsigned int i = 0; i < 2; i++) {
        struct lgemu_video_window w = xc_windows[i];

        if (window != 2 && window != i)
            continue;
        if (which == XCW_BLACK || which == XCW_BLACK_BOTH) {
            if (w.mute != (a[0] != 0))
                pr_info("XC black video %u: %s\n", i, a[0] ? "on" : "off");
            w.mute = a[0] != 0;
            if (w.open)
                xc_window_update(i, &w, WEBOS_VIDEO_MUTE);
            else
                xc_windows[i] = w;
            xc_riu_update();
        } else if (which == XCW_DISABLE_SUB || !a[0]) {
            memset(&w, 0, sizeof(w));
            w.mute = xc_windows[i].mute;
            if (xc_windows[i].open)
                xc_window_update(i, &w, WEBOS_VIDEO_UNREGISTER);
        }
    }
    mutex_unlock(&xc_windows_lock);
    return UTOPIA_STATUS_SUCCESS;
}

/* XC's S_Disp_Ctrl and HDR_Control for the sub-commands above, and its windows; the others keep
 * the plain success (their result field untouched: FALSE). */
static long xc_ioctl(u32 cmd, u32 uarg)
{
    for (int i = 0; i < xc_window_count; i++)
        if (xc_window[i] && cmd == xc_window[i])
            return i == XCW_SET_WINDOW ? xc_set_window(uarg) : xc_window_state(i, uarg);
    if (xc_r2bytemsk && cmd == xc_r2bytemsk)
        return xc_read_register(uarg);
    if (xc_disp_ctrl && cmd == xc_disp_ctrl) {
        struct xc_disp_ctrl32 a;
        const u16 de[4] = {0, 0, XC_PANEL_WIDTH, XC_PANEL_HEIGHT};
        u8 ok = 1;

        if (copy_from_user(&a, compat_ptr(uarg), sizeof(a)))
            return -EFAULT;
        if (a.cmd != xc_de_window || a.size < sizeof(de))
            return UTOPIA_STATUS_SUCCESS;
        if (copy_to_user(compat_ptr(a.pbuf), de, sizeof(de)) ||
            copy_to_user(compat_ptr(uarg + offsetof(struct xc_disp_ctrl32, ret)), &ok, 1))
            return -EFAULT;
    } else if (xc_hdr_control && cmd == xc_hdr_control) {
        struct xc_hdr_control32 a;
        u32 ok = 1;

        if (copy_from_user(&a, compat_ptr(uarg), sizeof(a)))
            return -EFAULT;
        if (a.ctrl == XC_HDR_CTRL_CFD_CONTROL &&
            copy_to_user(compat_ptr(uarg + offsetof(struct xc_hdr_control32, ret)), &ok, 4))
            return -EFAULT;
    }
    return UTOPIA_STATUS_SUCCESS;
}

/* ---- PNL (Utopia module 12): the panel's active area ----
 *
 * KADP's panel interface (g_IPanel: devPanel_HStart, _VStart, _WIDTH, _HEIGHT) asks the kernel-mode
 * PNL module for the panel's DE start and size, each a u16 the caller zeroes and passes by pointer
 * (E_PNL_CMD_GETHSTART, GETVSTART, GETWIDTH, GETHEIGHT: 70-73 in W18R's build). The vendor HAL
 * clamps every output region to them (W18R's HAL_VSC_SetOutputRegion: x = width - w when x + w
 * passes the width): with 0 the windows came out at -w, -h. The board's FHD panel: DE start 0,0,
 * 1920x1080, as XC's DE window above. pnl_area: the four commands, 0 for none. */
#define UTOPIA_MODULE_PNL 12
static unsigned int pnl_area[4];
static int pnl_area_count;
module_param_array(pnl_area, uint, &pnl_area_count, 0444);
MODULE_PARM_DESC(pnl_area, "PNL's GetHStart, GetVStart, GetWidth, GetHeight commands (0: none)");

static long pnl_ioctl(u32 cmd, u32 uarg)
{
    static const u16 area[4] = {0, 0, XC_PANEL_WIDTH, XC_PANEL_HEIGHT};

    for (int i = 0; i < pnl_area_count; i++)
        if (pnl_area[i] && cmd == pnl_area[i])
            return copy_to_user(compat_ptr(uarg), &area[i], sizeof(area[i]))
                       ? -EFAULT
                       : UTOPIA_STATUS_SUCCESS;
    return UTOPIA_STATUS_SUCCESS;
}

struct kdrv_mpool_info32 { /* KDrvMPool_Info_t from a 32-bit process, 20 bytes */
    u32 w[5];
};

static int utopia_show(char *buf, size_t size, const struct lgemu_count *e)
{
    return scnprintf(buf, size, "module %u command %u: %lu\n", e->a, e->b, e->count);
}

DEFINE_LGEMU_COUNTS(utopia_counts, 256, 40, utopia_show); /* (NULL, module, command) */

static ssize_t utopia_read(struct file *file, char __user *buf, size_t n, loff_t *pos)
{
    return lgemu_counts_read(&utopia_counts, buf, n, pos);
}

static int utopia_open(struct inode *inode, struct file *file)
{
    file->private_data = (void *)-1L; /* the module: none set */
    return 0;
}

static long utopia_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    void __user *uarg = (void __user *)arg;
    long module = (long)file->private_data;
    u32 w[2];

    ioctl_count("utopia", cmd);

    switch (cmd) {
    case UTOPIA_IOCTL_MODULE_DYNAMIC_MAPPING:
        return UTOPIA_STATUS_SUCCESS;
    case UTOPIA_IOCTL_SetMODULE_12:
    case UTOPIA_IOCTL_SetMODULE: /* UTOPIA_DDI_OPEN_ARG: module id, version, attributes */
        if (copy_from_user(w, uarg, sizeof(w[0])))
            return -EFAULT;
        file->private_data = (void *)(long)w[0];
        pr_info("utopia: module %u opened in kernel mode (not emulated)\n", w[0]);
        return UTOPIA_STATUS_SUCCESS;
    case UTOPIA_IOCTL_IoctlMODULE_8:
    case UTOPIA_IOCTL_IoctlMODULE: /* UTOPIA_DDI_IOCTL_ARG: command, pointer */
        if (module < 0)
            return UTOPIA_STATUS_FAIL;
        if (copy_from_user(w, uarg, sizeof(w)))
            return -EFAULT;
        lgemu_count(&utopia_counts, NULL, module, w[0]);
        if (module == UTOPIA_MODULE_HWI2C &&
            (w[0] == MDRV_CMD_HWI2C_READBYTES_CUST || w[0] == MDRV_CMD_HWI2C_WRITEBYTES_CUST)) {
            long ret = hwi2c_customer(w[0], w[1]);

            if (ret <= 0)
                return ret ? ret : UTOPIA_STATUS_SUCCESS;
        }
        /* No demux interrupt comes: the wait times out (the DMX_TSP_Task thread calls it in a
         * loop, which answering at once turns into a spin). */
        if (module == UTOPIA_MODULE_DMX && w[0] == MAPI_CMD_DMX_WAITTSPCBEVT) {
            msleep_interruptible(1000);
            return UTOPIA_STATUS_FAIL;
        }
        /* No HDCP 2.2 interrupt comes either (no HDMI source): the wait times out. Answering at
         * once turns tvservice's "HDCP int task" (libkadaptor's _mdrv_HDCP_InterruptTask: cmd 23,
         * then cmd 22 for each event bit) into a spin at millions of calls a second. Success is
         * the timeout's answer: the event-bits byte the caller reads stays 0, as MStar's
         * mdrv_HDCP22_WaitEvent answers when nothing fired (no error print either). */
        if (module == hdmirx_module && w[0] == MDRV_CMD_HDCP22_WAITEVENT) {
            msleep_interruptible(1000);
            return UTOPIA_STATUS_SUCCESS;
        }
        snd_out_decode(module, w[0], w[1]);
        if (module == UTOPIA_MODULE_XC)
            return xc_ioctl(w[0], w[1]);
        if (module == UTOPIA_MODULE_PNL)
            return pnl_ioctl(w[0], w[1]);
        if (module == adv_module && w[0] == adv_cmd)
            adv_fn009_decode(w[1]);
        if (module == UTOPIA_MODULE_GPIO && gpio_enabled())
            return gpio_ioctl(w[0], w[1]);
        if (module == ai_module)
            return audio_info_ioctl(w[0], w[1]);
        return UTOPIA_STATUS_SUCCESS;
    case UTOPIA_IOCTL_CloseMODULE:
        return UTOPIA_STATUS_SUCCESS;
    }
    return unhandled("utopia", cmd, -ENOTTY);
}

const struct proc_ops utopia_proc_ops = {
    .proc_open = utopia_open,
    .proc_read = utopia_read,
    .proc_ioctl = utopia_ioctl,
#ifdef CONFIG_COMPAT
    .proc_compat_ioctl = utopia_ioctl,
#endif
};

/* /proc/MsOS/PMLog (msos_pmlog): the stock kdriver's KADP debug log (W18R's kdrv_msd93xbg.ko;
 * GPL kdriver/mxlib/utopia_core/linux_kernel/utopia.c, MSOS_PMLOG_Debug_Proc_*): 0666, a write's
 * first 127 bytes to the kernel log, a read prints the buffer's size and gives 128. KADP's
 * oger_dbg_print and UtopiaPrintk open it to write and say so on stderr when they cannot.
 * W16R's kdriver has none. */
static bool msos_pmlog;
module_param(msos_pmlog, bool, 0444);
MODULE_PARM_DESC(msos_pmlog, "/proc/MsOS/PMLog, the stock kdriver's debug log (M3)");
#define PMLOG_LEN 128

static ssize_t pmlog_write(struct file *f, const char __user *buf, size_t n, loff_t *pos)
{
    char s[PMLOG_LEN];
    size_t len = min_t(size_t, n, PMLOG_LEN - 1);

    if (copy_from_user(s, buf, len))
        return -EFAULT;
    s[len] = 0;
    pr_info("%s", s);
    return len;
}

static ssize_t pmlog_read(struct file *f, char __user *buf, size_t n, loff_t *pos)
{
    pr_info("MSTAR PM buffer size is :%d\n", PMLOG_LEN);
    return PMLOG_LEN;
}

const struct file_operations system_lg_fops = {
    .owner = THIS_MODULE,
    .open = plain_open,
    .unlocked_ioctl = system_lg_ioctl,
    .compat_ioctl = compat_ptr_ioctl,
};
const struct proc_ops pmlog_proc_ops = {.proc_read = pmlog_read, .proc_write = pmlog_write};
bool utopia_has_pmlog(void)
{
    return msos_pmlog;
}
bool utopia_has_xc_riu(void)
{
    return xc_riu != NULL;
}
void utopia_counts_init(void)
{
    utopia_counts.trace = diagnostics_utopia_trace();
}
/* The XC windows' panel, where this module reports them (xc_window); elsewhere (LM21) the windows
 * are lgemu.ko's V4L2 scaler's, in the panel space its vsc_screen gave. */
void utopia_screen_init(void)
{
    if (xc_window_count)
        lgemu_video_screen(XC_PANEL_WIDTH, XC_PANEL_HEIGHT);
}
