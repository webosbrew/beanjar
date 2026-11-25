#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Check the hbchannel layer without a guest: build-layer.sh on a small IPK of the release's shape
(and on the pinned release, if Meson's package cache has it) with each capture of a real install
(captured/<release>), what the captures hold, the boot jobs, hbchannel-install's copy into place, and
hbchannel-start's key placement with a stand-in startup.sh."""

import configparser
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

TOP = Path(__file__).resolve().parents[3]
HERE = TOP / "guest/layers/hbchannel"  # the layer under test

sys.path.insert(0, str(TOP / "host/core"))
from beanjar.targets import WEBOS_ORDER  # noqa: E402

BUILD = HERE / "build-layer.sh"
ROOT = HERE / "rootfs"
APP = "org.webosbrew.hbchannel"
SVC = APP + ".service"
APPS = "/media/developer/apps"
SVC_DIR = f"{APPS}/usr/palm/services/{SVC}"
STAGE = "usr/share/beanjar/hbchannel"
DEV = "/var/luna-service2-dev"
CACHE = Path(os.environ.get("MESON_PACKAGE_CACHE_DIR", TOP / "subprojects/packagecache"))
VERSIONS_DIR = HERE / "versions"


def release_env(version):
    """versions/<version>/release.env's pin: {VERSION, TAG, URL, SHA256}, as sh reads it."""
    out = subprocess.run(
        [
            "sh",
            "-c",
            '. "$0"; printf "%s\\n" "$VERSION" "$TAG" "$URL" "$SHA256"',
            str(VERSIONS_DIR / version / "release.env"),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split("\n")
    return dict(zip(("VERSION", "TAG", "URL", "SHA256"), out))


# the HBC releases the layer can be built with, and the default one
VERSIONS = {p.name: release_env(p.name) for p in sorted(VERSIONS_DIR.iterdir()) if p.is_dir()}
DEFAULT = (VERSIONS_DIR / "default").read_text().strip()


def ipk_of(version):
    return CACHE / f"{VERSIONS[version]['SHA256']}-{Path(VERSIONS[version]['URL']).name}"


def captures(version):
    """An HBC release's captures of a real install, in LG's order of webOS releases."""
    return sorted(
        (p for p in (VERSIONS_DIR / version / "captured").iterdir() if p.is_dir()),
        key=lambda p: WEBOS_ORDER.index(p.name),
    )


CAPTURES = [c for v in VERSIONS for c in captures(v)]


def release_map(version):
    """versions/<version>/releases: {webOS release: its capture's name}."""
    lines = (VERSIONS_DIR / version / "releases").read_text().splitlines()
    targets = [line.split() for line in lines if line.strip() and not line.startswith("#")]
    return dict(targets)


def version_of(capture):
    return capture.parent.parent.name


def opkg_dir(root):
    """opkg's directory under a tree's /media/developer/apps: var/lib/opkg, usr/lib/opkg on the
    releases up to dreadlocks2."""
    (d,) = [p for p in (root / APPS.lstrip("/")).glob("*/lib/opkg") if p.is_dir()]
    return d


def tar_gz(files):
    """A .tar.gz of {path: (text, mode)}, directories as tar makes them."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        dirs = sorted({str(p) for f in files for p in Path(f).parents if str(p) != "."})
        for d in dirs:
            info = tarfile.TarInfo(d + "/")
            info.type, info.mode = tarfile.DIRTYPE, 0o777  # the release's are 0777 too
            tar.addfile(info)
        for name, (text, mode) in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


def make_ipk(path, package=APP, version="9.8.7"):
    """An IPK as the release's: ar of debian-binary, control.tar.gz (./control), data.tar.gz."""
    work = Path(tempfile.mkdtemp())
    try:
        (work / "debian-binary").write_text("2.0\n")
        (work / "control.tar.gz").write_bytes(
            tar_gz({"./control": (f"Package: {package}\nVersion: {version}\n", 0o644)})
        )
        (work / "data.tar.gz").write_bytes(
            tar_gz(
                {
                    f"usr/palm/applications/{APP}/appinfo.json": (
                        json.dumps({"id": APP, "version": version, "type": "web"}, indent=4),
                        0o644,
                    ),
                    f"usr/palm/packages/{APP}/packageinfo.json": (
                        json.dumps({"id": APP, "services": [SVC]}),
                        0o644,
                    ),
                    f"usr/palm/services/{SVC}/startup.sh": ("#!/bin/bash\n", 0o755),
                    f"usr/palm/services/{SVC}/run-js-service": ("#!/bin/sh\n", 0o755),
                    f"usr/palm/services/{SVC}/bin/dropbear": ("", 0o755),
                }
            )
        )
        subprocess.run(
            ["ar", "rc", str(path), "debian-binary", "control.tar.gz", "data.tar.gz"],
            cwd=work,
            check=True,
        )
    finally:
        shutil.rmtree(work)
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build(dest, capture, ipk=None, sha=None, check=True):
    env = dict(os.environ, DEST=str(dest), CAPTURE=str(capture), HBC=version_of(capture))
    env.pop("IPK", None)
    if ipk:
        env.update(IPK=str(ipk), IPK_SHA256=sha)
    return subprocess.run(["sh", str(BUILD)], env=env, check=check, capture_output=True, text=True)


def tree(root):
    """{path relative to root: bytes} of every file under root."""
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def ini(path):
    c = configparser.ConfigParser(strict=False, interpolation=None)
    c.optionxform = str
    c.read(path)
    return c


class LayerTest(unittest.TestCase):
    """build-layer.sh with each capture (captured/<release>) and a small IPK of the release's shape."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hbchannel-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.ipk = self.tmp / "hb.ipk"
        self.sha = make_ipk(self.ipk)

    def layer(self, capture):
        dest = self.tmp / version_of(capture) / capture.name
        build(dest, capture, self.ipk, self.sha)
        return dest

    def staged(self, dest, path):
        return dest / STAGE / path.lstrip("/")

    def check_app(self, dest):
        """The IPK's data staged as dev/install unpacks it; nothing at a system app path, and
        nothing directly under /media or /var, which the state disks hide."""
        self.assertTrue(
            self.staged(dest, f"{APPS}/usr/palm/applications/{APP}/appinfo.json").is_file()
        )
        self.assertTrue(
            self.staged(dest, f"{APPS}/usr/palm/packages/{APP}/packageinfo.json").is_file()
        )
        for f in ("startup.sh", "run-js-service", "bin/dropbear"):
            self.assertTrue(os.access(self.staged(dest, f"{SVC_DIR}/{f}"), os.X_OK), f)
        self.assertEqual((dest / STAGE / "ipk.sha256").read_text(), self.sha + "\n")
        for p in [dest] + list(dest.rglob("*")):
            if not p.is_symlink():
                self.assertFalse(p.stat().st_mode & 0o022, f"{p}: group or world writable")
        for gone in (
            "media",
            "var",
            "usr/palm",
            "usr/share/luna-service2",
            "usr/share/dbus-1",
            "usr/share/ls2",
        ):
            self.assertFalse((dest / gone).exists(), gone)

    JOBS = [
        "etc/init/emulator-hbchannel-install.conf",
        "etc/init/emulator-hbchannel.conf",
        "etc/systemd/system/basic.target.d/beanjar-hbchannel.conf",
        "etc/systemd/system/emulator-hbchannel-install.service",
        "etc/systemd/system/emulator-hbchannel.service",
        "etc/systemd/system/sam.service.d/beanjar-hbchannel.conf",
        "usr/libexec/beanjar/hbchannel-install",
        "usr/libexec/beanjar/hbchannel-start",
        f"{STAGE}/ipk.sha256",
    ]

    def test_captured_files_staged_as_written(self):
        """Besides the IPK's data and the jobs, the layer has exactly the capture's files, byte for
        byte: nothing written by hand."""
        for capture in CAPTURES:
            with self.subTest(f"{version_of(capture)} {capture.name}"):
                dest = self.layer(capture)
                self.check_app(dest)
                files = {
                    k: v
                    for k, v in tree(dest).items()
                    if not k.startswith(f"{STAGE}{APPS}/usr/palm/")
                }
                want = {f"{STAGE}/{k}": v for k, v in tree(capture / "files").items()}
                want.update({k: (dest / k).read_bytes() for k in self.JOBS})
                self.assertEqual(files, want)

    def test_refusals(self):
        capture = captures(DEFAULT)[-1]
        out = build(self.tmp / "x", capture, self.ipk, "0" * 64, check=False)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("expected " + "0" * 64, out.stderr)
        self.assertFalse(
            (self.tmp / "x").exists(), "nothing assembled from an IPK that fails its check"
        )
        other = self.tmp / "other.ipk"
        out = build(
            self.tmp / "y", capture, other, make_ipk(other, "org.example.other"), check=False
        )
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("is not " + APP, out.stderr)
        self.assertFalse((self.tmp / "y").exists())
        stale = self.tmp / "versions" / version_of(capture) / "captured" / capture.name
        shutil.copytree(capture, stale)
        (stale / "source.txt").write_text("firmware x\nipk " + "1" * 64 + "\n")
        out = build(self.tmp / "z", stale, self.ipk, self.sha, check=False)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("run capture.sh", out.stderr)
        self.assertFalse((self.tmp / "z").exists())

    def test_meson_builds_every_version(self):
        """meson.build's hbchannel_versions are the directories of versions/, default among them."""
        text = (HERE / "meson.build").read_text()
        (listed,) = re.findall(r"^hbchannel_versions = \[([^\]]*)\]", text, re.MULTILINE)
        self.assertEqual(sorted(re.findall(r"'([^']+)'", listed)), sorted(VERSIONS))

    def test_pin(self):
        """Each HBC release pins its IPK by tag and sha256, and its captures are of that IPK."""
        self.assertIn(DEFAULT, VERSIONS)
        for version, pin in VERSIONS.items():
            with self.subTest(version):
                self.assertEqual(pin["TAG"], "v" + version)
                self.assertRegex(pin["TAG"], rf"^v{re.escape(pin['VERSION'])}(-|$)")
                self.assertRegex(
                    pin["URL"],
                    rf"^https://github.com/[^/]+/webos-homebrew-channel/releases/download/"
                    rf"{re.escape(pin['TAG'])}/org.webosbrew.hbchannel_{re.escape(pin['VERSION'])}_all.ipk$",
                )
                self.assertRegex(pin["SHA256"], "^[0-9a-f]{64}$")
                for capture in captures(version):
                    self.assertIn(
                        f"\nipk {pin['SHA256']}\n",
                        "\n" + (capture / "source.txt").read_text(),
                        capture,
                    )


class CaptureTest(unittest.TestCase):
    """What the captures hold, so a capture that missed a step (no elevation, no install) shows."""

    def test_every_release_has_one(self):
        """versions/<version>/releases maps every webOS release to a capture, each capture is used
        and named after the first release (in LG's order) that it is the capture of."""
        for version in VERSIONS:
            with self.subTest(version):
                m = release_map(version)
                self.assertEqual(list(m), list(WEBOS_ORDER))
                self.assertEqual(set(m.values()), {c.name for c in captures(version)})
                for name in m.values():
                    self.assertEqual(next(r for r in WEBOS_ORDER if m[r] == name), name)

    def test_installed_and_elevated(self):
        for capture in CAPTURES:
            with self.subTest(f"{version_of(capture)} {capture.name}"):
                files = capture / "files"
                opkg = opkg_dir(files)
                self.assertIn(f"Package: {APP}\n", (opkg / "status").read_text())
                self.assertIn(f"Package: {APP}\n", (opkg / f"info/{APP}.control").read_text())
                services = [p for p in files.rglob(f"{SVC}.service") if p.is_file()]
                self.assertTrue(services)
                for svc in services:  # elevate-service: the app's own wrapper, no jailer
                    s = ini(svc)["D-BUS Service"]
                    self.assertEqual(s["Exec"], f"{SVC_DIR}/run-js-service -n {SVC_DIR}")
                roles = [p for p in files.rglob("*.json") if "/roles" in str(p) and SVC in p.name]
                self.assertTrue(roles)
                for role in roles:
                    r = json.loads(role.read_text())
                    names = r.get("role", r)["allowedNames"]
                    self.assertIn("*", names, role)
                since_080 = tuple(
                    map(int, VERSIONS[version_of(capture)]["VERSION"].split("."))
                ) >= (0, 8)
                self.assertEqual(
                    {p.name for p in files.rglob(f"{SVC}.api.lunabus.query.json")},
                    {f"{SVC}.api.lunabus.query.json"}
                    if since_080 and (files / DEV.lstrip("/")).is_dir()
                    else set(),
                    "0.8.0's elevate-service grant, wherever api-permissions.d exists",
                )

    def test_manifest_names_every_file(self):
        for capture in CAPTURES:
            d = capture / "files" / DEV.lstrip("/")
            m = d / f"manifests.d/{APP}.json"
            if not m.is_file():
                continue
            with self.subTest(f"{version_of(capture)} {capture.name}"):
                manifest = json.loads(m.read_text())
                listed = sorted(
                    f
                    for k in (
                        "serviceFiles",
                        "roleFiles",
                        "clientPermissionFiles",
                        "apiPermissionFiles",
                    )
                    for f in manifest[k]
                )
                on_disk = sorted(
                    DEV + "/" + str(p.relative_to(d))
                    for p in d.rglob("*")
                    if p.is_file() and "manifests.d" not in str(p)
                )
                self.assertEqual(listed, on_disk)

    def test_captures_differ(self):
        """One capture per distinct result: no two captures of a version hold the same files."""
        for version in VERSIONS:
            c = captures(version)
            for i, a in enumerate(c):
                for b in c[i + 1 :]:
                    self.assertNotEqual(
                        strip_time(tree(a / "files")), strip_time(tree(b / "files")), (a, b)
                    )


def strip_time(files):
    """The files without opkg's Installed-Time, which differs between any two installs."""
    return {k: re.sub(rb"\nInstalled-Time: \d+\n", b"\n", v) for k, v in files.items()}


class ReleaseBase:
    """An HBC release's own IPK: what the layer relies on in HBC's own files (one subclass per
    versions/ directory, skipped while Meson's package cache lacks its IPK)."""

    version = None

    @classmethod
    def setUpClass(cls):
        if not ipk_of(cls.version).is_file():
            raise unittest.SkipTest(
                f"{ipk_of(cls.version)} not downloaded (the guest build fetches it)"
            )
        cls.tmp = Path(tempfile.mkdtemp(prefix="hbchannel-release-"))
        build(cls.tmp / "layer", captures(cls.version)[-1])  # its IPK and its pinned hash
        cls.svc = cls.tmp / "layer" / STAGE / SVC_DIR.lstrip("/")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_payload_as_dev_install_unpacked_it(self):
        """The layer's unpacked IPK is what dev/install unpacked on every captured release: the same
        entries and contents, the same executables (the modes otherwise without group and world
        write, and the owner root)."""
        root = self.tmp / "layer" / STAGE
        top = root / APPS.lstrip("/") / "usr/palm"
        mine = {}
        for p in [top] + sorted(top.rglob("*")):
            kind = "d" if p.is_dir() else "f"
            digest = hashlib.sha256(p.read_bytes()).hexdigest() if kind == "f" else "-"
            mine["/" + str(p.relative_to(root))] = (kind, p.stat().st_mode & 0o111, digest)
        for capture in captures(self.version):
            with self.subTest(capture.name):
                theirs = {}
                for line in (capture / "payload.txt").read_text().splitlines():
                    kind, mode, _, digest, path = line.split(" ", 4)
                    theirs[path] = (kind, int(mode, 8) & 0o111, digest)
                self.assertEqual(mine, theirs)

    def test_startup_from_dev_path(self):
        """startup.sh's SERVICE_DIR defaults to the dev install's path, where the layer puts it;
        it starts Dropbear without -p (port 22) only with webosbrew_sshd_enabled, sets the
        placeholder password only without authorized_keys, and skips elevation with SKIP_ELEVATION."""
        s = (self.svc / "startup.sh").read_text()
        self.assertIn(f'SERVICE_DIR="${{SERVICE_DIR-{SVC_DIR}}}"', s)
        sshd = s[s.index("webosbrew_sshd_enabled") :]
        self.assertRegex(sshd.split("\n")[2], r'^\s*"\$\{SERVICE_DIR\}/bin/dropbear" -R 200>&-$')
        self.assertIn("if [ ! -f /home/root/.ssh/authorized_keys ]; then", s)
        self.assertIn('if [[ -z "${SKIP_ELEVATION}" && -x "${elevate_script}" ]]', s)

    def test_root_check_and_unjailed_wrapper(self):
        """The service is root by getuid() == 0; the wrapper runs the stock run-js-service with
        third-party jails off (so no jailer, and root)."""
        self.assertIn(
            "return 0===process.getuid()}", (self.svc / "service.js").read_text()
        )  # minified
        w = (self.svc / "run-js-service").read_text()
        self.assertIn("/^thirdparty_jail=/s/=.*/=off/", w)
        self.assertIn("'/usr/bin/run-js-service'", w)

    def test_dropbear_reads_host_key_from_var(self):
        data = (self.svc / "bin/dropbear").read_bytes()
        self.assertIn(b"/var/lib/webosbrew/sshd/dropbear_ed25519_host_key\0", data)
        self.assertIn(
            b"/var/lib/webosbrew/sshd/dropbear_ed25519_host_key",
            (ROOT / "usr/libexec/beanjar/hbchannel-start").read_bytes(),
        )

    def test_ca_bundle_is_public(self):
        certs = list((self.svc / "certs").glob("cacert-*.pem"))
        self.assertEqual(len(certs), 1)
        self.assertNotIn(b"PRIVATE KEY", certs[0].read_bytes())


for _v in VERSIONS:
    globals()["ReleaseTest_" + re.sub(r"\W", "_", _v)] = type(
        "ReleaseTest_" + _v, (ReleaseBase, unittest.TestCase), {"version": _v}
    )


class JobsTest(unittest.TestCase):
    """Both inits install it once /media and /var are bound from the state disks and before sam
    starts, then run hbchannel-start."""

    def test_systemd(self):
        unit = ini(ROOT / "etc/systemd/system/emulator-hbchannel-install.service")
        self.assertEqual(unit["Unit"]["DefaultDependencies"], "no")
        for edge in ("Wants", "After"):
            self.assertIn("mount-readwrite.service", unit["Unit"][edge].split())
        self.assertEqual(unit["Unit"]["Before"], "sam.service")
        self.assertEqual(unit["Service"]["ExecStart"], "/usr/libexec/beanjar/hbchannel-install")
        sam = ini(ROOT / "etc/systemd/system/sam.service.d/beanjar-hbchannel.conf")
        for edge in ("Wants", "After"):
            self.assertEqual(sam["Unit"][edge], "emulator-hbchannel-install.service")
        unit = ini(ROOT / "etc/systemd/system/emulator-hbchannel.service")
        self.assertEqual(unit["Unit"]["DefaultDependencies"], "no")
        for edge in ("Wants", "After"):
            self.assertEqual(unit["Unit"][edge], "emulator-hbchannel-install.service")
        self.assertNotIn("Before", unit["Unit"])
        self.assertEqual(unit["Service"]["Type"], "oneshot")
        self.assertEqual(
            unit["Service"]["RemainAfterExit"], "yes", "telnetd and Dropbear stay in its cgroup"
        )
        self.assertEqual(unit["Service"]["ExecStart"], "/usr/libexec/beanjar/hbchannel-start")
        wants = ini(ROOT / "etc/systemd/system/basic.target.d/beanjar-hbchannel.conf")
        self.assertEqual(
            wants["Unit"]["Wants"].split(),
            ["emulator-hbchannel-install.service", "emulator-hbchannel.service"],
        )

    def test_upstart(self):
        conf = (ROOT / "etc/init/emulator-hbchannel-install.conf").read_text()
        self.assertIn("\nstart on stopped mount-readwrite and starting sam\n", conf)
        self.assertIn("\ntask\n", conf)
        self.assertIn("\nexec /usr/libexec/beanjar/hbchannel-install\n", conf)
        conf = (ROOT / "etc/init/emulator-hbchannel.conf").read_text()
        self.assertIn("\nstart on stopped emulator-hbchannel-install\n", conf)
        self.assertIn("\ntask\n", conf)
        self.assertIn("\nexec /usr/libexec/beanjar/hbchannel-start\n", conf)


class InstallTest(unittest.TestCase):
    """hbchannel-install with its absolute paths moved into a temporary root, on a built layer."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hbchannel-install-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        ipk = self.tmp / "hb.ipk"
        build(self.tmp / "root", captures(DEFAULT)[-1], ipk, make_ipk(ipk))
        self.root = self.tmp / "root"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for tool in ("pidof", "ls-control"):  # no hub running; record a rescan if asked
            (self.bin / tool).write_text(
                f'#!/bin/sh\necho {tool} "$@" >> {self.tmp}/calls\n'
                + ("exit 1\n" if tool == "pidof" else "")
            )
            (self.bin / tool).chmod(0o755)

    def run_install(self):
        text = (ROOT / "usr/libexec/beanjar/hbchannel-install").read_text()
        self.assertIn("\nROOT=\t", text)
        text = text.replace("\nROOT=\t", f"\nROOT={self.root}\t", 1)
        subprocess.run(
            ["sh", "-c", text], check=True, env={"PATH": f"{self.bin}:{os.environ['PATH']}"}
        )

    def test_install(self):
        self.run_install()
        app = self.root / "media/developer/apps/usr/palm"
        self.assertTrue((app / f"applications/{APP}/appinfo.json").is_file())
        self.assertTrue(os.access(app / f"services/{SVC}/startup.sh", os.X_OK))
        self.assertTrue((app / f"packages/{APP}/packageinfo.json").is_file())
        svc = self.root / DEV.lstrip("/") / f"services.d/{SVC}.service"
        self.assertEqual(
            svc.read_bytes(),
            (self.root / STAGE / DEV.lstrip("/") / f"services.d/{SVC}.service").read_bytes(),
        )
        self.assertEqual(svc.stat().st_mode & 0o777, 0o644)
        self.assertEqual(
            (self.root / "var/lib/beanjar/hbchannel-ipk.sha256").read_text(),
            (self.root / STAGE / "ipk.sha256").read_text(),
        )
        self.assertEqual(
            (self.tmp / "calls").read_text(), "pidof ls-hubd\n", "no hub up: no rescan"
        )
        # the same IPK installed: the app is left alone; a part missing: copied again
        (app / f"applications/{APP}/mine").write_text("x")
        self.run_install()
        self.assertTrue((app / f"applications/{APP}/mine").exists())
        shutil.rmtree(app / f"services/{SVC}")
        self.run_install()
        self.assertTrue(os.access(app / f"services/{SVC}/startup.sh", os.X_OK))
        self.assertFalse((app / f"applications/{APP}/mine").exists())
        # another IPK of the same version: copied again
        (app / f"applications/{APP}/mine").write_text("x")
        (self.root / "var/lib/beanjar/hbchannel-ipk.sha256").write_text("old\n")
        self.run_install()
        self.assertFalse((app / f"applications/{APP}/mine").exists())

    def installed(self, version):
        """The state's HBC as if it had installed version itself: its appinfo.json's version, its
        own LS2 file, and the layer's IPK hash still in the stamp (it updated in place)."""
        app = self.root / "media/developer/apps/usr/palm"
        info = app / f"applications/{APP}/appinfo.json"
        info.write_text(json.dumps({"id": APP, "version": version, "type": "web"}, indent=4))
        svc = self.root / DEV.lstrip("/") / f"services.d/{SVC}.service"
        svc.write_text("its own\n")
        (app / f"applications/{APP}/mine").write_text("x")
        return info, svc

    def test_newer_installed_copy_stays(self):
        """HBC updated itself in the state to a newer version than the layer's: the app and its LS2
        files stay as it left them, at each boot, as on a TV that updated itself."""
        self.run_install()
        info, svc = self.installed("10.0.0")
        for _boot in range(2):
            self.run_install()
            self.assertIn('"10.0.0"', info.read_text())
            self.assertEqual(svc.read_text(), "its own\n")
            self.assertTrue((info.parent / "mine").exists())
        # the layer's own version again with another IPK of the layer still keeps the newer one
        (self.root / "var/lib/beanjar/hbchannel-ipk.sha256").write_text("old\n")
        self.run_install()
        self.assertIn('"10.0.0"', info.read_text())

    def test_older_installed_copy_upgraded(self):
        """An older version in the state (a layer of an older release, or HBC put there before):
        the layer's newer one is installed over it, LS2 files included."""
        self.run_install()
        info, svc = self.installed("9.8.6")
        self.run_install()
        self.assertIn('"9.8.7"', info.read_text())
        self.assertFalse((info.parent / "mine").exists())
        self.assertEqual(
            svc.read_bytes(),
            (self.root / STAGE / DEV.lstrip("/") / f"services.d/{SVC}.service").read_bytes(),
        )

    def test_version_order(self):
        """Versions compare field by field as numbers: 0.10.0 is newer than 0.9.9."""
        self.run_install()
        info, _svc = self.installed("9.10.0")
        self.run_install()
        self.assertIn('"9.10.0"', info.read_text(), "9.10.0 is newer than the layer's 9.8.7")
        info, _svc = self.installed("9.8")
        self.run_install()
        self.assertIn('"9.8.7"', info.read_text(), "9.8 is older than 9.8.7")

    def test_opkg_records(self):
        """opkg's info files are the package's; in the shared status file its entry replaces the
        package's old one and the other developer apps' entries stay."""
        staged = opkg_dir(self.root / STAGE)
        opkg = self.root / staged.relative_to(self.root / STAGE)
        opkg.mkdir(parents=True)
        other = "Package: com.example.other\nVersion: 1.0.0\nStatus: install ok unpacked\n\n"
        old = f"Package: {APP}\nVersion: 0.0.1\nStatus: install ok unpacked\n\n"
        (opkg / "status").write_text(old + other)
        self.run_install()
        for f in staged.glob("info/*"):
            self.assertEqual((opkg / "info" / f.name).read_bytes(), f.read_bytes())
        status = (opkg / "status").read_text()
        self.assertEqual(status, other + (staged / "status").read_text())
        self.assertNotIn("0.0.1", status)
        self.assertEqual((opkg / "status").stat().st_mode & 0o777, 0o644)
        (self.root / "var/lib/beanjar/hbchannel-ipk.sha256").write_text("old\n")
        self.run_install()
        self.assertEqual((opkg / "status").read_text(), status, "the same after another copy")

    def test_proxy_ca_in_service_certs(self):
        """mode=mitm: the proxy's CA beside HBC's certifi bundle, which its IPK download trusts alone."""
        certs = self.root / SVC_DIR.lstrip("/") / "certs"
        mine = certs / "beanjar-netstub-ca.pem"
        ca = self.root / "etc/emulator/netstub-ca.pem"
        ca.parent.mkdir(parents=True)
        ca.write_text("-----BEGIN CERTIFICATE-----\nproxy\n-----END CERTIFICATE-----\n")
        self.run_install()
        self.assertFalse(certs.exists(), "a service without certs/ gets none")
        certs.mkdir()
        (certs / "cacert-2024-03-11.pem").write_text("stock\n")
        self.run_install()
        self.assertEqual(mine.read_bytes(), ca.read_bytes())
        self.assertEqual(mine.stat().st_mode & 0o777, 0o644)
        self.assertEqual((certs / "cacert-2024-03-11.pem").read_text(), "stock\n")
        ca.unlink()  # mode is no longer mitm
        self.run_install()
        self.assertFalse(mine.exists())
        self.assertEqual(sorted(p.name for p in certs.iterdir()), ["cacert-2024-03-11.pem"])

    def test_rescan_with_hub_up(self):
        (self.bin / "pidof").write_text(f'#!/bin/sh\necho pidof "$@" >> {self.tmp}/calls\n')
        self.run_install()
        self.assertEqual(
            (self.tmp / "calls").read_text(), "pidof ls-hubd\nls-control scan-services\n"
        )


class StartTest(unittest.TestCase):
    """hbchannel-start with its absolute paths moved into a temporary root and a stand-in
    startup.sh that records how it was run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        keys = self.root / "etc/emulator/hbchannel"
        keys.mkdir(parents=True)
        (keys / "authorized_keys").write_text("ssh-ed25519 AAAAvm beanjar tv1\n")
        (keys / "dropbear_ed25519_host_key").write_bytes(b"\0\0\0\x0bssh-ed25519 host")
        svc = self.root / SVC_DIR.lstrip("/")
        svc.mkdir(parents=True)
        (svc / "startup.sh").write_text(
            "#!/bin/sh\n"
            f'{{ echo "SERVICE_DIR=$SERVICE_DIR"; echo "SKIP_ELEVATION=$SKIP_ELEVATION"; '
            f'echo "LD_PRELOAD=${{LD_PRELOAD-unset}}"; echo "PWD=$(pwd)"; }} > {self.root}/ran\n'
        )
        (svc / "startup.sh").chmod(0o755)

    def run_start(self):
        text = (ROOT / "usr/libexec/beanjar/hbchannel-start").read_text()
        for d in ("/etc/", "/home/", "/var/", "/media/"):
            text = text.replace(d, f"{self.root}{d}")
        text = text.replace("chown root:root", "true")
        subprocess.run(
            ["sh", "-c", text], check=True, env={"PATH": os.environ["PATH"], "LD_PRELOAD": " "}
        )  # set (blank: no real preload) and cleared

    def test_keys_flag_and_startup(self):
        (self.root / "home/root/.ssh").mkdir(parents=True)
        (self.root / "home/root/.ssh/authorized_keys").write_text("stale\n")
        self.run_start()
        auth = self.root / "home/root/.ssh/authorized_keys"
        self.assertEqual(
            auth.read_text(), "ssh-ed25519 AAAAvm beanjar tv1\n", "replaced by the VM's every boot"
        )
        self.assertEqual(auth.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "home/root/.ssh").stat().st_mode & 0o777, 0o700)
        host = self.root / "var/lib/webosbrew/sshd/dropbear_ed25519_host_key"
        self.assertEqual(host.read_bytes(), b"\0\0\0\x0bssh-ed25519 host")
        self.assertEqual(host.stat().st_mode & 0o777, 0o600)
        self.assertTrue((self.root / "var/luna/preferences/webosbrew_sshd_enabled").is_file())
        self.assertFalse((self.root / "var/luna/preferences/webosbrew_telnet_disabled").exists())
        ran = dict(line.split("=", 1) for line in (self.root / "ran").read_text().splitlines())
        self.assertEqual(
            ran,
            {
                "SERVICE_DIR": "",
                "SKIP_ELEVATION": "true",
                "LD_PRELOAD": "",
                "PWD": f"{self.root}/home/root",
            },
            "run from the dev install, with its own default",
        )
        self.assertFalse(
            (self.root / "var/lib/webosbrew/startup.sh").exists(), "run in place, not copied"
        )


if __name__ == "__main__":
    unittest.main()
