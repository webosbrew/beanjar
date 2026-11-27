#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Compile the actual provider against a controlled proc/seq/sleep API."""

import subprocess
from pathlib import Path

MODULE = Path(__file__).resolve().parents[3] / "guest/kernel/modules/lg115x"


def test_state_pacing_registration_and_unwind(tmp_path):
    root = tmp_path
    (root / "linux").mkdir()
    common = r"""
#ifndef FIXTURE_H
#define FIXTURE_H
#include <assert.h>
#include <stdbool.h>
#include <errno.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#define module_param(a,b,c)
#define MODULE_PARM_DESC(a,b)
struct proc_dir_entry {int value;};
struct seq_file {char text[32];};
void msleep(unsigned int);
void seq_printf(struct seq_file *, const char *, ...);
struct proc_dir_entry *proc_mkdir(const char *, struct proc_dir_entry *);
struct proc_dir_entry *proc_create_single(const char *, unsigned int, struct proc_dir_entry *, int (*)(struct seq_file *,void *));
void proc_remove(struct proc_dir_entry *);
#endif
"""
    (root / "fixture.h").write_text(common)
    for name in ("delay", "errno", "module", "proc_fs", "seq_file"):
        (root / "linux" / f"{name}.h").write_text('#include "fixture.h"\n')
    (root / "linux/errno.h").write_text('#include_next <linux/errno.h>\n#include "fixture.h"\n')
    # Use production declarations; substitute only its unrelated includes.
    (root / "linux/device.h").write_text('#include "fixture.h"\n')
    (root / "linux/mutex.h").write_text("struct mutex {int unused;};\n")
    (root / "linux/types.h").write_text("typedef unsigned int u32;\n")
    (root / "runner.c").write_text(r"""
#include "fixture.h"
#include "de-histogram.c"
static struct proc_dir_entry parent, dir, file;
static int failure, made, created, removed, sleeps;
static int (*reader)(struct seq_file *, void *);
void msleep(unsigned int duration) {assert(duration==5); sleeps++;}
void seq_printf(struct seq_file *seq,const char *format,...)
{va_list args;va_start(args,format);vsnprintf(seq->text,sizeof(seq->text),format,args);va_end(args);}
struct proc_dir_entry *proc_mkdir(const char *name,struct proc_dir_entry *p)
{assert(p==&parent && !strcmp(name,"de"));made++;return failure==1 ? NULL:&dir;}
struct proc_dir_entry *proc_create_single(const char *name,unsigned mode,struct proc_dir_entry *p,int (*show)(struct seq_file *,void *))
{assert(p==&dir && mode==0444 && !strcmp(name,"v4l2_capture_read"));created++;reader=show;return failure==2 ? NULL:&file;}
void proc_remove(struct proc_dir_entry *p) {if(p) {assert(p==&dir);removed++;}}
int main(void)
{
    assert(!de_histogram_proc && !lx_de_histogram_add(&parent) && !made);
    lx_de_histogram_remove();assert(!removed);
    de_histogram_proc=true;failure=1;
    assert(lx_de_histogram_add(&parent)==-ENOMEM && !created && !de_histogram_dir);
    failure=2;assert(lx_de_histogram_add(&parent)==-ENOMEM && removed==1 && !de_histogram_dir);
    failure=0;assert(!lx_de_histogram_add(&parent) && de_histogram_dir==&dir);
    struct seq_file seq;
    for(int i=0;i<3;i++) {assert(!reader(&seq,NULL));assert(!strcmp(seq.text,"2 2 \n"));assert(sleeps==i+1);}
    lx_de_histogram_remove();assert(removed==2 && !de_histogram_dir);
    lx_de_histogram_remove();assert(removed==2);
    return 0;
}
""")
    executable = root / "runner"
    subprocess.run(
        [
            "cc",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-Wno-unused-parameter",
            "-I" + str(root),
            "-I" + str(MODULE),
            str(root / "runner.c"),
            "-o",
            str(executable),
        ],
        check=True,
    )
    subprocess.run([str(executable)], check=True, timeout=5)
