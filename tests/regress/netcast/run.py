#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""NetCast-line boot check (tests/regress/netcast/README.md): one VM of a NetCast 4.5 or SimpleSmart
firmware, made and driven through the beanjar CLI, must draw its stock dashboard and follow the
remote. PASS or FAIL with the numbers it judged; no step needs a person to look.

  tests/regress/netcast/run.py --firmware FW [--name NAME] [--timeout S] [--out DIR] [--keep]

Steps, each timed from the start:
  create   beanjar create NAME --firmware FW --link off --audio none --tag scratch
  start    beanjar start NAME --reason ...; beanjar vnc start NAME (the remote's front end)
  gal      our GAL's "[libgal] Init" (NetCast 4.5) or "[halgal] Init" (SimpleSmart) in webos.log:
           the firmware initialised its graphics
  ready    run/micom-status ends in ready=1: the firmware's power stack read the micom
  home     the platform's dashboard key (HOME, 0x7c) when the screen is black, then screenshots
           until one is judged the dashboard (judge(), by what the host's accelerator is expected
           to show: EXPECT)
  locale   the country group the browser reports (lb6wk's "locale string" in webos.log) is the
           firmware's own, RELEASE.cfg's szCountryGroupCode: an EEPROM whose group is not the
           build's draws its UI in another group's language order (an EU set came up Swedish,
           with stray glyphs in its labels, on group KR)
  nav      KVM: RIGHT, then a screenshot that is still the dashboard and differs from the last one;
           TCG: SKIP (EXPECT)
  stop     beanjar stop --force, beanjar delete (unless --keep)
Results: OUT/result.json and the screenshots; the exit status is 0 only when every step passed.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

# per platform (vm-info.toml's platform): the GAL's init line, the dashboard key, and per accelerator
# the judge's thresholds on a 320x180 nearest-neighbour reduction of the screenshot (non-black share,
# distinct colours; None: not judged) and whether RIGHT must change the dashboard
PLATFORMS = {
    "NetCast": {"gal": b"[libgal] Init", "key": "0x7c"},
    "SimpleSmart": {"gal": b"[halgal] Init", "key": "0x7c"},
}
# What each host is expected to show (measured; tests/regress/netcast/README.md):
# - KVM (the Pi): NetCast 4.5's populated FX HOME, about 0.59 non-black and 6000 to 7000 colours
#   (the accepted captures of docs/research/netcast45-bringup.md); RIGHT moves its focus.
# - TCG (an x86 PC): FX HOME's grey tile skeleton, 0.56 and about 100 colours, with no text or
#   pictures: fxui's home logs D-Bus reply timeouts and never fills its tiles.
#   Measured at the commit before the presenter regression (4cfa3060) and on the fix alike, for 30
#   minutes after HOME, on a first and a second boot. So TCG judges only that the dashboard is drawn
#   (a black screen or the MyApps tray, 0.12, fail) and skips nav.
# - SimpleSmart: its HOME ribbon over black under both, 0.28 and about 600 colours (KVM 611, TCG
#   583); RIGHT moves its focus (about 16600 pixels change).
EXPECT = {
    ("NetCast", "kvm"): {"nonblack": 0.25, "colours": 2000, "nav": True},
    ("NetCast", "tcg"): {"nonblack": 0.25, "colours": None, "nav": False},
    ("SimpleSmart", "kvm"): {"nonblack": 0.15, "colours": 300, "nav": True},
    ("SimpleSmart", "tcg"): {"nonblack": 0.15, "colours": 300, "nav": True},
}
BLACK = 16  # a pixel whose luma is under this is black


def measure(png):
    """(non-black fraction, distinct colours) of the screenshot png at 320x180."""
    from PIL import Image

    im = Image.open(png).convert("RGB").resize((320, 180), Image.NEAREST)
    hist = im.convert("L").histogram()
    return 1 - sum(hist[:BLACK]) / sum(hist), len(set(im.getdata()))


def judge(nonblack, colours, platform, accel="kvm"):
    """Whether a screenshot of these numbers is the platform's dashboard as accel shows it."""
    e = EXPECT[(platform, accel)]
    return nonblack >= e["nonblack"] and (e["colours"] is None or colours >= e["colours"])


def host_accel():
    """The accelerator a VM of this host runs on (the launcher's own choice)."""
    sys.path.insert(0, str(ROOT / "host" / "core"))
    from beanjar import qemu

    return qemu.default_accel()


LOCALE = re.compile(rb"BROWSER_GetLocaleGroup\) locale group : \d+, locale string : (\w+)")


def country_group(fw_dir):
    """The firmware's country group (RELEASE.cfg's szCountryGroupCode in its mode.pak)."""
    cfg = subprocess.run(
        ["unsquashfs", "-cat", "-no-wildcards", str(Path(fw_dir) / "mode.pak"), "RELEASE.cfg"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    m = re.search(r"^szCountryGroupCode,(\w+)", cfg, re.M)
    if not m:
        raise ValueError("RELEASE.cfg has no szCountryGroupCode")
    return m.group(1)


def firmware_dir(info):
    """The directory of the paks a VM boots, from `beanjar show --json`: the external one its `fw`
    names (a collection name or a directory, used in place), else its own firmware/ (a .epk)."""
    own = Path(info["dir"]) / "firmware"
    if not info.get("fw"):
        return own
    return Path(os.path.normpath(Path(info["dir"]) / os.path.expanduser(info["fw"])))


def reported_group(log):
    """The last country group the browser reported in webos.log's bytes log; None for none."""
    found = LOCALE.findall(log)
    return found[-1].decode() if found else None


def differs(a, b):
    """Pixels that differ between two screenshots (at full size)."""
    from PIL import Image, ImageChops

    d = ImageChops.difference(Image.open(a).convert("RGB"), Image.open(b).convert("RGB"))
    return sum(1 for p in d.convert("L").getdata() if p)


class Check:
    def __init__(self, args):
        self.args = args
        self.name = args.name
        self.out = Path(args.out)
        self.t0 = time.monotonic()
        self.result = {"name": self.name, "firmware": args.firmware, "steps": {}}
        self.vm_dir = None
        self.fw_dir = None
        self.env = dict(os.environ, QT_QPA_PLATFORM="offscreen")

    def now(self):
        return round(time.monotonic() - self.t0, 1)

    def beanjar(self, *argv, check=True):
        p = subprocess.run(
            [sys.executable, "-m", "beanjar", *argv],
            env=dict(self.env, PYTHONPATH=f"{ROOT}/host/core:{ROOT}/host/ui"),
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
        if check and p.returncode:
            raise RuntimeError(f"beanjar {' '.join(argv)}: {p.stderr.strip() or p.returncode}")
        return p.stdout

    def step(self, name, ok, **facts):
        self.result["steps"][name] = dict(facts, verdict="PASS" if ok else "FAIL", at=self.now())
        print(f"{name:6} {'PASS' if ok else 'FAIL'} at {self.now()} s {facts}", flush=True)
        if not ok:
            raise StopIteration(name)

    def wait(self, until, timeout, every=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if until():
                return True
            time.sleep(every)
        return until()

    def shot(self, label):
        path = self.out / f"{label}.png"
        self.beanjar("screenshot", self.name, str(path))
        return path

    def run(self):
        a = self.args
        deadline = self.t0 + a.timeout

        def left():
            return max(0, deadline - time.monotonic())

        self.beanjar(
            "create",
            self.name,
            "--firmware",
            a.firmware,
            "--link",
            "off",
            "--audio",
            "none",
            "--tag",
            "scratch",
        )
        self.created = True
        info = json.loads(self.beanjar("show", self.name, "--json"))
        self.vm_dir = Path(info["dir"])
        self.fw_dir = firmware_dir(info)
        self.step("create", True)
        self.beanjar("start", self.name, "--reason", a.reason)
        port = ("--port", str(a.vnc_port)) if a.vnc_port else ()
        self.beanjar("vnc", "start", self.name, *port)
        vm_info = (self.vm_dir / "run" / "vm-info.toml").read_text()
        platform = next((p for p in PLATFORMS if f'platform = "{p}"' in vm_info), None)
        self.result["platform"] = platform
        self.step("start", platform is not None, platform=platform)
        t = PLATFORMS[platform]
        accel = self.result["accel"] = host_accel()
        e = EXPECT[(platform, accel)]
        log = self.vm_dir / "logs" / "webos.log"
        status = self.vm_dir / "run" / "micom-status"

        def has_gal():
            try:
                return t["gal"] in log.read_bytes()
            except OSError:
                return False

        self.step("gal", self.wait(has_gal, left()))

        def ready():
            try:
                lines = status.read_text().splitlines()
            except OSError:
                return False
            return bool(lines) and "ready=1" in lines[-1].split()

        self.step("ready", self.wait(ready, left()))

        state = {"n": 0, "last": None, "presses": 0}

        def dashboard():
            state["n"] += 1
            png = self.shot(f"home-{state['n']:02}")
            nonblack, colours = measure(png)
            state["last"] = (png, round(nonblack, 4), colours)
            print(f"  shot {png.name}: non-black {nonblack:.4f}, colours {colours}", flush=True)
            if judge(nonblack, colours, platform, accel):
                return True
            if nonblack < e["nonblack"]:  # nothing of it shows: (again) the key
                self.beanjar("remote", "press", self.name, t["key"])
                state["presses"] += 1
            return False

        ok = self.wait(dashboard, left(), every=a.settle)
        png, nonblack, colours = state["last"] or (None, 0, 0)
        self.step(
            "home",
            ok,
            screenshot=str(png),
            nonblack=nonblack,
            colours=colours,
            presses=state["presses"],
            shots=state["n"],
        )
        want = country_group(self.fw_dir)
        self.wait(lambda: reported_group(log.read_bytes()) is not None, min(left(), 300))
        got = reported_group(log.read_bytes())
        self.step("locale", got == want, firmware=want, reported=got)
        if not e["nav"]:
            self.result["steps"]["nav"] = {
                "verdict": "SKIP",
                "why": f"{accel}: the dashboard is not filled in (EXPECT)",
            }
            print(f"nav    SKIP ({accel}: the dashboard is not filled in)", flush=True)
            return
        self.beanjar("remote", "press", self.name, "RIGHT")
        time.sleep(a.settle)
        nav = self.shot("nav")
        nb, col = measure(nav)
        changed = differs(png, nav)
        self.step(
            "nav",
            judge(nb, col, platform, accel) and changed > 0,
            screenshot=str(nav),
            nonblack=round(nb, 4),
            colours=col,
            changed_pixels=changed,
        )

    def cleanup(self):
        if not getattr(self, "created", False):
            return
        for argv in (("vnc", "stop", self.name), ("stop", self.name, "--force")):
            self.beanjar(*argv, check=False)
        if self.vm_dir is not None:
            log = self.vm_dir / "logs" / "webos.log"
            if log.exists():
                (self.out / "webos.log").write_bytes(log.read_bytes())
        if not self.args.keep:
            self.beanjar("delete", self.name, check=False)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--firmware", required=True, help="a .epk, extracted paks or a collection name")
    p.add_argument("--name", default=f"netcast-check-{os.getpid()}", help="the VM's name")
    p.add_argument("--timeout", type=float, default=2400, help="seconds for everything up to nav")
    p.add_argument("--settle", type=float, default=30, help="seconds between screenshots")
    p.add_argument("--reason", default="NetCast boot check", help="beanjar start --reason")
    p.add_argument("--out", help="result directory (default build/regress/netcast/<name>)")
    p.add_argument(
        "--vnc-port", type=int, help="beanjar vnc start --port (where something else has 5900)"
    )
    p.add_argument("--keep", action="store_true", help="keep the VM (stopped) afterwards")
    a = p.parse_args(argv)
    a.out = a.out or str(ROOT / "build" / "regress" / "netcast" / a.name)
    Path(a.out).mkdir(parents=True, exist_ok=True)
    c = Check(a)
    ok = False
    try:
        c.run()
        ok = True
    except StopIteration:
        pass
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as e:
        # a step that could not run: FAIL, with why
        c.result["error"] = str(e)
        print(f"error: {e}", file=sys.stderr)
    finally:
        c.cleanup()
    c.result["verdict"] = "PASS" if ok else "FAIL"
    c.result["seconds"] = c.now()
    (Path(a.out) / "result.json").write_text(json.dumps(c.result, indent=2) + "\n")
    print(f"{c.result['verdict']} in {c.result['seconds']} s: {a.out}/result.json")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
