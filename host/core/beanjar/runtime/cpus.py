# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""CPU placement on hosts whose cores differ in capacity (big.LITTLE, e.g. RK3399: four Cortex-A53
and two Cortex-A72; docs/dev/architecture/vnc.md, "CPU placement"). beanjar-vnc and QEMU's main thread
(the display, virgl) go on the biggest cores; the vCPU threads keep the affinity they have. Hosts
whose cores are all alike are left alone."""

import glob
import os
import shutil

SYS_CPU = "/sys/devices/system/cpu"


def capacities(root=SYS_CPU):
    """{cpu: cpu_capacity} for the CPUs that report one."""
    caps = {}
    for path in glob.glob(os.path.join(root, "cpu[0-9]*", "cpu_capacity")):
        try:
            with open(path) as f:
                caps[int(os.path.basename(os.path.dirname(path))[3:])] = int(f.read())
        except (OSError, ValueError):
            pass
    return caps


def big_cpus(root=SYS_CPU):
    """The biggest cores this process may run on, as a sorted list, where the cores differ in
    capacity; None elsewhere."""
    caps = capacities(root)
    if len(set(caps.values())) < 2:
        return None
    top = max(caps.values())
    big = {cpu for cpu, cap in caps.items() if cap == top} & os.sched_getaffinity(0)
    return sorted(big) or None


def on_big(argv, root=SYS_CPU):
    """argv started on the biggest cores (every thread it makes inherits them) through taskset;
    argv itself where the cores are alike or there is no taskset."""
    big = big_cpus(root)
    taskset = shutil.which("taskset")
    if not big or not taskset:
        return list(argv)
    return [taskset, "-c", ",".join(map(str, big)), *argv]


def thread_on_big(tid, root=SYS_CPU):
    """One thread (a process's main thread: its pid) moved to the biggest cores; the threads it
    already made keep theirs. Whether it was moved."""
    big = big_cpus(root)
    if not big or not tid:
        return False
    try:
        os.sched_setaffinity(tid, big)
    except OSError:
        return False
    return True
