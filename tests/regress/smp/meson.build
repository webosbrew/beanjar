# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# smpcheck, build/tests/regress/smp/smpcheck: a StarfishMediaAPIs (SMP) client as ss4s's (moonlight-tv's)
# player is one, run as a developer-mode native app (tests/regress/smp/pack.sh packs it with app/ and a
# clip) so that the firmware gives it its app role on the bus. libplayerAPIs, libAcbAPI and the
# webOS shell's protocol library are loaded at run time; libwayland-client (every firmware has one;
# the SDK's 1.11 headers, whose requests the firmwares' have) and pthread are linked.
guest_programs += {
  'tests/regress/smp/smpcheck': executable('smpcheck', 'smpcheck.c', link_args: ['-ldl', '-lwayland-client', '-lpthread']),
}
