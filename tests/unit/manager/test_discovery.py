# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Focused discovery policy tests (system Python and dnspython): pytest tests/unit/manager/test_discovery.py."""

import io
import socket
import threading
from unittest.mock import MagicMock, Mock, patch

import dns.message
import pytest

from beanjar.network import discovery


class Admission(discovery.Outer):
    def __init__(self):
        self.address = "192.0.2.7"
        self.ports = []
        self.descriptions = {}
        self.description_resources = {}
        self.ssdp = {}
        self.mdns = {}
        self.endpoints = {}
        self.lock = threading.RLock()

    def endpoint(self, transport, port, **kwargs):
        self.ports.append((transport, port, kwargs))
        return True


class HttpClient:
    def __init__(self, method="GET"):
        self.output = bytearray()
        self.request = (
            method + " /desc.xml HTTP/1.1\r\nHost: 192.0.2.7\r\nConnection: close\r\n\r\n"
        ).encode()

    def makefile(self, *_args):
        return io.BytesIO(self.request)

    def settimeout(self, _timeout):
        pass

    def sendall(self, data):
        self.output.extend(data)

    def close(self):
        pass


def test_address_boundaries():
    assert (
        discovery.replace_address(b"http://10.0.2.15:123/a 10.0.2.150 110.0.2.15", "192.0.2.7")
        == b"http://192.0.2.7:123/a 10.0.2.150 110.0.2.15"
    )


def test_only_guest_urls_admit_ports():
    bridge = Admission()
    bridge.admit_urls(b"http://10.0.2.15:123/a http://10.0.2.150:456/a http://example.org:789/a")
    assert [(t, p) for t, p, _ in bridge.ports] == [("tcp", 123)]
    with pytest.raises(ValueError):
        bridge.admit_urls(b"http://10.0.2.15:65536/a")


def test_description_and_application_header_admitted_before_advertisement():
    bridge = Admission()

    class Response:
        def read(self, _size):
            return b"<controlURL>http://10.0.2.15:234/control</controlURL>"

        def getheaders(self):
            return [("Application-URL", "http://10.0.2.15:345/apps")]

    class Backend:
        def __init__(self, *_args):
            pass

        def request(self, method, path):
            self.requested = (method, path)

        def getresponse(self):
            return Response()

        def close(self):
            pass

    wire = b"HTTP/1.1 200 OK\r\nLOCATION: http://10.0.2.15:123/desc.xml\r\nCACHE-CONTROL: max-age=10\r\n\r\n"
    with patch.object(discovery, "Backend", Backend):
        result = bridge.advertisement(1900, wire)
    assert b"http://192.0.2.7:123/desc.xml" in result
    assert {p for _, p, _ in bridge.ports} == {123, 234, 345}
    assert bridge.ports[0][2]["ttl"] == 10
    assert (123, "/desc.xml") in bridge.description_resources


def test_head_text_response_omits_unknown_transformed_length_and_body():
    bridge = Admission()
    response = Mock()
    response.status, response.reason = 200, "OK"
    headers = {
        "Content-Type": "text/xml",
        "Content-Length": "123",
        "ETag": '"stock"',
        "Content-MD5": "stock-digest",
    }
    response.getheaders.return_value = list(headers.items())
    response.getheader.side_effect = lambda name, default=None: headers.get(name, default)

    class Client:
        def __init__(self):
            self.output = bytearray()

        def makefile(self, *_args):
            return io.BytesIO(
                b"HEAD /desc.xml HTTP/1.1\r\nHost: 192.0.2.7\r\nConnection: close\r\n\r\n"
            )

        def settimeout(self, _timeout):
            pass

        def sendall(self, data):
            self.output.extend(data)

        def close(self):
            pass

    client = Client()
    with patch.object(discovery, "Backend") as backend:
        backend.return_value.getresponse.return_value = response
        bridge.tcp_client(client, 123, [None, 0, True])
    head, body = bytes(client.output).split(b"\r\n\r\n", 1)
    assert head.startswith(b"HTTP/1.1 200 OK\r\n")
    for name in (b"Content-Length:", b"ETag:", b"Content-MD5:"):
        assert name not in head
    assert body == b""
    response.read.assert_not_called()
    assert backend.return_value.request.call_args.args[0] == "HEAD"


@pytest.mark.parametrize(
    "registered, method",
    [(True, "GET"), (False, "GET"), (True, "HEAD")],
    ids=["registered_get", "unregistered_get", "registered_head"],
)
def test_headerless_registered_xml_rewrites_but_unrelated_binary_passes(registered, method):
    body = b"<root><X_LGLINK-LOCATION>10.0.2.15</X_LGLINK-LOCATION></root>"
    bridge = Admission()
    if registered:
        bridge.register_description(123, "/desc.xml", 1800)
    response = Mock()
    response.status, response.reason = 200, "OK"
    response_body = body if registered else b"\x00\xff" + body
    headers = {"Content-Length": str(len(response_body)), "ETag": '"stock"'}
    response.getheaders.return_value = list(headers.items())
    response.getheader.side_effect = lambda name, default=None: headers.get(name, default)
    response.read.side_effect = [response_body, b""]
    client = HttpClient(method)
    with patch.object(discovery, "Backend") as backend:
        backend.return_value.getresponse.return_value = response
        bridge.tcp_client(client, 123, [None, 0, True])
    head, payload = bytes(client.output).split(b"\r\n\r\n", 1)
    assert head.startswith(b"HTTP/1.1 200 OK\r\n")
    if registered:
        assert b"ETag:" not in head
        if method == "HEAD":
            assert b"Content-Length:" not in head
            assert payload == b""
            response.read.assert_not_called()
        else:
            expected = body.replace(b"10.0.2.15", b"192.0.2.7")
            assert payload == expected
            assert b"Content-Length: " + str(len(expected)).encode() in head
    else:
        assert payload == response_body
        assert b'ETag: "stock"' in head


def test_goodbye_keeps_other_protocol_reference():
    bridge = Admission()
    endpoint = ("tcp", 123)
    bridge.endpoints[endpoint] = [None, 500, True]
    bridge.ssdp[b"synthetic-usn"] = (500, {endpoint})
    bridge.mdns["synthetic._example._tcp.local."] = (endpoint, 500)
    with patch.object(discovery.time, "monotonic", return_value=100):
        bridge.advertisement(
            1900, b"NOTIFY * HTTP/1.1\r\nUSN: synthetic-usn\r\nNTS: ssdp:byebye\r\n\r\n"
        )
    assert bridge.endpoints[endpoint][1] == 500
    bridge.mdns.clear()
    bridge.ssdp[b"synthetic-usn"] = (500, {endpoint})
    bridge.retire_unreferenced({endpoint}, 100)
    assert bridge.endpoints[endpoint][1] == 500
    bridge.ssdp.clear()
    bridge.retire_unreferenced({endpoint}, 100)
    assert bridge.endpoints[endpoint][1] == 100


def test_positive_refresh_does_not_shorten_listener():
    bridge = object.__new__(discovery.Outer)
    bridge.static = set()
    bridge.lock = threading.RLock()
    bridge.endpoints = {("tcp", 123): [None, 1900, False]}
    with patch.object(discovery.time, "monotonic", return_value=100):
        assert bridge.endpoint("tcp", 123, ttl=10)
        assert bridge.endpoints[("tcp", 123)][1] == 1900
        bridge.endpoint("tcp", 123, ttl=5000)
        assert bridge.endpoints[("tcp", 123)][1] == 5100


def test_expired_listener_replaced_and_old_cleanup_keeps_replacement():
    bridge = object.__new__(discovery.Outer)
    bridge.address = "192.0.2.7"
    bridge.static = set()
    bridge.lock = threading.RLock()
    key = ("tcp", 123)
    old_socket, new_socket = Mock(), Mock()
    old_entry = [old_socket, 99, False]
    bridge.endpoints = {key: old_entry}
    with (
        patch.object(discovery.time, "monotonic", return_value=100),
        patch.object(discovery.socket, "socket", return_value=new_socket),
        patch.object(discovery.threading, "Thread"),
    ):
        assert bridge.endpoint("tcp", 123, ttl=200)
        replacement = bridge.endpoints[key]
        assert replacement is not old_entry
        assert replacement[0] is new_socket
        assert replacement[1] == 300
        assert old_entry[1] == 99
        old_socket.close.assert_called_once()
        new_socket.bind.assert_called_once_with((bridge.address, 123))
        # Run the retired thread's actual cleanup deterministically after
        # the replacement has been registered, without scheduling sleeps.
        bridge.serve_endpoint(key, old_entry)
    assert bridge.endpoints[key] is replacement
    old_socket.accept.assert_not_called()
    new_socket.close.assert_not_called()


def test_reflection_cache_bounded_and_expiring():
    bridge = object.__new__(discovery.Outer)
    bridge.lock = threading.RLock()
    bridge.sent = {}
    with patch.object(discovery.time, "monotonic", return_value=100):
        for number in range(discovery.MAX_SENT + 10):
            bridge.sent_packet((5353, number), remember=True)
        assert len(bridge.sent) == discovery.MAX_SENT
        assert bridge.sent_packet((5353, discovery.MAX_SENT))
    with patch.object(discovery.time, "monotonic", return_value=103):
        assert not (bridge.sent_packet((5353, discovery.MAX_SENT)))
        assert bridge.sent == {}


def test_lan_advertisement_keeps_payload_and_destination_without_admission():
    bridge = object.__new__(discovery.Outer)
    bridge.address = "192.0.2.7"
    bridge.sent_packet = Mock(return_value=False)
    bridge.into_guest = Mock()
    bridge.advertisement = Mock(side_effect=AssertionError("LAN data cannot admit ports"))
    wire = b"NOTIFY * HTTP/1.1\r\nLOCATION: http://192.0.2.9:123/desc\r\n\r\n"
    bridge.lan_packet(1900, wire, ("192.0.2.9", 1900), True)
    bridge.into_guest.assert_called_once_with(1900, wire, True)
    bridge.into_guest.reset_mock()
    response = dns.message.make_response(dns.message.make_query("peer.local", "A")).to_wire()
    bridge.lan_packet(5353, response, ("192.0.2.9", 5353), False)
    bridge.into_guest.assert_called_once_with(5353, response, False)
    bridge.advertisement.assert_not_called()


def test_lan_reply_routes_to_original_guest_port():
    bridge = object.__new__(discovery.Outer)
    bridge.channel = MagicMock()
    wire = b"HTTP/1.1 200 OK\r\n\r\n"
    bridge.into_guest(1900, wire, False, 45678)
    bridge.channel.assert_called_once_with(
        "lan-datagram",
        port=1900,
        data=discovery.encoded(wire),
        multicast=False,
        destination_port=45678,
    )


def test_guest_query_event_preserves_source_port():
    bridge = object.__new__(discovery.Outer)
    bridge.guest_query = Mock()
    bridge.publish = Mock()
    wire = b"M-SEARCH * HTTP/1.1\r\n\r\n"
    with patch.object(discovery, "worker") as worker:
        bridge.handle_event(
            {"port": 1900, "data": discovery.encoded(wire), "query": True, "source_port": 45678}
        )
    worker.assert_called_once_with(bridge.guest_query, 1900, wire, 45678)
    bridge.publish.assert_not_called()


def test_mdns_query_modes():
    query = dns.message.make_query("peer.local", "A").to_wire()
    assert not (discovery.isolated_query(5353, query, 5353))
    assert discovery.isolated_query(5353, query, 45678)
    qu = bytearray(query)
    offset = discovery.discovery_dns._classes(query)[0]
    qu[offset] |= 0x80
    assert discovery.isolated_query(5353, bytes(qu), 5353)


def test_private_frame_round_trip():
    left, right = socket.socketpair()
    with left, right:
        thread = threading.Thread(
            target=discovery.send, args=(left, {"data": discovery.encoded(b"hello")})
        )
        thread.start()
        assert discovery.decoded(discovery.receive(right)["data"]) == b"hello"
        thread.join()


def test_frame_bound():
    left, right = socket.socketpair()
    with left, right, pytest.raises(ValueError):
        left.sendall(b"\xff\xff\xff\xff")
        discovery.receive(right)
