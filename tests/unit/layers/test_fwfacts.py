#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The firmware facts (host/core/beanjar/firmware/facts.py): the file format, what is read from readelf and
build.ninja, the answers from an extracted firmware, and the stub libraries the guest build links
against instead of the firmware's own. With a host C compiler, a stub built from the facts of a
versioned library must give a program the same NEEDED entries, dynamic symbols and version needs as
the library itself."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

TOP = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(TOP / "host/core"))
from beanjar.firmware import facts as firmware_facts  # noqa: E402
from beanjar.firmware import probe  # noqa: E402

FACTS = """# demo
defines bsppart:bsp/usr/lib/libdemo.so:demo_f yes
exports etc/demo.env:DEMO_PLATFORM=demo yes
gbmformats bsppart:bsp/usr/lib/libmali.so.0 XR24,AB24,RG16
gbmformats rootfs:usr/lib/libmali.so.0 none
lib usr/lib/libdemo.so.1 libdemo.so.1
path usr/lib/nyx/modules/nyxDrm.module no
path usr/lib/libsestore.so.0.1.0 yes
sym usr/lib/libdemo.so.1 demo_f@DEMO_2 func
sym usr/lib/libdemo.so.1 demo_var object 8
version usr/lib/libwayland-client.so.0 20.0
"""

DYNSYMS = """
Symbol table '.dynsym' contains 7 entries:
   Num:    Value  Size Type    Bind   Vis      Ndx Name
     0: 00000000     0 NOTYPE  LOCAL  DEFAULT  UND
     1: 00000000     0 FUNC    GLOBAL DEFAULT  UND malloc@GLIBC_2.4 (2)
     2: 00001000    12 FUNC    GLOBAL DEFAULT   11 demo_f@@DEMO_2
     3: 00001010    12 FUNC    GLOBAL DEFAULT   11 demo_f@DEMO_1
     4: 00002000     8 OBJECT  GLOBAL DEFAULT   20 demo_var
     5: 00001020     4 FUNC    WEAK   DEFAULT   11 demo_old@DEMO_1
     6: 00000000     0 OBJECT  GLOBAL DEFAULT  ABS DEMO_2
     7: 00001030     4 FUNC    LOCAL  DEFAULT   11 hidden
"""

NINJA = """build lib.so.p/a.c.o: c_COMPILER ../a.c
 ARGS = -O2

build lib.so: c_LINKER lib.so.p/a.c.o lib.so.p/b.c.o $
 | /fw/usr/lib/libdemo.so.1 || order
 LINK_ARGS = -shared -Wl,--as-needed /fw/usr/lib/libdemo.so.1 -lrt

build exe: c_LINKER exe.p/m.c.o
 LINK_ARGS = -lrt
"""


class Format(unittest.TestCase):
    def test_parse_and_lines(self):
        f = firmware_facts.parse(FACTS)
        self.assertEqual(
            f["path"],
            {"usr/lib/nyx/modules/nyxDrm.module": False, "usr/lib/libsestore.so.0.1.0": True},
        )
        self.assertEqual(f["version"], {"usr/lib/libwayland-client.so.0": "20.0"})
        self.assertEqual(f["lib"], {"usr/lib/libdemo.so.1": "libdemo.so.1"})
        self.assertEqual(f["defines"], {"bsppart:bsp/usr/lib/libdemo.so:demo_f": True})
        self.assertEqual(f["exports"], {"etc/demo.env:DEMO_PLATFORM=demo": True})
        self.assertEqual(
            f["gbmformats"],
            {
                "bsppart:bsp/usr/lib/libmali.so.0": ["XR24", "AB24", "RG16"],
                "rootfs:usr/lib/libmali.so.0": [],
            },
        )
        self.assertEqual(
            f["sym"]["usr/lib/libdemo.so.1"],
            {"demo_f": ("DEMO_2", "func", None), "demo_var": (None, "object", 8)},
        )
        body = [line for line in FACTS.splitlines() if not line.startswith("#")]
        self.assertEqual(firmware_facts.lines(f), sorted(body))
        text = firmware_facts.format_facts("h13-afro", f, "FW")
        self.assertTrue(text.startswith("# h13-afro: "))
        self.assertEqual(firmware_facts.parse(text), f)

    def test_bad_lines(self):
        for bad in (
            "path usr/lib/x maybe",
            "sym usr/lib/libdemo.so.1 x object",  # data without its size
            "sym usr/lib/libdemo.so.1 x func 4",
            "defines usr/lib/libdemo.so:demo_f yes",  # no image
            "defines kernel:usr/lib/libdemo.so:demo_f yes",  # not an image it reads
            "defines bsppart:bsp/usr/lib/libdemo.so:demo_f maybe",
            "exports etc/demo.env yes",  # no name
            "exports etc/demo.env:1X=a yes",  # not a shell name
            "gbmformats usr/lib/libmali.so.0 XB24",  # no image
            "gbmformats bsppart:bsp/usr/lib/libmali.so.0 xb24",  # not a fourcc
            "gbmformats bsppart:bsp/usr/lib/libmali.so.0 XB24,",
            "whatever",
        ):
            with self.assertRaisesRegex(firmware_facts.Error, "facts:1: not a fact"):
                firmware_facts.parse(bad + "\nlib usr/lib/libdemo.so.1 libdemo.so.1\n")
        with self.assertRaisesRegex(firmware_facts.Error, "no lib line"):
            firmware_facts.parse("sym usr/lib/libx.so.1 f func\n")

    def test_questions(self):
        q = firmware_facts.parse_questions(
            "lib usr/lib/libdemo.so.1\npath a/b\npath a/b\nversion usr/lib/w.so.0\n"
            "defines rootfs:usr/lib/x.so:f\nexports etc/x.env:X=\n"
            "gbmformats bsppart:bsp/usr/lib/libmali.so.0\ngbmformats rootfs:usr/lib/libmali.so\n"
        )
        self.assertEqual(
            q,
            {
                "path": ["a/b"],
                "version": ["usr/lib/w.so.0"],
                "lib": ["usr/lib/libdemo.so.1"],
                "defines": ["rootfs:usr/lib/x.so:f"],
                "exports": ["etc/x.env:X="],
                "gbmformats": ["bsppart:bsp/usr/lib/libmali.so.0", "rootfs:usr/lib/libmali.so"],
            },
        )
        f = firmware_facts.parse(FACTS)
        self.assertEqual(
            firmware_facts.unanswered(f, q),
            [
                "path a/b",
                "version usr/lib/w.so.0",
                "defines rootfs:usr/lib/x.so:f",
                "exports etc/x.env:X=",
                "gbmformats rootfs:usr/lib/libmali.so",
            ],
        )
        with self.assertRaises(firmware_facts.Error):
            firmware_facts.parse_questions("sym x\n")
        with self.assertRaises(firmware_facts.Error):
            firmware_facts.parse_questions("defines usr/lib/x.so\n")
        with self.assertRaises(firmware_facts.Error):
            firmware_facts.parse_questions("exports etc/x.env\n")
        with self.assertRaises(firmware_facts.Error):
            firmware_facts.parse_questions("gbmformats usr/lib/libmali.so.0\n")

    def test_gbm_formats(self):
        """Mali's GBM window formats: the fourcc table right after gbm_get_configs' name, at the
        next 4-byte boundary or past its own alignment's padding, as r46p0's, r51's and W23A
        33.31.66's builds have it; none without the name or a table after it (r28's builds set
        the formats in code)."""
        pad = b"\x7fELF" + b"x" * 13  # the name then starts off a 4-byte boundary
        r46 = pad + b"gbm_get_configs\0" + b"\0" * 3 + b"XB24AB24EGL_KHR_partial_update\0"
        self.assertEqual(firmware_facts.gbm_formats(r46), ["XB24", "AB24"])
        r51 = b"SurfaceLG\0\0\0gbm_get_configs\0XR24AB24RG16AR15AR12AYUVYUYV\0\0\0\0EGL_"
        self.assertEqual(
            firmware_facts.gbm_formats(r51),
            ["XR24", "AB24", "RG16", "AR15", "AR12", "AYUV", "YUYV"],
        )
        padded = b"Width\0\0\0gbm_get_configs\0\0\0\0\0XR24AR24RG16\0\0\0\0"
        self.assertEqual(firmware_facts.gbm_formats(padded), ["XR24", "AR24", "RG16"])
        r28 = b"buffer\0\0gbm_get_configs\0\0\0\0\0EGL_KHR_partial_update\0"
        self.assertEqual(firmware_facts.gbm_formats(r28), [])
        self.assertEqual(firmware_facts.gbm_formats(b"\x7fELF no winsys"), [])


class Reading(unittest.TestCase):
    def test_exports(self):
        self.assertEqual(
            firmware_facts.exports(DYNSYMS),
            {
                "demo_f": ("DEMO_2", "func", None),  # the default version, not DEMO_1
                "demo_var": (None, "object", 8),
                "demo_old": ("DEMO_1", "func", None),  # only a non-default one
                "DEMO_2": (None, "object", 0),
            },
        )

    def test_global_names(self):
        self.assertEqual(
            firmware_facts.global_names(DYNSYMS),
            {"malloc", "demo_f", "demo_var", "demo_old", "DEMO_2"},
        )

    def test_soname(self):
        self.assertEqual(
            firmware_facts.soname(
                " 0x0000000e (SONAME)                     Library soname: [libdemo.so.1]\n"
            ),
            "libdemo.so.1",
        )
        self.assertIsNone(firmware_facts.soname(""))

    def test_link_steps(self):
        steps = firmware_facts.link_steps(NINJA)
        self.assertEqual(
            steps,
            [
                (
                    "lib.so",
                    ["lib.so.p/a.c.o", "lib.so.p/b.c.o"],
                    ["-shared", "-Wl,--as-needed", "/fw/usr/lib/libdemo.so.1", "-lrt"],
                ),
                ("exe", ["exe.p/m.c.o"], ["-lrt"]),
            ],
        )


class Stub(unittest.TestCase):
    def test_source(self):
        syms = firmware_facts.parse(FACTS)["sym"]["usr/lib/libdemo.so.1"]
        src = firmware_facts.stub_source(syms)
        self.assertIn("\t.globl demo_f\n\t.type demo_f, %function\ndemo_f:", src)
        self.assertIn(
            "\t.data\n\t.globl demo_var\n\t.type demo_var, %object\n\t.size demo_var, 8\ndemo_var:\n\t.zero 8",
            src,
        )
        self.assertEqual(
            firmware_facts.stub_version_script(syms), "DEMO_2 {\n  global:\n    demo_f;\n};\n"
        )
        self.assertIsNone(firmware_facts.stub_version_script({"f": (None, "func", None)}))


CC = shutil.which("cc")


class Expanded(unittest.TestCase):
    """A firmware directory that already shows its images as files (an epkfs 0.3+ mount: rootfs/
    beside rootfs.pak, or the partitions under super/) is read as it is: no squashfuse mount and no
    unsquashfs run, whatever the pak files hold."""

    def tree(self, top, extra=""):
        lib = top / "usr/lib"
        lib.mkdir(parents=True)
        (lib / "libdemo.so.1.2.3").write_bytes(b"x")
        os.symlink("libdemo.so.1.2.3", lib / "libdemo.so.1")
        (top / "etc").mkdir()
        (top / "etc/demo.env").write_text("export DEMO_PLATFORM='demo'\n")

    def questions(self):
        return {
            "path": ["etc/demo.env", "etc/none"],
            "version": ["usr/lib/libdemo.so.1", "usr/lib/libnone.so.0"],
            "lib": [],
            "exports": ["etc/demo.env:DEMO_PLATFORM=demo"],
        }

    def check(self, fw):
        def refuse(*a, **k):
            raise AssertionError(f"ran {a[0]}")

        with unittest.mock.patch("subprocess.run", refuse):
            f = firmware_facts.answer(str(fw), self.questions(), {})
        self.assertEqual(f["path"], {"etc/demo.env": True, "etc/none": False})
        self.assertEqual(
            f["version"], {"usr/lib/libdemo.so.1": "2.3", "usr/lib/libnone.so.0": "none"}
        )
        self.assertEqual(f["exports"], {"etc/demo.env:DEMO_PLATFORM=demo": True})

    def test_rootfs_beside_pak(self):
        with tempfile.TemporaryDirectory() as tmp:
            fw = Path(tmp)
            (fw / "rootfs.pak").write_bytes(b"not a squashfs image")
            self.tree(fw / "rootfs")
            self.check(fw)

    def test_super_partitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            fw = Path(tmp)
            (fw / "super.pak").write_bytes(b"not a super image")
            self.tree(fw / "super/rootfs")
            self.check(fw)

    def test_pak_only_is_not_expanded(self):
        """A rootfs/ directory without its pak beside it is not taken for the image."""
        with tempfile.TemporaryDirectory() as tmp:
            self.tree(Path(tmp) / "rootfs")
            self.assertIsNone(probe.expanded_image(tmp))
            (Path(tmp) / "rootfs.pak").write_bytes(b"")
            self.assertEqual(probe.expanded_image(tmp), os.path.join(tmp, "rootfs"))

    def test_image_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            fw = Path(tmp)
            (fw / "bsppart.pak").write_bytes(b"")
            (fw / "bsppart/bsp").mkdir(parents=True)
            (fw / "bsppart/bsp/f").write_bytes(b"data")
            self.assertEqual(firmware_facts.image_file(tmp, "bsppart", "/bsp/f"), b"data")
            self.assertIsNone(firmware_facts.image_file(tmp, "bsppart", "/bsp/none"))


@unittest.skipUnless(CC and shutil.which("readelf"), "no host C compiler or readelf")
@unittest.skipUnless(
    shutil.which("mksquashfs") and shutil.which("squashfuse") and os.path.exists("/dev/fuse"),
    "no mksquashfs, squashfuse or FUSE",
)
class Firmware(unittest.TestCase):
    """A firmware (its rootfs.pak) with one versioned library, a build that links against it, and
    the stub."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        t = Path(self.tmp)
        lib = t / "rootfs/usr/lib"
        lib.mkdir(parents=True)
        (t / "demo.c").write_text(
            "int demo_var[2] = {1, 2};\n"
            "int demo_f_1(void) { return 1; }\n"
            "int demo_f_2(void) { return 2; }\n"
            "int demo_unused(void) { return 3; }\n"
            "int demo_both(void) { return 4; }\n"
            '__asm__(".symver demo_f_1, demo_f@DEMO_1");\n'
            '__asm__(".symver demo_f_2, demo_f@@DEMO_2");\n'
        )
        (t / "demo.map").write_text(
            "DEMO_1 { global: demo_var; demo_unused; demo_both; local: *; };\nDEMO_2 { } DEMO_1;\n"
        )
        self.cc(
            "-shared",
            "-fPIC",
            "-Wl,-soname,libdemo.so.1",
            "-Wl,--version-script=" + str(t / "demo.map"),
            "-o",
            str(lib / "libdemo.so.1.2.3"),
            str(t / "demo.c"),
        )
        os.symlink("libdemo.so.1.2.3", lib / "libdemo.so.1")
        os.symlink("libdemo.so.1", lib / "libdemo.so")
        build = t / "build"
        (build / "prog.p").mkdir(parents=True)
        (t / "prog.c").write_text(
            "extern int demo_var[2]; int demo_f(void);\n"
            "int main(void) { return demo_f() + demo_var[1]; }\n"
            "int demo_both(void) { return 5; }\n"  # the program's own, exported as the library has one
        )
        self.cc("-c", "-fPIC", "-o", str(build / "prog.p/prog.c.o"), str(t / "prog.c"))
        self.real = str(lib / "libdemo.so.1")
        (build / "build.ninja").write_text(
            f"build prog: c_LINKER prog.p/prog.c.o\n LINK_ARGS = -Wl,--as-needed {self.real}\n"
        )
        (build / firmware_facts.QUESTIONS).write_text(
            "lib usr/lib/libdemo.so.1\nversion usr/lib/libdemo.so\nversion usr/lib/libnone.so.0\n"
        )
        (t / "fw").mkdir()
        self.squash(t / "rootfs", t / "fw/rootfs.pak")
        self.t, self.build = t, build

    def squash(self, tree, pak):
        subprocess.run(
            ["mksquashfs", str(tree), str(pak), "-quiet", "-noappend"],
            check=True,
            stdout=subprocess.DEVNULL,
        )

    def cc(self, *argv):
        subprocess.run([CC, *argv], check=True)

    def readelf(self, *argv):
        out = subprocess.run(
            ["readelf", "-W", *argv], check=True, stdout=subprocess.PIPE, text=True
        ).stdout
        return out

    def test_generate_and_stub(self):
        facts_file = self.t / "facts.txt"
        text = firmware_facts.generate(
            str(TOP), "demo-target", str(self.t / "fw"), str(self.build), str(facts_file), "FW"
        )
        facts = firmware_facts.parse(text)
        self.assertEqual(facts["lib"], {"usr/lib/libdemo.so.1": "libdemo.so.1"})
        self.assertEqual(
            facts["sym"]["usr/lib/libdemo.so.1"],
            {
                "demo_f": ("DEMO_2", "func", None),
                "demo_var": ("DEMO_1", "object", 8),
                "demo_both": ("DEMO_1", "func", None),
            },
        )
        # libdemo.so -> libdemo.so.1 -> libdemo.so.1.2.3
        self.assertEqual(
            facts["version"], {"usr/lib/libdemo.so": "1.2.3", "usr/lib/libnone.so.0": "none"}
        )
        facts_file.write_text(text)
        # without the build: the facts file's own questions and symbols, the same answers
        self.assertEqual(
            firmware_facts.generate(
                str(TOP), "demo-target", str(self.t / "fw"), None, str(facts_file), "FW"
            ),
            text,
        )

        stub = self.t / "stub/libdemo.so.1"
        stub.parent.mkdir()
        firmware_facts.build_stub(
            facts, "usr/lib/libdemo.so.1", [CC], str(stub), str(self.t / "stub-src")
        )
        self.assertEqual(firmware_facts.soname(self.readelf("-d", str(stub))), "libdemo.so.1")
        self.assertNotIn("NEEDED", self.readelf("-d", str(stub)))
        self.assertNotIn("demo_unused", self.readelf("--dyn-syms", str(stub)))

        def link(lib, out):
            self.cc(
                "-o", str(out), str(self.build / "prog.p/prog.c.o"), "-Wl,--as-needed", str(lib)
            )
            return [self.readelf(f, str(out)) for f in ("-d", "--dyn-syms", "-V")]

        real, stubbed = link(self.real, self.t / "prog-real"), link(stub, self.t / "prog-stub")
        self.assertIn("libdemo.so.1", real[0])
        self.assertIn("DEMO_2", real[2])
        self.assertIn("demo_both", real[1])
        for a, b in zip(real, stubbed):
            self.assertEqual(
                a.replace(str(self.t / "prog-real"), "P"), b.replace(str(self.t / "prog-stub"), "P")
            )

    def test_paths_and_missing(self):
        q = {"path": ["usr/lib/a", "usr/lib/b"], "version": [], "lib": []}
        f = firmware_facts.answer(str(self.t / "fw"), q, {}, entries=["usr/lib/a", "usr/lib/c"])
        self.assertEqual(f["path"], {"usr/lib/a": True, "usr/lib/b": False})
        q = {"path": [], "version": [], "lib": ["usr/lib/libmissing.so.1"]}
        with self.assertRaisesRegex(firmware_facts.Error, "no usr/lib/libmissing.so.1"):
            firmware_facts.answer(str(self.t / "fw"), q, {})

    def test_defines(self):
        """A defines question is answered from the image itself (bsppart.pak here), through the
        library's symlinks; a library the image has not is a no."""
        bsp = self.t / "bsp-tree/bsp/usr/lib"
        bsp.mkdir(parents=True)
        shutil.copy(self.t / "rootfs/usr/lib/libdemo.so.1.2.3", bsp)
        os.symlink("libdemo.so.1.2.3", bsp / "libdemo.so")
        self.squash(self.t / "bsp-tree", self.t / "fw/bsppart.pak")
        q = {
            "path": [],
            "version": [],
            "lib": [],
            "defines": [
                "bsppart:bsp/usr/lib/libdemo.so:demo_f",
                "bsppart:bsp/usr/lib/libdemo.so:demo_absent",
                "bsppart:bsp/usr/lib/libnone.so:demo_f",
            ],
        }
        f = firmware_facts.answer(str(self.t / "fw"), q, {})
        self.assertEqual(
            f["defines"],
            {
                "bsppart:bsp/usr/lib/libdemo.so:demo_f": True,
                "bsppart:bsp/usr/lib/libdemo.so:demo_absent": False,
                "bsppart:bsp/usr/lib/libnone.so:demo_f": False,
            },
        )
        q["defines"] = ["rootfs:usr/lib/libdemo.so:demo_f"]
        self.assertEqual(
            firmware_facts.answer(str(self.t / "fw"), q, {})["defines"], {q["defines"][0]: True}
        )
        os.remove(self.t / "fw/rootfs.pak")
        with self.assertRaisesRegex(firmware_facts.Error, "no rootfs.pak or super.pak rootfs"):
            firmware_facts.answer(str(self.t / "fw"), q, {})

    def test_exports(self):
        """An exports question is answered from the rootfs image's shell file: an export of that
        value, bare or quoted, not a commented one, another value or an assignment alone."""
        etc = self.t / "rootfs/etc"
        etc.mkdir()
        (etc / "demo.env").write_text(
            "# export DEMO_C=yes\n"
            "export DEMO_A=eglfs_demo\n"
            '  export DEMO_B="two words"  # quoted\n'
            "DEMO_D=plain\n"
            "export DEMO_E=\n"
        )
        self.squash(self.t / "rootfs", self.t / "fw/rootfs.pak")
        want = {
            "etc/demo.env:DEMO_A=eglfs_demo": True,
            "etc/demo.env:DEMO_A=eglfs": False,
            "etc/demo.env:DEMO_B=two words": True,
            "etc/demo.env:DEMO_C=yes": False,
            "etc/demo.env:DEMO_D=plain": False,
            "etc/demo.env:DEMO_E=": True,
            "etc/none.env:DEMO_A=eglfs_demo": False,
        }
        q = {"path": [], "version": [], "lib": [], "exports": list(want)}
        self.assertEqual(firmware_facts.answer(str(self.t / "fw"), q, {})["exports"], want)

    def test_gbmformats(self):
        """A gbmformats question is answered from the image's library itself, through its
        symlinks; a library the image has not is none."""
        bsp = self.t / "bsp-tree/bsp/usr/lib"
        bsp.mkdir(parents=True)
        (bsp / "libmali.so.0.51.0").write_bytes(b"\x7fELF....gbm_get_configs\0XR24AB24RG16\0\0\0\0")
        os.symlink("libmali.so.0.51.0", bsp / "libmali.so.0")
        self.squash(self.t / "bsp-tree", self.t / "fw/bsppart.pak")
        want = {
            "bsppart:bsp/usr/lib/libmali.so.0": ["XR24", "AB24", "RG16"],
            "bsppart:bsp/usr/lib/libnone.so.0": [],
            "rootfs:usr/lib/libdemo.so.1": [],
        }
        q = {"path": [], "version": [], "lib": [], "gbmformats": list(want)}
        self.assertEqual(firmware_facts.answer(str(self.t / "fw"), q, {})["gbmformats"], want)

    def test_mount_cleaned_up(self):
        """The rootfs image is mounted for the answers only: no mount or mount point is left, also
        when answering fails."""
        q = {"path": [], "version": ["usr/lib/libdemo.so"], "lib": ["usr/lib/libmissing.so.1"]}
        before = set(os.listdir(tempfile.gettempdir()))
        with self.assertRaisesRegex(firmware_facts.Error, "no usr/lib/libmissing.so.1"):
            firmware_facts.answer(str(self.t / "fw"), q, {})
        left = {
            n
            for n in set(os.listdir(tempfile.gettempdir())) - before
            if n.startswith("beanjar-fwfacts-")
        }
        self.assertEqual(left, set())

    def test_no_questions(self):
        with self.assertRaisesRegex(firmware_facts.Error, "no .* and no firmware-questions.txt"):
            firmware_facts.generate(
                str(TOP), "demo-target", str(self.t / "fw"), None, str(self.t / "none.txt")
            )

    def test_not_compiled(self):
        os.remove(self.build / "prog.p/prog.c.o")
        with self.assertRaisesRegex(firmware_facts.Error, "not compiled yet"):
            firmware_facts.used_symbols(str(self.build), ["usr/lib/libdemo.so.1"])


if __name__ == "__main__":
    unittest.main()
