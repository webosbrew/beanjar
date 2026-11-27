# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Initialize the stock secondary display on paused QEMU, then resume once."""

import argparse
import os
import signal
import sys
import time

from .dbus import set_ui_info


class Error(Exception):
    pass


def prepare(
    address,
    sock,
    parent,
    *,
    deadline=30.0,
    resume=True,
    configure=set_ui_info,
    command=None,
    alive=None,
    clock=time.monotonic,
    sleep=time.sleep,
):
    from ..runtime.launch import qmp

    command = command or qmp
    alive = alive or (lambda: os.getppid() == parent)
    end = clock() + deadline
    last = "QEMU display has not appeared"
    while alive() and clock() < end:
        try:
            configure(address, min(1.0, end - clock()))
            break
        except (ImportError, ModuleNotFoundError):
            raise Error("secondary display setup requires Python Gio (python3-gi)") from None
        except Exception as exc:
            last = str(exc)
            sleep(min(0.1, max(0, end - clock())))
    else:
        raise Error(
            f"secondary display setup: {last}"
            if alive()
            else "secondary display setup: launcher exited"
        )
    if not alive() or clock() >= end:
        raise Error("secondary display setup: launcher exited or deadline expired")
    if not resume:
        return
    reply = command(sock, '{"execute":"cont"}', timeout=max(0.001, min(1.0, end - clock())))
    if "error" in reply:
        raise Error(f"secondary display setup: QMP cont: {reply['error']}")


def main():
    parent, address, sock = int(sys.argv[1]), sys.argv[2], sys.argv[3]
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--keep-paused", action="store_true")
        args = parser.parse_args(sys.argv[4:])
        prepare(address, sock, parent, resume=not args.keep_paused)
    except Exception as exc:
        print(f"display setup failed: {exc}; stopping paused QEMU", file=sys.stderr, flush=True)
        from ..runtime.launch import qmp

        try:
            reply = qmp(sock, '{"execute":"quit"}', timeout=1.0)
            if "error" in reply:
                raise Error(str(reply["error"]))
        except Exception:
            # The original launcher PID is still our parent (possibly now QEMU).
            # Wrappers handle SIGTERM by stopping their QEMU and reaping helpers.
            if os.getppid() == parent:
                os.kill(parent, signal.SIGTERM)
        return 1
    print("display: secondary console initialized (768x2160)", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
