# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# mode=mitm's mitmweb is the user's own mitmproxy (host/core/beanjar/network/mitmtool.py), nothing built
# here; its addon and tlsca.py are run from the source tree.

# link = "lan" (docs/dev/architecture/network.md): the one privileged step, a macvlan on the library's
# lan_parent made straight into the VM's rootless namespace. Installed with CAP_NET_ADMIN only
# (the package's postinst); published to build/network/ for the checkout, where it needs the same
# capability (sudo setcap cap_net_admin=ep build/network/beanjar-lan-helper) to work.
lan_helper = executable('beanjar-lan-helper', 'lan-helper.c',
  override_options: ['c_std=gnu11', 'warning_level=2', 'werror=true'],
  build_by_default: true)
published += [['file', lan_helper.full_path(), 'network/beanjar-lan-helper']]
publish_depends += lan_helper

# mode=mitm's port bridges (host/core/beanjar/network/netns.py): an epoll relay between tcp and unix
# sockets, one process per namespace (host, LAN, inner), published to build/network/.
relay = executable('beanjar-relay', 'relay.c',
  override_options: ['c_std=gnu11', 'warning_level=2', 'werror=true'],
  build_by_default: true)
published += [['file', relay.full_path(), 'network/beanjar-relay']]
publish_depends += relay
