// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
/* The part of the OpenMAX IL 1.1.2 interface (Khronos' OMX_Core.h, OMX_Component.h, OMX_Index.h,
 * OMX_Audio.h, OMX_Video.h) the emulator's MStar core uses: the types and layouts of the 32-bit
 * ARM EABI (the 64-bit timestamp 8-aligned), the values of the indices, commands, states, events
 * and errors. Written out here, as the guest SDK has no IL headers. */
#ifndef OMX_IL_H
#define OMX_IL_H
#include <stdint.h>

typedef uint32_t OMX_U32;
typedef int32_t OMX_S32;
typedef uint8_t OMX_U8;
typedef int64_t OMX_TICKS;
typedef void *OMX_HANDLETYPE;
typedef void *OMX_PTR;
typedef char *OMX_STRING;
typedef uint32_t OMX_BOOL;
typedef uint32_t OMX_ERRORTYPE;
typedef uint32_t OMX_INDEXTYPE;
typedef uint32_t OMX_STATETYPE;
typedef uint32_t OMX_COMMANDTYPE;
typedef uint32_t OMX_EVENTTYPE;
typedef union {
    struct {
        uint8_t nVersionMajor, nVersionMinor, nRevision, nStep;
    } s;
    uint32_t nVersion;
} OMX_VERSIONTYPE;

/* OMX_ERRORTYPE */
#define OMX_ErrorNone                    0u
#define OMX_ErrorInsufficientResources   0x80001000u
#define OMX_ErrorUndefined               0x80001001u
#define OMX_ErrorInvalidComponentName    0x80001002u
#define OMX_ErrorComponentNotFound       0x80001003u
#define OMX_ErrorBadParameter            0x80001005u
#define OMX_ErrorNotImplemented          0x80001006u
#define OMX_ErrorUnsupportedIndex        0x8000101Au
#define OMX_ErrorBadPortIndex            0x8000101Bu
#define OMX_ErrorIncorrectStateOperation 0x80001018u
#define OMX_ErrorSameState               0x80001012u
#define OMX_ErrorTunnelingUnsupported    0x80001024u
#define OMX_ErrorNoMore                  0x8000100Eu

/* OMX_STATETYPE */
#define OMX_StateInvalid   0u
#define OMX_StateLoaded    1u
#define OMX_StateIdle      2u
#define OMX_StateExecuting 3u
#define OMX_StatePause     4u

/* OMX_COMMANDTYPE */
#define OMX_CommandStateSet    0u
#define OMX_CommandFlush       1u
#define OMX_CommandPortDisable 2u
#define OMX_CommandPortEnable  3u
#define OMX_CommandMarkBuffer  4u
#define OMX_ALL                0xFFFFFFFFu

/* OMX_EVENTTYPE */
#define OMX_EventCmdComplete         0u
#define OMX_EventError               1u
#define OMX_EventMark                2u
#define OMX_EventPortSettingsChanged 3u
#define OMX_EventBufferFlag          4u

/* nFlags */
#define OMX_BUFFERFLAG_EOS         0x00000001u
#define OMX_BUFFERFLAG_SYNCFRAME   0x00000020u
#define OMX_BUFFERFLAG_ENDOFFRAME  0x00000010u
#define OMX_BUFFERFLAG_CODECCONFIG 0x00000080u

/* OMX_INDEXTYPE */
#define OMX_IndexParamPortDefinition 0x02000001u
#define OMX_IndexParamAudioPcm       0x04000002u
#define OMX_IndexParamAudioAac       0x04000003u
#define OMX_IndexParamAudioMp3       0x04000005u

/* OMX_DIRTYPE, OMX_PORTDOMAINTYPE */
#define OMX_DirInput        0u
#define OMX_DirOutput       1u
#define OMX_PortDomainAudio 0u
#define OMX_PortDomainVideo 1u
#define OMX_PortDomainOther 3u

/* OMX_VIDEO_CODINGTYPE, OMX_AUDIO_CODINGTYPE (the values the client sets) */
#define OMX_VIDEO_CodingMPEG2      2u
#define OMX_VIDEO_CodingAVC        7u
#define OMX_AUDIO_CodingAutoDetect 1u
#define OMX_AUDIO_CodingPCM        2u
#define OMX_AUDIO_CodingAAC        21u
#define OMX_AUDIO_CodingMP3        22u

typedef struct OMX_BUFFERHEADERTYPE {
    OMX_U32 nSize;                       /* 0 */
    OMX_VERSIONTYPE nVersion;            /* 4 */
    OMX_U8 *pBuffer;                     /* 8 */
    OMX_U32 nAllocLen;                   /* 12 */
    OMX_U32 nFilledLen;                  /* 16 */
    OMX_U32 nOffset;                     /* 20 */
    OMX_PTR pAppPrivate;                 /* 24 */
    OMX_PTR pPlatformPrivate;            /* 28 */
    OMX_PTR pInputPortPrivate;           /* 32 */
    OMX_PTR pOutputPortPrivate;          /* 36 */
    OMX_HANDLETYPE hMarkTargetComponent; /* 40 */
    OMX_PTR pMarkData;                   /* 44 */
    OMX_U32 nTickCount;                  /* 48 */
    OMX_TICKS nTimeStamp;                /* 56 */
    OMX_U32 nFlags;                      /* 64 */
    OMX_U32 nOutputPortIndex;            /* 68 */
    OMX_U32 nInputPortIndex;             /* 72 */
} OMX_BUFFERHEADERTYPE;

typedef struct {
    OMX_ERRORTYPE (*EventHandler)(OMX_HANDLETYPE, OMX_PTR app, OMX_EVENTTYPE, OMX_U32 data1,
                                  OMX_U32 data2, OMX_PTR data);
    OMX_ERRORTYPE (*EmptyBufferDone)(OMX_HANDLETYPE, OMX_PTR app, OMX_BUFFERHEADERTYPE *);
    OMX_ERRORTYPE (*FillBufferDone)(OMX_HANDLETYPE, OMX_PTR app, OMX_BUFFERHEADERTYPE *);
} OMX_CALLBACKTYPE;

typedef struct OMX_COMPONENTTYPE {
    OMX_U32 nSize;               /* 0x00 */
    OMX_VERSIONTYPE nVersion;    /* 0x04 */
    OMX_PTR pComponentPrivate;   /* 0x08 */
    OMX_PTR pApplicationPrivate; /* 0x0c */
    OMX_ERRORTYPE (*GetComponentVersion)(OMX_HANDLETYPE, OMX_STRING, OMX_VERSIONTYPE *,
                                         OMX_VERSIONTYPE *, OMX_U8 (*)[128]);        /* 0x10 */
    OMX_ERRORTYPE (*SendCommand)(OMX_HANDLETYPE, OMX_COMMANDTYPE, OMX_U32, OMX_PTR); /* 0x14 */
    OMX_ERRORTYPE (*GetParameter)(OMX_HANDLETYPE, OMX_INDEXTYPE, OMX_PTR);           /* 0x18 */
    OMX_ERRORTYPE (*SetParameter)(OMX_HANDLETYPE, OMX_INDEXTYPE, OMX_PTR);           /* 0x1c */
    OMX_ERRORTYPE (*GetConfig)(OMX_HANDLETYPE, OMX_INDEXTYPE, OMX_PTR);              /* 0x20 */
    OMX_ERRORTYPE (*SetConfig)(OMX_HANDLETYPE, OMX_INDEXTYPE, OMX_PTR);              /* 0x24 */
    OMX_ERRORTYPE (*GetExtensionIndex)(OMX_HANDLETYPE, OMX_STRING, OMX_INDEXTYPE *); /* 0x28 */
    OMX_ERRORTYPE (*GetState)(OMX_HANDLETYPE, OMX_STATETYPE *);                      /* 0x2c */
    OMX_ERRORTYPE (*ComponentTunnelRequest)(OMX_HANDLETYPE, OMX_U32, OMX_HANDLETYPE, OMX_U32,
                                            void *); /* 0x30 */
    OMX_ERRORTYPE (*UseBuffer)(OMX_HANDLETYPE, OMX_BUFFERHEADERTYPE **, OMX_U32, OMX_PTR, OMX_U32,
                               OMX_U8 *); /* 0x34 */
    OMX_ERRORTYPE (*AllocateBuffer)(OMX_HANDLETYPE, OMX_BUFFERHEADERTYPE **, OMX_U32, OMX_PTR,
                                    OMX_U32);                                     /* 0x38 */
    OMX_ERRORTYPE (*FreeBuffer)(OMX_HANDLETYPE, OMX_U32, OMX_BUFFERHEADERTYPE *); /* 0x3c */
    OMX_ERRORTYPE (*EmptyThisBuffer)(OMX_HANDLETYPE, OMX_BUFFERHEADERTYPE *);     /* 0x40 */
    OMX_ERRORTYPE (*FillThisBuffer)(OMX_HANDLETYPE, OMX_BUFFERHEADERTYPE *);      /* 0x44 */
    OMX_ERRORTYPE (*SetCallbacks)(OMX_HANDLETYPE, OMX_CALLBACKTYPE *, OMX_PTR);   /* 0x48 */
    OMX_ERRORTYPE (*ComponentDeInit)(OMX_HANDLETYPE);                             /* 0x4c */
    OMX_ERRORTYPE (*UseEGLImage)(OMX_HANDLETYPE, OMX_BUFFERHEADERTYPE **, OMX_U32, OMX_PTR,
                                 void *);                                  /* 0x50 */
    OMX_ERRORTYPE (*ComponentRoleEnum)(OMX_HANDLETYPE, OMX_U8 *, OMX_U32); /* 0x54 */
} OMX_COMPONENTTYPE;

typedef struct {
    OMX_U32 nSize;              /* 0 */
    OMX_VERSIONTYPE nVersion;   /* 4 */
    OMX_U32 nPortIndex;         /* 8 */
    OMX_U32 eDir;               /* 12 */
    OMX_U32 nBufferCountActual; /* 16 */
    OMX_U32 nBufferCountMin;    /* 20 */
    OMX_U32 nBufferSize;        /* 24 */
    OMX_BOOL bEnabled;          /* 28 */
    OMX_BOOL bPopulated;        /* 32 */
    OMX_U32 eDomain;            /* 36 */
    union {                     /* 40 */
        struct {
            OMX_STRING cMIMEType;           /* 40 */
            OMX_PTR pNativeRender;          /* 44 */
            OMX_BOOL bFlagErrorConcealment; /* 48 */
            OMX_U32 eEncoding;              /* 52 */
        } audio;
        struct {
            OMX_STRING cMIMEType;           /* 40 */
            OMX_PTR pNativeRender;          /* 44 */
            OMX_U32 nFrameWidth;            /* 48 */
            OMX_U32 nFrameHeight;           /* 52 */
            OMX_S32 nStride;                /* 56 */
            OMX_U32 nSliceHeight;           /* 60 */
            OMX_U32 nBitrate;               /* 64 */
            OMX_U32 xFramerate;             /* 68 */
            OMX_BOOL bFlagErrorConcealment; /* 72 */
            OMX_U32 eCompressionFormat;     /* 76 */
            OMX_U32 eColorFormat;           /* 80 */
            OMX_PTR pNativeWindow;          /* 84 */
        } video;
    } format;
    OMX_BOOL bBuffersContiguous; /* 88 */
    OMX_U32 nBufferAlignment;    /* 92 */
} OMX_PARAM_PORTDEFINITIONTYPE;

typedef struct {
    OMX_U32 nSize;
    OMX_VERSIONTYPE nVersion;
    OMX_U32 nPortIndex;          /* 8 */
    OMX_U32 nChannels;           /* 12 */
    OMX_U32 eNumData;            /* 16 */
    OMX_U32 eEndian;             /* 20 */
    OMX_BOOL bInterleaved;       /* 24 */
    OMX_U32 nBitPerSample;       /* 28 */
    OMX_U32 nSamplingRate;       /* 32 */
    OMX_U32 ePCMMode;            /* 36 */
    OMX_U32 eChannelMapping[16]; /* 40 */
} OMX_AUDIO_PARAM_PCMMODETYPE;

typedef struct {
    OMX_U32 nSize;
    OMX_VERSIONTYPE nVersion;
    OMX_U32 nPortIndex;       /* 8 */
    OMX_U32 nChannels;        /* 12 */
    OMX_U32 nSampleRate;      /* 16 */
    OMX_U32 nBitRate;         /* 20 */
    OMX_U32 nAudioBandWidth;  /* 24 */
    OMX_U32 nFrameLength;     /* 28 */
    OMX_U32 nAACtools;        /* 32 */
    OMX_U32 nAACERtools;      /* 36 */
    OMX_U32 eAACProfile;      /* 40 */
    OMX_U32 eAACStreamFormat; /* 44 */
    OMX_U32 eChannelMode;     /* 48 */
} OMX_AUDIO_PARAM_AACPROFILETYPE;

typedef struct {
    OMX_U32 nSize;
    OMX_VERSIONTYPE nVersion;
    OMX_U32 nPortIndex;      /* 8 */
    OMX_U32 nChannels;       /* 12 */
    OMX_U32 nBitRate;        /* 16 */
    OMX_U32 nSampleRate;     /* 20 */
    OMX_U32 nAudioBandWidth; /* 24 */
    OMX_U32 eChannelMode;    /* 28 */
    OMX_U32 eFormat;         /* 32 */
} OMX_AUDIO_PARAM_MP3TYPE;

_Static_assert(sizeof(OMX_BUFFERHEADERTYPE) == 80, "buffer header");
_Static_assert(__builtin_offsetof(OMX_BUFFERHEADERTYPE, nTimeStamp) == 56, "nTimeStamp");
_Static_assert(__builtin_offsetof(OMX_BUFFERHEADERTYPE, nFlags) == 64, "nFlags");
_Static_assert(sizeof(OMX_PARAM_PORTDEFINITIONTYPE) == 96, "port definition");
_Static_assert(sizeof(OMX_AUDIO_PARAM_PCMMODETYPE) == 104, "PCM");
_Static_assert(sizeof(OMX_AUDIO_PARAM_AACPROFILETYPE) == 52, "AAC");
_Static_assert(sizeof(OMX_AUDIO_PARAM_MP3TYPE) == 36, "MP3");
_Static_assert(__builtin_offsetof(OMX_COMPONENTTYPE, FillThisBuffer) == 0x44, "component");

#endif
