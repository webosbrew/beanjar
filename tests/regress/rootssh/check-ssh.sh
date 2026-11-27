#!/bin/sh
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# Check root SSH (guest/layers/rootssh) on a running VM from the QEMU host.
#   tests/regress/rootssh/check-ssh.sh [IDENTITY_DIR [WAIT]]
# IDENTITY_DIR is the VM's (<vm>/identity; default the development VM's, `host/scripts/vm-launcher config IDENTITY`): its client key
# id_ed25519 logs in, and its known_hosts pins the host key (StrictHostKeyChecking=yes, so another
# host key fails the check). KEY=... logs in with another key of its authorized_keys instead;
# HOST is the VM's host address (default: `host/scripts/vm-launcher config ADDRESS`, of IDENTITY_DIR's VM)
# and SSH_PORT its root SSH port (default 6622).
# Waits up to WAIT seconds (default 180) for the server, then checks: the session is inside the
# webOS namespace (its hostname is the one the firmware's etc/hostname names, which webos-start
# sets: LGSmartTV on webOS 1, LGwebOSTV from webOS 2; and the firmware's etc/starfish-release is
# the root), a pty session works, sftp put/get round trip, scp (sftp mode) and legacy scp
# (-O), and that password logins are refused. Prints the host key fingerprint.
set -u
TOP=$(cd "$(dirname "$0")/../../.." && pwd)
IDENT=${1:-$("$TOP/host/scripts/vm-launcher" config IDENTITY)}
WAIT=${2:-180}
KEY=${KEY:-$IDENT/id_ed25519}
PORT=${SSH_PORT:-6622}
# the VM's host address, 127.0.1.<its slot>: IDENTITY_DIR's VM's, else the development VM's (INSTANCE's)
if [ -z "${HOST:-}" ]; then
	if [ -n "${1:-}" ]; then
		HOST=$("$TOP/host/scripts/vm-launcher" config ADDRESS --dir "$(dirname "$IDENT")") || exit 1
	else
		HOST=$("$TOP/host/scripts/vm-launcher" config ADDRESS) || exit 1
	fi
fi
KH=$IDENT/known_hosts
O="-o UserKnownHostsFile=$KH -o StrictHostKeyChecking=yes -o ConnectTimeout=5 -o LogLevel=ERROR"
S="ssh -p $PORT -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes $O"
fail=0
ok() { echo "PASS $*"; }
bad() { echo "FAIL $*"; fail=1; }

t0=$(date +%s)
until $S root@"$HOST" true 2>/dev/null; do
	[ $(($(date +%s) - t0)) -ge "$WAIT" ] && { $S root@"$HOST" true; echo "FAIL no ssh after $WAIT s"; exit 1; }
	sleep 2
done
echo "ssh up after $(($(date +%s) - t0)) s of waiting"
ssh-keyscan -p "$PORT" -t ed25519 "$HOST" 2>/dev/null | ssh-keygen -lf - | sed 's/^/host key: /'

out=$($S root@"$HOST" 'uname -a; id; test -f /etc/starfish-release && echo webos-root; cat /etc/hostname; hostname')
printf '%s\n' "$out" | sed 's/^/  /'
case $out in *"uid=0(root)"*) ok "logged in as root" ;; *) bad "not root" ;; esac
want=$(printf '%s\n' "$out" | tail -n 2 | head -n 1)
have=$(printf '%s\n' "$out" | tail -n 1)
case $out in *webos-root*) ;; *) want= ;; esac
[ -n "$want" ] && [ "$have" = "$want" ] && ok "webOS namespace (hostname $have, the firmware's)" ||
	bad "not the webOS namespace: hostname '$have', firmware's etc/hostname '${want:-?}'"

# stdin stays open: with an immediate EOF, dropbear may close the pty before the shell opens it.
pty=$(sleep 2 | $S -tt root@"$HOST" tty 2>/dev/null | tr -d '\r')
case $pty in /dev/pts/*) ok "pty session ($pty)" ;; *) bad "pty session: '$pty'" ;; esac

tmp=$(mktemp -d)
head -c 1048576 /dev/urandom > "$tmp/up"
sum=$(sha256sum < "$tmp/up")
printf 'put %s /tmp/sftp-test\nget /tmp/sftp-test %s\n' "$tmp/up" "$tmp/down" |
	sftp -P "$PORT" -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes $O -b - root@"$HOST" >/dev/null &&
	[ "$(sha256sum < "$tmp/down")" = "$sum" ] && ok "sftp put/get (1 MiB)" || bad "sftp put/get"
scp -P "$PORT" -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes $O "$tmp/up" root@"$HOST":/tmp/scp-test &&
	scp -P "$PORT" -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes $O root@"$HOST":/tmp/scp-test "$tmp/scp" &&
	[ "$(sha256sum < "$tmp/scp")" = "$sum" ] && ok "scp (sftp mode)" || bad "scp (sftp mode)"
scp -O -P "$PORT" -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes $O "$tmp/up" root@"$HOST":/tmp/scp-o-test &&
	[ "$($S root@"$HOST" 'sha256sum < /tmp/scp-o-test')" = "$sum" ] && ok "scp -O (legacy protocol, guest scp)" || bad "scp -O"
$S root@"$HOST" 'rm -f /tmp/sftp-test /tmp/scp-test /tmp/scp-o-test'
rm -rf "$tmp"

# No key: only password or keyboard-interactive could work, and the server must offer neither.
methods=$(ssh -p "$PORT" $O -o BatchMode=yes -o PubkeyAuthentication=no -o PreferredAuthentications=password,keyboard-interactive \
	-v root@"$HOST" true 2>&1 | tr -d '\r' | sed -n 's/.*Authentications that can continue: //p' | head -n 1)
case $methods in
publickey) ok "password refused (server offers: $methods)" ;;
*) bad "server offers: ${methods:-?}" ;;
esac
exit $fail
