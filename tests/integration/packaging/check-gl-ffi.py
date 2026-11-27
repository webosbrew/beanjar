#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Published Wayland clients must carry their own matching libffi ABI."""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


def dynamic(path):
    return subprocess.check_output(["readelf", "-d", str(path)], text=True)


def symbols(path, undefined):
    result = set()
    for line in subprocess.check_output(
        ["readelf", "--dyn-syms", "--wide", str(path)], text=True
    ).splitlines():
        fields = line.split()
        if len(fields) >= 8 and fields[0].endswith(":") and (fields[6] == "UND") == undefined:
            name = fields[7].split("@")[0]
            if name.startswith("ffi_"):
                result.add(name)
    return result


def check(layer):
    client = layer / "usr/lib/libwayland-client.so.0"
    dependencies = re.findall(r"\(NEEDED\).*\[([^]]+)\]", dynamic(client))
    for name in dependencies:
        if not name.startswith("libffi.so."):
            continue
        library = layer / "usr/lib" / name
        if not library.is_file():
            raise ValueError(f"{client}: missing shipped dependency {name}")
        soname = re.search(r"\(SONAME\).*\[([^]]+)\]", dynamic(library))
        if not soname or soname.group(1) != name:
            raise ValueError(f"{library}: SONAME does not match {name}")
        missing = symbols(client, True) - symbols(library, False)
        if missing:
            raise ValueError(f"{library}: missing Wayland symbols {sorted(missing)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "publish", nargs="?", type=Path, default=Path(__file__).resolve().parents[3] / "build"
    )
    parser.add_argument("--target", action="append", required=True)
    args = parser.parse_args()
    root = args.publish
    gl = root / "components/gl"
    selection = gl / "selections.json"
    if not selection.exists():
        return 77
    selected = json.loads(selection.read_text())
    if any(target not in selected for target in args.target):
        return 77
    variants = {selected[target] for target in args.target}
    for variant in sorted(variants):
        check(gl / "variants" / variant)
    print(f"libffi ABI verified for {len(variants)} selected GL variants")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, subprocess.CalledProcessError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
