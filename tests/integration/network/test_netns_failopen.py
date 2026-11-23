#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Optional proxy lifecycle, including real policy-rule withdrawal and guest HTTP traffic: with no
proxy and with one that dies before it listens, the guest's HTTP goes directly; with one whose TUN
device appears only after the guest's first connection was opened ("late"), that connection is
held (its SYN dropped and sent again) and then carried through the device, neither refused nor
reset."""

import io
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TOP = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(TOP / "host/core"))
from beanjar import netns, paths


class StopHandler(unittest.TestCase):
    def test_second_signal_does_not_interrupt_cleanup(self):
        old = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        stops = []
        done = []
        try:
            netns._raise_on(signal.SIGTERM, signal.SIGINT)
            try:
                os.kill(os.getpid(), signal.SIGTERM)
                self.fail("first signal did not raise")
            except netns._Stop as e:
                stops.append(e.sig)
            finally:
                os.kill(os.getpid(), signal.SIGTERM)  # arrives before SIG_IGN is installed
                os.kill(os.getpid(), signal.SIGINT)
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                done.append(True)
        finally:
            for s, h in old.items():
                signal.signal(s, h)
        self.assertEqual(stops, [signal.SIGTERM])
        self.assertEqual(done, [True])


class OptionalProxy(unittest.TestCase):
    def test_missing_proxy_does_not_generate_ca(self):
        tree = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            env = {"NETNS_MITMWEB": "/nonexistent/beanjar-mitmweb", "RUN_DIR": directory}
            with mock.patch.object(netns.subprocess, "run") as run:
                self.assertIsNone(netns._start_proxy(env, tree, directory, directory, "18083"))
            run.assert_not_called()
            info = netns.mitm_info(directory)
            self.assertIn("pipx install mitmproxy", info["problem"])

    def test_missing_proxy_has_no_address_line(self):
        """Without a usable mitmweb the start output names no inspector address, only why it is
        off; with one it does."""
        for path, shown in (("/nonexistent/beanjar-mitmweb", False), (sys.executable, True)):
            with (
                self.subTest(path=path),
                mock.patch.object(
                    netns.mitmtool,
                    "check",
                    return_value=netns.mitmtool.Tool(
                        path, "12.3.1" if shown else "", "" if shown else "x"
                    ),
                ),
                mock.patch.object(netns.sys, "stderr", new_callable=io.StringIO) as err,
            ):
                netns._announce_inspector({}, "traffic inspector (mitmweb): http://a:1")
                self.assertEqual(bool(err.getvalue()), shown)

    def test_start_does_not_wait(self):
        """_start_proxy returns at once, whatever mitmweb does; a Proxy whose mitmweb exited
        before it listened is not ready (False), one still starting is pending (None)."""
        with tempfile.TemporaryDirectory() as directory:
            tree = mock.Mock()
            tree.src.return_value = "/unused"
            tree.build.return_value = directory + "/flows"
            process = mock.Mock()
            process.poll.return_value = None
            with (
                mock.patch.object(
                    netns.subprocess, "run", return_value=mock.Mock(stdout=directory + "/ca.pem")
                ),
                mock.patch.object(netns.subprocess, "Popen", return_value=process),
                mock.patch.object(
                    netns.mitmtool,
                    "check",
                    return_value=netns.mitmtool.Tool(sys.executable, "12.3.1", ""),
                ),
            ):
                proxy = netns._start_proxy({}, tree, directory, directory, "1")
            self.assertIs(proxy.process, process)
            self.assertIsNone(proxy.poll())
            process.poll.return_value = 1
            self.assertIs(proxy.poll(), False)

    def proxy(self, ready):
        p = netns.Proxy(mock.Mock(), "/nonexistent/ready", "1", "/unused/log")
        p.ready = ready
        return p

    def test_never_ready_proxy_is_reaped_and_releases_the_hold(self):
        guest = mock.Mock()
        guest.wait.side_effect = [subprocess.TimeoutExpired("guest", 0.2), 7]
        proxy = self.proxy(False)
        proxy.process.poll.return_value = 1
        with mock.patch.object(netns, "_ip") as ip:
            self.assertEqual(netns._wait_guest(guest, proxy, {}, "tap0"), 7)
        self.assertEqual(
            [c.args[1:4] for c in ip.call_args_list],
            [("rule", "del", "pref")] * len(netns.TUN_PROTOCOLS),
        )
        self.assertTrue(all(c.args[4] == netns.HOLD_PREF for c in ip.call_args_list))
        proxy.process.wait.assert_called()
        guest.terminate.assert_not_called()

    def test_ready_then_dead_proxy_intercepts_then_withdraws(self):
        """Ready: interception added before the hold is withdrawn; its exit withdraws interception."""
        guest = mock.Mock()
        guest.wait.side_effect = [subprocess.TimeoutExpired("guest", 0.2)] * 2 + [7]
        proxy = self.proxy(True)
        proxy.process.poll.side_effect = [1, 1, 1]
        with mock.patch.object(netns, "_ip") as ip:
            self.assertEqual(netns._wait_guest(guest, proxy, {}, "tap0"), 7)
        steps = [(c.args[2], c.args[4]) for c in ip.call_args_list]
        n = len(netns.TUN_PROTOCOLS)
        # with LG's services mocked (the default) the resolver's upstream is intercepted too
        m = len(netns.intercept_rules("tap0", "add", netns.MOCK_DNS))
        self.assertEqual(
            steps,
            [("replace", "dev")]  # the route into the device, first
            + [("add", netns.INTERCEPT_PREF)] * m
            + [("del", netns.HOLD_PREF)] * n
            + [("del", netns.INTERCEPT_PREF)] * m,
        )
        guest.terminate.assert_not_called()

    def test_network_rule_errors_remain_fatal(self):
        with mock.patch.object(netns, "_ip", side_effect=netns.Error("routing failed")):
            with self.assertRaisesRegex(netns.Error, "routing failed"):
                netns._intercept({}, "tap0", "add")


# This probe runs in its own user/network namespace and uses the existing TAP guest.
# No host interfaces, port bindings or installed dependencies are changed.
PROBE = r'''
import http.server, json, os, pathlib, subprocess, sys, threading, time
from unittest import mock
from beanjar import netns, paths
root, directory, mode = sys.argv[1:]
env = os.environ
def ip(*args): subprocess.run(['ip', *args], check=True)
# Put the HTTP server beyond a real uplink. Leaving a rule into the TUN device installed would
# send the guest's packets to a device no one reads, and fail.
server_code = """
import http.server, os, pathlib, subprocess, sys, time
os.unshare(os.CLONE_NEWNET)
pathlib.Path(sys.argv[1], 'server-ns').touch()
while subprocess.run(['ip', 'link', 'show', 'server0'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode: time.sleep(.02)
for args in [('link','set','lo','up'), ('addr','add','192.0.2.1/24','dev','server0'), ('link','set','server0','up'), ('route','add','default','via','192.0.2.2')]: subprocess.run(['ip',*args], check=True)
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'direct-ok')
    def log_message(self, *args): pass
server = http.server.HTTPServer(('192.0.2.1', 80), Handler)
pathlib.Path(sys.argv[1], 'server-ready').touch()
server.serve_forever()
"""
server = subprocess.Popen([sys.executable, '-c', server_code, directory], preexec_fn=netns._die_with_parent)
while not pathlib.Path(directory, 'server-ns').exists():
    assert server.poll() is None
    time.sleep(.02)
ip('link', 'add', 'up0', 'type', 'veth', 'peer', 'name', 'server0')
ip('link', 'set', 'server0', 'netns', str(server.pid))
ip('addr', 'add', '192.0.2.2/24', 'dev', 'up0')
ip('link', 'set', 'up0', 'up')
ip('route', 'add', 'default', 'via', '192.0.2.1')
while not pathlib.Path(directory, 'server-ready').exists():
    assert server.poll() is None
    time.sleep(.02)
spec = {'http': {'kind': 'tcp', 'host': '192.0.2.1', 'port': 80, 'send': 'GET / HTTP/1.0\r\n\r\n'}}
pathlib.Path(directory, 'spec').write_text(json.dumps(spec))
env.update(NETNS_RUNDIR=directory, NETNS_LOG_DIR=directory, NETNS_TAP='tap0',
           FAKE_GUEST_SPEC=directory+'/spec', FAKE_GUEST_RESULTS=directory+'/results',
           FAKE_GUEST_STOP=directory+'/stop', FAKE_GUEST_PORT='2222',
           NET_LG_SERVICES='real')  # the proxy's fail-open alone; test_netns.py has the mock
proxy = None
if mode == 'late':
    # mitmweb's stand-in: its TUN device (mitm0), its web port and its ready file only 1.5 s
    # after the guest has its lease and starts its probe, so that probe's SYN arrives while the
    # proxy is not up yet and is held (netns._hold). It writes every packet the device gets back
    # into it, which the namespace then forwards like any other (the rules match the TAP only),
    # and counts them.
    late = """
import fcntl, os, pathlib, socket, struct, subprocess, sys, time
directory = sys.argv[1]
while not pathlib.Path(directory, 'leased').exists(): time.sleep(.02)
time.sleep(1.5)
fd = os.open('/dev/net/tun', os.O_RDWR)
fcntl.ioctl(fd, 0x400454ca, struct.pack('16sH', b'mitm0', 0x0001 | 0x1000))  # TUNSETIFF: IFF_TUN, IFF_NO_PI
subprocess.run(['ip', 'link', 'set', 'mitm0', 'up'], check=True)
pathlib.Path('/proc/sys/net/ipv4/conf/mitm0/rp_filter').write_text('0')
web = socket.create_server(('127.0.0.1', 18099))
pathlib.Path(directory, 'mitmweb.ready').touch()
n = 0
while True:
    packet = os.read(fd, 65536)
    n += 1
    pathlib.Path(directory, 'tun-packets').write_text(str(n))
    os.write(fd, packet)
"""
    process = subprocess.Popen([sys.executable, '-c', late, directory], preexec_fn=netns._die_with_parent)
    proxy = netns.Proxy(process, directory + '/mitmweb.ready', '18099', directory + '/late.log')
    env['FAKE_GUEST_LEASED'] = directory + '/leased'
    spec['http']['timeout'] = 20
    pathlib.Path(directory, 'spec').write_text(json.dumps(spec))
elif mode == 'dead':
    proxy = netns.Proxy(subprocess.Popen(['sleep', '60'], preexec_fn=netns._die_with_parent),
                        directory + '/never-ready', '1', directory + '/dead.log')
    # The guest starts only after inner has installed its hold rules. Its wrapper kills the
    # proxy, which never got ready, and allows the monitor to remove the rules before making
    # requests: they go directly.
    wrapper = 'import os, runpy, signal, subprocess, time; rules = subprocess.check_output(["ip", "rule"], text=True); assert "ipproto tcp blackhole" in rules and "ipproto udp blackhole" in rules, rules; os.kill(%d, signal.SIGTERM); time.sleep(0.6); runpy.run_path(%r, run_name="__main__")' % (proxy.process.pid, root+'/tests/fakes/fake-tap-guest')
if mode != 'dead':
    wrapper = 'import runpy; runpy.run_path(%r, run_name="__main__")' % (root+'/tests/fakes/fake-tap-guest')
def stop_when_done():
    while not pathlib.Path(directory, 'results').exists(): time.sleep(.05)
    pathlib.Path(directory, 'stop').touch()
threading.Thread(target=stop_when_done, daemon=True).start()
try:
    with mock.patch.object(netns, '_start_proxy', return_value=proxy):
        status = netns.inner([sys.executable, '-c', wrapper])
    result = json.loads(pathlib.Path(directory, 'results').read_text())
    assert status == 0, status
    assert '200 OK' in result['http'].get('data', ''), result
    assert 'direct-ok' in result['http']['data'], result
    if mode == 'late':
        # held, not refused or reset: it waited for the proxy (TCP sent its SYN again) and was
        # then carried through its device
        assert result['http']['seconds'] >= 1.0, result
        assert int(pathlib.Path(directory, 'tun-packets').read_text()) > 0
    rules = subprocess.check_output(['ip', 'rule'], text=True)
    assert 'blackhole' not in rules, rules  # the hold is gone either way
    if mode == 'late':  # intercepting still (the namespace ends with the VM)
        assert 'ipproto tcp lookup 101' in rules and 'ipproto udp lookup 101' in rules, rules
    else:
        assert 'ipproto' not in rules, rules
    print('http', result['http']['seconds'], 's', file=sys.stderr)
finally:
    server.terminate()
    server.wait()
    if proxy is not None:
        if proxy.process.poll() is None: proxy.process.terminate()
        proxy.process.wait()
'''


class RealRouting(unittest.TestCase):
    def test_direct_http_without_proxy_and_after_proxy_death_held_while_it_starts(self):
        for tool in ("unshare", "ip"):
            if not shutil.which(tool):
                self.skipTest(tool + " unavailable")
        if not os.access("/dev/net/tun", os.R_OK | os.W_OK):
            self.skipTest("TUN unavailable")
        if subprocess.run(["unshare", "-rn", "true"], stderr=subprocess.DEVNULL).returncode:
            self.skipTest("unprivileged namespaces unavailable")
        # the late proxy sets its device's rp_filter: a container's /proc/sys is read-only
        sysctl = "/proc/sys/net/ipv4/conf/lo/rp_filter"
        if subprocess.run(
            ["unshare", "-rn", "sh", "-c", f"cat {sysctl} > {sysctl}"], stderr=subprocess.DEVNULL
        ).returncode:
            self.skipTest("/proc/sys not writable in a namespace")
        for mode in os.environ.get("MODES", "missing dead late").split():
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                env = dict(os.environ, PYTHONPATH=str(TOP / "host/core"))
                process = subprocess.Popen(
                    ["unshare", "-rn", sys.executable, "-c", PROBE, str(TOP), directory, mode],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
                try:
                    stdout, stderr = process.communicate(timeout=60)
                    self.assertEqual(process.returncode, 0, stdout + stderr)
                    if os.environ.get("VERBOSE"):
                        print(stderr, file=sys.stderr)
                finally:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.communicate()


if __name__ == "__main__":
    unittest.main()
