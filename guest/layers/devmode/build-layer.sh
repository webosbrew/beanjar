#!/bin/sh
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# Assemble the generated "devmode" layer in build/layers/<chip>-<release>/devmode: the emulator behaves like a TV
# with LG's Developer Mode app switched on, so the ares tools can install, launch and remove
# apps. Select it with LAYERS=...,devmode; the launcher (beanjar/runtime/qemu.py) then forwards host
# 127.0.0.1:9922 (ssh, prisoner) and 127.0.0.1:9991 (key server) to the guest.
#
#   guest/layers/devmode/build-layer.sh
#
# Env: DEST  output directory (default build/layers/<chip>-<release>/devmode, host/scripts/vm-launcher config TARGET)
#      BIN   the SSH programs (default build/ssh/bin)
#
# The layer is the same for every VM: it holds no key. The developer key (unencrypted) and the
# host key are the VM's own (<vm>/identity, the checkout VM's local/; beanjar/vm/keys.py), and the
# launcher puts them into the per-VM layer at /etc/emulator/devmode/{developer_rsa,
# developer_rsa.pub,ssh_host_rsa_key}. start-devmode.sh encrypts the developer key at boot with
# the guest's own passphrase (its NDUID's first six characters, upper case: getPassphrase's
# answer), as a TV's Developer Mode app does.
#
# Contents:
#   guest/layers/devmode/rootfs/                 devmode.override (runs our start-devmode.sh),
#                                          emulator-devmode-flag.conf, emulator-devmode-keyserver.conf,
#                                          etc/systemd/system/devmode.service (masks the stock unit on
#                                          systemd releases: same replacement, past the store app's
#                                          signature gate), emulator-devmode-flag.service,
#                                          usr/libexec/emulator-devmode/{start-devmode.sh,keyserver.sh,
#                                          wait-key.sh},
#                                          var/luna/preferences/devmode_enabled
#   usr/libexec/emulator-devmode/dropbear, sftp-server
#                                          build/ssh/bin/{dropbear-devmode,sftp-server}
#                                          (guest/tools/ssh, the guest build); start-devmode.sh installs them
#                                          where the jail expects the app's OpenSSH
set -eu
HERE=$(dirname "$(readlink -f "$0")")
TOP=$(readlink -f "$HERE/../../..")
DEST=${DEST:-$TOP/build/layers/$("$TOP/host/scripts/vm-launcher" config TARGET)/devmode}
BIN=${BIN:-$TOP/build/ssh/bin}

for p in dropbear-devmode sftp-server; do
	[ -x "$BIN/$p" ] || { echo "devmode: missing $BIN/$p (run scripts/build.sh guest)" >&2; exit 1; }
done

rm -rf "$DEST"
mkdir -p "$DEST"
cp -a "$HERE/rootfs/." "$DEST/"
install -m 755 "$BIN/dropbear-devmode" "$DEST/usr/libexec/emulator-devmode/dropbear"
install -m 755 "$BIN/sftp-server" "$DEST/usr/libexec/emulator-devmode/sftp-server"
echo "devmode: $DEST"
