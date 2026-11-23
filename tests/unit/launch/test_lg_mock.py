# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""lg_services=mock's table and resolver: host/helpers/network/lg_hosts.py's host classes and
lg_mock.py's DNS answers (refused, the online check, forwarded) and its online-check HTTP answer.
On the loopback only, with a stand-in upstream resolver: no request leaves the host."""

import socket
import struct
import sys
import threading
from pathlib import Path

import pytest

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "host/helpers/network"))
import lg_hosts
import lg_mock
import lg_sdp


@pytest.mark.parametrize(
    "name, cls",
    [
        # the study's endpoint table, one or more per class
        ("lgtvonline.lge.com", "online"),
        ("LGTVonline.LGE.com.", "online"),  # case and the root's dot
        ("rdx2.lgtvsdp.com", "upload"),
        ("qt2-rdx2.lgtvsdp.com", "upload"),
        ("rdx2.nextlgsdp.com", "upload"),
        ("aic.rdl.lgtvcommon.com", "upload"),
        ("aic.cdpbeacon2.lgtvcommon.com", "beacon"),
        ("us.ad.lgsmartad.com", "ads"),
        ("gb.info.lgsmartad.com", "ads"),
        ("cdn-aas-campaigns.alphonso.tv", "acr"),
        ("eu-api-v2.acrcloud.com", "acr"),
        ("qt2-kic.lgtviot.com", "iot"),
        ("aic.lgtviot.com", "iot"),
        ("common.lgthinq.com", "iot"),
        ("he-us-ai.lgthinq.com", "iot"),
        ("aic-core.ebp2.lgsmartplatform.com", "iot"),
        ("www.ueiwsp.com", "quickset"),
        ("interface.quicksetcloud.com", "quickset"),
        ("global.azure-devices-provisioning.net", "quickset"),
        ("NevoAI-IOTHub-37-Prod.azure-devices.net", "quickset"),
        ("snu.lge.com", "update"),
        ("su-ssl.lge.com", "update"),
        ("nsu.lge.com", "update"),
        ("rone-aic.lge.com", "remotediag"),
        ("rone-tv-saic.lge.com", "remotediag"),
        ("netflixvoice.lgtvcommon.com", "voice"),
        ("lgtv2020-ncs-us-gwssl.nuancemobility.net", "voice"),
        # answered (lg_sdp.py)
        ("us.lgtvsdp.com", "sdp"),
        ("qt2-us.lgtvsdp.com", "sdp"),
        ("us.nextlgsdp.com", "sdp"),
        ("us.tv.wiselg.com", "sdp"),
        ("aic-ngfts.lge.com", "lg"),
        ("aic.cdpsvc.lgtvcommon.com", "lg"),
        ("us.lgrecommends.lgappstv.com", "lg"),
        ("us.membership.lgwebostv.com", "lg"),
    ],
)
def test_classes(name, cls):
    assert lg_hosts.lookup(name)["class"] == cls


@pytest.mark.parametrize(
    "name",
    [
        "www.youtube.com",
        "googlevideo.com",
        "www.netflix.com",
        "api.accuweather.com",
        "notlge.com",  # a suffix of a name, not of its labels
        "lge.com.example.org",
        "other.azure-devices.net",
        "rdx2.example.org",
        "",
        ".",
    ],
)
def test_not_lgs(name):
    assert lg_hosts.lookup(name) is None
    assert lg_hosts.mock_action(name) is None


def test_actions_and_table_shape():
    assert lg_hosts.mock_action("snu.lge.com") == "refuse"
    assert lg_hosts.mock_action("lgtvonline.lge.com") == "online"
    assert lg_hosts.mock_action("us.lgtvsdp.com") == "answer"
    assert lg_hosts.mock_action("www.lge.com") == "answer"
    assert lg_hosts.refused_addresses() == ["156.147.69.32", "165.244.62.249"]
    names = [c["class"] for c in lg_hosts.CLASSES]
    assert len(names) == len(set(names))
    for c in lg_hosts.CLASSES:
        assert c["mock"] in lg_hosts.MOCKS
        assert all(p == p.lower() and p.strip(".") == p for p in c["names"]), c
        if c.get("addresses"):
            assert c["mock"] == "refuse"
    # the broad answered classes come after every refusal, so a refusal is never shadowed
    last_refuse = max(i for i, c in enumerate(lg_hosts.CLASSES) if c["mock"] == "refuse")
    assert all(c["mock"] == "answer" for c in lg_hosts.CLASSES[last_refuse + 1 :])
    assert [c["class"] for c in lg_hosts.CLASSES[last_refuse + 1 :]] == ["sdp", "lg"]


def query(name, qtype=1, qid=0x1234):
    q = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    for label in name.split("."):
        q += bytes([len(label)]) + label.encode()
    return q + b"\0" + struct.pack(">HH", qtype, 1)


def rcode(msg):
    return struct.unpack(">H", msg[2:4])[0] & 0xF


def counts(msg):
    return struct.unpack(">HHHH", msg[4:12])


class Upstream:
    """A stand-in for slirp's resolver on the loopback: answers every query with one A record,
    192.0.2.7, and records the names asked."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port, self.asked = self.sock.getsockname()[1], []
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            msg, peer = self.sock.recvfrom(65535)
            name, _qtype, _qclass, end = lg_mock.question(msg)
            self.asked.append((name, peer[0]))
            out = msg[:2] + struct.pack(">HHHHH", 0x8180, 1, 1, 0, 0) + msg[12:end]
            out += struct.pack(">HHHIH", 0xC00C, 1, 1, 30, 4) + socket.inet_aton("192.0.2.7")
            self.sock.sendto(out, peer)


@pytest.fixture
def resolver():
    up = Upstream()
    m = lg_mock.Mock("127.0.0.1", "127.0.0.1", up.port)
    yield m, up
    up.sock.close()


def test_refused_names_are_nxdomain_and_not_forwarded(resolver):
    m, up = resolver
    for name in ("rdx2.lgtvsdp.com", "aic.cdpbeacon2.lgtvcommon.com", "snu.lge.com"):
        for qtype in (1, 28, 255):  # A, AAAA, ANY
            out = m.answer(query(name, qtype, qid=0xBEEF))
            assert out[:2] == b"\xbe\xef"
            assert rcode(out) == lg_mock.NXDOMAIN
            assert counts(out) == (1, 0, 0, 0)
            assert out[2] & 0x80 and out[3] & 0x80  # QR, RA
            assert out[2] & 0x01  # RD kept
    assert up.asked == []


@pytest.mark.parametrize(
    "name",
    ["lgtvonline.lge.com", "us.lgtvsdp.com", "US7.nextlgsdp.com", "aic.cdpsvc.lgtvcommon.com"],
)
def test_online_check_and_answered_names_resolve_to_the_mock(resolver, name):
    m, up = resolver
    out = m.answer(query(name))
    assert rcode(out) == lg_mock.NOERROR and counts(out) == (1, 1, 0, 0)
    assert out[-4:] == socket.inet_aton("127.0.0.1")
    aaaa = m.answer(query(name, 28))
    assert rcode(aaaa) == lg_mock.NOERROR and counts(aaaa) == (1, 0, 0, 0)
    assert up.asked == []


def test_other_names_are_forwarded_unchanged_from_the_listen_address(resolver):
    m, up = resolver
    for name in ("www.youtube.com", "api.accuweather.com"):
        out = m.answer(query(name))
        assert rcode(out) == lg_mock.NOERROR and out[-4:] == socket.inet_aton("192.0.2.7")
    assert up.asked == [("www.youtube.com", "127.0.0.1"), ("api.accuweather.com", "127.0.0.1")]


def test_no_upstream_is_servfail():
    dead = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dead.bind(("127.0.0.1", 0))  # bound, never answers
    m = lg_mock.Mock("127.0.0.1", "127.0.0.1", dead.getsockname()[1])
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(lg_mock, "UPSTREAM_TIMEOUT", 0.05)
        out = m.answer(query("www.youtube.com"))
    dead.close()
    assert rcode(out) == lg_mock.SERVFAIL and counts(out)[0] == 1


def test_malformed_queries():
    assert lg_mock.question(b"\0" * 5) is None
    assert lg_mock.question(query("x.example")[:-2]) is None
    response = bytearray(query("x.example"))
    response[2] |= 0x80
    assert lg_mock.question(bytes(response)) is None


def test_servers_answer_dns_over_udp_and_tcp_and_the_online_check(resolver):
    m, _up = resolver
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 0))
    threading.Thread(target=m.serve_udp, args=(udp,), daemon=True).start()
    tcp = lg_mock.Server(("127.0.0.1", 0), lg_mock.tcp_dns_handler(m))
    http = lg_sdp.Server(("127.0.0.1", 0), lambda line: None, lg_mock.ONLINE_RESPONSE)
    for s in (tcp, http):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as c:
            c.settimeout(5)
            c.sendto(query("su.lge.com"), udp.getsockname())
            assert rcode(c.recv(512)) == lg_mock.NXDOMAIN
        with socket.create_connection(tcp.server_address, 5) as c:
            q = query("lgtvonline.lge.com")
            c.sendall(struct.pack(">H", len(q)) + q)
            f = c.makefile("rb")
            out = f.read(struct.unpack(">H", f.read(2))[0])
            assert counts(out)[1] == 1
        with socket.create_connection(http.server_address, 5) as c:
            c.sendall(b"GET / HTTP/1.1\r\nHost: lgtvonline.lge.com\r\nAccept: */*\r\n\r\n")
            got = b""
            while chunk := c.recv(4096):
                got += chunk
        assert got == lg_mock.ONLINE_RESPONSE
        assert got.startswith(b"HTTP/1.1 200 ")
    finally:
        for s in (tcp, http):
            s.shutdown()
            s.server_close()
        udp.close()
