#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Build libluna-scale.so (guest/layers/webos/src/luna-scale) for the host and preload it into a
program whose curl_easy_setopt is a stand-in libcurl's: the timeout options come out multiplied by
LUNA_TIMEOUT_SCALE, every other option (a long, a curl_off_t, a pointer) unchanged, and nothing at
all changes without the factor."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[3] / "guest/layers/webos/src/luna-scale/luna-scale.c"

FAKE_CURL = r"""
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
int curl_easy_setopt(void *curl, int opt, ...)
{
    va_list ap;
    va_start(ap, opt);
    if (opt >= 30000 && opt < 40000)
        printf("%d %lld\n", opt, (long long)va_arg(ap, int64_t));
    else if (opt < 10000)
        printf("%d %ld\n", opt, va_arg(ap, long));
    else
        printf("%d %s\n", opt, va_arg(ap, const char *));
    va_end(ap);
    (void)curl;
    return 0;
}
"""

PROGRAM = r"""
#include <stdint.h>
int curl_easy_setopt(void *curl, int opt, ...);
int main(void)
{
    curl_easy_setopt(0, 13, 10L);            /* CURLOPT_TIMEOUT */
    curl_easy_setopt(0, 78, 5L);             /* CURLOPT_CONNECTTIMEOUT */
    curl_easy_setopt(0, 155, 2500L);         /* CURLOPT_TIMEOUT_MS */
    curl_easy_setopt(0, 156, 0L);            /* CURLOPT_CONNECTTIMEOUT_MS: 0, no limit */
    curl_easy_setopt(0, 64, 1L);             /* CURLOPT_SSL_VERIFYPEER */
    curl_easy_setopt(0, 30120, (int64_t)1 << 40); /* CURLOPT_MAXFILESIZE_LARGE */
    curl_easy_setopt(0, 10002, "https://US.tv.wiselg.com/"); /* CURLOPT_URL */
    return 0;
}
"""


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    cc = shutil.which("cc")
    if not cc:
        pytest.skip("no host C compiler")
    d = tmp_path_factory.mktemp("luna-scale")
    (d / "fakecurl.c").write_text(FAKE_CURL)
    (d / "prog.c").write_text(PROGRAM)
    run = lambda *a: subprocess.run([cc, *a], check=True, cwd=d)  # noqa: E731
    run("-shared", "-fPIC", "-o", "libfakecurl.so", "fakecurl.c")
    run("-o", "prog", "prog.c", "-L.", "-lfakecurl", "-Wl,-rpath," + str(d))
    run("-shared", "-fPIC", "-fvisibility=hidden", "-o", "libluna-scale.so", str(SRC), "-ldl")
    return d


def outputs(d, scale):
    env = {k: v for k, v in os.environ.items() if k not in ("LD_PRELOAD", "LUNA_TIMEOUT_SCALE")}
    env["LD_PRELOAD"] = str(d / "libluna-scale.so")
    if scale is not None:
        env["LUNA_TIMEOUT_SCALE"] = str(scale)
    out = subprocess.run([d / "prog"], env=env, check=True, capture_output=True, text=True).stdout
    return dict(line.split(" ", 1) for line in out.splitlines())


UNCHANGED = {"64": "1", "30120": str(1 << 40), "10002": "https://US.tv.wiselg.com/"}


def test_timeouts_scaled(built):
    got = outputs(built, 4)
    assert got == {"13": "40", "78": "20", "155": "10000", "156": "0", **UNCHANGED}


@pytest.mark.parametrize("scale", [None, 1, 0, 101, "x"])
def test_nothing_without_a_factor(built, scale):
    got = outputs(built, scale)
    assert got == {"13": "10", "78": "5", "155": "2500", "156": "0", **UNCHANGED}
