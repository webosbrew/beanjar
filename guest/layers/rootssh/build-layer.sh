#!/bin/sh
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# Assemble the generated "rootssh" layer in build/layers/<chip>-<release>/rootssh: key-only root SSH on port 22,
# like a rooted TV. Select it with LAYERS=...,rootssh; the launcher (beanjar/runtime/qemu.py) then forwards
# host 127.0.0.1:${SSH_PORT:-6622} to the guest's port 22.
#
#   guest/layers/rootssh/build-layer.sh
#
# Env: DEST  output directory (default build/layers/<chip>-<release>/rootssh, host/scripts/vm-launcher config TARGET)
#      BIN   the SSH programs (default build/ssh/bin, published by the guest build)
#
# The layer is the same for every VM: it holds no key. The host key and root's authorized_keys are
# the VM's own (<vm>/identity, the checkout VM's local/; beanjar/vm/keys.py), and the launcher puts
# them into the per-VM layer at /etc/emulator/rootssh/{dropbear_ed25519_host_key,authorized_keys}
# and /home/root/.ssh/authorized_keys.
#
# Contents:
#   guest/layers/rootssh/rootfs/                  the Upstart jobs emulator-rootssh.conf (dropbear) and
#                                           emulator-rootssh-keys.conf (authorized_keys into /home,
#                                           once mount-readwrite has bound it from the state disk)
#   usr/sbin/dropbear, usr/bin/{dbclient,dropbearkey,scp}, usr/libexec/sftp-server
#                                           from build/ssh/bin (guest/tools/ssh, the guest build)
set -eu
HERE=$(dirname "$(readlink -f "$0")")
TOP=$(readlink -f "$HERE/../../..")
DEST=${DEST:-$TOP/build/layers/$("$TOP/host/scripts/vm-launcher" config TARGET)/rootssh}
BIN=${BIN:-$TOP/build/ssh/bin}

for p in dropbear dbclient dropbearkey scp sftp-server; do
	[ -x "$BIN/$p" ] || { echo "rootssh: missing $BIN/$p (run scripts/build.sh guest)" >&2; exit 1; }
done

rm -rf "$DEST"
mkdir -p "$DEST"
cp -a "$HERE/rootfs/." "$DEST/"
install -d -m 755 "$DEST/usr/sbin" "$DEST/usr/bin" "$DEST/usr/libexec"
install -m 755 "$BIN/dropbear" "$DEST/usr/sbin/dropbear"
for p in dbclient dropbearkey scp; do install -m 755 "$BIN/$p" "$DEST/usr/bin/$p"; done
install -m 755 "$BIN/sftp-server" "$DEST/usr/libexec/sftp-server"
echo "rootssh: $DEST"
