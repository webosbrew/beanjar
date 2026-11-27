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


# eula-service's lsCallSync: an executable that exports LunaCommon::lsCallSync (or, built with
# -DNOSYM, does not) waits on a condition variable with a deadline 100 ms ahead: from its own code
# on CLOCK_MONOTONIC, on CLOCK_REALTIME, from a library, and once woken by a signal at 50 ms and
# waiting again on the same deadline, as lsCallSync's loop does. It prints each wait's length.
WAITER = r"""
#define _GNU_SOURCE
#include <pthread.h>
#include <time.h>
int lib_wait(pthread_cond_t *c, pthread_mutex_t *m, clockid_t clk, const struct timespec *t)
{
    return pthread_cond_clockwait(c, m, clk, t);
}
"""

CALLER = r"""
#define _GNU_SOURCE
#include <pthread.h>
#include <stdio.h>
#include <time.h>
#include <unistd.h>
int lib_wait(pthread_cond_t *, pthread_mutex_t *, clockid_t, const struct timespec *);
#ifdef SDXSYM
__attribute__((visibility("default"))) int
lsCallSyncImpl(void *t, const char *a, const char *b, void *c, int d) __asm__("_ZN3sdx8mainbody13XpaLunaHandle14lsCallSyncImplEPKcS3_RNSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEEEi");
int lsCallSyncImpl(void *t, const char *a, const char *b, void *c, int d) { (void)t, (void)a, (void)b, (void)c; return d; }
#elif !defined(NOSYM)
__attribute__((visibility("default"))) int
lsCallSync(const char *a, const char *b, void *c, int d) __asm__("_ZN10LunaCommon10lsCallSyncEPKcS1_RN7pbnjson6JValueEi");
int lsCallSync(const char *a, const char *b, void *c, int d) { (void)a, (void)b, (void)c; return d; }
#endif
static pthread_cond_t cond = PTHREAD_COND_INITIALIZER;
static pthread_mutex_t mutex = PTHREAD_MUTEX_INITIALIZER;
static long ms(clockid_t clk, struct timespec a)
{
    struct timespec b;
    clock_gettime(clk, &b);
    return (b.tv_sec - a.tv_sec) * 1000 + (b.tv_nsec - a.tv_nsec) / 1000000;
}
static struct timespec ahead(clockid_t clk, long n)
{
    struct timespec t;
    clock_gettime(clk, &t);
    t.tv_nsec += n * 1000000;
    t.tv_sec += t.tv_nsec / 1000000000, t.tv_nsec %= 1000000000;
    return t;
}
static void *wake(void *p)
{
    (void)p;
    usleep(50000);
    pthread_mutex_lock(&mutex);
    pthread_cond_signal(&cond);
    pthread_mutex_unlock(&mutex);
    return NULL;
}
static void wait_main(const char *name, clockid_t clk, int (*fn)(pthread_cond_t *, pthread_mutex_t *, clockid_t, const struct timespec *))
{
    struct timespec start, t;
    clock_gettime(clk, &start);
    t = ahead(clk, 100);
    pthread_mutex_lock(&mutex);
    while (fn(&cond, &mutex, clk, &t) == 0)
        ;
    pthread_mutex_unlock(&mutex);
    printf("%s %ld\n", name, ms(clk, start));
}
int main(void)
{
    pthread_t th;
    wait_main("monotonic", CLOCK_MONOTONIC, pthread_cond_clockwait);
    wait_main("realtime", CLOCK_REALTIME, pthread_cond_clockwait);
    wait_main("library", CLOCK_MONOTONIC, lib_wait);
    pthread_create(&th, NULL, wake, NULL);
    wait_main("woken", CLOCK_MONOTONIC, pthread_cond_clockwait);
    pthread_join(th, NULL);
    return 0;
}
"""


@pytest.fixture(scope="module")
def waiters(built):
    cc = shutil.which("cc")
    d = built
    (d / "waiter.c").write_text(WAITER)
    (d / "caller.c").write_text(CALLER)
    run = lambda *a: subprocess.run([cc, *a], check=True, cwd=d)  # noqa: E731
    run("-shared", "-fPIC", "-o", "libwaiter.so", "waiter.c", "-pthread")
    for name, extra in (("eula", []), ("sdx", ["-DSDXSYM"]), ("other", ["-DNOSYM"])):
        run(
            "-o",
            name,
            *extra,
            "caller.c",
            "-rdynamic",
            "-L.",
            "-lwaiter",
            "-pthread",
            "-Wl,-rpath," + str(d),
        )
    return d


def waits(d, prog, scale):
    env = {k: v for k, v in os.environ.items() if k not in ("LD_PRELOAD", "LUNA_TIMEOUT_SCALE")}
    env["LD_PRELOAD"] = str(d / "libluna-scale.so")
    if scale is not None:
        env["LUNA_TIMEOUT_SCALE"] = str(scale)
    out = subprocess.run([d / prog], env=env, check=True, capture_output=True, text=True).stdout
    return {k: int(v) for k, v in (line.split() for line in out.splitlines())}


@pytest.mark.parametrize("prog", ["eula", "sdx"])
def test_lscallsync_waits_scaled(waiters, prog):
    got = waits(waiters, prog, 4)
    assert 390 <= got["monotonic"] < 600, got
    assert 390 <= got["woken"] < 600, got  # the second wait keeps the stretched deadline
    assert got["realtime"] < 300 and got["library"] < 300, got


@pytest.mark.parametrize(
    "prog,scale", [("other", 4), ("eula", None), ("eula", 1), ("sdx", None), ("sdx", 1)]
)
def test_lscallsync_waits_unchanged(waiters, prog, scale):
    got = waits(waiters, prog, scale)
    assert all(90 <= v < 300 for v in got.values()), got
