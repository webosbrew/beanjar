#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""The mode=mitm proxy's certificate authority, made with the openssl command-line tool.

    host/helpers/network/tlsca.py [DIR]     create the CA in DIR once and print its certificate's path

DIR is explicit or supplied by NETSTUB_CA; there is no checkout-local default. The VMs of a library share one,
<library>/ca (host/core/beanjar/keys.py ensure_ca, at the first create and at a start that finds it missing). DIR is mitmproxy's confdir as well (host/core/beanjar/netns.py),
which terminates the guest's HTTPS and mints a leaf per server from this CA:
    ca.key, ca.pem          the CA: RSA 2048, SHA-256, X.509v3 (the guest's OpenSSL is 1.0.1h)
    mitmproxy-ca.pem        key and certificate together, the file mitmproxy loads as its CA
    mitmproxy-ca-cert.pem   the certificate alone, as mitmproxy names it
Only ca.pem reaches the guest (the launcher's per-VM layer; guest/initramfs/rootfs/init appends it to the
firmware's trust stores); the key stays on the host with the proxy. The CA is created once and
kept. Its validity runs from 2000-01-01 to 2049-12-31 (UTCTime), so a guest clock that starts in
the past still accepts it. No identity data is committed or distributed.
"""

import fcntl
import os
import shutil
import subprocess
import sys
import tempfile

DEFAULT_DIR = os.environ.get("NETSTUB_CA")
START, END = "20000101000000Z", "20491231235959Z"
SUBJECT = "/O=Beanjar/CN=Beanjar netstub local CA"

CONFIG = """
[ca]
default_ca = netstub
[netstub]
dir = {tmp}
database = $dir/index.txt
serial = $dir/serial
new_certs_dir = $dir
default_md = sha256
policy = any
unique_subject = no
copy_extensions = none
[any]
organizationName = optional
commonName = supplied
[req]
distinguished_name = dn
prompt = no
[dn]
CN = x
[v3_ca]
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
"""


def _openssl(*args):
    r = subprocess.run(
        ("openssl",) + args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if r.returncode:
        raise RuntimeError("openssl %s: %s" % (args[0], r.stderr.decode(errors="replace").strip()))


def init(d=DEFAULT_DIR):
    """Create the CA in d unless it exists; return the certificate's path."""
    if not d:
        raise RuntimeError("CA directory required: pass DIR or set NETSTUB_CA")
    os.makedirs(d, exist_ok=True)
    os.chmod(d, 0o700)
    key, pem = os.path.join(d, "ca.key"), os.path.join(d, "ca.pem")
    with open(os.path.join(d, ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not (os.path.isfile(key) and os.path.isfile(pem)):
            tmp = tempfile.mkdtemp(prefix="tlsca.", dir=d)
            try:
                cfg = os.path.join(tmp, "openssl.cnf")
                with open(cfg, "w") as f:
                    f.write(CONFIG.format(tmp=tmp))
                open(os.path.join(tmp, "index.txt"), "w").close()
                with open(os.path.join(tmp, "serial"), "w") as f:  # a random positive serial
                    f.write("%032X\n" % (int.from_bytes(os.urandom(16), "big") >> 2 | 1 << 120))
                _openssl(
                    "genpkey",
                    "-algorithm",
                    "RSA",
                    "-pkeyopt",
                    "rsa_keygen_bits:2048",
                    "-out",
                    key + ".tmp",
                )
                _openssl(
                    "req",
                    "-new",
                    "-config",
                    cfg,
                    "-key",
                    key + ".tmp",
                    "-subj",
                    SUBJECT,
                    "-out",
                    os.path.join(tmp, "ca.csr"),
                )
                # openssl ca: the only openssl 3.0 command that takes an explicit -startdate
                _openssl(
                    "ca",
                    "-batch",
                    "-config",
                    cfg,
                    "-selfsign",
                    "-keyfile",
                    key + ".tmp",
                    "-in",
                    os.path.join(tmp, "ca.csr"),
                    "-out",
                    pem + ".tmp",
                    "-notext",
                    "-startdate",
                    START,
                    "-enddate",
                    END,
                    "-extensions",
                    "v3_ca",
                )
                os.chmod(key + ".tmp", 0o600)
                os.replace(key + ".tmp", key)
                os.replace(pem + ".tmp", pem)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        with open(key) as k, open(pem) as c:
            key_text, cert_text = k.read(), c.read()
        both = os.path.join(d, "mitmproxy-ca.pem")
        with open(both + ".tmp", "w") as f:
            f.write(key_text + cert_text)
        os.chmod(both + ".tmp", 0o600)
        os.replace(both + ".tmp", both)
        shutil.copyfile(pem, os.path.join(d, "mitmproxy-ca-cert.pem"))
    return pem


LEAF_EXTENSIONS = """
[leaf]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
subjectAltName = {san}
"""


def new_key(path):
    """An RSA 2048 private key at path (0600), for leaf()."""
    _openssl("genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", path)
    os.chmod(path, 0o600)


def leaf(d, names, key, out):
    """A server certificate at out for the DNS names (the first its CN, all of them its
    subjectAltName; "*." wildcards allowed), with the private key file key, issued by the CA in d
    (made by init: ca.key, ca.pem). Valid over the CA's whole span, as the CA itself, so a guest
    clock that starts in the past accepts it too; SHA-256, X.509v3, as the guest's OpenSSL 1.0.1h
    takes it. Used by lg_mock.py to answer LG's SDP names; the CA's key never leaves the host."""
    if not names:
        raise ValueError("leaf: no names")
    for n in names:
        if not n or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-*" for c in n):
            raise ValueError(f"leaf: not a DNS name: {n!r}")
    tmp = tempfile.mkdtemp(prefix="tlsleaf.", dir=os.path.dirname(os.path.abspath(out)))
    try:
        cfg = os.path.join(tmp, "openssl.cnf")
        with open(cfg, "w") as f:
            f.write(CONFIG.format(tmp=tmp))
            f.write(LEAF_EXTENSIONS.format(san=",".join("DNS:" + n for n in names)))
        open(os.path.join(tmp, "index.txt"), "w").close()
        with open(os.path.join(tmp, "serial"), "w") as f:
            f.write("%032X\n" % (int.from_bytes(os.urandom(16), "big") >> 2 | 1 << 120))
        csr = os.path.join(tmp, "leaf.csr")
        _openssl(
            "req", "-new", "-config", cfg, "-key", key, "-subj", "/CN=" + names[0], "-out", csr
        )
        _openssl(
            "ca",
            "-batch",
            "-config",
            cfg,
            "-cert",
            os.path.join(d, "ca.pem"),
            "-keyfile",
            os.path.join(d, "ca.key"),
            "-in",
            csr,
            "-out",
            out + ".tmp",
            "-notext",
            "-startdate",
            START,
            "-enddate",
            END,
            "-extensions",
            "leaf",
        )
        os.replace(out + ".tmp", out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


if __name__ == "__main__":
    if len(sys.argv) > 2 or (len(sys.argv) == 2 and sys.argv[1].startswith("-")):
        sys.exit(__doc__)
    if len(sys.argv) == 1 and not DEFAULT_DIR:
        sys.exit("CA directory required: pass DIR or set NETSTUB_CA")
    print(init(sys.argv[1] if len(sys.argv) == 2 else DEFAULT_DIR))
