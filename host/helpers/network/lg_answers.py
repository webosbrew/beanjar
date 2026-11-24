# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""What lg_services = "mock" answers to LG's SDP server and LG's other web hosts (lg_sdp.py): data
only, no code. A TV whose LG account side works, with every logging and upload switch off and no
content.

ROUTES is read in order; the first entry whose host class (lg_hosts.py's "class", "sdp" unless
listed in "classes"), method ("GET" and "POST" unless listed in "methods") and path (a regular
expression searched in the request's path, query left out) match is the answer:
  name     what it is, for the log
  status   the HTTP status (200 unless given)
  body     a JSON value, sent compact; or
  text     a string body, with "type" its Content-Type
  type     the Content-Type (application/json;charset=UTF-8 unless given)
  headers  extra (name, value) pairs
In strings of body, text and headers: ${now_ms} and ${now_s} are the host's clock (Unix ms and s),
${country}, ${country3} and ${ric} the requesting TV's country (alpha-2, alpha-3) and LG's region
code for it (COUNTRIES, from the request's X-Device-Country, else the SDP host's prefix). A 2xx
answer also carries X-Server-Time (Unix ms), the TV's clock source "sdp".

No match is DEFAULT, logged as "unknown" in netns-lgmock.log so the path can be added here.
Shapes are the firmware clients' (docs/dev/architecture/network.md, "LG services").

Python 3 stdlib only, no imports.
"""

# alpha-2: (alpha-3, LG's ric: AIC the Americas, EIC Europe, KIC Korea and Asia)
COUNTRIES = {
    "US": ("USA", "AIC"),
    "CA": ("CAN", "AIC"),
    "MX": ("MEX", "AIC"),
    "BR": ("BRA", "AIC"),
    "GB": ("GBR", "EIC"),
    "DE": ("DEU", "EIC"),
    "FR": ("FRA", "EIC"),
    "IT": ("ITA", "EIC"),
    "ES": ("ESP", "EIC"),
    "NL": ("NLD", "EIC"),
    "KR": ("KOR", "KIC"),
    "JP": ("JPN", "KIC"),
    "AU": ("AUS", "KIC"),
    "IN": ("IND", "KIC"),
}
DEFAULT_COUNTRY = "US"
# LG's region code for a country the table above lacks, by the firmware's RMM group of it (the VM's
# country, lg_mock.py --country): the Americas' groups AIC, Europe's and CIS's EIC, the rest KIC
RIC_BY_GROUP = {"US": "AIC", "BR": "AIC", "EU": "EIC", "CS": "EIC"}

# A path the table does not know: what LG's server answers to an unknown resource, without the
# server time (LG sends X-Server-Time on success only).
DEFAULT = {"name": "unknown", "status": 404, "body": {}}

# Upload policy with every unit off: uploadd (WT1H to queue) schedules a unit only when its "switch"
# is "on" ("There is no server-side consent for uploading log files." otherwise) and tells rdxd
# (setuploadswitch); pmlogd fetches no whitelist when updateYn starts with "N".
LOG_UNIT = {
    "switch": "off",
    "requestInterval": "86400",
    "retryIntervalOnError": "0",
    "retryIntervalOnFail": "0",
    "retryCount": "0",
}
LOGGING_STATUS = {
    "logUnit": [
        dict(LOG_UNIT, name=name) for name in ("normal_log", "crash_log", "crash_log_nosyslog")
    ],
    "checkInterval": "86400",
    "anonymousInterval": "24",
    "whitelist": {"checksum": "", "updateYn": "N", "url": "", "version": 0},
}

# services.itemList: LG's per-feature server switches (Home, oobe, homeconnect compare to "on");
# the names W22O's clients and LG's own mock data ask for, all off. sdx needs the list present.
ITEM_NAMES = (
    "google_home", "naver_clova", "kakao_home", "channel_plus", "personalized_recommendation",
    "membership_service", "rdp_service", "billing_service", "efs_service", "nudge_service",
    "nudge_server_request", "nudge_log_transfer", "airplay", "home_effect", "cpin_firstuse",
    "music_discovery", "ai_link", "search_recommendation", "room_connect", "amazon_echo",
    "auto_home_launch", "avmsd", "freeview_play", "full_home_boot", "get_now", "get_now_full",
    "lgshop_overlay", "streaming_live_tv", "thetake_acr", "yandex_alice", "igallery_tripadvisor",
    "matter_service",
)  # fmt: skip
COUNTRY = {"code": "${country}", "threecode": "${country3}", "ricCode": "${ric}"}
NOTICE_LIST = {"lastUpdatedTime": "0", "notice": []}
# TermsManager::parse_downloadTermsJson needs eulaList.eulas and eulaMergedFileUrl; no terms are
# listed, so nothing is downloaded and the firmware's own texts stand
EULA_LIST = {"eulas": [], "eulaMergedFileUrl": ""}
EULA_MAPPING_LIST = {"maxEulaMappingGroupId": "GR0000", "eulaInfo": []}
APP_LIST = {"appCount": 0, "appList": []}

# The services object (initservices' and services' answer from v9.2 on, cached by sdx for its
# callers): what LK5900's capture and W22O's parsers have, empty, every switch off. serverDomain's
# version 0 is never newer than the firmware's own (server_addr_version.conf), so no domain list is
# fetched.
SERVICES = {
    "needCheckHardwareFeature": "N",
    "loggingStatus": LOGGING_STATUS,
    "itemList": [{"name": name, "switch": "off"} for name in ITEM_NAMES],
    "noticeList": NOTICE_LIST,
    "country": COUNTRY,
    "eulaList": EULA_LIST,
    "eulaMappingList": EULA_MAPPING_LIST,
    "blockedAppList": APP_LIST,
    "premiumAppList": APP_LIST,
    "launcherPromotion": {"promotionId": "", "promotionCount": 0},
    "serverDomain": {"url": "", "version": "0"},
}

# One authentication object for every layout: sessionID (v9.2 to v14.0, sent back as
# X-Authentication) and accessToken, refreshToken and accessTokenDuration (6.5.0, 9.0.0, 11.0.0:
# AuthState::getCreateTokenData). blockStolenTV must be "N" (AuthDataManager::set_stolen). No
# session exists anywhere: the values are fixed strings.
AUTHENTICATION = {
    "deviceSecret": "emulator-device-secret",
    "sessionID": "emulator-session",
    "deviceUniqueID": "emulator-device",
    "accessToken": "emulator-access-token",
    "refreshToken": "emulator-refresh-token",
    "accessTokenDuration": 86400,
    "blockStolenTV": "N",
}
COOKIES = (
    ("Set-Cookie", "WMONID=emulator; Expires=Fri, 31-Dec-2049 23:59:59 GMT; Path=/"),
    ("Set-Cookie", "JSESSIONID=emulator; Path=/; HttpOnly"),
)

# The version part of an SDP path: rest/sdp/v14.0/ (v7.0 to v14.0), rest/apps/webos8.0/,
# rest/appstore/v10.0/, or the token layout's <service>/<x.y.z>/ (auth/9.0.0/, init/6.5.0/, ...)
SDP = r"^/(rest/(sdp|appstore|apps)/(v|webos)[0-9.]+|[a-z-]+/[0-9]+\.[0-9]+\.[0-9]+)/"

ROUTES = (
    # --- WT1H, SDP v5.1 (sdx's own parsers, docs/research/appdrm-sdp.md "Endpoints") ---
    # SDX_DeviceAuth: 200, X-Server-Time and Set-Cookie read; SDX_GetDeviceAuthJson the three keys.
    # DELETE too: sdx authenticates again after the terms are agreed.
    {
        "name": "v5.1 authentication",
        "path": r"^/rest/sdp/v5\.1/authentication$",
        "methods": ("GET", "POST", "DELETE"),
        "headers": (("Set-Cookie", "JSESSIONID=emulator; Path=/; HttpOnly"),),
        "body": {
            "authentication": {
                "deviceSecret": "emulator-device-secret",
                "sessionID": "emulator-session",
                "deviceUniqueID": "emulator-device",
            }
        },
    },
    # SDX_GetNotificationJson: noticeList's popupNotice and serviceNotice, each a time and a list
    {
        "name": "v5.1 notice",
        "path": r"^/rest/sdp/v5\.1/notice$",
        "body": {
            "noticeList": {
                "popupNotice": {"time": "0", "notice": []},
                "serviceNotice": {"time": "0", "notice": []},
            }
        },
    },
    # SDX_GetDevProvisioningJson: deviceFeatureResult.fck, an integer, becomes X-Device-FCK
    {
        "name": "v5.1 hardwarefeature",
        "path": r"^/rest/appstore/v5\.1/device/hardwarefeature$",
        "body": {"deviceFeatureResult": {"fck": 0}},
    },
    # appinstalld AppUpdater::onReadyAppUpdate: appUpdateList an object, appUpdateCheck an array
    {
        "name": "v5.1 app update",
        "path": r"^/rest/appstore/v5\.1/app/update$",
        "body": {"appUpdateList": {"appUpdateCheck": []}},
    },
    # SDX_Get{Notification,Country,Terms}JsonFromService pick noticeList, country and account
    # (termsList) from it; uploadd reads loggingStatus. Country without ricCode, as v5.1 has it.
    {
        "name": "v5.1 services",
        "path": r"^/rest/sdp/v5\.1/services$",
        "body": {
            "services": {
                "noticeList": {
                    "popupNotice": {"time": "0", "notice": []},
                    "serviceNotice": {"time": "0", "notice": []},
                },
                "country": {"code": "${country}", "threecode": "${country3}"},
                "account": {"termsList": []},
                "loggingStatus": LOGGING_STATUS,
            }
        },
    },
    {
        "name": "v5.1 loggingstatus",
        "path": r"^/rest/sdp/v5\.1/loggingstatus$",
        "body": {"loggingStatus": LOGGING_STATUS},
    },
    # Country detection is not answered: sdx's change-country path asks the TV to reboot, and the
    # TV keeps the country it was set up with.
    # --- v7.0 to v14.0 (lgtvsdp.com), 6.5.0 and 9.0.0 (nextlgsdp.com), 11.0.0 (wiselg) ---
    # W19P to W22O: one POST does authentication, provisioning and services
    # (ReqInitServicesHandler, a 200 needed); 9.0.0 reads blockStolenTV at the root
    {
        "name": "initservices",
        "path": SDP + r"initservices$",
        "headers": COOKIES,
        "body": {
            "blockStolenTV": "N",
            "initServices": {
                "authentication": AUTHENTICATION,
                "activationDate": {"updatedActivationDate": "N", "updatedMessage": "0"},
                "deviceFeatureResult": {"fck": 0, "rcmdCategoryCount": 0},
                "services": SERVICES,
            },
        },
    },
    # 6.5.0 on: AuthState::createToken (POST) and refreshToken, both a 200 with x-server-time;
    # W22O deletes its session with DELETE before authenticating again after the terms change
    {
        "name": "authentication",
        "path": SDP + r"authentication(/refresh)?$",
        "methods": ("GET", "POST", "DELETE"),
        "headers": COOKIES,
        "body": {"authentication": AUTHENTICATION},
    },
    {"name": "services", "path": SDP + r"services$", "body": {"services": SERVICES}},
    {
        "name": "loggingstatus",
        "path": SDP + r"loggingstatus$",
        "body": {"loggingStatus": LOGGING_STATUS},
    },
    {"name": "notice", "path": SDP + r"notice(/target)?$", "body": {"noticeList": NOTICE_LIST}},
    # the terms: none listed, nothing to download; an agreement report and a withdrawal accepted
    {"name": "eula", "path": SDP + r"eula$", "methods": ("GET",), "body": {"eulaList": EULA_LIST}},
    {"name": "eula agreement", "path": SDP + r"eula$", "methods": ("POST",), "body": {}},
    {
        "name": "eulamapping",
        "path": SDP + r"eulamapping$",
        "body": {"eulaMappingList": EULA_MAPPING_LIST},
    },
    {
        "name": "terms withdrawal",
        "path": SDP + r"deviceinfo$",
        "methods": ("GET", "POST", "DELETE"),
        "body": {},
    },
    # ReqSetTimeHandler reads only X-Server-Time from it
    {"name": "server time", "path": SDP + r"serverstatus/status$", "methods": ("GET",), "body": {}},
    {
        "name": "app update",
        "path": SDP + r"app/update$",
        "body": {"appUpdateList": {"appUpdateCheck": []}},
    },
    # ACR's configuration refused: no ACR or overlay ads start (ReqACRHandler: "Invalid server
    # response")
    {"name": "acrconfig refused", "path": SDP + r"acrconfig$", "status": 404, "body": {}},
    # --- LG's other hosts (the "lg" class) ---
    # sdx's smartConfig (wiseconfig.lgtvcommon.com, service.lgtvcommon.com): its own default
    {
        "name": "smartConfig",
        "path": r"/getConfig$",
        "classes": ("lg",),
        "body": {"smartConfigVer": "0.0", "smartConfig": []},
    },
)
