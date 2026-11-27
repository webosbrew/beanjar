#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""lgfs.ko's option tables (lgfs-options.h) against the option strings the stock PDMs pass for tfat,
tntfs and ntfs: compiled on the host with the genuine header, every option is dropped, renamed or one the
kernel driver (vfat, ntfs3) declares in its parameter list in the kernel source (subprojects/linux.wrap's, or LINUX_SRC)."""

import re
import subprocess
from pathlib import Path

import pytest
from linuxsrc import linux_source

MODULE = Path(__file__).resolve().parents[3] / "guest/kernel/modules/lgfs"
DEFCONFIG = MODULE.parents[1] / "configs/webos_virt_defconfig"

# physical-device-manager's mount(2) data for each LG type, the same on every line whose PDM mounts
# them (webOS 1 to 23; live with gdb on N21D 04.44.16 and W22A 04.60.90, the strings in the others),
# and for ntfs, webOS 24 on (live on W24P 33.20.23 and W22O queue)
PDM_OPTIONS = {
    "tfat": "iocharset=utf8,fastmount=1,max_prealloc_size=32m,uid=0,gid=5000,umask=0002",
    "tntfs": "nls=utf8,max_prealloc_size=64m,uid=0,gid=5000,umask=0002",
    "ntfs": "uid=0,gid=5000,umask=0002,nls=utf8",
}
TABLES = {"tfat": "lgfs_tfat_options", "tntfs": "lgfs_tntfs_options", "ntfs": "lgfs_ntfs_options"}
# the kernel driver each type is, and where its parameters are declared
BACKENDS = {
    "tfat": ("fs/fat/inode.c", ("fat_param_spec", "vfat_param_spec")),
    "tntfs": ("fs/ntfs3/super.c", ("ntfs_fs_parameters",)),
    "ntfs": ("fs/ntfs3/super.c", ("ntfs_fs_parameters",)),
}


def kernel_params(source, specs):
    """The keys of the fs_parameter_spec arrays named in specs, from the kernel source."""
    text = source.read_text()
    keys = set()
    for spec in specs:
        start = text.index(f"{spec}[] = {{")
        body = text[start : text.index("};", start)]
        keys.update(re.findall(r'fsparam_\w+\s*\(\s*"([^"]+)"', body))
        keys.update(re.findall(r'__fsparam\([^,]+,\s*"([^"]+)"', body))
    return keys


def lookup(table, keys, directory):
    """What the header's lgfs_option() makes of each key: (action, to) or None, from a host build."""
    runner = directory / "runner.c"
    runner.write_text(
        "#include <stdio.h>\n#include <string.h>\n"
        f'#include "{MODULE / "lgfs-options.h"}"\n'
        "int main(int argc, char **argv) {\n"
        "    for (int i = 1; i < argc; i++) {\n"
        f"        const struct lgfs_option *o = lgfs_option({table}, argv[i]);\n"
        '        if (!o) printf("pass\\n");\n'
        '        else if (o->action == LGFS_DROP) printf("drop\\n");\n'
        '        else printf("rename %s\\n", o->to);\n'
        "    }\n    return 0;\n}\n"
    )
    binary = directory / "runner"
    subprocess.run(["cc", "-std=c99", "-Wall", "-Werror", "-o", binary, runner], check=True)
    out = subprocess.run([binary, *keys], check=True, capture_output=True, text=True)
    return [line.split() for line in out.stdout.splitlines()]


@pytest.fixture
def linux():
    path = linux_source()
    if not (path / "fs/fat/inode.c").exists():
        pytest.skip(f"no unpacked kernel source: {path} (LINUX_SRC)")
    return path


def test_pdm_options_reach_the_kernel_driver(linux, tmp_path):
    for fstype, options in PDM_OPTIONS.items():
        source, specs = BACKENDS[fstype]
        accepted = kernel_params(linux / source, specs)
        assert "uid" in accepted  # the parser found the spec
        keys = [o.split("=", 1)[0] for o in options.split(",")]
        for key, result in zip(keys, lookup(TABLES[fstype], keys, tmp_path), strict=True):
            if result[0] == "drop":
                assert key not in accepted, f"{fstype} {key}: dropped, but the driver has it"
            elif result[0] == "rename":
                assert result[1] in accepted, f"{fstype} {key}"
            else:
                assert key in accepted, f"{fstype} {key}"


def test_kernel_options_pass(linux, tmp_path):
    # nothing the kernel driver has is dropped: only LG's own options are filtered
    for fstype, table in TABLES.items():
        source, specs = BACKENDS[fstype]
        accepted = sorted(kernel_params(linux / source, specs))
        for key, result in zip(accepted, lookup(table, accepted, tmp_path), strict=True):
            assert result == ["pass"], f"{fstype} {key}"


def test_ntfs_is_free_for_lgfs():
    # NTFS_FS makes ntfs3 register "ntfs" itself, and lgfs's registration would then fail
    options = dict(
        line.split("=", 1)
        for line in DEFCONFIG.read_text().splitlines()
        if line.startswith("CONFIG_")
    )
    assert "CONFIG_NTFS_FS" not in options
    assert options.get("CONFIG_NTFS3_FS") in ("y", "m")
