#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Every build-deps control file parses, and build-deps.sh builds a .deb for each."""

import glob
import os
import shutil
import subprocess
import tempfile

import pytest


def test_build_deps():
    top = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    controls = sorted(glob.glob(os.path.join(top, "packaging/debian/build-deps/*.control")))
    assert controls, "no control files"
    for path in controls:
        role = os.path.basename(path)[: -len(".control")]
        fields = {}
        for line in open(path):
            if line[0] not in " \t" and ":" in line:
                k, v = line.split(":", 1)
                fields[k] = v.strip()
        assert fields.get("Package") == "beanjar-build-deps-" + role, path
        assert fields.get("Architecture") == "all", path
        assert fields.get("Depends") and fields.get("Description"), path

    if not shutil.which("equivs-build"):
        pytest.skip("equivs-build not found")
    with tempfile.TemporaryDirectory() as tmp:
        # The script writes to build/packages; use a copy of the tree layout so the test leaves it alone.
        os.makedirs(os.path.join(tmp, "packaging/debian"))
        shutil.copytree(
            os.path.join(top, "packaging/debian/build-deps"),
            os.path.join(tmp, "packaging/debian/build-deps"),
        )
        shutil.copy(
            os.path.join(top, "packaging/debian/build-deps.sh"),
            os.path.join(tmp, "packaging/debian"),
        )
        env = dict(os.environ, SOURCE_DATE_EPOCH="1760000000")
        subprocess.run(
            [os.path.join(tmp, "packaging/debian/build-deps.sh")],
            check=True,
            env=env,
            stdout=subprocess.DEVNULL,
        )
        for path in controls:
            role = os.path.basename(path)[: -len(".control")]
            debs = glob.glob(
                os.path.join(tmp, "build/packages/beanjar-build-deps-%s_0.*_all.deb" % role)
            )
            assert len(debs) == 1, role
