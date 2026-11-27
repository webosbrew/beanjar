#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Test the real Qt companion against genuine Qt objects and a native-interface fixture.

Requires host Qt5Gui public and QPA development headers (BEANJAR_QPA_INCLUDE may
select a separately supplied, matching QPA header directory). No display server, IM
worker or DRM device is used; the guest still proves actual resource provenance.
"""

import os
import pathlib
import shlex
import subprocess
import tempfile

import pytest


def test_native_input_qt():
    ROOT = pathlib.Path(__file__).resolve().parents[3]

    def pkg(*args):
        return subprocess.check_output(["pkg-config", *args, "Qt5Gui"], text=True).strip()

    if subprocess.run(["pkg-config", "--exists", "Qt5Gui"]).returncode:
        pytest.skip("host Qt5Gui development files unavailable")
    qt_version = pkg("--modversion")
    qpa = pathlib.Path(
        os.environ.get(
            "BEANJAR_QPA_INCLUDE",
            str(pathlib.Path(pkg("--variable=includedir")) / "QtGui" / qt_version / "QtGui"),
        )
    )
    flags = shlex.split(pkg("--cflags", "--libs")) + [f"-I{qpa}"]
    if not (qpa / "qpa/qplatformnativeinterface.h").is_file():
        pytest.skip("matching Qt QPA development header unavailable")
    with tempfile.TemporaryDirectory(prefix="beanjar-native-input-qt-") as directory:
        work = pathlib.Path(directory)
        fixture = work / "fixture.cpp"
        fixture.write_text(r"""
    #include <QtGui/QGuiApplication>
    #include <qpa/qplatformnativeinterface.h>
    #include <cerrno>
    #include <cstdint>
    #include <cstdio>
    #include <cstdlib>
    #include <cstring>
    #include <dlfcn.h>
    #include <fcntl.h>
    #include <unistd.h>
    static const char *mode;
    static int owned_fd;
    static QScreen *const screen = reinterpret_cast<QScreen *>(uintptr_t(0x1234));
    class Native : public QPlatformNativeInterface {
    public:
        void *nativeResourceForIntegration(const QByteArray &name) override {
            if (name != "dri_fd") std::abort();
            std::puts("FD_RESOURCE");
            if (!std::strcmp(mode, "no-fd")) return nullptr;
            if (!std::strcmp(mode, "negative-fd")) return reinterpret_cast<void *>(intptr_t(-1));
            if (!std::strcmp(mode, "closed-fd")) return reinterpret_cast<void *>(uintptr_t(100000));
            return reinterpret_cast<void *>(uintptr_t(owned_fd));
        }
        void *nativeResourceForScreen(const QByteArray &name, QScreen *value) override {
            if (name != "dri_crtcid" || value != screen) std::abort();
            std::puts("CRTC_RESOURCE");
            if (!std::strcmp(mode, "no-crtc")) return nullptr;
            if (!std::strcmp(mode, "negative-crtc")) return reinterpret_cast<void *>(intptr_t(-1));
            return reinterpret_cast<void *>(uintptr_t(0x12345));
        }
    };
    QPlatformNativeInterface *QGuiApplication::platformNativeInterface() {
        static Native native;
        return !std::strcmp(mode, "no-native") ? nullptr : &native;
    }
    QScreen *QGuiApplication::primaryScreen() {
        return !std::strcmp(mode, "no-screen") ? nullptr : screen;
    }
    int main(int argc, char **argv) {
        if (argc != 3) return 1;
        mode = argv[1];
        owned_fd = open("/dev/null", O_RDONLY);
        if (owned_fd < 0) return 2;
        char text[32];
        std::snprintf(text, sizeof(text), "%d", owned_fd);
        setenv("EXPECTED_FD", text, 1);
        setenv("BIND_MODE", mode, 1);
        void *stock = dlopen(argv[2], RTLD_NOW | RTLD_LOCAL);
        void *helper = dlopen("libkms-native-input-qt.so", RTLD_NOW | RTLD_LOCAL);
        if (!stock || !helper) { std::fprintf(stderr, "%s\n", dlerror()); return 3; }
        auto bind = reinterpret_cast<void (*)(void *)>(dlsym(helper, "beanjar_native_input_bind_cursor"));
        if (!bind) return 4;
        bind(stock);
        if (fcntl(owned_fd, F_GETFD) < 0) return 5;
        close(owned_fd);
        return 0;
    }
    """)
        stock = work / "stock.c"
        stock.write_text(r"""
    #include <stdio.h>
    #include <stdlib.h>
    #include <string.h>
    #ifndef MISSING_BIND
    int IM_SetCursorDrmDevice(int fd, int crtc) {
        if (fd != atoi(getenv("EXPECTED_FD")) || crtc != 0x12345) abort();
        puts("BOUND_NATIVE_RESOURCES");
        return !strcmp(getenv("BIND_MODE"), "rejected") ? -7 : 0;
    }
    #endif
    """)
        subprocess.run(
            [
                "c++",
                "-shared",
                "-fPIC",
                "-fvisibility=hidden",
                "-fno-exceptions",
                "-fno-rtti",
                "-Wall",
                "-Wextra",
                "-Werror",
                str(ROOT / "guest/layers/gl/src/kms-native-input-qt.cpp"),
                *flags,
                "-ldl",
                "-o",
                str(work / "libkms-native-input-qt.so"),
            ],
            check=True,
        )
        subprocess.run(
            [
                "c++",
                "-fPIC",
                "-Wall",
                "-Wextra",
                "-Werror",
                str(fixture),
                *flags,
                "-ldl",
                "-Wl,--export-dynamic",
                "-o",
                str(work / "probe"),
            ],
            check=True,
        )
        for name, extra in (("stock.so", []), ("missing.so", ["-DMISSING_BIND"])):
            subprocess.run(
                [
                    "cc",
                    "-shared",
                    "-fPIC",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    str(stock),
                    *extra,
                    "-o",
                    str(work / name),
                ],
                check=True,
            )
        env = dict(os.environ, LD_LIBRARY_PATH=str(work))
        for mode, diagnostic, bound, queried in (
            ("ok", "binding result=0", True, True),
            ("rejected", "binding result=-7", True, True),
            ("no-native", "interface/screen unavailable", False, False),
            ("no-screen", "interface/screen unavailable", False, False),
            ("no-fd", "resources unavailable", False, True),
            ("negative-fd", "resources unavailable", False, True),
            ("closed-fd", "descriptor invalid", False, True),
            ("no-crtc", "resources unavailable", False, True),
            ("negative-crtc", "resources unavailable", False, True),
            ("missing", "DRM export missing", False, False),
        ):
            library = work / ("missing.so" if mode == "missing" else "stock.so")
            result = subprocess.run(
                [str(work / "probe"), mode, str(library)],
                env=env,
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
            expected = ["FD_RESOURCE", "CRTC_RESOURCE"] if queried else []
            if bound:
                expected.append("BOUND_NATIVE_RESOURCES")
            assert result.stdout.splitlines() == expected, (mode, result.stdout)
            assert diagnostic in result.stderr, (mode, result.stderr)

    print(
        "native input Qt: genuine QByteArray/native API, resource provenance, borrowed fd and failures PASS"
    )
