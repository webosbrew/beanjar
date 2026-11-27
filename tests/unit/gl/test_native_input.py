#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Exercise the production guarded loader and symbol interposition, without Qt/devices.

Fixtures use the supplied firmware's actual C++ symbol names and Qt_5 version.
They test loader/lifetime behavior; native IM delivery still needs guest validation.
"""

import os
import pathlib
import subprocess
import tempfile


def test_native_input():
    ROOT = pathlib.Path(__file__).resolve().parents[3]
    EXEC = "_ZN15QGuiApplication4execEv"
    INSTANCE = "_ZN21QStarfishInputManager8instanceEv"
    START = "_ZN21QStarfishInputManager17startInputServiceEv"

    with tempfile.TemporaryDirectory(prefix="beanjar-native-input-") as directory:
        work = pathlib.Path(directory)
        version = work / "qt.map"
        version.write_text("Qt_5 { global: *; };\n")

        def compile_c(name, source, *args):
            source_path = work / (name + ".c")
            source_path.write_text(source)
            output = work / name
            subprocess.run(
                ["cc", "-Wall", "-Wextra", "-Werror", str(source_path), *args, "-o", str(output)],
                check=True,
            )
            return output

        shim = work / "libkms-native-input.so"
        subprocess.run(
            [
                "cc",
                "-shared",
                "-fPIC",
                "-fvisibility=hidden",
                "-Wall",
                "-Wextra",
                "-Werror",
                str(ROOT / "guest/layers/gl/src/kms-native-input.c"),
                "-ldl",
                "-lpthread",
                "-o",
                str(shim),
            ],
            check=True,
        )
        compile_c(
            "libfixture-qt.so",
            f'''
    #include <errno.h>
    #include <stdio.h>
    int gui_exec(void) __asm__("{EXEC}");
    int gui_exec(void) {{
        static int calls;
        if (errno != E2BIG) return -99;
        puts("REAL_EXEC");
        errno = ERANGE;
        return 40 + ++calls;
    }}
    ''',
            "-shared",
            "-fPIC",
            f"-Wl,--version-script={version}",
        )
        probe = compile_c(
            "probe",
            f'''
    #include <errno.h>
    #include <stdio.h>
    #include <stdlib.h>
    int gui_exec(void) __asm__("{EXEC}");
    int main(void) {{
        const char *preload = getenv("LD_PRELOAD");
        printf("PRELOAD=%s\\n", preload ? preload : "");
        for (int i = 1; i <= 2; i++) {{
            errno = E2BIG;
            int result = gui_exec();
            if (result != 40 + i || errno != ERANGE) return 1;
        }}
        puts("COMPLETE");
        return 0;
    }}
    ''',
            f"-L{work}",
            "-lfixture-qt",
            f"-Wl,-rpath,{work}",
        )
        compositor = work / "surface-manager"
        compositor.write_bytes(probe.read_bytes())
        compositor.chmod(0o755)
        stock_source = f'''
    #include <errno.h>
    #include <stdio.h>
    static int manager, instances, starts;
    __attribute__((constructor)) static void loaded(void) {{ puts("LOADED"); }}
    __attribute__((destructor)) static void unloaded(void) {{ puts("UNLOADED"); }}
    void *instance(void) __asm__("{INSTANCE}");
    void *instance(void) {{
        printf("INSTANCE=%d\\n", ++instances);
        errno = EIO;
    #ifdef NULL_MANAGER
        return NULL;
    #else
        return &manager;
    #endif
    }}
    #ifndef MISSING_START
    void start(void *) __asm__("{START}");
    void start(void *self) {{
        printf("START=%d SELF=%d\\n", ++starts, self == &manager);
        errno = ENOMEM;
    }}
    #endif
    '''
        helper_source = """
    #include <errno.h>
    #include <stdio.h>
    __attribute__((constructor)) static void loaded(void) { puts("QT_LOADED"); }
    __attribute__((destructor)) static void unloaded(void) { puts("QT_UNLOADED"); }
    #ifndef MISSING_BIND
    void beanjar_native_input_bind_cursor(void *module) {
        printf("BIND MODULE=%d\\n", module != NULL);
        errno = EFAULT;
    }
    #endif
    """

        def helper(*flags):
            return compile_c("libkms-native-input-qt.so", helper_source, "-shared", "-fPIC", *flags)

        helper()

        # Each variant has a genuine dynamic loader boundary, not mocked dlsym.
        def stock(*flags):
            return compile_c(
                "libStarfishInput.so.0",
                stock_source,
                "-shared",
                "-fPIC",
                "-Wno-unused-variable",
                *flags,
            )

        env = dict(
            os.environ,
            LD_PRELOAD=f"{shim}:libm.so.6",
            LD_LIBRARY_PATH=str(work),
            BEANJAR_NATIVE_INPUT="1",
        )

        def run(binary=compositor, flag="1"):
            values = dict(env)
            if flag is None:
                values.pop("BEANJAR_NATIVE_INPUT")
            else:
                values["BEANJAR_NATIVE_INPUT"] = flag
            result = subprocess.run(
                [str(binary)], env=values, capture_output=True, text=True, check=True, timeout=5
            )
            lines = result.stdout.splitlines()
            # dlclose is never called; destructor ordering at process exit is not
            # contractual. Both callbacks must remain loaded past COMPLETE.
            if set(lines[-2:]) == {"UNLOADED", "QT_UNLOADED"}:
                lines[-2:] = ["QT_UNLOADED", "UNLOADED"]
            return lines, result.stderr

        stock()
        for binary, flag in ((probe, "1"), (compositor, None), (compositor, "0")):
            out, err = run(binary, flag)
            assert out == [f"PRELOAD={env['LD_PRELOAD']}", "REAL_EXEC", "REAL_EXEC", "COMPLETE"], (
                out
            )
            assert not err, err
        out, err = run()
        assert out == [
            "PRELOAD=libm.so.6",
            "LOADED",
            "QT_LOADED",
            "BIND MODULE=1",
            "INSTANCE=1",
            "START=1 SELF=1",
            "REAL_EXEC",
            "REAL_EXEC",
            "COMPLETE",
            "QT_UNLOADED",
            "UNLOADED",
        ], out
        assert err.count("invoked stock input service startup") == 1, err

        (work / "libStarfishInput.so.0").unlink()
        out, err = run()
        assert out == ["PRELOAD=libm.so.6", "REAL_EXEC", "REAL_EXEC", "COMPLETE"], out
        assert err.count("cannot load stock input") == 1, err

        stock("-DMISSING_START")
        out, err = run()
        assert out == [
            "PRELOAD=libm.so.6",
            "LOADED",
            "REAL_EXEC",
            "REAL_EXEC",
            "COMPLETE",
            "UNLOADED",
        ], out
        assert err.count("exports missing") == 1, err

        stock("-DNULL_MANAGER")
        out, err = run()
        assert out == [
            "PRELOAD=libm.so.6",
            "LOADED",
            "QT_LOADED",
            "BIND MODULE=1",
            "INSTANCE=1",
            "REAL_EXEC",
            "REAL_EXEC",
            "COMPLETE",
            "QT_UNLOADED",
            "UNLOADED",
        ], out
        assert err.count("manager unavailable") == 1, err

        stock()
        (work / "libkms-native-input-qt.so").unlink()
        out, err = run()
        assert out == [
            "PRELOAD=libm.so.6",
            "LOADED",
            "INSTANCE=1",
            "START=1 SELF=1",
            "REAL_EXEC",
            "REAL_EXEC",
            "COMPLETE",
            "UNLOADED",
        ], out
        assert err.count("cannot load cursor binding") == 1, err

        helper("-DMISSING_BIND")
        out, err = run()
        assert out == [
            "PRELOAD=libm.so.6",
            "LOADED",
            "QT_LOADED",
            "INSTANCE=1",
            "START=1 SELF=1",
            "REAL_EXEC",
            "REAL_EXEC",
            "COMPLETE",
            "QT_UNLOADED",
            "UNLOADED",
        ], out
        assert err.count("cursor binding export missing") == 1, err

    print(
        "native input: guards, Qt_5 interposition, startup once, module lifetime, failures and errno PASS"
    )
