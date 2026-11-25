#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""link = "lan" with mode = "mitm", and with mode = "direct" (LanDirect, its own docstring), in
host/core/beanjar/netns.py's lan(), end to end, without a VM
and without a real LAN: test_netns_lan.py runs these cases under `unshare -rn`, where a veth pair is the LAN
(lanp, the "wired interface", and lanpeer, the rest of the LAN: a DHCP server of the test's own on
192.0.2.1, and a TCP server there). A stand-in for the CAP_NET_ADMIN helper (BEANJAR_LAN_HELPER)
makes the macvlan with the same `ip link add ... netns PID` the real one runs, unprivileged here
because the harness's namespace owns the VM's. fake-tap-guest is the guest, as in test-netns.py.

Checks: the lease (192.0.2.50, the VM's MAC asked for it) on lan0 and in RUN_DIR/lan.json; the
guest's TCP reaching the LAN from the VM's LAN address, through slirp in the LAN namespace; the
forwarded port and the inspector (mitmweb, behind its password) served on the LAN address from the
LAN, and on VM_ADDRESS from the host side; VNC's port 5900 on the LAN address joined to the socket
dir's vnc.sock (beanjar vnc's --unix, here a stand-in that sends RFB's greeting); the
lease released when the VM stops; no lan0, no lan.json and no process left, also after the
wrapper is killed outright (LanCrash, with a static address). Skips as test_netns.py does, and
without busybox. The cases, run by test_netns_lan.py in the harness's namespace."""

import http.client
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import testunits

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
PKG = os.path.join(TOP, "host", "core")
sys.path.insert(0, PKG)
from beanjar import mitmtool  # noqa: E402

MITMWEB = mitmtool.check()
SBIN = os.environ.get("PATH", os.defpath) + ":/usr/sbin:/sbin"
SERVER, LEASE = "192.0.2.1", "192.0.2.50"
MAC = "02:42:be:00:00:42"
VM_ADDRESS = "127.0.1.250"
GUEST_PORT, FWD = 2222, 6622


def unavailable():
    for tool in ("unshare", "ip", "slirp4netns", "busybox"):
        if not shutil.which(tool, path=SBIN):
            return f"{tool} not found"
    if not MITMWEB.usable:
        return MITMWEB.problem
    if not os.access("/dev/net/tun", os.R_OK | os.W_OK):
        return "no /dev/net/tun"
    if subprocess.run(["unshare", "-rn", "true"], stderr=subprocess.DEVNULL).returncode != 0:
        return "no unprivileged user namespaces"
    if (
        subprocess.run(
            [
                "unshare",
                "-rn",
                "sh",
                "-c",
                "ip link add lp0 type veth peer name lq0 && "
                "ip link add link lp0 name lm0 type macvlan mode bridge",
            ],
            env=dict(os.environ, PATH=SBIN),
            stderr=subprocess.DEVNULL,
        ).returncode
        != 0
    ):
        return "no veth or macvlan in this kernel"
    return None


def ip(*args):
    subprocess.run(["ip", *args], check=True, env=dict(os.environ, PATH=SBIN))


def load_dhcp():
    spec = importlib.util.spec_from_file_location(
        "netns_dhcp", os.path.join(TOP, "host/helpers/network/netns_dhcp.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Dhcp:
    """The LAN's DHCP server: netns_dhcp.py's replies, and a record of each request's type and
    client MAC (RELEASE is 7)."""

    def __init__(self):
        self.mod = load_dhcp()
        self.seen = []
        self.sock = self.mod.dhcp_socket("lanpeer")
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while True:
            try:
                data, _ = self.sock.recvfrom(2048)
            except OSError:
                return
            if len(data) < 240:
                continue
            opts = self.mod.dhcp_options(data[240:])
            self.seen.append((opts.get(53, b"\0")[0], data[28:34].hex(":")))
            reply = self.mod.dhcp_reply(data, SERVER, LEASE, SERVER, SERVER, 600)
            if reply:
                self.sock.sendto(reply, ("255.255.255.255", 68))


class Server:
    """A TCP server on the LAN that answers with the address the connection came from."""

    def __init__(self):
        self.sock = socket.create_server((SERVER, 0))
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while True:
            try:
                c, peer = self.sock.accept()
            except OSError:
                return
            with c:
                c.sendall(f"from:{peer[0]}".encode())


LAN = {}


def setUpModule():
    """The LAN: lanp (the wired interface) and lanpeer, its DHCP server and a TCP server on it."""
    ip("link", "set", "lo", "up")
    ip("link", "add", "lanp", "type", "veth", "peer", "name", "lanpeer")
    ip("addr", "add", f"{SERVER}/24", "dev", "lanpeer")
    for dev in ("lanp", "lanpeer"):
        ip("link", "set", dev, "up")
    LAN.update(dhcp=Dhcp(), server=Server())


class Lan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = t = tempfile.mkdtemp(prefix="test-netns-lan-")
        cls.dhcp, cls.server = LAN["dhcp"], LAN["server"]
        with open(f"{t}/helper", "w") as f:
            f.write(
                '#!/bin/sh\n[ "$1" = macvlan ] || exit 2\n'
                'exec ip link add link "$2" name lan0 address "$3" netns "$4" type macvlan mode bridge\n'
            )
        os.chmod(f"{t}/helper", 0o755)
        spec = {"lan": {"kind": "tcp", "host": SERVER, "port": cls.server.port}}
        with open(f"{t}/spec.json", "w") as f:
            json.dump(spec, f)
        os.makedirs(f"{t}/vm-run")
        env = dict(
            os.environ,
            PATH=SBIN,
            PYTHONPATH=os.pathsep.join(filter(None, (PKG, os.environ.get("PYTHONPATH")))),
            NETNS_SOCK_DIR=f"{t}/run",
            RUN_DIR=f"{t}/vm-run",
            NETNS_LOG_DIR=t,
            NETSTUB_LOG=f"{t}/netstub.log",
            NETSTUB_CA=f"{t}/ca",
            NETNS_TAP="tap0",
            NETNS_FWD=f"{FWD}:{GUEST_PORT}",
            VM_ADDRESS=VM_ADDRESS,
            NET_LINK="lan",
            NET_LAN_PARENT="lanp",
            NET_LAN_MAC=MAC,
            NET_LAN_HELPER=f"{t}/helper",
            NET_ADDRESS="dhcp",
            FAKE_GUEST_SPEC=f"{t}/spec.json",
            FAKE_GUEST_RESULTS=f"{t}/results.json",
            FAKE_GUEST_STOP=f"{t}/stop",
            FAKE_GUEST_PORT=str(GUEST_PORT),
            BEANJAR_MITM_PASSWORD="pw",
            NETNS_MITMWEB=MITMWEB.path,
        )
        with open(f"{t}/out.log", "w") as out:
            cls.proc = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import os, sys; from beanjar import netns; "
                    "sys.exit(netns.run(sys.argv[1:], os.environ))",
                    sys.executable,
                    os.path.join(TOP, "tests", "fakes", "fake-tap-guest"),
                ],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        end = time.monotonic() + 90
        while not os.path.exists(f"{t}/results.json"):
            if cls.proc.poll() is not None or time.monotonic() > end:
                cls.stop()
                raise AssertionError("the fake guest gave no results:\n" + cls.logs())
            time.sleep(0.2)
        with open(f"{t}/results.json") as f:
            cls.results = json.load(f)
        try:
            with open(f"{t}/vm-run/lan.json") as f:
                cls.info = json.load(f)
        except OSError:
            cls.info = None
        cls.from_lan = cls.connect(LEASE)
        cls.from_host = cls.connect(VM_ADDRESS)
        cls.web = {host: cls.inspector(host) for host in (LEASE, VM_ADDRESS)}
        cls.vnc = cls.vnc_from_lan(f"{t}/run/vnc.sock")
        cls.rc = cls.stop()
        # at once: lan0 is deleted at the stop, not left to the namespace's teardown
        cls.promiscuity = subprocess.run(
            ["ip", "-d", "link", "show", "lanp"],
            env=dict(os.environ, PATH=SBIN),
            stdout=subprocess.PIPE,
            text=True,
        ).stdout
        time.sleep(0.5)
        cls.links = subprocess.run(
            ["ip", "-br", "link"],
            env=dict(os.environ, PATH=SBIN),
            stdout=subprocess.PIPE,
            text=True,
        ).stdout

    @staticmethod
    def connect(host):
        return Lan.connect_port(host, FWD)

    @staticmethod
    def connect_port(host, port):
        for _ in range(20):
            try:
                with socket.create_connection((host, port), 3) as c:
                    return c.recv(100)
            except OSError as e:
                why = repr(e).encode()
                time.sleep(0.3)
        return why

    @staticmethod
    def vnc_from_lan(path):
        """What LEASE:5900 says with a stand-in beanjar-vnc on path."""
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(path)
            server.listen()

            def greet():
                while True:  # the relay's probe connects first and leaves again
                    try:
                        c, _ = server.accept()
                    except OSError:
                        return
                    with c:
                        try:
                            c.sendall(b"RFB 003.008\n")
                        except OSError:
                            pass

            threading.Thread(target=greet, daemon=True).start()
            for _ in range(20):
                try:
                    with socket.create_connection((LEASE, 5900), 3) as c:
                        return c.recv(100)
                except OSError as e:
                    why = repr(e).encode()
                    time.sleep(0.3)
            return why

    @staticmethod
    def inspector(host):
        """mitmweb on host:18083: (Server header, status without the password, status with it)."""
        out = []
        for path in ("/", "/?token=nope", "/?token=pw"):
            for _ in range(20):
                c = http.client.HTTPConnection(host, 18083, timeout=3)
                try:
                    c.request("GET", path)
                    r = c.getresponse()
                    r.read()
                    if not out:
                        out.append(r.getheader("Server", ""))
                    out.append(r.status)
                    break
                except (OSError, http.client.HTTPException) as e:
                    why = repr(e)
                    time.sleep(0.3)
                finally:
                    c.close()
            else:
                out.append(why)
        return out

    @classmethod
    def stop(cls):
        open(f"{cls.tmp}/stop", "w").close()
        try:
            return cls.proc.wait(timeout=40)
        except subprocess.TimeoutExpired:
            testunits.end(cls.proc.pid, group=True)
            cls.proc.wait()
            return None

    @classmethod
    def logs(cls):
        out = []
        for name in (
            "out.log",
            "netns-dhcp.log",
            "netns-uplink.log",
            "netns-relay-lan.log",
            "netstub-mitm.log",
        ):
            try:
                with open(os.path.join(cls.tmp, name)) as f:
                    out.append(f"--- {name}\n{f.read()[-3000:]}")
            except OSError:
                pass
        return "\n".join(out)

    @classmethod
    def tearDownClass(cls):
        if not os.environ.get("KEEP_TMP"):
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_exit_and_cleanup(self):
        self.assertEqual(self.rc, 0, self.logs())
        self.assertNotIn("lan0", self.links, "the macvlan outlived the VM")
        self.assertIn(
            "promiscuity 0 ", self.promiscuity, "a macvlan still on the parent right after the stop"
        )
        self.assertFalse(os.path.exists(f"{self.tmp}/vm-run/lan.json"))
        self.assertFalse(os.path.exists(f"{self.tmp}/run"), "socket dir left behind")
        left = []
        for pid in filter(str.isdigit, os.listdir("/proc")):
            try:
                with open(f"/proc/{pid}/environ", "rb") as f:
                    if self.tmp.encode() in f.read():
                        left.append(pid)
            except OSError:
                pass
        self.assertEqual(left, [], "processes of the run still alive")

    def test_lease(self):
        self.assertEqual(
            {
                k: self.info.get(k)
                for k in ("address", "prefix", "router", "dns", "parent", "mac", "source")
            },
            {
                "address": LEASE,
                "prefix": 24,
                "router": SERVER,
                "dns": [SERVER],
                "parent": "lanp",
                "mac": MAC,
                "source": "dhcp",
            },
            self.logs(),
        )
        self.assertIn((3, MAC), self.dhcp.seen, "no DHCPREQUEST from the VM's MAC")

    def test_lease_released(self):
        self.assertIn((7, MAC), self.dhcp.seen, f"no DHCPRELEASE: {self.dhcp.seen}")

    def test_guest_reaches_the_lan_from_its_lan_address(self):
        self.assertEqual(self.results["lan"].get("data"), f"from:{LEASE}", self.results)

    def test_forwarded_port_on_the_lan_and_the_host(self):
        self.assertEqual(self.from_lan, b"guest-banner\n")
        self.assertEqual(self.from_host, b"guest-banner\n")

    def test_vnc_on_the_lan(self):
        self.assertEqual(self.vnc, b"RFB 003.008\n")

    def test_inspector_on_the_lan_and_the_host_behind_its_password(self):
        for host, (server, *statuses) in self.web.items():
            self.assertTrue(str(server).lower().startswith("mitmproxy"), (host, server, statuses))
            self.assertEqual(
                statuses, [403, 403, 200], host
            )  # no password, a wrong one, the right one


class LanDirect(unittest.TestCase):
    """link = "lan" with mode = "direct": the fake guest's TAP joined to lan0 by tc, nothing of
    Beanjar's in between. The guest has the macvlan's MAC (the fake guest's own, here), asks the
    LAN's DHCP server itself, reaches the LAN from its lease and is reached there at every port;
    the lease watch writes lan.json from the server's ACK; the LAN namespace has no address of its
    own; at the stop lan0 and lan.json are gone and nothing of the run is left."""

    GUEST_MAC = "a8:23:fe:12:34:56"  # tests/fakes/fake-tap-guest's

    @classmethod
    def setUpClass(cls):
        if not shutil.which("tc", path=SBIN):
            raise unittest.SkipTest("tc not found")
        cls.tmp = t = tempfile.mkdtemp(prefix="test-netns-lan-direct-")
        cls.dhcp, cls.server = LAN["dhcp"], LAN["server"]
        # the Lan case's guest had the same lease with another MAC
        ip("neigh", "flush", "dev", "lanpeer")
        with open(f"{t}/helper", "w") as f:
            f.write(
                '#!/bin/sh\n[ "$1" = macvlan ] || exit 2\n'
                'exec ip link add link "$2" name lan0 address "$3" netns "$4" type macvlan mode bridge\n'
            )
        os.chmod(f"{t}/helper", 0o755)
        spec = {"lan": {"kind": "tcp", "host": SERVER, "port": cls.server.port}}
        with open(f"{t}/spec.json", "w") as f:
            json.dump(spec, f)
        os.makedirs(f"{t}/vm-run")
        env = dict(
            os.environ,
            PATH=SBIN,
            PYTHONPATH=os.pathsep.join(filter(None, (PKG, os.environ.get("PYTHONPATH")))),
            NETNS_SOCK_DIR=f"{t}/run",
            RUN_DIR=f"{t}/vm-run",
            NETNS_LOG_DIR=t,
            NETNS_TAP="tap0",
            NETNS_FWD="",
            VM_ADDRESS=VM_ADDRESS,
            NET_LINK="lan",
            NET_MODE="direct",
            NET_LG_SERVICES="real",
            NET_LAN_PARENT="lanp",
            NET_LAN_MAC=cls.GUEST_MAC,
            NET_LAN_HELPER=f"{t}/helper",
            FAKE_GUEST_SPEC=f"{t}/spec.json",
            FAKE_GUEST_RESULTS=f"{t}/results.json",
            FAKE_GUEST_STOP=f"{t}/stop",
            FAKE_GUEST_PORT=str(GUEST_PORT),
        )
        with open(f"{t}/out.log", "w") as out:
            cls.proc = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import os, sys; from beanjar import netns; "
                    "sys.exit(netns.run(sys.argv[1:], os.environ))",
                    sys.executable,
                    os.path.join(TOP, "tests", "fakes", "fake-tap-guest"),
                ],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        end = time.monotonic() + 60
        while not os.path.exists(f"{t}/results.json"):
            if cls.proc.poll() is not None or time.monotonic() > end:
                cls.stop()
                raise AssertionError("the fake guest gave no results:\n" + cls.logs())
            time.sleep(0.2)
        with open(f"{t}/results.json") as f:
            cls.results = json.load(f)
        cls.info = None
        for _ in range(20):
            try:
                with open(f"{t}/vm-run/lan.json") as f:
                    cls.info = json.load(f)
                break
            except OSError:
                time.sleep(0.2)
        cls.from_lan = Lan.connect_port(LEASE, GUEST_PORT)
        cls.ns_addresses = subprocess.run(
            ["nsenter", "-t", str(cls.lan_pid()), "-U", "-n", "--preserve-credentials"]
            + ["ip", "-o", "addr"],
            env=dict(os.environ, PATH=SBIN),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        ).stdout
        cls.rc = cls.stop()
        time.sleep(0.5)
        cls.links = subprocess.run(
            ["ip", "-br", "link"],
            env=dict(os.environ, PATH=SBIN),
            stdout=subprocess.PIPE,
            text=True,
        ).stdout

    @classmethod
    def lan_pid(cls):
        """The pid of netns.lan, in the LAN namespace (the fake guest has a namespace of its own)."""
        for pid in filter(str.isdigit, os.listdir("/proc")):
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmd = f.read().split(b"\0")
                with open(f"/proc/{pid}/environ", "rb") as f:
                    mine = cls.tmp.encode() in f.read()
            except OSError:
                continue
            if mine and b"beanjar.netns" in cmd and b"lan" in cmd:
                return pid
        return 0

    @classmethod
    def stop(cls):
        open(f"{cls.tmp}/stop", "w").close()
        try:
            return cls.proc.wait(timeout=40)
        except subprocess.TimeoutExpired:
            testunits.end(cls.proc.pid, group=True)
            cls.proc.wait()
            return None

    @classmethod
    def logs(cls):
        try:
            with open(os.path.join(cls.tmp, "out.log")) as f:
                return f.read()[-3000:]
        except OSError:
            return ""

    @classmethod
    def tearDownClass(cls):
        if not os.environ.get("KEEP_TMP"):
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_guest_takes_the_lan_lease_itself(self):
        self.assertEqual(self.results["dhcp"]["ack"], LEASE, self.results)
        self.assertEqual(self.results["dhcp"]["router"], SERVER)
        self.assertEqual(
            self.results["dhcp"]["renew"], LEASE, "unicast renewal to the LAN's server"
        )
        self.assertIn((3, self.GUEST_MAC), self.dhcp.seen, "no DHCPREQUEST from the guest's MAC")

    def test_guest_reaches_the_lan_from_its_own_address(self):
        self.assertEqual(self.results["lan"].get("data"), f"from:{LEASE}", self.results)

    def test_every_guest_port_on_its_lan_address(self):
        self.assertEqual(self.from_lan, b"guest-banner\n")

    def test_lan_json_from_the_guests_lease(self):
        self.assertEqual(
            {k: (self.info or {}).get(k) for k in ("address", "prefix", "router", "mac", "source")},
            {
                "address": LEASE,
                "prefix": 24,
                "router": SERVER,
                "mac": self.GUEST_MAC,
                "source": "guest",
            },
            self.logs(),
        )

    def test_lan_namespace_has_no_address(self):
        """No IPv4 or IPv6 address of the namespace's own on lan0 or tap0: it only carries frames."""
        self.assertIn("lo", self.ns_addresses, f"nsenter failed: {self.ns_addresses}")
        self.assertNotIn("lan0", self.ns_addresses)
        self.assertNotIn("tap0", self.ns_addresses)

    def test_exit_and_cleanup(self):
        self.assertEqual(self.rc, 0, self.logs())
        self.assertNotIn("lan0", self.links, "the macvlan outlived the VM")
        self.assertFalse(os.path.exists(f"{self.tmp}/vm-run/lan.json"))
        self.assertFalse(os.path.exists(f"{self.tmp}/run"), "socket dir left behind")
        left = []
        for pid in filter(str.isdigit, os.listdir("/proc")):
            try:
                with open(f"/proc/{pid}/environ", "rb") as f:
                    if self.tmp.encode() in f.read():
                        left.append(pid)
            except OSError:
                pass
        self.assertEqual(left, [], "processes of the run still alive")


class LanCrash(unittest.TestCase):
    """The VM's whole wrapper killed (SIGKILL, no cleanup of its own): everything of the run dies
    with it, and so the LAN namespace and its lan0."""

    def test_nothing_outlives_a_kill(self):
        t = tempfile.mkdtemp(prefix="test-netns-lan-crash-")
        self.addCleanup(shutil.rmtree, t, True)
        with open(f"{t}/helper", "w") as f:
            f.write(
                '#!/bin/sh\nexec ip link add link "$2" name lan0 address "$3" netns "$4" type macvlan mode bridge\n'
            )
        os.chmod(f"{t}/helper", 0o755)
        with open(f"{t}/spec.json", "w") as f:
            json.dump({}, f)
        os.makedirs(f"{t}/vm-run")
        env = dict(
            os.environ,
            PATH=SBIN,
            PYTHONPATH=os.pathsep.join(filter(None, (PKG, os.environ.get("PYTHONPATH")))),
            NETNS_SOCK_DIR=f"{t}/run",
            RUN_DIR=f"{t}/vm-run",
            NETNS_LOG_DIR=t,
            NETSTUB_LOG=f"{t}/netstub.log",
            NETSTUB_CA=f"{t}/ca",
            NETNS_TAP="tap0",
            NETNS_FWD=f"{FWD}:{GUEST_PORT}",
            VM_ADDRESS=VM_ADDRESS,
            NET_LINK="lan",
            NET_LAN_PARENT="lanp",
            NET_LAN_MAC=MAC,
            NET_LAN_HELPER=f"{t}/helper",
            NET_ADDRESS="192.0.2.60/24,192.0.2.1",
            FAKE_GUEST_SPEC=f"{t}/spec.json",
            FAKE_GUEST_RESULTS=f"{t}/results.json",
            FAKE_GUEST_STOP=f"{t}/stop",
            FAKE_GUEST_PORT=str(GUEST_PORT),
            BEANJAR_MITM_PASSWORD="pw",
            NETNS_MITMWEB=MITMWEB.path,
        )
        with open(f"{t}/out.log", "w") as out:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import os, sys; from beanjar import netns; "
                    "sys.exit(netns.run(sys.argv[1:], os.environ))",
                    sys.executable,
                    os.path.join(TOP, "tests", "fakes", "fake-tap-guest"),
                ],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
            )
        end = time.monotonic() + 90
        while (
            not os.path.exists(f"{t}/results.json")
            and proc.poll() is None
            and time.monotonic() < end
        ):
            time.sleep(0.2)
        with open(f"{t}/vm-run/lan.json") as f:
            self.assertEqual(json.load(f)["source"], "static")
        proc.kill()
        proc.wait()
        left = None
        for _ in range(50):
            time.sleep(0.2)
            left = []
            for pid in filter(str.isdigit, os.listdir("/proc")):
                try:
                    with open(f"/proc/{pid}/environ", "rb") as f:
                        if t.encode() in f.read():
                            left.append(pid)
                except OSError:
                    pass
            links = subprocess.run(
                ["ip", "-br", "link"],
                env=dict(os.environ, PATH=SBIN),
                stdout=subprocess.PIPE,
                text=True,
            ).stdout
            if not left and "lan0" not in links:
                break
        self.assertEqual(left, [], "processes outlived the kill")
        self.assertNotIn("lan0", links)


def run_in_harness():
    """The cases in this process (the harness's namespace): {test id: [outcome, detail]} as JSON."""

    class Result(unittest.TestResult):
        def __init__(self):
            super().__init__()
            self.outcomes = {}

        def addSuccess(self, test):
            self.outcomes[test.id()] = ["passed", ""]

        def addFailure(self, test, err):
            self.outcomes[test.id()] = ["failed", self._exc_info_to_string(err, test)]

        addError = addFailure

        def addSkip(self, test, reason):
            self.outcomes[test.id()] = ["skipped", reason]

    result = Result()
    unittest.TestLoader().loadTestsFromModule(sys.modules[__name__]).run(result)
    print(json.dumps(result.outcomes))


if __name__ == "__main__":
    # tests/integration/network/test_netns_lan.py runs this under `unshare -rn`
    run_in_harness()
