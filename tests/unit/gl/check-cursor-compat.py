#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Exercise genuine variable-size GBM cursor transport with a local DRM backend."""

import ctypes
import pathlib
import shlex
import struct
import subprocess
import sys
import tempfile
import time

root = pathlib.Path(__file__).resolve().parents[3]
with tempfile.TemporaryDirectory(prefix="beanjar-cursor-") as temp:
    temp = pathlib.Path(temp)
    mock = temp / "mock.c"
    mock.write_text(
        """#include <gbm.h>
#include <xf86drmMode.h>
#include <stdlib.h>
#include <errno.h>
struct gbm_bo {unsigned w,h;};
int map_failure;unsigned destroys,unmaps;
static unsigned char pixels[256*1040];
struct gbm_bo *gbm_bo_create(struct gbm_device*d,unsigned w,unsigned h,unsigned f,unsigned flags){
 (void)d;(void)f;(void)flags;struct gbm_bo*b=malloc(sizeof*b);b->w=w;b->h=h;return b;}
void gbm_bo_destroy(struct gbm_bo*b){destroys++;free(b);}
union gbm_bo_handle gbm_bo_get_handle(struct gbm_bo*b){(void)b;return (union gbm_bo_handle){.u32=44};}
int gbm_device_get_fd(struct gbm_device*d){(void)d;return21;}
unsigned gbm_bo_get_width(struct gbm_bo*b){return b->w;}
unsigned gbm_bo_get_height(struct gbm_bo*b){return b->h;}
unsigned gbm_bo_get_format(struct gbm_bo*b){(void)b;return GBM_FORMAT_ARGB8888;}
void *gbm_bo_map(struct gbm_bo*b,unsigned x,unsigned y,unsigned w,unsigned h,unsigned flags,unsigned*stride,void**data){
 (void)b;(void)x;(void)y;if(map_failure){errno=EIO;return NULL;}
 if(flags!=GBM_BO_TRANSFER_READ){errno=EINVAL;return NULL;}
 *stride=w*4+12;*data=pixels;
 for(unsigned row=0;row<h;row++)for(unsigned col=0;col<w;col++){
 unsigned char*p=pixels+row**stride+col*4;p[0]=col;p[1]=row;p[2]=123;p[3]=255;}
 return pixels;}
void gbm_bo_unmap(struct gbm_bo*b,void*d){(void)b;(void)d;unmaps++;}
drmModeCrtc *drmModeGetCrtc(int fd,unsigned crtc){
 if(fd!=21||crtc!=37){errno=EINVAL;return NULL;}
 drmModeCrtc*c=calloc(1,sizeof*c);c->width=1920;c->height=1080;return c;}
void drmModeFreeCrtc(drmModeCrtc*c){free(c);}
""".replace("return21", "return 21")
    )
    result = subprocess.run(
        ["pkg-config", "--cflags", "gbm", "libdrm"], capture_output=True, text=True
    )
    if result.returncode:
        print("SKIP: GBM/libdrm development headers unavailable")
        sys.exit(77)
    includes = shlex.split(result.stdout)
    subprocess.run(
        [
            "cc",
            "-shared",
            "-fPIC",
            "-Wl,-soname,libgbm.so.1",
            *includes,
            str(mock),
            "-o",
            str(temp / "libgbm.so.1"),
        ],
        check=True,
    )
    # Both API families are local, matching stock's backend loading.
    subprocess.run(
        [
            "cc",
            "-shared",
            "-fPIC",
            "-Wl,-soname,libdrm.so.2",
            *includes,
            str(mock),
            "-o",
            str(temp / "libdrm.so.2"),
        ],
        check=True,
    )
    port = temp / "cursor"
    subprocess.run(
        [
            "cc",
            "-shared",
            "-fPIC",
            "-Wall",
            "-Wextra",
            "-Werror",
            *includes,
            "-DCURSOR_TEST",
            f'-DCURSOR_PORT="{port}"',
            str(root / "guest/layers/gl/src/kms-cursor-compat.c"),
            "-ldl",
            "-lpthread",
            "-o",
            str(temp / "compat.so"),
        ],
        check=True,
    )
    gbm = ctypes.CDLL(str(temp / "libgbm.so.1"), mode=ctypes.RTLD_LOCAL)
    drm = ctypes.CDLL(str(temp / "libdrm.so.2"), mode=ctypes.RTLD_LOCAL)
    lib = ctypes.CDLL(str(temp / "compat.so"), use_errno=True)
    lib.gbm_bo_create.argtypes = [ctypes.c_void_p] + [ctypes.c_uint] * 4
    lib.gbm_bo_create.restype = ctypes.c_void_p
    lib.gbm_bo_destroy.argtypes = [ctypes.c_void_p]
    lib.drmModeSetCursor2.argtypes = [ctypes.c_int] + [ctypes.c_uint] * 4 + [ctypes.c_int] * 2
    lib.drmModeMoveCursor.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.c_int, ctypes.c_int]

    def packet():
        time.sleep(0.3)
        data = port.read_bytes()
        pos = 0
        last = None
        while pos + 56 <= len(data):
            fields = struct.unpack_from("<8sIIii8I", data, pos)
            length = fields[1]
            if pos + length > len(data):
                break
            last = (fields, data[pos + 56 : pos + length])
            pos += length
        assert last is not None
        return last

    # Root measured the native Spacebuns BO as 80x95 with hotspot 12,11.
    # Keep the earlier variable-size case alongside that live geometry.
    for w, h, hx, hy in ((60, 85, 9, 11), (80, 95, 12, 11)):
        bo = lib.gbm_bo_create(None, w, h, 0x34325241, 10)
        assert lib.drmModeSetCursor2(21, 37, 44, w, h, hx, hy) == 0
        # A late viewer receives the unchanged genuine state on heartbeat.
        if not port.exists():
            port.touch()
        fields, pixels = packet()
        assert fields[0] == b"WOSCUR1\0" and fields[2] == 1
        assert fields[5:] == (1920, 1080, w, h, w, h, hx, hy)
        assert len(pixels) == w * h * 4
        assert pixels[:4] == bytes((0, 0, 123, 255))
        assert pixels[-4:] == bytes((w - 1, h - 1, 123, 255))
        assert lib.drmModeMoveCursor(21, 37, -7, 799) == 0
        assert packet()[0][3:5] == (-7, 799)
        ctypes.c_int.in_dll(gbm, "map_failure").value = 1
        assert lib.drmModeSetCursor2(21, 37, 44, w, h, hx, hy) == -1 and ctypes.get_errno() == 5
        ctypes.c_int.in_dll(gbm, "map_failure").value = 0
        assert lib.drmModeSetCursor2(21, 37, 999, w, h, hx, hy) == -1 and ctypes.get_errno() == 2
        assert (
            lib.drmModeSetCursor2(21, 37, 44, w + 1, h, hx, hy) == -1 and ctypes.get_errno() == 22
        )
        assert lib.drmModeSetCursor2(21, 37, 0, 0, 0, 0, 0) == 0
        assert packet()[0][2] == 0
        assert lib.drmModeSetCursor2(21, 37, 44, w, h, hx, hy) == 0
        lib.gbm_bo_destroy(bo)
        assert packet()[0][2] == 0
        assert lib.drmModeSetCursor2(21, 37, 44, w, h, hx, hy) == -1
    assert ctypes.c_uint.in_dll(gbm, "destroys").value == 2
    assert ctypes.c_uint.in_dll(gbm, "unmaps").value == 4
    # Stop owned sender before its temporary port/library disappear.
    # dlclose executes the library destructor and joins the bounded sender.
    close = ctypes.CDLL(None).dlclose
    close.argtypes = [ctypes.c_void_p]
    assert close(lib._handle) == 0
print(
    "cursor transport: real pixels/stride, geometry, hotspot, position, visibility, failures, cleanup PASS"
)
