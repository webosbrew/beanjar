# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Per-VM secure-store inputs. TOML stays on the host; the guest receives exact item bytes."""

import os
from pathlib import Path
import re
import stat
import struct
import tempfile
import tomllib

from . import modelkey

CONFIG = "sestore.toml"
MODEL = "1D492999"
MODELS = (MODEL, MODEL + "_a", MODEL + "_d", MODEL + "_r", "SDP_MODEL")
# The regional item LG's model-key readers ask for first from kisscurl on, by the board's broadcast
# (DILE_TVREGION_getTVRegion: 1 ATSC, 2 DVB, 3 ARIB); docs/research/model-key.md.
MODEL_REGIONAL = {"atsc": MODEL + "_a", "dvb": MODEL + "_d", "arib": MODEL + "_r"}
SSG = "77D1DD90"
YOUTUBE = "C1FFB834"
# The DVR's secure key, MStar's sedata item (include/webos-sestore-items.h): 16 bytes.
DVR_KEY = "dvr_key"
DVR_KEY_BYTES = 16
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
MAX_BYTES = 65536
MAX_ITEMS = 256
MAX_BUNDLE = 8 + MAX_ITEMS * (72 + MAX_BYTES)
# Text encodings expand the payload; a complete hex export must fit one document.
MAX_CONFIG_BYTES = 64 * 1024 * 1024
# Keep the existing synthetic identity stable when no model key is provisioned.
DEFAULT_MODEL = b"cJbhRLJFZD9VYKzaaWeU7v1BUOafcEreZkElG7PI3+k="


def effective(identity):
    """Resolve TOML items and the stable synthetic default, which only a VM with no model item of
    any name gets; legacy files are migration-only."""
    missing, items = load(identity)
    if missing == "fallback" and not any(name in items for name in MODELS):
        items[MODEL] = DEFAULT_MODEL
    return missing, items


def ensure_ssg(identity, warn=None):
    """Provision a missing gateway key directly into TOML when fallback permits it."""
    from . import keys

    missing, items = load(identity)
    if missing == "error" or SSG in items:
        return
    value = keys.generate_ssg_key()
    validate_item(SSG, value)
    data = document(identity) or {"missing": "fallback", "items": {}}
    data.setdefault("items", {})[SSG] = {"text": value.decode("ascii")}
    _write(identity, data)
    if warn:
        warn("made a new second screen gateway key in sestore.toml")


def ensure_dvr_key(identity, warn=None):
    """The VM's own DVR key (DVR_KEY: 16 os.urandom bytes, hex in TOML), made when missing and kept;
    an explicit inventory (missing = "error") is left as it is. On the TV it is factory data that
    the TEE decrypts; without it, MStar's DVR middleware (tvservice's DASY) fails to start."""
    missing, items = load(identity)
    if missing == "error" or DVR_KEY in items:
        return
    data = document(identity) or {"missing": "fallback", "items": {}}
    data.setdefault("items", {})[DVR_KEY] = {"hex": os.urandom(DVR_KEY_BYTES).hex()}
    _write(identity, data)
    if warn:
        warn("made a new DVR key in sestore.toml")


def drop_dvr_key(identity):
    """A new identity's DVR key: the old one removed, so the next start makes another (an explicit
    inventory, missing = "error", is left as it is)."""
    data = document(identity)
    if data is None or data.get("missing", "fallback") == "error":
        return
    if data.get("items", {}).pop(DVR_KEY, None) is not None:
        _write(identity, data)


def bundle(missing, items):
    """The boot helper transports this bounded little-endian snapshot directly to the kernel."""
    if missing not in ("error", "fallback") or len(items) > MAX_ITEMS:
        raise Error("invalid policy or more than 256 items")
    out = bytearray(struct.pack("<II", int(missing == "fallback"), len(items)))
    for name, value in sorted(items.items()):
        if not NAME.fullmatch(name) or len(value) > MAX_BYTES:
            raise Error("invalid item name or size")
        out.extend(struct.pack("<65s3xI", name.encode("ascii"), len(value)))
        out.extend(value)
    return bytes(out)


class Error(Exception):
    pass


def _private_read(path, limit=MAX_BYTES):
    """Bounded read; diagnostics never include file contents or parser excerpts."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise Error("expected a regular file with owner-only permissions")
            value = stream.read(limit + 1)
        if len(value) > limit:
            raise Error("file is too large")
        return value
    except OSError as error:
        raise Error(error.strerror) from None


def document(identity):
    return _document(Path(identity) / CONFIG)


def _document(path):
    if not os.path.lexists(path):
        return None
    try:
        data = tomllib.loads(_private_read(path, MAX_CONFIG_BYTES).decode("utf-8"))
    except (ValueError, UnicodeError):
        raise Error("sestore.toml: invalid TOML") from None
    except Error as error:
        raise Error(f"sestore.toml: {error}") from None
    if set(data) - {"missing", "items"}:
        raise Error("sestore.toml: unknown root property")
    if data.get("missing", "fallback") not in ("fallback", "error"):
        raise Error('sestore.toml: missing must be "fallback" or "error"')
    if not isinstance(data.get("items", {}), dict):
        raise Error("sestore.toml: items must be a table")
    if len(data.get("items", {})) > MAX_ITEMS:
        raise Error("sestore.toml: more than 256 items")
    return data


def validate_item(name, value):
    if name in MODELS:
        try:
            # TOML inputs are exact bytes, without the legacy file's optional newline.
            if len(value) != 44:
                raise ValueError()
            modelkey.validate(value)
        except ValueError:
            raise Error("expected 44 canonical base64 characters for a 32-byte model key") from None
    elif name == SSG:
        if not value or len(value) > 1999 or b"\0" in value:
            raise Error("expected nonempty PEM text, at most 1999 bytes, without NUL")
    elif name == DVR_KEY:
        if len(value) != DVR_KEY_BYTES:
            raise Error(f"expected {DVR_KEY_BYTES} bytes")
    elif name == YOUTUBE:
        # Stock DILE replaces the last stored byte in its copied output with NUL.
        # The observed native OLED C2 record ends in LF; authored entries also use NUL.
        part1, separator, part2 = value[:-1].partition(b";")
        if (
            not value.endswith((b"\0", b"\n"))
            or len(value) > 100
            or not separator
            or not part1
            or not part2
            or any(c < 0x21 or c > 0x7E for c in value[:-1])
        ):
            raise Error(
                "expected nonempty printable <part1>;<part2> followed by one NUL or LF, at most 100 bytes"
            )


def load(identity):
    """(missing policy, decoded items); absence is the current fallback behavior."""
    return load_file(Path(identity) / CONFIG)


def load_file(path):
    """Load an explicitly named export; relative item files resolve beside it."""
    path = Path(path)
    identity = path.parent
    data = _document(path)
    if data is None:
        return "fallback", {}
    items = {}
    for name, entry in data.get("items", {}).items():
        if not NAME.fullmatch(name):
            raise Error("sestore.toml: invalid item name")
        try:
            if not isinstance(entry, dict) or set(entry) - {
                "text",
                "hex",
                "file",
                "nul_terminated",
            }:
                raise Error("unknown item property or non-table value")
            sources = set(entry) & {"text", "hex", "file"}
            if len(sources) != 1:
                raise Error("expected exactly one of text, hex or file")
            source = sources.pop()
            if not isinstance(entry[source], str):
                raise Error("source must be a string")
            terminate = entry.get("nul_terminated", False)
            if type(terminate) is not bool:
                raise Error("nul_terminated must be a boolean")
            if source == "text":
                value = entry[source].encode("utf-8")
            elif source == "hex":
                value = bytes.fromhex(entry[source])
            else:
                if not entry[source]:
                    raise Error("file path is empty")
                value = _private_read(Path(identity) / entry[source])
            if terminate:
                value += b"\0"
            if len(value) > MAX_BYTES:
                raise Error("item exceeds 65536 bytes")
            validate_item(name, value)
            items[name] = value
        except (ValueError, UnicodeError):
            raise Error(f"sestore.toml item {name}: invalid source encoding") from None
        except Error as error:
            raise Error(f"sestore.toml item {name}: {error}") from None
    return data.get("missing", "fallback"), items


def export(path, items, missing="error"):
    """Create one owner-only exact-byte TOML export, refusing to replace a file."""
    import json

    bundle(missing, items)
    for name, value in items.items():
        validate_item(name, value)
    content = f'missing = "{missing}"\n\n[items]\n'
    content += "".join(
        json.dumps(name) + ' = { hex = "' + value.hex() + '" }\n'
        for name, value in sorted(items.items())
    )
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".sestore-export-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        if load_file(temporary) != (missing, items):
            raise Error("export verification failed")
        # Publish only a complete validated document; an existing path is never replaced.
        os.link(temporary, path)
    finally:
        os.unlink(temporary)


def _write(identity, data):
    from .tomlfile import toml_value

    quote = toml_value
    text = "# Secure-store inputs. Edit while the VM is off.\n"
    text += "missing = " + quote(data.get("missing", "fallback")) + "\n\n[items]\n"
    for name, entry in data.get("items", {}).items():
        fields = ", ".join(
            key + " = " + (str(value).lower() if type(value) is bool else quote(value))
            for key, value in entry.items()
        )
        text += quote(name) + " = { " + fields + " }\n"
    fd, temporary = tempfile.mkstemp(prefix=".sestore-", dir=identity)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, Path(identity) / CONFIG)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def migrate(identity, merge=False):
    """Import legacy inputs once, or explicitly fill gaps. Keep sources and existing entries."""
    data = document(identity)
    load(identity)  # refuse invalid existing entries before any mutation
    if data is not None and not merge:
        return []
    data = data or {"missing": "fallback", "items": {}}
    items = data.setdefault("items", {})
    imported = []
    for name, filename in ((MODEL, "model-secret"), (SSG, "ssg-key"), (YOUTUBE, "youtube-key")):
        path = Path(identity) / filename
        # Earlier migrations kept SSG as a legacy file reference. Make that import self-contained.
        if name in items and not (name == SSG and items[name] == {"file": filename}):
            continue
        if not os.path.lexists(path):
            continue
        try:
            value = _private_read(path)
            if name == MODEL:
                value = modelkey.validate(value)
                entry = {"text": value.decode("ascii")}
            elif name == YOUTUBE:
                if value.endswith(b"\n"):
                    value = value[:-1]
                validate_item(name, value + b"\0")
                entry = {"text": value.decode("ascii"), "nul_terminated": True}
            else:
                validate_item(name, value)
                entry = {"text": value.decode("ascii")}
            items[name] = entry
            imported.append(name)
        except (ValueError, UnicodeError, Error):
            raise Error(f"cannot migrate {filename}: invalid or unreadable key") from None
    if imported:
        _write(identity, data)
    return imported


def model_item(regional, broadcast):
    """The item a model key is installed as: the board's regional one (MODEL_REGIONAL) where the
    firmware's readers ask for it (regional), else the plain MODEL."""
    return MODEL_REGIONAL.get(broadcast, MODEL) if regional else MODEL


def set_model(identity, value, item=MODEL):
    """The VM's one model key, as item (model_item), replacing every model item; None clears them."""
    data = document(identity) or {"missing": "fallback", "items": {}}
    load(identity)
    items = data.setdefault("items", {})
    for name in MODELS:
        items.pop(name, None)
    if value is not None:
        if item not in MODELS:
            raise Error(f"{item} is not a model key item")
        validate_item(item, value)
        items[item] = {"text": value.decode("ascii")}
    _write(identity, data)


def has_model(identity):
    _missing, items = load(identity)
    return any(name in items for name in MODELS)


def main():
    """Standalone host compiler for provisioning without the VM manager."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Compile VM TOML items into a kernel-store bundle")
    parser.add_argument("identity", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        value = bundle(*effective(args.identity))
        fd, temporary = tempfile.mkstemp(prefix=".sestore-bundle-", dir=args.output.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(value)
            os.replace(temporary, args.output)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    except (Error, OSError) as error:
        print(f"sestore: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
