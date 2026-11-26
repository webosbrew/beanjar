#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Compile the microphone pump's step (guest/initramfs/src/virtio-mic.c) on the host and check it:
frames the port splits are carried to the next read and written whole, and a port with no host
program behind it (it polls readable and reads nothing, as a virtio port does) is a hang-up, which
the pump sleeps on instead of spinning."""

import subprocess
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[3] / "guest/initramfs/src/virtio-mic.c"


def test_step(tmp_path):
    source = SOURCE.read_text()
    section = source[source.index("/* all of buf to fd") : source.index("/* ---- end of the step")]
    runner = tmp_path / "runner.c"
    runner.write_text(
        r"""
#include <assert.h>
#include <errno.h>
#include <poll.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>
#define FRAME 4
"""
        + section
        + r"""
int main(void)
{
    int port[2], dev[2];
    unsigned char buf[64 + FRAME], got[64];
    size_t have = 0;
    assert(socketpair(AF_UNIX, SOCK_STREAM, 0, port) == 0);
    assert(pipe(dev) == 0);

    /* six bytes: one whole frame written, two kept */
    assert(write(port[1], "abcdef", 6) == 6);
    assert(pump_step(port[0], dev[1], buf, sizeof buf, &have) == MOVED);
    assert(have == 2 && read(dev[0], got, sizeof got) == 4 && !memcmp(got, "abcd", 4));
    /* the rest of that frame and one more */
    assert(write(port[1], "ghijkl", 6) == 6);
    assert(pump_step(port[0], dev[1], buf, sizeof buf, &have) == MOVED);
    assert(have == 0 && read(dev[0], got, sizeof got) == 8 && !memcmp(got, "efghijkl", 8));

    /* the host program goes away: readable, nothing to read; a hang-up, every time */
    assert(write(port[1], "mn", 2) == 2);
    assert(pump_step(port[0], dev[1], buf, sizeof buf, &have) == MOVED && have == 2);
    close(port[1]);
    for (int i = 0; i < 3; i++)
        assert(pump_step(port[0], dev[1], buf, sizeof buf, &have) == HUNG_UP && have == 0);
    return 0;
}
"""
    )
    binary = tmp_path / "runner"
    subprocess.run(
        [
            "cc",
            "-std=gnu11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-Wno-unused-function",
            "-o",
            binary,
            runner,
        ],
        check=True,
    )
    subprocess.run([binary], check=True, timeout=10)
