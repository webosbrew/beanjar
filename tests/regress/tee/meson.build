# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# tee-probe, build/tests/regress/tee/tee-probe: the TEE through the firmware's own TEE client (its libkadaptor's
# libteec, loaded at run time) in webOS; tests/regress/tv.sh tee runs it (scripts/regress --tv). The
# library needs librt and libpthread from the executable, as its stock users have them.
guest_programs += {
  'tests/regress/tee/tee-probe': executable('tee-probe', 'tee-probe.c',
    link_args: ['-Wl,--no-as-needed', '-ldl', '-lrt', '-lpthread']),
}
