# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Who may open beanjar-web (docs/dev/architecture/web-viewer.md, "Access"): --auth token, password or
none, the files their secrets live in, and the address rules.

- token (the default): a random token, kept in web.token in the user's beanjar config directory
  (the manager's, $XDG_CONFIG_HOME/beanjar, beside config.toml), so the URL stays the same across
  restarts and `beanjar-web status` can print it. The URL carries it once; it becomes the cookie.
- password: web.password beside it, made as beanjar-vnc's vnc.password is (beanjar/vm/keys.py: 20
  characters of an alphabet without look-alikes) when it is missing; the login page checks it, and
  the cookie is an HMAC of it, so a restart keeps the session and a new password ends it.
- none: only on a loopback address given as a number (127.0.0.0/8, ::1), beanjar-vnc's rule for a
  listener without a password (docs/dev/architecture/vnc.md): a host name, `localhost` included, might
  resolve elsewhere.
Each file is one line, owner-only (0600), made in a 0700 directory; one that is missing or empty
is made anew.
"""

import hashlib
import hmac
import ipaddress
import os
import secrets
import tempfile

# beanjar/vm/keys.py's VNC_ALPHABET and VNC_LENGTH: about 99 bits, typed easily off the screen
PASSWORD_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"  # gitleaks:allow (an alphabet, not a key)
PASSWORD_LENGTH = 20
MODES = ("token", "password", "none")


class Error(Exception):
    pass


def config_dir():
    """The manager's (beanjar.runtime.hostconfig.global_config_dir): $XDG_CONFIG_HOME/beanjar."""
    return os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "beanjar"
    )


def default_token_file():
    return os.path.join(config_dir(), "web.token")


def default_password_file(token_file=None):
    """web.password beside the token file."""
    return os.path.join(os.path.dirname(token_file or default_token_file()), "web.password")


def new_token():
    return secrets.token_urlsafe(24)


def new_password():
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(PASSWORD_LENGTH))


def read_secret(path):
    """The file's first line, or None when it is missing or empty."""
    try:
        with open(path) as f:
            line = f.readline().rstrip("\r\n")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        raise Error(f"cannot read {path}: {e}") from e
    return line or None


def ensure_secret(path, make):
    """path's secret, made with make() (0600, in a 0700 directory) when it is missing or empty;
    its mode set to 0600 when it is wider."""
    secret = read_secret(path)
    if secret is not None:
        if os.stat(path).st_mode & 0o777 != 0o600:
            os.chmod(path, 0o600)
        return secret
    secret = make()
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + "-", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(secret + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
    return secret


def is_loopback(address):
    """A loopback address as a number (127.0.0.0/8, ::1); a host name never is."""
    try:
        return ipaddress.ip_address(address.split("%")[0]).is_loopback
    except ValueError:
        return False


class Access:
    """mode, its secret (the token or the password; None for none) and the session cookie's value."""

    def __init__(self, mode, secret=None, path=None):
        self.mode, self.secret, self.path = mode, secret, path
        if mode == "token":
            self.session = secret
        elif mode == "password":
            self.session = hmac.new(
                secret.encode(), b"beanjar-web session", hashlib.sha256
            ).hexdigest()
        else:
            self.session = None

    def allows(self, cookie):
        if self.mode == "none":
            return True
        return hmac.compare_digest(cookie.encode(), self.session.encode())


def load(mode, listen, token_file=None, password_file=None):
    """The Access for --auth mode on --listen; Error when none is asked for off loopback."""
    if mode == "none":
        if not is_loopback(listen):
            raise Error(
                f"--auth none only on a loopback address (127.0.0.1, ::1), not {listen}: "
                "anyone who reaches it would drive the VMs (--auth token or password)"
            )
        return Access("none")
    token_file = os.path.abspath(token_file or default_token_file())
    if mode == "token":
        return Access("token", ensure_secret(token_file, new_token), token_file)
    path = os.path.abspath(password_file or default_password_file(token_file))
    return Access("password", ensure_secret(path, new_password), path)


def url_host(listen):
    """(the host to put in a URL one can open, whether it listens on every address)."""
    if listen in ("0.0.0.0", ""):
        return "127.0.0.1", True
    if listen == "::":
        return "[::1]", True
    return (f"[{listen}]" if ":" in listen else listen), False


def url(scheme, listen, port, access, with_token=True):
    host, _every = url_host(listen)
    query = f"?token={access.secret}" if access.mode == "token" and with_token else ""
    return f"{scheme}://{host}:{port}/{query}"
