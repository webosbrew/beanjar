# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# hist-probe.so, build/tests/regress/histogram/hist-probe.so: libvtcapture's histogram calls, preloaded
# into the firmware's vtCaptureTestSuite (its luna role); tests/regress/tv.sh histogram runs it
# (scripts/regress --tv). The calls are the firmware's, looked up with dlsym in the suite: nothing is
# left undefined, so the preload loads in any process (libdl: the firmwares' glibc before 2.34).
guest_programs += {
  'tests/regress/histogram/hist-probe.so': shared_module('hist-probe', 'hist-probe.c', name_prefix: '',
    link_args: ['-Wl,--no-as-needed', '-ldl', '-Wl,-z,defs']),
}
