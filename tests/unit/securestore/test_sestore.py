#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Exact host-to-kernel/client bytes, missing policy without a VM."""

import base64
import ctypes
import errno
import hashlib
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

TOP = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(TOP / "host/core"))
from beanjar.vm import sestore


class TestInputsTest:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        self.identity = tmp_path
        self.config = self.identity / sestore.CONFIG

    def write(self, text):
        self.config.write_text(text)
        self.config.chmod(0o600)

    def private(self, name, data):
        path = self.identity / name
        path.write_bytes(data)
        path.chmod(0o600)
        return path

    def test_exact_sources_and_no_header(self):
        self.private("input", b"\0\xff\r\n")
        self.write("""missing = "error"
[items]
"browser_secret" = { text = "hello\\n", nul_terminated = true }
"12345678_r" = { hex = "00 ff 80" }
"87654321" = { file = "input" }
"empty" = { text = "" }
""")
        assert sestore.load(self.identity) == (
            "error",
            {
                "browser_secret": b"hello\n\0",
                "12345678_r": b"\0\xff\x80",
                "87654321": b"\0\xff\r\n",
                "empty": b"",
            },
        )
        self.config.unlink()
        assert sestore.load(self.identity) == ("fallback", {})

    def test_youtube_native_lf_and_authored_nul_stay_exact(self):
        for value in (b"dummy-id;dummy-secret\n", b"dummy-id;dummy-secret\0"):
            self.write('missing="error"\n[items]\n"C1FFB834"={hex="' + value.hex() + '"}')
            missing, items = sestore.load(self.identity)
            assert (missing, items) == ("error", {sestore.YOUTUBE: value})
            encoded = sestore.bundle(missing, items)
            assert struct.unpack_from("<II", encoded) == (0, 1)
            name, size = struct.unpack_from("<65s3xI", encoded, 8)
            assert (name.rstrip(b"\0"), size, encoded[80:]) == (
                sestore.YOUTUBE.encode("ascii"),
                len(value),
                value,
            )
        self.write('[items]\n"C1FFB834"={text="dummy-id;dummy-secret",nul_terminated=true}')
        assert sestore.load(self.identity)[1][sestore.YOUTUBE] == b"dummy-id;dummy-secret\0"

    def test_one_file_export_exceeds_old_document_limit_and_preserves_bytes(self):
        # One valid maximum-size value alone exceeds the old document cap in hex.
        items = {"large": bytes(range(256)) * 256, "other": b"\0\xff\r\n"}
        output = self.identity / "dump.toml"
        sestore.export(output, items)
        assert output.stat().st_size > 65536
        assert output.stat().st_mode & 0o777 == 0o600
        assert sestore.load_file(output) == ("error", items)
        snapshot = sestore.bundle(*sestore.load_file(output))
        offset = 8
        recovered = {}
        for _ in items:
            name, length = struct.unpack_from("<65s3xI", snapshot, offset)
            offset += 72
            recovered[name.rstrip(b"\0").decode()] = snapshot[offset : offset + length]
            offset += length
        assert recovered == items
        assert offset == len(snapshot)
        before = output.read_bytes()
        with pytest.raises(FileExistsError):
            sestore.export(output, {"replacement": b"wrong"})
        assert output.read_bytes() == before
        assert not list(self.identity.glob(".sestore-export-*"))
        output.rename(self.config)
        assert sestore.load(self.identity) == ("error", items)

    def test_document_and_value_limits_are_separate(self):
        self.write('[items]\nlarge={hex="' + (b"x" * 65537).hex() + '"}')
        with pytest.raises(sestore.Error, match="item exceeds 65536"):
            sestore.load(self.identity)
        self.private("oversized", b"x" * 65537)
        self.write('[items]\nlarge={file="oversized"}')
        with pytest.raises(sestore.Error, match="file is too large"):
            sestore.load(self.identity)
        self.write("[items]\n" + "".join(f'item{i}={{hex=""}}\n' for i in range(257)))
        with pytest.raises(sestore.Error, match="more than 256 items"):
            sestore.load(self.identity)

    def test_youtube_rejects_control_injection_and_invalid_formats(self):
        for value in (
            b"dummy-id;dummy-secret",
            b"dummy-id;dummy-secret\r",
            b"dummy-id;dummy-secret\r\n",
            b"dummy-id;dummy-secretx",
            b"dummy-id;dummy\nsecret\n",
            b"dummy-id;dummy-secret\n\0",
            b"dummy-id;dummy\0secret\n",
            b"dummy-id;dummy\tsecret\0",
            b"dummy-id;dummy\x7fsecret\n",
            b"dummy-id dummy-secret\0",
            b"dummy-id\n",
            b";dummy-secret\n",
            b"dummy-id;\0",
            b"\n",
            b"",
            b"a;" + b"b" * 98 + b"\n",
        ):
            self.write('[items]\n"C1FFB834"={hex="' + value.hex() + '"}')
            with pytest.raises(sestore.Error):
                sestore.load(self.identity)
        for terminator in (b"\n", b"\0"):
            value = b"a;" + b"b" * 97 + terminator
            self.write('[items]\n"C1FFB834"={hex="' + value.hex() + '"}')
            assert sestore.load(self.identity)[1][sestore.YOUTUBE] == value

    def test_invalid_inputs_do_not_expose_values(self):
        secret = "private-test-value"
        for text in (
            "version = 1",
            'missing = "unknown"',
            "missing = 1",
            "items = []",
            '[items]\n"../escape" = {text="value"}',
            '[items]\na = "value"',
            '[items]\na = {text="value",hex="00"}',
            "[items]\na = {text=1}",
            '[items]\na = {hex="xy"}',
            '[items]\na = {text="value",nul_terminated="yes"}',
            '[items]\na = {unknown="value"}',
            '[items]\na = {file="absent"}',
            '[items]\na = {file=""}',
            '[items]\na = {text="' + secret + '" BROKEN}',
            '[items]\n"1D492999" = {text="' + secret + '"}',
            '[items]\n"C1FFB834" = {text="a;b"}',
        ):
            self.write(text)
            with pytest.raises(sestore.Error) as caught:
                sestore.load(self.identity)
            assert secret not in str(caught.value)
        self.write('[items]\na={text="value"}')
        self.config.chmod(0o644)
        with pytest.raises(sestore.Error):
            sestore.load(self.identity)
        self.config.chmod(0o600)
        self.private("input", b"private").chmod(0o644)
        self.write('[items]\na={file="input"}')
        with pytest.raises(sestore.Error):
            sestore.load(self.identity)
        os.mkfifo(self.identity / "pipe", 0o600)
        self.write('[items]\na={file="pipe"}')
        with pytest.raises(sestore.Error):
            sestore.load(self.identity)

    def test_model_command_updates_the_document(self):
        value = base64.b64encode(bytes(range(32)))
        self.write('missing="error"\n[items]\n"browser_secret"={text="hello\\u007f"}\n')
        sestore.set_model(self.identity, value)
        assert sestore.has_model(self.identity)
        assert sestore.load(self.identity) == (
            "error",
            {"browser_secret": b"hello\x7f", sestore.MODEL: value},
        )
        sestore.set_model(self.identity, None)
        assert not sestore.has_model(self.identity)
        assert sestore.load(self.identity) == ("error", {"browser_secret": b"hello\x7f"})

    def test_model_key_goes_in_the_item_the_line_reads(self):
        from beanjar.firmware import targets

        value = base64.b64encode(bytes(range(32)))
        assert [
            targets.regional_model_key(t)
            for t in (
                ("m3", "goldilocks"),
                ("k6lp", "jhericurl"),
                ("k7lp", "kisscurl"),
                ("o22", "queue"),
            )
        ] == [False, False, True, True]
        assert [sestore.model_item(True, b) for b in ("atsc", "dvb", "arib", None)] == [
            sestore.MODEL + "_a",
            sestore.MODEL + "_d",
            sestore.MODEL + "_r",
            sestore.MODEL,
        ]
        assert sestore.model_item(False, "arib") == sestore.MODEL
        sestore.set_model(self.identity, value)
        sestore.set_model(self.identity, value, sestore.MODEL + "_r")
        assert sestore.load(self.identity) == ("fallback", {sestore.MODEL + "_r": value})
        # A regional key alone gets no synthetic plain key beside it.
        assert sestore.effective(self.identity) == ("fallback", {sestore.MODEL + "_r": value})
        with pytest.raises(sestore.Error):
            sestore.set_model(self.identity, value, "3382732D")

    def test_effective_defaults_and_strict_policy(self):
        default = base64.b64encode(
            hashlib.sha256(b"webos-hypervisor emulator SDP_MODEL secret for 42LB650V").digest()
        )
        assert sestore.effective(self.identity) == ("fallback", {sestore.MODEL: default})
        supplied = base64.b64encode(bytes(range(32)))
        self.private("model-secret", supplied + b"\n")
        assert not sestore.has_model(self.identity)  # a legacy file is not read
        assert sestore.effective(self.identity) == ("fallback", {sestore.MODEL: default})
        sestore.set_model(self.identity, supplied)
        assert sestore.effective(self.identity) == ("fallback", {sestore.MODEL: supplied})
        self.write('missing="error"\n[items]\n"item"={hex="00ff"}')
        assert sestore.effective(self.identity) == ("error", {"item": b"\0\xff"})
        encoded = sestore.bundle(*sestore.effective(self.identity))
        assert struct.unpack_from("<II", encoded) == (0, 1)
        name, size = struct.unpack_from("<65s3xI", encoded, 8)
        assert (name.rstrip(b"\0"), size, encoded[80:]) == (b"item", 2, b"\0\xff")

    def test_bundle_caps_and_compiler_command(self):
        self.write('missing="error"\n[items]\n"item"={text="hello"}')
        output = self.identity / "snapshot.bin"
        subprocess.run(
            [sys.executable, "-m", "beanjar.vm.sestore", str(self.identity), str(output)],
            env=dict(os.environ, PYTHONPATH=str(TOP / "host/core")),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert output.read_bytes() == sestore.bundle("error", {"item": b"hello"})
        assert output.stat().st_mode & 0o777 == 0o600
        for policy, items in [
            ("bad", {}),
            ("error", {"a": b"x" * 65537}),
            ("error", {str(i): b"" for i in range(257)}),
        ]:
            with pytest.raises(sestore.Error):
                sestore.bundle(policy, items)

    def test_dvr_key_made_once_kept_and_dropped_for_a_new_identity(self):
        said = []
        sestore.ensure_dvr_key(self.identity, warn=said.append)
        assert len(said) == 1 and self.config.stat().st_mode & 0o777 == 0o600
        key = sestore.load(self.identity)[1][sestore.DVR_KEY]
        assert len(key) == sestore.DVR_KEY_BYTES
        sestore.ensure_dvr_key(self.identity, warn=said.append)
        assert sestore.load(self.identity)[1][sestore.DVR_KEY] == key and len(said) == 1
        assert sestore.DVR_KEY in sestore.bundle(*sestore.effective(self.identity))[8:].decode(
            "latin-1"
        )
        sestore.drop_dvr_key(self.identity)
        assert sestore.DVR_KEY not in sestore.load(self.identity)[1]
        sestore.ensure_dvr_key(self.identity)
        assert sestore.load(self.identity)[1][sestore.DVR_KEY] != key

    def test_dvr_key_leaves_explicit_inventories_and_checks_length(self):
        self.write('missing="error"\n[items]\n')
        sestore.ensure_dvr_key(self.identity)
        assert sestore.load(self.identity) == ("error", {})
        self.write('missing="error"\n[items]\n"dvr_key"={hex="00112233445566778899aabbccddeeff"}\n')
        sestore.drop_dvr_key(self.identity)
        assert sestore.load(self.identity)[1][sestore.DVR_KEY] == bytes.fromhex(
            "00112233445566778899aabbccddeeff"
        )
        self.write('[items]\n"dvr_key"={hex="0011"}\n')
        with pytest.raises(sestore.Error):
            sestore.load(self.identity)


class TestShimTest:
    @pytest.fixture(scope="class", autouse=True)
    def built(self, request, tmp_path_factory):
        cls = request.cls
        cls.root = tmp_path_factory.mktemp("sestore")
        kernel = (TOP / "guest/kernel/modules/lgemu/sestore.c").read_text()
        start = kernel.index("struct store_item {")
        end = kernel.index("static const struct file_operations")
        (cls.root / "kernel-test-section.h").write_text(kernel[start:end])
        cls.flags = [
            "cc",
            "-shared",
            "-fPIC",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-Wno-unused-parameter",
        ]
        subprocess.run(
            cls.flags
            + [
                "-I" + str(cls.root),
                str(TOP / "tests/unit/securestore/device-harness.c"),
                "-o",
                str(cls.root / "libtestdevice.so"),
            ],
            check=True,
        )
        cls.device = ctypes.CDLL(str(cls.root / "libtestdevice.so"))
        cls.device.test_load.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int]
        cls.device.test_load.restype = ctypes.c_int
        # LG's region library as the stock DILE reads it: the region a test sets.
        region = cls.root / "tvregion.c"
        region.write_text("int region; int DILE_TVREGION_getTVRegion(void) { return region; }")
        subprocess.run(
            cls.flags
            + [
                str(region),
                "-Wl,-soname,libdile_tvregion.so.1",
                "-o",
                str(cls.root / "tvregion.so"),
            ],
            check=True,
        )
        flags = cls.flags + [
            "-Dopen=emulator_test_open",
            "-Dclose=emulator_test_close",
            "-Dioctl=emulator_test_ioctl",
            "-I" + str(TOP / "include"),
        ]
        link = [
            "-L" + str(cls.root),
            "-Wl,--no-as-needed",
            "-ltestdevice",
            "-Wl,-rpath," + str(cls.root),
        ]
        source = TOP / "guest/layers/soc/src"
        target = cls.root / "store.so"
        subprocess.run(
            flags
            + [
                str(source / "libsestore/sestore.c"),
                *link,
                "-Wl,-soname,libsestore.so.0",
                "-o",
                str(target),
            ],
            check=True,
        )
        cls.lib = ctypes.CDLL(str(target))
        cls.call = cls.lib.SESTORE_ReadData
        cls.call.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
        cls.call.restype = ctypes.c_uint
        cls.models = []
        for name, symbol in [
            ("libhal_crypto/hal-crypto.c", "HAL_CRYPTO_ReadMskToSecureStore"),
            ("libdile_crypto/dile-crypto.c", "DILE_CRYPTO_ReadMskToSecureStore"),
        ]:
            target = cls.root / (symbol + ".so")
            subprocess.run(
                flags + [str(source / name), *link, "-ldl", "-o", str(target)], check=True
            )
            lib = ctypes.CDLL(str(target))
            call = getattr(lib, symbol)
            call.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
            call.restype = ctypes.c_int
            cls.models.append((lib, call))
        cls.dile = cls.models[1][1]
        cls.region = None
        yield
        cls.device.test_reset()

    @pytest.fixture(autouse=True)
    def reset(self, built):
        self.device.test_reset()

    def load(self, blob, privileged=True):
        return self.device.test_load(ctypes.create_string_buffer(blob), len(blob), int(privileged))

    def provision(self, items, missing="fallback"):
        self.device.test_reset()
        assert self.load(sestore.bundle(missing, items)) == 0

    def read(self, name, probe=False):
        buf = None if probe else ctypes.create_string_buffer(b"x" * 256, 256)
        size = ctypes.c_uint(0)
        code = self.call(name.encode(), buf, ctypes.byref(size))
        return code, size.value, None if buf is None else buf.raw

    def test_binary_size_query_empty_and_no_terminator(self):
        value = bytes([0, 255, 128, 10])
        self.provision({"3382732D": value, "empty": b""})
        assert self.read("3382732D", probe=True)[:2] == (0, len(value))
        code, size, buf = self.read("3382732D")
        assert (code, size, buf[:size], buf[size:]) == (0, len(value), value, b"x" * (256 - size))
        assert self.read("empty")[:2] == (0, 0)

    def test_missing_policy_and_device_failure(self):
        self.provision({})
        assert self.read("12345678")[0] == 0xFFFF0002
        self.provision({}, "error")
        assert self.read("12345678")[0] == 0xFFFF0002
        self.provision({})
        assert self.read("../escape")[0] == 0xFFFF0002
        self.device.test_reset()
        assert self.read("12345678")[0] == 0xFFFF0002
        self.device.test_present(0)
        assert self.read("12345678")[0] == 0xFFFF0002

    def test_distinct_regional_model_keys_and_all_readers(self):
        plain = base64.b64encode(bytes(range(32)))
        regional = base64.b64encode(bytes(reversed(range(32))))
        self.provision({sestore.MODEL: plain, sestore.MODEL + "_r": regional}, "error")
        assert self.read(sestore.MODEL)[2][:44] == plain
        assert self.read(sestore.MODEL + "_r")[2][:44] == regional
        assert self.read(sestore.MODEL + "_a")[0] == 0xFFFF0002
        for _lib, call in self.models:
            buf = ctypes.create_string_buffer(b"x" * 48, 48)
            size = ctypes.c_uint(48)
            assert call(buf, ctypes.byref(size)) == 0
            assert (size.value, buf.raw) == (44, plain + b"xxxx")
            size = ctypes.c_uint(43)
            assert call(buf, ctypes.byref(size)) == -1
            assert call(None, ctypes.byref(size)) == -1
            assert call(buf, None) == -1
        self.provision({sestore.MODEL + "_r": regional}, "error")
        for _lib, call in self.models:
            size = ctypes.c_uint(44)
            assert call(ctypes.create_string_buffer(44), ctypes.byref(size)) == -1

    def test_regional_model_items_are_exact_under_fallback(self):
        self.provision({sestore.MODEL: sestore.DEFAULT_MODEL})
        assert self.read(sestore.MODEL + "_r")[0] == 0xFFFF0002
        regional = base64.b64encode(bytes(range(32)))
        self.provision({sestore.MODEL: sestore.DEFAULT_MODEL, sestore.MODEL + "_r": regional})
        assert self.read(sestore.MODEL + "_r")[2][:44] == regional
        assert self.read(sestore.MODEL)[2][:44] == sestore.DEFAULT_MODEL

    def test_load_privileges_sealing_and_no_partial_publication(self):
        good = sestore.bundle("error", {"item": b"bytes"})
        assert self.load(good, privileged=False) == -errno.EPERM
        malformed = [
            b"",
            struct.pack("<II", 2, 0),
            struct.pack("<II", 0, 257),
            good[:-1],
            good + b"x",
            struct.pack("<II", 0, 1) + struct.pack("<65s3xI", b"../bad", 0),
            struct.pack("<II", 0, 1) + struct.pack("<65s3xI", b"a" * 65, 0),
            struct.pack("<II", 0, 1) + struct.pack("<65s3xI", b"name", 65537),
            struct.pack("<II", 0, 2) + (struct.pack("<65s3xI", b"duplicate", 0) * 2),
        ]
        for blob in malformed:
            assert self.load(blob) == -errno.EINVAL
            assert self.read("item")[0] == 0xFFFF0002
        assert self.load(good) == 0
        assert self.load(good) == -errno.EBUSY
        assert self.read("item")[2][:5] == b"bytes"

    def dile_read(self):
        buf = ctypes.create_string_buffer(b"x" * 48, 48)
        size = ctypes.c_uint(44)
        code = self.dile(buf, ctypes.byref(size))
        return code, buf.raw[: size.value] if code == 0 else None

    # Last in the class: once the region library is loaded the DILE stand-in takes the regional
    # path for the rest of the process, as on a kisscurl and later firmware.
    def test_dile_reads_the_region_item_then_plain_like_stock(self):
        cls = type(self)
        if cls.region is None:
            cls.region = ctypes.CDLL(str(cls.root / "tvregion.so"), ctypes.RTLD_GLOBAL)
        region = ctypes.c_int.in_dll(cls.region, "region")
        plain = base64.b64encode(bytes(range(32)))
        keys = {s: base64.b64encode(bytes([i]) * 32) for i, s in enumerate("adr", 1)}
        self.provision(
            {sestore.MODEL: plain, **{sestore.MODEL + "_" + s: k for s, k in keys.items()}}
        )
        for number, suffix in ((1, "a"), (2, "d"), (3, "r")):
            region.value = number
            assert self.dile_read() == (0, keys[suffix])
        for number in (0, 4, -1):
            region.value = number
            assert self.dile_read() == (0, plain)
        # Only the board's item: the region decides, with no fallback to another region's.
        self.provision({sestore.MODEL + "_r": keys["r"]}, "error")
        region.value = 3
        assert self.dile_read() == (0, keys["r"])
        region.value = 1
        assert self.dile_read()[0] == 0xFFFF0002 - (1 << 32)
        # A plain-only VM: the regional read misses, the plain one answers.
        self.provision({sestore.MODEL: plain})
        assert self.dile_read() == (0, plain)
