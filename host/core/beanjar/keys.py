# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""A VM's SSH keys, plain files in its identity directory, and the mode=mitm proxy's CA.

<vm>/identity/ (including development VMs) holds, made with ssh-keygen when the
VM is created and again, with a warning, at a start that finds one missing:

  id_ed25519, id_ed25519.pub        the VM's client key: its public half may always log in as root
                                    (guest/layers/rootssh or hbchannel), so the manager and the tests can ssh in. It
                                    stays on the host: it never goes into the guest's layer.
  authorized_keys                   more public keys that may log in as root, the user's to edit; made
                                    with none (AUTHORIZED_HEADER): the guest, which may run untrusted
                                    code as root, can read every key it is given, and the user's own
                                    keys identify them (a code host publishes its users' public keys;
                                    a key's comment is often user@host). Only an explicit
                                    `beanjar identity authorized-keys add NAME --mine` (add_mine) or the
                                    user's own lines put theirs in; guest_authorized strips every
                                    comment
  rootssh_host_key(.pub)            root SSH's host key (ed25519), in Dropbear's format, which
                                    Homebrew Channel's Dropbear (guest/layers/hbchannel) takes too
                                    (host/helpers/ssh/dropbear-hostkey.py); .pub is OpenSSH's
  devmode_host_key(.pub)            developer mode's host key (RSA 3072), the same way
  devmode_rsa, devmode_rsa.pub      the developer key (RSA 2048, PEM, unencrypted, "developer@device").
                                    The guest encrypts it at boot with its own passphrase and serves
                                    that on port 9991, as a TV does (guest/layers/devmode); this file opens
                                    prisoner@9922 as it is
  known_hosts                       both host keys at their forwarded ports (6622 or SSH_PORT, 9922) on
                                    the VM's address (<addresses>.<slot>), written at create and each
                                    launch, for StrictHostKeyChecking=yes

  tee-huk                           the TEE's hardware-unique key (ensure_tee_huk): 32 random bytes
                                    (hex), made here for this VM and nowhere else, from which rtk.ko's
                                    stand-in TAs derive every key they answer (tee_huk=, boot.conf);
                                    no key of a TV or a firmware is ever in it
  vnc.password                      beanjar-vnc's password (ensure_vnc_password, 0600): one line, made
                                    random the first time VNC starts or the password is shown, kept
                                    across VNC and VM restarts, set or reset with `beanjar vnc
                                    password`, made anew with a new identity (renew_vnc_password)
  sestore.toml                      the second screen gateway's RSA-2048 PEM item is provisioned by
                                    beanjar.sestore; generate_ssg_key() returns its bytes, and
                                    ssg_cert_data() makes the matching public certificate


Private keys are 0600, everything else 0644. A pair missing either half is made again as a whole.

The CA (ensure_ca) is not a VM's: the VMs of a library share <library>/ca, including the isolated development library. It is a directory (0700) of host/helpers/network/tlsca.py's files (ca.key 0600, ca.pem, and
mitmproxy's names for them), mitmweb's confdir. Only ca.pem goes into a guest's layer.
"""

import base64
import glob
import hashlib
import importlib.util
import ipaddress
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile

from . import base, paths

# keys' names for the base's (base.py: the `_ares` query reads them without this module)
Error = base.KeysError
CLIENT, DEVMODE = base.CLIENT, base.DEVMODE
ROOTSSH_PORT, DEVMODE_PORT = base.ROOTSSH_PORT, base.DEVMODE_PORT
SLOTS, DEFAULT_ADDRESSES = base.SLOTS, base.DEFAULT_ADDRESSES
parse_addresses, address, service_host = base.parse_addresses, base.address, base.service_host

AUTHORIZED = "authorized_keys"
ROOTSSH_HOST = "rootssh_host_key"
DEVMODE_HOST = "devmode_host_key"
KNOWN_HOSTS = "known_hosts"
TEE_HUK = "tee-huk"
VNC_PASSWORD = "vnc.password"
# A generated VNC password: 20 of these 31 characters (about 99 bits), none that reads like another
# (no 0/o, 1/l, i), no symbol a keyboard layout moves, so it can be typed off the screen
VNC_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
VNC_LENGTH = 20
# neatvnc's RSA-AES sends the password with a length byte and copies it into a C string
# (src/auth/rsa-aes.c, 0.9.1 and 1.0.3), as TigerVNC sends it (CSecurityRSAAES): 255 bytes at most
VNC_MAX = 255
# The gateway's certificate (ssg_cert): LG's ssg.server.cert.pem's subject, validity and leaf
# extensions, self-signed by the VM's key: no issuer of LG's.
SSG_SUBJECT = "/C=KR/ST=Seoul/O=LG Electronics Inc./OU=HE Lab./CN=LGE TV SSG"
SSG_VALIDITY = ("20180312014952Z", "20340815014952Z")
SSG_EXTENSIONS = (
    "basicConstraints=CA:FALSE",
    "keyUsage=critical,digitalSignature,keyEncipherment",
    "extendedKeyUsage=serverAuth",
    "subjectKeyIdentifier=hash",
    "authorityKeyIdentifier=none",
)
SSG_KEY_MAX = 1999  # securitymanager's getSSGSSL buffer (2000 bytes), less the terminator
CA = "ca"
# A VM's host ports (base.py has root SSH's and developer mode's, with the slots and addresses)
KEYSERVER_PORT = "9991"
INSPECTOR_PORT = "18083"
AUTHORIZED_HEADER = (
    "# Public keys that may log in as root over SSH (guest/layers/rootssh, hbchannel), one per line,\n"
    "# besides the VM's own id_ed25519.pub, which always may and is what Beanjar's ssh commands use\n"
    "# (beanjar show NAME, beanjar ares-TOOL --root). Read at each start.\n"
    "# Every key here goes into the guest, which can read it (without its comment). Your own keys\n"
    "# identify you (a code host publishes its users' public keys), so none is here unless you add\n"
    "# it: `beanjar identity authorized-keys add NAME --mine` adds your ~/.ssh/*.pub, `reset` empties it.\n"
)
# an authorized_keys line: [options] TYPE BASE64 [comment]; options have no unquoted blank
_KEY_TYPE = re.compile(r"(?:ssh|ecdsa|sk)-[A-Za-z0-9@._-]+")
_KEY_BLOB = re.compile(r"[A-Za-z0-9+/]+={0,3}")
_OPTIONS = re.compile(r'(?:[^\s"]|"(?:[^"\\]|\\.)*")+')


def listen_address(value):
    """vm.toml's listen (LISTEN_ADDRESS): the IPv4 address the VM's TV-service host ports
    (layermeta.forwards: SSAP 3000 and 3001, root SSH 6622, developer mode 9922 and its key server
    9991) listen on instead of its slot address, e.g. 0.0.0.0 (every address: the LAN too) or one
    of the host's. Normalised."""
    try:
        return str(ipaddress.IPv4Address(str(value).strip()))
    except ValueError:
        raise Error(
            f"listen {value!r}: an IPv4 address, e.g. 0.0.0.0 (the LAN too) or one of the host's"
        ) from None


def lan_address(value):
    """vm.toml's address (NET_ADDRESS), link = "lan" only: "dhcp", or a static
    "A.B.C.D/NN,GATEWAY" on the LAN, the gateway inside that network. Normalised."""
    text = str(value).strip()
    if text.lower() == "dhcp":
        return "dhcp"
    why = f"address {value!r}: dhcp, or A.B.C.D/NN,GATEWAY (e.g. 192.168.1.50/24,192.168.1.1)"
    try:
        iface, gateway = (part.strip() for part in text.split(","))
        if "/" not in iface:
            raise ValueError
        iface, gateway = ipaddress.IPv4Interface(iface), ipaddress.IPv4Address(gateway)
    except ValueError:
        raise Error(why) from None
    net = iface.network
    usable = net.prefixlen < 31 and iface.ip not in (net.network_address, net.broadcast_address)
    if (
        not usable
        or gateway not in net
        or gateway == iface.ip
        or gateway in (net.network_address, net.broadcast_address)
    ):
        raise Error(f"{why}; the address a host of its network, the gateway another one in it")
    return f"{iface},{gateway}"


def label_of(d):
    """The name put in the keys' comments: the VM's (<vm>/identity), or "checkout" (local/)."""
    d = os.path.abspath(d)
    return os.path.basename(os.path.dirname(d)) if os.path.basename(d) == "identity" else "checkout"


def _write(path, text, mode):
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + "-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def _keygen(out, args, comment):
    if not shutil.which("ssh-keygen"):
        raise Error("ssh-keygen not found (install OpenSSH's client tools)")
    r = subprocess.run(
        ["ssh-keygen", "-q", *args, "-N", "", "-C", comment, "-f", out],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if r.returncode != 0:
        raise Error(f"ssh-keygen: {r.stdout.strip()}")


def _pair(d, name, args, comment, tree, dropbear=False):
    """Make the pair name/name.pub in d, through a scratch directory in d and renames."""
    work = tempfile.mkdtemp(prefix=".keygen-", dir=d)
    try:
        key = os.path.join(work, "key")
        _keygen(key, args, comment)
        if dropbear:
            conv = os.path.join(work, "dropbear")
            r = subprocess.run(
                [
                    sys.executable,
                    tree.src("host", "helpers", "ssh", "dropbear-hostkey.py"),
                    key,
                    conv,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            if r.returncode != 0:
                raise Error(f"dropbear-hostkey.py: {r.stdout.strip()}")
            key_out = conv
        else:
            key_out = key
        os.chmod(key_out, 0o600)
        os.chmod(key + ".pub", 0o644)
        os.replace(key + ".pub", os.path.join(d, name + ".pub"))
        os.replace(key_out, os.path.join(d, name))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def user_keys(home=None):
    """The public keys of ~/.ssh/*.pub (certificates left out), in file name order."""
    lines = []
    for f in sorted(glob.glob(os.path.join(home or os.path.expanduser("~"), ".ssh", "*.pub"))):
        if f.endswith("-cert.pub"):
            continue
        try:
            with open(f) as fh:
                text = fh.read()
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if (
                line
                and not line.startswith("#")
                and "PRIVATE KEY" not in line
                and line not in lines
            ):
                lines.append(line)
    return lines


def _pairs(label):
    """(name, ssh-keygen's arguments, comment, Dropbear's format, what a new one breaks) of each pair."""
    return (
        (
            CLIENT,
            ["-t", "ed25519"],
            f"beanjar {label}",
            False,
            "the manager's and tests' key for root SSH",
        ),
        (
            ROOTSSH_HOST,
            ["-t", "ed25519"],
            f"beanjar {label} rootssh host key",
            True,
            "a known_hosts entry for its port 6622 is now wrong",
        ),
        (
            DEVMODE_HOST,
            ["-t", "rsa", "-b", "3072"],
            f"beanjar {label} devmode host key",
            True,
            "a known_hosts entry for port 9922 is now wrong",
        ),
        (
            DEVMODE,
            ["-t", "rsa", "-b", "2048", "-m", "PEM"],
            "developer@device",
            False,
            "a key fetched before (ares-novacom --getkey) no longer opens prisoner@9922",
        ),
    )


def renew(d, label=None, address=None, tree=paths.TREE):
    """Every key pair of d made anew (a new identity; each pair replaced by renames, so an
    interrupted run leaves old and new pairs, never half of one) and, with the VM's address,
    known_hosts rewritten. The user's authorized_keys and the model key are left as they are.
    Returns the names made."""
    label = label or label_of(d)
    made = []
    for name, args, comment, dropbear, _why in _pairs(label):
        _pair(d, name, args, comment, tree, dropbear)
        made.append(name)
    if address:
        known_hosts(d, address)
    return made


def relabel(d, label):
    """The comments of d's public halves that name a VM (beanjar NAME..., _pairs) made to name label
    (core.Library.rename); a comment of another form (the user's own) is left. Only the .pub files
    are written, each by a rename: the private halves, the keys themselves, stay as they are (the
    OpenSSH client key keeps the old name in its own comment). Returns the names changed."""
    changed = []
    for name, _args, comment, _dropbear, _why in _pairs("\0"):
        if "\0" not in comment:
            continue  # developer@device names no VM
        prefix, suffix = comment.split("\0")
        path = os.path.join(d, name + ".pub")
        try:
            with open(path) as f:
                fields = f.read().strip().split(None, 2)
        except FileNotFoundError:
            continue
        if len(fields) != 3:
            continue
        old = fields[2]
        middle = old[len(prefix) : len(old) - len(suffix)] if suffix else old[len(prefix) :]
        if not old.startswith(prefix) or not old.endswith(suffix) or not middle or " " in middle:
            continue
        new = prefix + label + suffix
        if old != new:
            _write(path, f"{fields[0]} {fields[1]} {new}\n", 0o644)
            changed.append(name + ".pub")
    return changed


def renewed():
    """The names of the files of an identity directory that a new identity replaces (core.renew_keys):
    every key pair's two halves, known_hosts and the VNC password."""
    return [n for name, *_ in _pairs("") for n in (name, name + ".pub")] + [
        KNOWN_HOSTS,
        VNC_PASSWORD,
    ]


def key_fields(line):
    """(options, type, base64, comment) of an authorized_keys line (options and comment "" when it
    has none), or None if it is not one."""
    line = line.strip()
    options = ""
    first = line.split(None, 1)[0] if line else ""
    if not _KEY_TYPE.fullmatch(first):
        m = _OPTIONS.match(line)
        if not m:
            return None
        options, line = m.group(0), line[m.end() :].lstrip()
    fields = line.split(None, 2)
    if len(fields) < 2 or not _KEY_TYPE.fullmatch(fields[0]) or not _KEY_BLOB.fullmatch(fields[1]):
        return None
    return options, fields[0], fields[1], fields[2] if len(fields) > 2 else ""


def without_comment(line):
    """An authorized_keys line (key_fields) as [options] TYPE BASE64, its comment (often
    user@host) left out."""
    return " ".join(x for x in key_fields(line)[:3] if x)


def fingerprint(line):
    """OpenSSH's SHA256 fingerprint of an authorized_keys line's key, or None."""
    f = key_fields(line)
    if f is None:
        return None
    try:
        blob = base64.b64decode(f[2], validate=True)
    except ValueError:
        return None
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def _key_lines_of(text):
    """The lines of an authorized_keys text that are not blank or a comment, stripped."""
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _key_lines(path):
    with open(path) as f:
        return _key_lines_of(f.read())


def reset_authorized(d):
    """d/authorized_keys as a new VM has it: no key (AUTHORIZED_HEADER), so the guest gets only the
    VM's own client key. Returns the key lines it had."""
    path = os.path.join(d, AUTHORIZED)
    try:
        old = _key_lines(path)
    except FileNotFoundError:
        old = []
    _write(path, AUTHORIZED_HEADER, 0o644)
    return old


def add_mine(d, home=None):
    """The user's own public keys (user_keys: ~/.ssh/*.pub, or home's), their comments left out,
    added to d/authorized_keys if it lacks them: the user's explicit choice to let them log in as
    root, and to hand them to the guest. Returns the lines added."""
    path = os.path.join(d, AUTHORIZED)
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        text = AUTHORIZED_HEADER
    have = {key_fields(k)[2] for k in _key_lines_of(text) if key_fields(k)}
    added = []
    for k in user_keys(home):
        f = key_fields(k)
        if f is None or f[2] in have:
            continue
        have.add(f[2])
        added.append(without_comment(k))
    if added or not os.path.isfile(path):
        if text and not text.endswith("\n"):
            text += "\n"
        _write(path, text + "".join(k + "\n" for k in added), 0o644)
    return added


def authorized(d, home=None):
    """What d/authorized_keys lets log in as root and the guest gets, for `beanjar identity
    authorized-keys NAME show`: a dict per key of guest_authorized's, its fingerprint, its type and
    whose it is: "vm" (the client key), "mine" (one of the user's ~/.ssh/*.pub) or "other"."""
    mine = {key_fields(k)[2] for k in user_keys(home) if key_fields(k)}
    client = _pub(d, CLIENT).split()[1]
    out = []
    for line in guest_authorized(d).splitlines():
        f = key_fields(line)
        blob = f[2] if f else None
        out.append(
            {
                "type": f[1] if f else None,
                "fingerprint": fingerprint(line),
                "whose": "vm" if blob == client else "mine" if blob in mine else "other",
            }
        )
    return out


def ensure(d, label=None, warn=None, address=None, tree=paths.TREE):
    """Make whatever of the keys is missing in d (which exists); returns the names made. warn(line),
    if given, is told about each (at a start; at create nothing is said). With the VM's address,
    known_hosts is written if a key was made or it is missing."""
    label = label or label_of(d)
    made = []
    for name, args, comment, dropbear, why in _pairs(label):
        if os.path.isfile(os.path.join(d, name)) and os.path.isfile(os.path.join(d, name + ".pub")):
            continue
        _pair(d, name, args, comment, tree, dropbear)
        made.append(name)
        if warn:
            warn(f"warning: {d} had no {name}; made a new one: {why}")
    auth = os.path.join(d, AUTHORIZED)
    if not os.path.isfile(auth):
        # with no key: the user's own go in only if they add them (AUTHORIZED_HEADER)
        _write(auth, AUTHORIZED_HEADER, 0o644)
        made.append(AUTHORIZED)
        if warn:
            warn(f"warning: {d} had no {AUTHORIZED}; made it with no key but the VM's own")
    if address and (made or not os.path.isfile(os.path.join(d, KNOWN_HOSTS))):
        known_hosts(d, address)
    return made


def ensure_tee_huk(d, warn=None):
    """The VM's TEE hardware-unique key in d (TEE_HUK, 0600): 64 hex digits, os.urandom's, made if the
    file is missing or not such a key; returns them. warn(line), if given, is told when it is made
    (what the TEE kept for the guest, its secure store, no longer opens)."""
    path = os.path.join(d, TEE_HUK)
    try:
        with open(path) as f:
            huk = f.read().strip()
        if len(huk) == 64 and all(c in "0123456789abcdef" for c in huk):
            return huk
    except FileNotFoundError:
        pass
    huk = os.urandom(32).hex()
    _write(path, huk + "\n", 0o600)
    if warn:
        warn(
            f"warning: {d} had no usable {TEE_HUK}; made a new one: what the guest's secure store "
            "kept no longer decrypts"
        )
    return huk


def check_vnc_password(password):
    """Error unless password is one beanjar-vnc and its clients take: 1 to VNC_MAX characters of
    printable ASCII (what every client's keyboard and field types; no line end, which ends the
    file's line, no NUL, which ends neatvnc's string), no space at either end (the manager's Copy
    Password strips the line). Returns it."""
    if not password:
        raise Error("the VNC password is empty")
    bad = sorted({c for c in password if not " " <= c <= "~"})
    if bad:
        raise Error(
            "the VNC password must be printable ASCII (letters, digits, punctuation, spaces): "
            f"not {', '.join(repr(c) for c in bad[:5])}"
        )
    if len(password) > VNC_MAX:
        raise Error(f"the VNC password is {len(password)} characters; RSA-AES takes {VNC_MAX}")
    if password != password.strip():
        raise Error("the VNC password must not begin or end with a space")
    return password


def set_vnc_password(d, password=None):
    """Write password (checked; None: a new random one) to d's VNC_PASSWORD, 0600, replacing it
    by a rename. Returns the path."""
    if password is None:
        password = "".join(secrets.choice(VNC_ALPHABET) for _ in range(VNC_LENGTH))
    path = os.path.join(d, VNC_PASSWORD)
    _write(path, check_vnc_password(password) + "\n", 0o600)
    return path


def read_vnc_password(d):
    """d's VNC password (its file's first line), or None when there is none or it is not usable."""
    try:
        with open(os.path.join(d, VNC_PASSWORD)) as f:
            password = f.readline().rstrip("\r\n")
        return check_vnc_password(password)
    except (OSError, UnicodeDecodeError, Error):
        return None


def ensure_vnc_password(d):
    """d's VNC password file, made with a random password when it is missing or unusable; its path."""
    if read_vnc_password(d) is None:
        return set_vnc_password(d)
    path = os.path.join(d, VNC_PASSWORD)
    if os.stat(path).st_mode & 0o777 != 0o600:
        os.chmod(path, 0o600)
    return path


def generate_ssg_key():
    """Generate exact RSA-2048 PEM bytes for a TOML secure-store item."""
    r = subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if r.returncode != 0 or not r.stdout or len(r.stdout) > SSG_KEY_MAX or b"\0" in r.stdout:
        raise Error("could not generate the second screen gateway key")
    return r.stdout


def ssg_cert_data(key):
    """Generate the matching certificate from exact provisioned PEM bytes, without logging them."""
    pub = subprocess.run(
        ["openssl", "pkey", "-in", "/dev/stdin", "-pubout", "-outform", "DER"],
        input=key,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if pub.returncode != 0:
        raise Error("SSG item is not a usable private PEM key")
    serial = "0x" + hashlib.sha256(pub.stdout).hexdigest()[:16]
    args = [
        "openssl",
        "req",
        "-new",
        "-x509",
        "-key",
        "/dev/stdin",
        "-subj",
        SSG_SUBJECT,
        "-sha256",
        "-set_serial",
        serial,
        "-not_before",
        SSG_VALIDITY[0],
        "-not_after",
        SSG_VALIDITY[1],
    ]
    for ext in SSG_EXTENSIONS:
        args += ["-addext", ext]
    r = subprocess.run(args, input=key, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0 or not r.stdout.startswith(b"-----BEGIN CERTIFICATE-----"):
        raise Error("could not generate the SSG item's matching certificate")
    return r.stdout


def _tlsca(tree):
    spec = importlib.util.spec_from_file_location(
        "beanjar_tlsca", tree.src("host", "helpers", "network", "tlsca.py")
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def ensure_ca(ca, warn=None, tree=paths.TREE):
    """The CA directory ca (host/helpers/network/tlsca.py, its backdated validity), made if its key or
    certificate is missing; True if it was made. warn(line), if given, is told. All of it under the
    lock beside it (<ca>.lock), as the library's VMs, which share it, may start at once."""
    try:
        with paths.file_lock(ca.rstrip("/") + ".lock"):
            made = not (
                os.path.isfile(os.path.join(ca, "ca.key"))
                and os.path.isfile(os.path.join(ca, "ca.pem"))
            )
            if made:
                # a half, if one is left, is replaced as a whole
                for n in ("ca.key", "ca.pem"):
                    try:
                        os.unlink(os.path.join(ca, n))
                    except FileNotFoundError:
                        pass
            _tlsca(tree).init(ca)
    except (RuntimeError, OSError) as e:
        raise Error(f"the proxy's CA in {ca}: {e}") from None
    if made and warn:
        warn(
            f"warning: no CA in {ca}; made a new one: the guests trust it from their next boot "
            "with mode=mitm, and whatever trusted the old one (a browser, a TV) no longer does"
        )
    return made


def _pub(d, name):
    """The "type base64" of name.pub."""
    with open(os.path.join(d, name + ".pub")) as f:
        return " ".join(f.read().split()[:2])


def known_hosts(d, address, ssh_port=None):
    """d/known_hosts: the two host keys at their forwarded ports on the VM's address (address()),
    so each VM's pins are its own."""
    lines = []
    for name, port in ((ROOTSSH_HOST, ssh_port or ROOTSSH_PORT), (DEVMODE_HOST, DEVMODE_PORT)):
        if os.path.isfile(os.path.join(d, name + ".pub")):
            lines.append(f"[{address}]:{port} {_pub(d, name)}\n")
    text = "".join(lines)
    path = os.path.join(d, KNOWN_HOSTS)
    try:
        with open(path) as f:
            if f.read() == text and os.stat(path).st_mode & 0o777 == 0o644:
                return
    except OSError:
        pass
    _write(path, text, 0o644)


def guest_authorized(d):
    """The root authorized_keys the guest gets: d/authorized_keys and the client key's public half,
    each key once, every key's comment left out (without_comment: it is often user@host, and the
    guest can read the file). A private key in authorized_keys, or a line that is no key, is
    refused."""
    path = os.path.join(d, AUTHORIZED)
    keys = _key_lines(path)
    if any("PRIVATE KEY" in k for k in keys):
        raise Error(f"{path} contains a private key; it must hold public keys only")
    bad = [i + 1 for i, k in enumerate(keys) if key_fields(k) is None]
    if bad:
        raise Error(
            f"{path}: key {bad[0]} is not a public key line ([options] TYPE BASE64 [comment])"
        )
    keys = [without_comment(k) for k in keys]
    client = _pub(d, CLIENT)
    if not any(client in k for k in keys):  # also a line with options in front
        keys.append(client)
    return "".join(k + "\n" for k in dict.fromkeys(keys))
