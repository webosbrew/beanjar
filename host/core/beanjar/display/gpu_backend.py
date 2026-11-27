# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The VM's GPU process: QEMU's vhost-user-gpu backend on the launcher's socket.

    python3 -m beanjar.display.gpu_backend PID LISTEN_FD BACKEND [ARG...]

The launcher listens on the vhost-user socket (qemu.Plan.gpu_socket) before it starts this and QEMU,
so QEMU's connect at realize never races the backend's own listen. This takes the one connection
(QEMU's) and becomes the backend with it: `BACKEND ARG... --fd=N`. It gives up when PID (the
launcher's, QEMU's after its exec) ends first. The backend then lives as long as that connection:
it exits when QEMU closes it, a crash included, and the VM's unit (KillMode=control-group) ends it
with the rest of the VM on any stop (docs/dev/architecture/gpu-process.md).
"""

import os
import select
import socket
import sys


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    pid, fd, backend = int(argv[0]), int(argv[1]), argv[2:]
    listener = socket.socket(fileno=fd)
    while alive(pid):
        if select.select([listener], [], [], 1.0)[0]:
            conn, _ = listener.accept()
            listener.close()
            os.set_inheritable(conn.fileno(), True)
            sys.stdout.flush()
            os.execv(backend[0], [*backend, f"--fd={conn.fileno()}"])
    print(f"gpu_backend: {pid} ended before QEMU connected", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
