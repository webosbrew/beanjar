#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Bounded evidence report for ONE save-logs snapshot; never a coverage verdict.

  python3 tests/regress/lib/device_census.py SAVED_SNAPSHOT [--target TARGET]
  python3 tests/regress/lib/device_census.py SAVED_PARENT --latest [--target TARGET]

--latest selects the newest .done marker (mtime, then name) from at most 64 children.
Counters are observations, not proof of modeled behavior. Log occurrences are not
request counts (the same kernel message may occur in several captured files).
"""

import argparse
import json
import os
import re
import stat
from pathlib import Path

MAX_FILES = 64
MAX_BYTES = 2 * 1024 * 1024
MAX_TOTAL = 16 * 1024 * 1024
MAX_ROWS = 2048
MAX_LINE = 4096
SOURCES = {
    "mstar-ioctls": (
        "/proc/mstar-utopia/ioctls",
        "guest/kernel/modules/mstar-utopia/diagnostics.c:ioctls_show",
    ),
    "utopia": ("/proc/utopia", "guest/kernel/modules/mstar-utopia/utopia.c:utopia_show"),
    "mstar-tee": ("/proc/mstar-utopia/tee", "guest/kernel/modules/mstar-utopia/mailbox.c:tee_read"),
    "webos-properties": (
        "/proc/webos_tv/props",
        "guest/kernel/modules/webos_tv/webos_tv.c:props_show",
    ),
    "rtk-ioctls": ("/proc/rtk/ioctl", "guest/kernel/modules/rtk/rtk-main.c:ioctl_show"),
    "rtk-rpc": ("/proc/rtk/rpc", "guest/kernel/modules/rtk/rtk-rpc.c:rpc_show"),
    "rtk-tee": ("/proc/rtk/tee", "guest/kernel/modules/rtk/tee-ta.c:tee_show"),
}
HEX = r"(?:0x[0-9a-fA-F]+|0)"
TOKEN = r"[a-zA-Z0-9_./:-]{1,80}"
UNSUPPORTED = re.compile(
    r"(?P<module>[\w-]+): (?P<device>"
    + TOKEN
    + r"): ioctl (?P<command>"
    + HEX
    + r")(?: \([^\n]*?\))? not emulated: (?P<result>-?\d+)"
)
TEE_UNKNOWN = re.compile(
    r'tee: session "(' + TOKEN + r')" command (\d+) \(from [^)]*\) not implemented: failed'
)
# Only explicit failure text associated with a /dev, /proc or /sys path is evidence. Generic
# mount/stat warnings and messages about missing application files are excluded.
PATH = r"/(?:dev|proc|sys)/[a-zA-Z0-9_./-]+"
OPEN_FAILURE = re.compile(
    r"(?:cannot|can.t|could not|failed to|unable to)\s+open\b[^\n]{0,100}?(?P<first>" + PATH + r")"
    r"|open(?:\s*\(|\s+)[\"\x27]?(?P<second>"
    + PATH
    + r")[^\n]{0,160}?(?:failed|failure|No such file|Permission denied|errno\s*[=:]\s*\d+)"
    r"|(?P<third>" + PATH + r")\s+open\s+(?:failed|failure)",
    re.I,
)
INTERFACE_ERROR = re.compile(
    r"(?P<path>"
    + PATH
    + r")(?P<error>\s+access\s+Error|:\s*Can.t lookup blockdev|:\s*No such device or address)",
    re.I,
)


def command(value):
    return f"0x{int(value, 16):x}"


def disposition(device, cmd, result=None):
    if device == "system_lg" and cmd == "0x80044c02":
        return {
            "meaning": "IOCTL_SYS_LG_PCMCIA_CONFIG; emulator omits PCMCIA MMIO effects; necessity unclassified",
            "source": "guest/kernel/modules/lgemu/drv.c:lgemu_ioctl_unhandled",
            "vendor_source": "drivers/mstar2/include/mdrv_system_io_LG.h; drv/system_LG/mdrv_system_io_LG.c; drv/system_LG/mdrv_system_LG.c",
        }
    if device == "atv" and cmd in ("0xc0405602", "0x80045626"):
        return {
            "meaning": "expected ENOTTY by the current ATV stand-in contract",
            "source": [
                "guest/kernel/modules/webos_tv/webos_tv.c:atv_ioctl",
            ],
            "vendor_scope": "Examined W21P GPL disables these handlers; not selected W22H source proof",
            "expected_result": -25,
            "observed_result": "not-recorded"
            if result is None
            else "matches"
            if result == -25
            else "discrepancy",
        }
    return {"meaning": "unclassified"}


def counter_line(key, line):
    """Parse the source-emitted formats; absence of a marker never means success."""
    row = {"interpretation": "observed; behavior unclassified"}
    if key in ("mstar-ioctls", "webos-properties"):
        m = re.fullmatch(r"(" + TOKEN + r") (" + HEX + r") (\d+)( not-emulated)?", line)
        if not m:
            return None
        row.update(
            interface=m[1],
            command=command(m[2]),
            count=int(m[3]),
            not_emulated=bool(m[4]),
            disposition=disposition(m[1], command(m[2])),
        )
    elif key == "utopia":
        m = re.fullmatch(r"module (\d+) command (\d+): (\d+)", line)
        if not m:
            return None
        row.update(module=int(m[1]), command=int(m[2]), count=int(m[3]))
    elif key == "rtk-ioctls":
        m = re.fullmatch(r"(" + TOKEN + r") ioctl (" + HEX + r"): (\d+)", line)
        if not m:
            return None
        row.update(interface=m[1], command=command(m[2]), count=int(m[3]))
    elif key == "rtk-rpc":
        m = re.fullmatch(
            r"program (\d+) procedure (\d+): (\d+) \((\d+) bytes(, not given)?\)", line
        )
        if not m:
            return None
        row.update(
            program=int(m[1]),
            procedure=int(m[2]),
            count=int(m[3]),
            response_bytes=int(m[4]),
            response_table_entry=not bool(m[5]),
        )
    elif key == "mstar-tee":
        m = re.fullmatch(
            r"(" + TOKEN + r") (command|query) (\d+): (\d+), (done|failed) \(first from [^)]*\)",
            line,
        )
        if not m:
            return None
        row.update(interface=m[1], operation=m[2], command=int(m[3]), count=int(m[4]), outcome=m[5])
    elif key == "rtk-tee":
        m = re.fullmatch(
            r"(" + TOKEN + r") (open|command (" + HEX + r")): (\d+) \((" + HEX + r")\)", line
        )
        if not m:
            return None
        row.update(
            interface=m[1],
            operation="open" if m[2] == "open" else "command",
            count=int(m[4]),
            result=command(m[5]),
        )
        if m[3]:
            row["command"] = command(m[3])
    else:
        return None
    return row


class Reader:
    def __init__(self, root):
        self.root = root
        self.remaining = MAX_TOTAL
        self.issues = []
        self.inputs = []

    def text(self, path):
        rel = path.relative_to(self.root).as_posix()
        try:
            if (
                any(
                    p.is_symlink()
                    for p in [path, *path.parents]
                    if p != self.root and self.root in p.parents
                )
                or not path.is_file()
            ):
                self.issues.append({"file": rel, "status": "missing-or-nonregular"})
                return None
            limit = min(MAX_BYTES, self.remaining)
            with path.open("rb") as f:
                data = f.read(limit + 1)
            truncated = len(data) > limit
            data = data[:limit]
            self.remaining -= len(data)
            self.inputs.append({"file": rel, "bytes_read": len(data), "truncated": truncated})
            if truncated:
                self.issues.append({"file": rel, "status": "truncated"})
                data = data.rsplit(b"\n", 1)[0] if b"\n" in data else b""
            return data.decode("utf-8", errors="replace")
        except OSError:
            self.issues.append({"file": rel, "status": "read-failed"})
            return None


def select_latest(parent):
    entries = []
    try:
        if parent.is_symlink():
            return None, "saved-parent-symlink"
        with os.scandir(parent) as it:
            for e in it:
                if len(entries) >= MAX_FILES:
                    return None, "snapshot-selection-limit"
                entries.append(e.name)
    except OSError:
        return None, "saved-parent-unavailable"
    complete = []
    for name in sorted(entries):
        candidate = parent / name
        marker = candidate / ".done"
        try:
            if not stat.S_ISDIR(candidate.lstat().st_mode):
                continue
            # lstat preserves errors and never follows a completion-marker link.
            try:
                metadata = marker.lstat()
            except FileNotFoundError:
                continue  # An unfinished snapshot is not eligible.
            if not stat.S_ISREG(metadata.st_mode):
                return None, "completion-metadata-invalid"
            complete.append((metadata.st_mtime_ns, name, candidate))
        except OSError:
            return None, "completion-metadata-unavailable"
    return (max(complete)[2], None) if complete else (None, "completed-snapshot-unavailable")


def report(root, target=None):
    root = Path(root)
    reader = Reader(root)
    counters, unknown, opens, interface_errors = [], {}, {}, {}
    if not (root / ".done").is_file():
        reader.issues.append({"file": ".done", "status": "completion-unconfirmed"})
    if (root / "device-counters-error.txt").is_file():
        reader.issues.append({"file": "device-counters-error.txt", "status": "collector-failed"})
    namespaces = [n for n in ("outer", "firmware") if (root / "device-counters" / n).is_dir()]
    for namespace in namespaces or ["firmware"]:
        directory = root / "device-counters" / namespace
        inventory = reader.text(directory / "inventory.tsv")
        observed = {}
        if inventory is not None:
            for line in inventory.splitlines():
                fields = line.split("\t")
                if len(fields) == 3:
                    observed[fields[0]] = fields[1:]
                else:
                    reader.issues.append(
                        {
                            "file": f"device-counters/{namespace}/inventory.tsv",
                            "status": "malformed-inventory",
                        }
                    )
        for key, (proc, source) in SOURCES.items():
            status, filename = observed.get(proc, ("not-collected", key + ".txt"))
            table = {
                "namespace": namespace,
                "path": proc,
                "status": status,
                "source": source,
                "rows": [],
            }
            counters.append(table)
            if status not in (
                "available",
                "absent",
                "unreadable",
                "read-failed",
                "write-failed",
                "timeout-unavailable",
                "not-collected",
            ):
                table["status"] = "invalid-inventory-status"
                reader.issues.append(
                    {
                        "file": f"device-counters/{namespace}/inventory.tsv",
                        "status": "invalid-inventory-status",
                    }
                )
                continue
            if status != "available":
                continue
            if filename != key + ".txt":
                table["status"] = "invalid-inventory-path"
                reader.issues.append(
                    {
                        "file": f"device-counters/{namespace}/inventory.tsv",
                        "status": "invalid-inventory-path",
                    }
                )
                continue
            content = reader.text(directory / filename)
            if content is None:
                table["status"] = "captured-file-unavailable"
                continue
            unparsed = 0
            for number, line in enumerate(content.splitlines(), 1):
                if len(table["rows"]) >= MAX_ROWS:
                    reader.issues.append(
                        {"file": f"device-counters/{namespace}/{filename}", "status": "row-limit"}
                    )
                    break
                row = counter_line(key, line) if len(line) <= MAX_LINE else None
                if row is None:
                    unparsed += 1
                else:
                    row["line"] = number
                    table["rows"].append(row)
            if unparsed:
                table["unparsed_lines"] = unparsed
                reader.issues.append(
                    {
                        "file": f"device-counters/{namespace}/{filename}",
                        "status": "unparsed-counter-lines",
                    }
                )
    logfiles = [root / "dmesg.txt"]
    optional_logs = [
        {"file": name, "status": "present" if (root / name).is_file() else "not-captured"}
        for name in ("console.log", "webos.log")
    ]
    logfiles.extend(root / e["file"] for e in optional_logs if e["status"] == "present")
    # Only immediate regular files in known log directories. No recursive tree walk.
    for folder in ("var-log", "tmp-logs"):
        directory = root / folder
        try:
            with os.scandir(directory) as it:
                names = []
                entries = 0
                for entry in it:
                    entries += 1
                    if entries > MAX_FILES:
                        reader.issues.append({"file": folder, "status": "file-limit"})
                        names = []
                        break
                    if entry.is_file(follow_symlinks=False):
                        names.append(entry.name)
            logfiles.extend(directory / n for n in sorted(names))
        except OSError:
            reader.issues.append({"file": folder, "status": "directory-unavailable"})
    for path in logfiles[:MAX_FILES]:
        text = reader.text(path)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if len(line) > MAX_LINE:
                reader.issues.append(
                    {"file": path.relative_to(root).as_posix(), "status": "long-line-skipped"}
                )
                continue
            location = {"file": path.relative_to(root).as_posix(), "line": number}
            match = UNSUPPORTED.search(line)
            tee = TEE_UNKNOWN.search(line)
            if match:
                record = {
                    "kind": "ioctl",
                    "module": match["module"],
                    "interface": match["device"],
                    "command": command(match["command"]),
                    "reported_result": int(match["result"]),
                    "source": "guest/kernel/modules/lgemu/drv.c:lgemu_ioctl_unhandled",
                    "disposition": disposition(
                        match["device"], command(match["command"]), int(match["result"])
                    ),
                }
            elif tee:
                record = {
                    "kind": "tee",
                    "interface": tee[1],
                    "command": int(tee[2]),
                    "outcome": "failed",
                    "source": "guest/kernel/modules/mstar-utopia/mailbox.c:tee_count",
                }
            else:
                record = None
            if record:
                if not add_event(unknown, record, location):
                    reader.issues.append({"file": "unknown_commands", "status": "event-limit"})
            failure = OPEN_FAILURE.search(line)
            if failure:
                device = failure["first"] or failure["second"] or failure["third"]
                error = next(
                    (e for e in ("No such file", "Permission denied") if e.lower() in line.lower()),
                    "unspecified",
                )
                if not add_event(
                    opens,
                    {
                        "path": device,
                        "error": error,
                        "operation": "open",
                        "disposition": "unclassified",
                    },
                    location,
                ):
                    reader.issues.append({"file": "failed_device_opens", "status": "event-limit"})
            interface = INTERFACE_ERROR.search(line)
            if interface:
                kind = (
                    "access"
                    if "access" in interface["error"].lower()
                    else "lookup"
                    if "lookup" in interface["error"].lower()
                    else "device-error"
                )
                if not add_event(
                    interface_errors,
                    {"path": interface["path"], "operation": kind, "disposition": "unclassified"},
                    location,
                ):
                    reader.issues.append(
                        {"file": "logged_interface_errors", "status": "event-limit"}
                    )
    if len(logfiles) > MAX_FILES:
        reader.issues.append({"file": "logs", "status": "file-limit"})
    absent = [t["path"] for t in counters if t["status"] not in ("available", "absent")]
    partial = bool(reader.issues or absent)
    return {
        "schema": 1,
        "snapshot": root.name,
        "target": target,
        "status": "partial-evidence" if partial else "captured-evidence",
        "limits": {"max_files": MAX_FILES, "bytes_per_file": MAX_BYTES, "total_bytes": MAX_TOTAL},
        "scope": "Captured requests only; missing opens can be unlogged. Empty findings do not establish coverage or absence.",
        "counter_limitations": [
            "LG115x has no request counter table",
            "lgemu has no independent request counter table",
            "Unmarked ioctl/UTopia/RPC observations do not prove modeled behavior",
            "Counter tables have finite capacity; capture does not prove every request was retained",
            "Pathless/multiline open errors are not attributed; mount/stat warnings are excluded",
            "Explicitly inventoried absent tables are capture outcomes, not proof that their interfaces are unused",
        ],
        "unsupported_counter_rows": sum(
            bool(r.get("not_emulated")) for t in counters for r in t["rows"]
        ),
        "counters": counters,
        "unknown_commands": list(unknown.values()),
        "failed_device_opens": list(opens.values()),
        "logged_interface_errors": list(interface_errors.values()),
        "optional_logs": optional_logs,
        "inputs": reader.inputs,
        "issues": list({json.dumps(i, sort_keys=True): i for i in reader.issues}.values()),
    }


def add_event(events, record, location):
    key = json.dumps(record, sort_keys=True)
    if key not in events:
        if len(events) >= MAX_ROWS:
            return False
        events[key] = dict(record, log_occurrences=0, locations=[])
    event = events[key]
    event["log_occurrences"] += 1
    if len(event["locations"]) < 4:
        event["locations"].append(location)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--latest", action="store_true")
    parser.add_argument("--target")
    args = parser.parse_args()
    root = args.snapshot
    if args.latest:
        root, error = select_latest(root)
        if error:
            print(
                json.dumps(
                    {"schema": 1, "status": "unavailable-evidence", "issues": [{"status": error}]}
                )
            )
            return
    print(json.dumps(report(root, args.target), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
