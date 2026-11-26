# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""lg_services=mock's SDP and LG web responder: host/helpers/network/lg_sdp.py's certificates
(minted from a CA made here with tlsca.py), its answers from lg_answers.py per SDP version, and the
fallback for unknown paths. On the loopback only: no request leaves the host."""

import http.client
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "host/helpers/network"))
import lg_answers
import lg_hosts
import lg_mock
import lg_sdp
import tlsca

pytestmark = pytest.mark.skipif(not shutil.which("openssl"), reason="needs the openssl tool")


@pytest.fixture(scope="module")
def ca(tmp_path_factory):
    d = tmp_path_factory.mktemp("lgsdp")
    tlsca.init(str(d / "ca"))
    return d


@pytest.fixture(scope="module")
def https(ca):
    """The HTTPS responder on the loopback, its log lines, and a client context trusting only the
    CA."""
    lines = []
    certs = lg_sdp.Certs(str(ca / "ca"), str(ca / "certs"))
    server = lg_sdp.Server(("127.0.0.1", 0), lines.append, None, certs)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = ssl.create_default_context(cafile=str(ca / "ca" / "ca.pem"))
    yield server, lines, client, ca
    server.shutdown()
    server.server_close()


def request(https, host, method, path, headers=None, body=None, sni=True):
    server, _lines, client, _ca = https
    raw = socket.create_connection(server.server_address, 5)
    s = client.wrap_socket(raw, server_hostname=host if sni else None)
    conn = http.client.HTTPConnection(host, timeout=5)
    conn.sock = s
    conn.request(method, path, body=body, headers=headers or {})
    r = conn.getresponse()
    data = r.read()
    out = (r.status, {k.lower(): v for k, v in r.getheaders()}, data, s.getpeercert())
    conn.close()
    return out


def test_certificate_per_name_from_the_ca(https):
    status, _h, _b, cert = request(https, "us.lgtvsdp.com", "GET", "/x")
    assert status == 404
    assert ("DNS", "us.lgtvsdp.com") in cert["subjectAltName"]
    assert dict(x[0] for x in cert["issuer"])["commonName"] == "Beanjar netstub local CA"
    # another answered name gets its own; the cache holds both, the CA's key is not copied
    request(https, "aic.cdpsvc.lgtvcommon.com", "GET", "/x")
    certs = sorted(os.listdir(https[3] / "certs"))
    assert "us.lgtvsdp.com.pem" in certs and "aic.cdpsvc.lgtvcommon.com.pem" in certs
    assert "ca.key" not in certs
    assert oct(os.stat(https[3] / "certs").st_mode & 0o777) == "0o700"


def test_certificate_validity_and_extensions(https):
    pem = https[3] / "certs" / "us.lgtvsdp.com.pem"
    if not pem.exists():
        request(https, "us.lgtvsdp.com", "GET", "/x")
    text = subprocess.run(
        ["openssl", "x509", "-in", str(pem), "-noout", "-text"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "Not Before: Jan  1 00:00:00 2000 GMT" in text
    assert "sha256WithRSAEncryption" in text
    assert "CA:FALSE" in text and "TLS Web Server Authentication" in text
    verify = subprocess.run(
        ["openssl", "verify", "-CAfile", str(https[3] / "ca" / "ca.pem"), str(pem)],
        capture_output=True,
        text=True,
    )
    assert verify.returncode == 0, verify.stderr


def test_no_sni_gets_the_wildcard_certificate(https):
    server, _lines, _client, ca = https
    ctx = ssl.create_default_context(cafile=str(ca / "ca" / "ca.pem"))
    ctx.check_hostname = False
    with socket.create_connection(server.server_address, 5) as raw:
        with ctx.wrap_socket(raw) as s:
            names = {v for k, v in s.getpeercert()["subjectAltName"]}
    for name in ("lgtvsdp.com", "*.lgtvsdp.com", "*.nextlgsdp.com", "*.lgtvcommon.com"):
        assert name in names


def test_names_not_answered_get_no_certificate_of_their_own(https):
    certs = https[0].certs
    assert certs.context("www.youtube.com") is certs.default
    assert certs.context("rdx2.lgtvsdp.com") is certs.default  # refused, never minted
    assert certs.context("bad name") is certs.default
    assert not (https[3] / "certs" / "www.youtube.com.pem").exists()


def test_unknown_path_is_404_empty_json_and_logged(https):
    _server, lines, _client, _ca = https
    status, headers, body, _ = request(
        https, "us.lgtvsdp.com", "POST", "/rest/sdp/v99.9/nothing?id=SECRET", body=b"a=1"
    )
    assert status == lg_answers.DEFAULT["status"] == 404
    assert json.loads(body) == {}
    assert headers["content-type"].startswith("application/json")
    assert "x-server-time" not in headers  # LG's server sends it on success only
    assert any(
        line == "unknown POST https://us.lgtvsdp.com/rest/sdp/v99.9/nothing?id 404"
        for line in lines
    ), lines
    assert not any("SECRET" in line for line in lines)


def test_kept_alive_and_chunked(https):
    server, _lines, client, _ca = https
    raw = socket.create_connection(server.server_address, 5)
    s = client.wrap_socket(raw, server_hostname="us.lgtvsdp.com")
    conn = http.client.HTTPConnection("us.lgtvsdp.com", timeout=5)
    conn.sock = s
    for _ in range(3):  # one connection, as sdx's curl keeps it
        conn.request("POST", "/a", body=iter([b"x=1", b"&y=2"]), encode_chunked=True)
        r = conn.getresponse()
        r.read()
        assert r.status == 404
    conn.close()


def test_plain_http_online_check_and_answered_hosts():
    lines = []
    server = lg_sdp.Server(("127.0.0.1", 0), lines.append, lg_mock.ONLINE_RESPONSE)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for host in ("lgtvonline.lge.com", None):
            with socket.create_connection(server.server_address, 5) as c:
                c.sendall(
                    b"GET / HTTP/1.1\r\n"
                    + (b"Host: %s\r\n" % host.encode() if host else b"")
                    + b"\r\n"
                )
                got = b""
                while chunk := c.recv(4096):
                    got += chunk
            assert got == lg_mock.ONLINE_RESPONSE
        conn = http.client.HTTPConnection(*server.server_address, timeout=5)
        conn.request("GET", "/img/a.png", headers={"Host": "aic-ngfts.lge.com"})
        r = conn.getresponse()
        assert r.status == 404 and r.read() == b"{}"
        conn.close()
        assert "unknown GET http://aic-ngfts.lge.com/img/a.png 404" in lines
    finally:
        server.shutdown()
        server.server_close()


def test_country_from_the_request():
    assert lg_sdp.country({"X-Device-Country": "DEU"}, "x") == ("DE", "DEU", "EIC")
    assert lg_sdp.country({}, "qt2-kr.lgtvsdp.com") == ("KR", "KOR", "KIC")
    assert lg_sdp.country({}, "EU7.lgtvsdp.com")[0] == lg_answers.DEFAULT_COUNTRY
    assert lg_sdp.country({}, "")[0] == lg_answers.DEFAULT_COUNTRY


def test_country_where_the_tv_is(monkeypatch):
    """lg_mock.py --country (the VM's country): a request that names no country of the table (a
    TV whose service country is still its group's Others row, at us7.lgtvsdp.com) is told the
    VM's, as LG's server answers from the TV's address; one the table knows still answers itself.
    The ric is the table's, else the RMM group's."""
    monkeypatch.setattr(lg_sdp, "LOCATION", lg_sdp.location("can,ca,US"))
    assert lg_sdp.country({"X-Device-Country": "US7"}, "us7.lgtvsdp.com") == ("CA", "CAN", "AIC")
    assert lg_sdp.country({}, "us7.lgtvsdp.com") == ("CA", "CAN", "AIC")
    assert lg_sdp.country({"X-Device-Country": "DEU"}, "x") == ("DE", "DEU", "EIC")
    monkeypatch.setattr(lg_sdp, "LOCATION", lg_sdp.location("AUT,AT,EU"))
    assert lg_sdp.country({"X-Device-Country": "AUT"}, "EU7.lgtvsdp.com") == ("AT", "AUT", "EIC")
    assert lg_sdp.location("SGP,SG,AJ") == ("SG", "SGP", "KIC")
    for bad in ("CAN", "CA,CAN,US", "CAN,CA"):
        with pytest.raises(ValueError, match="ALPHA3,ALPHA2,GROUP"):
            lg_sdp.location(bad)


def test_first_use_pending():
    # an Others row of sdx's table (US7, EU7...) in X-Device-Country, else the SDP host's label
    assert lg_sdp.first_use_pending({"X-Device-Country": "US7"}, "us.lgtvsdp.com")
    assert lg_sdp.first_use_pending({}, "US7.nextlgsdp.com")
    assert lg_sdp.first_use_pending({}, "qt2-eu7.lgtvsdp.com")
    assert not lg_sdp.first_use_pending({"X-Device-Country": "USA"}, "us7.lgtvsdp.com")
    assert not lg_sdp.first_use_pending({}, "us.nextlgsdp.com")
    assert not lg_sdp.first_use_pending({"X-Device-Country": "US"}, "")


def test_request_of_an_others_row_is_withheld(https):
    """An SDP request naming an Others row gets no answer (the connection closes, as without a
    network) and is logged as withheld; the same request with a country is answered."""
    _server, lines, _client, _ca = https
    with pytest.raises((http.client.RemoteDisconnected, ConnectionError)):
        request(
            https,
            "us.nextlgsdp.com",
            "POST",
            "/init/9.0.0/initservices",
            headers={"X-Device-Country": "US7"},
        )
    assert any(
        ln.startswith("withheld POST https://us.nextlgsdp.com/init/9.0.0/initservices")
        and "US7" in ln
        for ln in lines
    )
    status, _h, _b, _c = request(
        https,
        "us.nextlgsdp.com",
        "POST",
        "/init/9.0.0/initservices",
        headers={"X-Device-Country": "USA"},
    )
    assert status == 200


def test_render_substitutes_and_times():
    a = {
        "status": 200,
        "body": {"t": "${now_ms}", "c": ["${country}", "${ric}"]},
        "headers": [("X-A", "${now_s}")],
    }
    status, headers, body = lg_sdp.render(
        a, {"X-Device-Country": "USA"}, "us.lgtvsdp.com", now=1700000000.5
    )
    assert status == 200
    assert json.loads(body) == {"t": "1700000000500", "c": ["US", "AIC"]}
    assert ("X-Server-Time", "1700000000500") in headers and ("X-A", "1700000000") in headers


def test_routes_table_shape():
    names = set()
    for r in lg_answers.ROUTES:
        assert set(r) <= {
            "name",
            "path",
            "methods",
            "classes",
            "status",
            "body",
            "text",
            "type",
            "headers",
        }, r
        assert r["name"] not in names, r["name"]
        names.add(r["name"])
        re.compile(r["path"])
        assert not ("body" in r and "text" in r), r
        for c in r.get("classes", ("sdp",)):
            assert any(x["class"] == c and x["mock"] == "answer" for x in lg_hosts.CLASSES), r
        json.dumps(r.get("body", {}))  # JSON values only
    assert lg_answers.DEFAULT_COUNTRY in lg_answers.COUNTRIES


@pytest.mark.parametrize(
    "method, host, path, name",
    [
        # WT1H, v5.1
        ("GET", "EU7.lgtvsdp.com", "/rest/sdp/v5.1/authentication", "v5.1 authentication"),
        ("DELETE", "EU7.lgtvsdp.com", "/rest/sdp/v5.1/authentication", "v5.1 authentication"),
        ("GET", "EU7.lgtvsdp.com", "/rest/sdp/v5.1/notice", "v5.1 notice"),
        (
            "POST",
            "EU7.lgtvsdp.com",
            "/rest/appstore/v5.1/device/hardwarefeature",
            "v5.1 hardwarefeature",
        ),
        ("GET", "EU7.lgtvsdp.com", "/rest/sdp/v5.1/services", "v5.1 services"),
        # W19P v10.2, W22O number1 v14.0
        ("POST", "US.lgtvsdp.com", "/rest/sdp/v10.2/initservices", "initservices"),
        ("POST", "US.lgtvsdp.com", "/rest/sdp/v14.0/initservices", "initservices"),
        ("DELETE", "US.lgtvsdp.com", "/rest/sdp/v14.0/authentication", "authentication"),
        (
            "GET",
            "US.lgtvsdp.com",
            "/rest/sdp/v14.0/loggingstatus?whitelistVersion=0",
            "loggingstatus",
        ),
        ("GET", "US.lgtvsdp.com", "/rest/apps/webos8.0/serverstatus/status", "server time"),
        ("GET", "US.lgtvsdp.com", "/rest/appstore/v10.0/serverstatus/status", "server time"),
        ("GET", "US.lgtvsdp.com", "/rest/sdp/v14.0/eula?voice=Y", "eula"),
        ("POST", "US.lgtvsdp.com", "/rest/sdp/v14.0/eula", "eula agreement"),
        ("GET", "US.lgtvsdp.com", "/rest/sdp/v14.0/acrconfig", "acrconfig refused"),
        # W21A 6.5.0, W24P 9.0.0 (nextlgsdp.com), queue 11.0.0 (wiselg)
        ("POST", "US.nextlgsdp.com", "/auth/6.5.0/authentication", "authentication"),
        (
            "POST",
            "US.nextlgsdp.com",
            "/auth/9.0.0/authentication/refresh",
            "authentication refresh",
        ),
        (
            "POST",
            "US.tv.wiselg.com",
            "/auth/12.0.0/authentication/refresh",
            "authentication refresh",
        ),
        ("POST", "US.nextlgsdp.com", "/init/9.0.0/initservices", "initservices"),
        ("GET", "US.nextlgsdp.com", "/logging/9.0.0/loggingstatus", "loggingstatus"),
        ("GET", "US.nextlgsdp.com", "/home/9.0.0/eulamapping?voice=N", "eulamapping"),
        ("GET", "JP.nextlgsdp.com", "/apps/9.0.0/serverstatus/status", "server time"),
        ("POST", "us.tv.wiselg.com", "/init/11.0.0/initservices", "initservices"),
        ("POST", "AIC.wiseconfig.lgtvcommon.com", "/smartConfig/getConfig", "smartConfig"),
    ],
)
def test_routes_per_version(method, host, path, name):
    answer, known = lg_sdp.lookup(method, host, path)
    assert known and answer["name"] == name


def test_unknown_and_licence_paths_fall_back():
    for method, host, path in [
        ("GET", "US.lgtvsdp.com", "/rest/apps/webos8.0/applicense/applicenseurl?app_id=x"),
        ("GET", "US.lgtvsdp.com", "/rest/sdp/v14.0/country"),
        ("GET", "aic.cdpsvc.lgtvcommon.com", "/api/v1.0/channelmap"),
        ("GET", "www.example.org", "/rest/sdp/v14.0/initservices"),  # not LG's
    ]:
        answer, known = lg_sdp.lookup(method, host, path)
        assert not known and answer is lg_answers.DEFAULT


@pytest.mark.parametrize(
    "path",
    ["/rest/sdp/v14.0/initservices", "/init/9.0.0/initservices", "/rest/sdp/v10.2/initservices"],
)
def test_initservices_authenticates_with_every_switch_off(path):
    answer, _ = lg_sdp.lookup("POST", "US.lgtvsdp.com", path)
    status, headers, body = lg_sdp.render(answer, {"X-Device-Country": "USA"}, "US.lgtvsdp.com")
    body = json.loads(body)
    assert status == 200 and any(k == "X-Server-Time" for k, _ in headers)
    assert [v for k, v in headers if k == "Set-Cookie"][0].startswith("WMONID=")
    auth = body["initServices"]["authentication"]
    for key in ("deviceSecret", "sessionID", "deviceUniqueID", "accessToken", "refreshToken"):
        assert isinstance(auth[key], str) and auth[key]
    assert auth["blockStolenTV"] == "N" and body["blockStolenTV"] == "N"
    s = body["initServices"]["services"]
    units = s["loggingStatus"]["logUnit"]
    assert {u["name"] for u in units} >= {"normal_log", "crash_log"}
    assert all(u["switch"] == "off" for u in units)
    assert s["loggingStatus"]["whitelist"]["updateYn"] == "N"
    assert s["itemList"] and all(i["switch"] == "off" for i in s["itemList"])
    assert s["country"] == {"code": "US", "threecode": "USA", "ricCode": "AIC"}
    assert s["eulaList"]["eulas"] == [] and s["noticeList"]["notice"] == []
    assert s["blockedAppList"] == {"appCount": 0, "appList": []}


@pytest.mark.parametrize(
    "path", ["/auth/11.0.0/authentication", "/auth/12.0.0/authentication/refresh"]
)
def test_token_lasts_past_sdx_refresh_margin(path):
    """sdx reads accessTokenDuration in ms and refreshes a token with 600 s or less left at once;
    the refresh answer carries accessToken at its root (AuthState::getRefreshTokenData)."""
    answer, _ = lg_sdp.lookup("POST", "US.tv.wiselg.com", path)
    status, headers, body = lg_sdp.render(answer, {}, "US.tv.wiselg.com")
    body = json.loads(body)
    assert status == 200 and any(k == "X-Server-Time" for k, _ in headers)
    token = body if path.endswith("/refresh") else body["authentication"]
    assert isinstance(token["accessToken"], str) and token["accessToken"]
    assert token["accessTokenDuration"] > 600 * 1000 * 2
    if not path.endswith("/refresh"):
        for key in ("deviceSecret", "refreshToken", "deviceUniqueID"):
            assert token[key]


def test_v51_answers():
    a, _ = lg_sdp.lookup("GET", "EU7.lgtvsdp.com", "/rest/sdp/v5.1/authentication")
    _s, headers, body = lg_sdp.render(a, {}, "EU7.lgtvsdp.com")
    assert set(json.loads(body)["authentication"]) == {
        "deviceSecret",
        "sessionID",
        "deviceUniqueID",
    }
    assert any(k == "Set-Cookie" for k, _ in headers)
    a, _ = lg_sdp.lookup("POST", "EU7.lgtvsdp.com", "/rest/appstore/v5.1/device/hardwarefeature")
    assert json.loads(lg_sdp.render(a, {}, "x")[2]) == {"deviceFeatureResult": {"fck": 0}}
    a, _ = lg_sdp.lookup("GET", "EU7.lgtvsdp.com", "/rest/sdp/v5.1/services")
    s = json.loads(lg_sdp.render(a, {}, "x")[2])["services"]
    assert all(u["switch"] == "off" for u in s["loggingStatus"]["logUnit"])
