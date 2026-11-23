# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""LG's online services by host class, and what a VM with `lg_services = "mock"` (the default) does
with each: the one table the network side reads (docs/dev/architecture/network.md, "LG services").

Each class has
  mock       what mock mode does: "refuse" (the name does not resolve, its literal addresses are
             refused), "online" (the name resolves to the namespace's own answer of LG's online
             check, lg_mock.py), "answer" (the name resolves to the namespace's own HTTP and HTTPS
             responder, lg_sdp.py, which answers from lg_answers.py and forwards nothing) or
             "reach" (resolved and forwarded as any host is). Every class is LG's: in mitm mode
             none of them is passed through undecrypted when its client rejects the proxy's CA
             (mitm_addon.py's Capture); such a connection fails instead.
  names      DNS name patterns, lower-case. A pattern without "*" is that name and every name
             below it ("lgsmartad.com" is also "us.ad.lgsmartad.com"); one with "*" is a glob over
             the whole name, "*" standing for any characters, dots included.
  addresses  literal IPv4 addresses the firmware dials without DNS, refused like the names.

The study is docs/research/lg-services-block-or-mock.md ("the study" below): its endpoint table
(section 2) and the mock column (section 5). `lg_services = "real"` uses none of this.

Python 3 stdlib only, no imports: netns.py, lg_mock.py (the namespace's resolver) and
mitm_addon.py (inside mitmweb's sandbox) all read it.
"""

CLASSES = (
    # The study, endpoint table "lgtvonline.lge.com": connman's online check over plain HTTP.
    # Answered in the namespace, the same answer LG's server gives (lg_mock.ONLINE_RESPONSE).
    {"class": "online", "mock": "online", "names": ("lgtvonline.lge.com",)},
    # Endpoint table "rdx2.<sdp domain>, <ric>.rdl.lgtvcommon.com": rdxd's and uploadd's crash and
    # analytics uploads (with or without the QA prefix qt2-), pmlogd's dev-log fileInfo.
    {
        "class": "upload",
        "mock": "refuse",
        "names": (
            "*rdx2.lgtvsdp.com",
            "*rdx2.nextlgsdp.com",
            "*rdx2.tv.wiselg.com",
            "*rdx2.tv.wiselg.cn",
            "rdl.lgtvcommon.com",
        ),
    },
    # Endpoint table "<ric>.cdpbeacon[2].lgtvcommon.com": viewing beacons (cdpbeacon2 rejected
    # the proxy's CA and passed through before this table).
    {"class": "beacon", "mock": "refuse", "names": ("*cdpbeacon*.lgtvcommon.com",)},
    # Endpoint table "info.lgsmartad.com, <cc>.ad.lgsmartad.com": ads and impressions (admanager).
    {"class": "ads", "mock": "refuse", "names": ("lgsmartad.com",)},
    # Endpoint table "Alphonso ...; ACRCloud, Gracenote (WT1H)": ACR / Live Plus and overlay ads.
    {
        "class": "acr",
        "mock": "refuse",
        "names": ("alphonso.tv", "acrcloud.com", "gracenote.com"),
    },
    # Endpoint table "[qt2-]<ric>.lgtviot.com ... *.lgthinq.com ... *-core.ebp2.lgsmartplatform.com":
    # ThinQ and home connect, IoT registration and MQTT, push, sports alerts, billing. Node's own
    # roots reject the proxy's CA, MQTT uses client certificates: refused by name, not answered.
    # lgthinq.com also holds the voice hosts (he-*-ai.lgthinq.com).
    {
        "class": "iot",
        "mock": "refuse",
        "names": ("lgtviot.com", "lgthinq.com", "ebp2.lgsmartplatform.com"),
    },
    # Endpoint table "www.ueiwsp.com, interface.quicksetcloud.com, Azure IoT": UEI QuickSet
    # (librim_qsp.so) and its Azure IoT hub over MQTT on 8883. Only its own Azure names, not all
    # of azure-devices.net.
    {
        "class": "quickset",
        "mock": "refuse",
        "names": (
            "ueiwsp.com",
            "quicksetcloud.com",
            "global.azure-devices-provisioning.net",
            "nevoai-iothub-*.azure-devices.net",
        ),
    },
    # Endpoint table "snu.lge.com ... IP 156.147.69.32:8080": usr/sbin/update's firmware update
    # check and its download logs; the IP is dialled without DNS.
    {
        "class": "update",
        "mock": "refuse",
        "names": (
            "snu.lge.com",
            "su.lge.com",
            "su-ssl.lge.com",
            "nsu.lge.com",
            "snu-dev.lge.com",
            "su-dev.lge.com",
        ),
        "addresses": ("156.147.69.32",),
    },
    # Endpoint table "rone-{,tv-}{aic,eic,kic,cic,saic}.lge.com, IP 165.244.62.249": remotediag.
    {
        "class": "remotediag",
        "mock": "refuse",
        "names": ("rone-*.lge.com",),
        "addresses": ("165.244.62.249",),
    },
    # Endpoint table "he-*-ai.lgthinq.com, netflixvoice.lgtvcommon.com, Nuance ..., *.lpoong.com,
    # xiaoi": the voice cloud (the lgthinq.com hosts are in "iot" already).
    {
        "class": "voice",
        "mock": "refuse",
        "names": (
            "netflixvoice.lgtvcommon.com",
            "lgtv*.nuancemobility.net",
            "lpoong.com",
            "xiaoi.com",
        ),
    },
    # Endpoint table "<cc>.lgtvsdp.com, <cc>.nextlgsdp.com, <cc>.tv.wiselg.com": SDP (device auth,
    # services, terms, store lists). Answered by lg_sdp.py (the study's section 5): an
    # authenticated session with every logging switch off and no content.
    {
        "class": "sdp",
        "mock": "answer",
        "names": ("lgtvsdp.com", "nextlgsdp.com", "wiselg.com", "wiselg.cn"),
    },
    # Endpoint table rows for Home content (cdpsvc, nudge, homeprv, ... on lgtvcommon.com, the
    # ngfts static files on lge.com), store and LG Account (lgappstv.com, lgwebostv.com),
    # IBIS (ibs.*) and the rest of LG's own domains: answered by lg_sdp.py too, with no content
    # (the answer lg_answers.py has for the path, else 404 with an empty JSON object).
    {
        "class": "lg",
        "mock": "answer",
        "names": (
            "lge.com",
            "lgtvcommon.com",
            "lgappstv.com",
            "lgwebostv.com",
            "lgsmartplatform.com",
        ),
    },
)

MOCKS = ("refuse", "online", "answer", "reach")


def _match(pattern, name):
    if "*" not in pattern:
        return name == pattern or name.endswith("." + pattern)
    # a glob whose "*" crosses dots; the table's patterns have no other special characters
    parts = pattern.split("*")
    if not name.startswith(parts[0]) or not name.endswith(parts[-1]):
        return False
    pos, end = len(parts[0]), len(name) - len(parts[-1])
    if end < pos:
        return False
    for part in parts[1:-1]:
        i = name.find(part, pos, end)
        if i < 0:
            return False
        pos = i + len(part)
    return True


def normal(name):
    """A DNS name as the table compares it: lower-case, without the root's trailing dot."""
    return (name or "").strip().rstrip(".").lower()


def lookup(name):
    """The table's class of a DNS name (a CLASSES entry), or None if it is not LG's. The first
    class that matches wins: the refused classes come before the broad "reach" ones."""
    name = normal(name)
    if not name:
        return None
    for c in CLASSES:
        if any(_match(p, name) for p in c["names"]):
            return c
    return None


def mock_action(name):
    """What mock mode does with a DNS name: "refuse", "online", "answer", "reach" (an LG host
    forwarded as usual) or None (not LG's)."""
    c = lookup(name)
    return c["mock"] if c else None


def refused_addresses():
    """The literal addresses mock mode refuses, sorted."""
    return sorted({a for c in CLASSES if c["mock"] == "refuse" for a in c.get("addresses", ())})
