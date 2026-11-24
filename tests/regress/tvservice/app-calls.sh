#!/bin/sh
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
# Inside webOS: the tvservice methods and keys the firmware's own apps call, each checked for the reply LG's tvservice gives
# on this emulator, and what the soc layer's HAL and DILE shims let it do: master volume, AV on
# and off, screenshots. Leaves nothing behind (the volume is set to what it was). The replies that
# name the firmware or differ between its tvservice releases are chosen by the board's chip= and modelName=
# (guest/layers/soc/<chip>/rootfs/etc/emulator/board.cmdline): WT1H on H13, W15M on M14, W15U on LM15U, W16R on M2, W17H
# on M16P, W17M on M16P lite (a 43UJ6300-UA), W17R on M2R, W18R on M3, W19R on M3R and W19P on K5LP. Every later board (webOS 5 on: only the services they still
# have) takes its expectations from the firmware's own files (model_id.json, tvconfig) and the firmware's name.
#   (the VM gets it with `host/scripts/vm-launcher put tests/regress/tvservice/app-calls.sh` on the host)
#   webos-enter sh /mnt/host/tests/regress/tvservice/app-calls.sh [FIRMWARE]
# FIRMWARE: the firmware's name (05.06.10.01-HE_DTV_WT1H_AFAAABAA), whose version every board from
# webOS 5 on expects (the model, OTA id and boardType it takes from the firmware's own files).
# TIME_SCALE (default 1; 4 under TCG, from tests/regress/guest.sh) stretches each call's limit.
scale=${TIME_SCALE:-1}
# busybox's timeout takes -t on the older firmwares (before busybox 1.30), the plain number from webOS 7
if timeout -t 1 true 2>/dev/null; then T="timeout -t"; else T="timeout"; fi
fails=0
cmdline() { tr ' ' '\n' < /proc/cmdline | sed -n "s/^$1=//p"; }
# Each board's model (guest/layers/soc/<chip>/rootfs/etc/emulator/board.cmdline): its tool options as tvservice decodes them from the EEPROM, and its
# rear panel, the input row of ToolOpt2 (inputs: getExternalInputStatusList's count).
case $(cmdline chip)/$(cmdline modelName) in
# WT1H: a 49UB850V, UHD table row 1: SCART, AV, component, HDMI x4
H13*)   model=WT1H ota=AFAAABAA fw=05.06.10 platform=lg115x sound_bad_type=SOUND_ERROR_1001
	capture_no_video=CAPTURE_ERROR_09 inputs=7 reset3d='"returnValue":true'
	tool_options='"panelInch":"49","backlightType":"edgeLed","cameraReady":"ready","headphone":"true"'
	max_devices='"validDeviceNumber":7,"deviceList":[{"deviceID":"SCART_1","deviceNumber":1},{"deviceID":"AV_1","deviceNumber":1},{"deviceID":"COMP_1","deviceNumber":1},{"deviceID":"HDMI_1","deviceNumber":1},{"deviceID":"HDMI_2","deviceNumber":1},{"deviceID":"HDMI_3","deviceNumber":1},{"deviceID":"HDMI_4","deviceNumber":1}]' ;;
# W15M: LG1311 is the M14's name. Its DILE_VT is LG's own (the newer interface, no shim on M14:
# guest/layers/soc/m14), over lg115x.ko's de0 VTM. tv.capture's VIDEO without video answers
# CAPTURE_ERROR_09 ("no signal state"), as on a set: _CAPTURE_VT_Capture asks
# CAPTURE_Pipeline_IsVideoOnScreen once _CAPTURE_VT_Start has its DILE_VT handle and frame
# buffers, and that is false without a video sync (API_VIDEO_GetVideoSyncStatus, V_SYNC_BAD). A
# capture backend that fails DILE_VT_Create answers CAPTURE_ERROR_01 before the check is made.
# Its getMaximumExternalDeviceNumber is not called: LG's interface_externaldevice_getMaximum...
# mallocs 18 bytes per device of externaldevice_makeList's count (6), which leaves out input types
# 7 and 8, then writes a "_1" entry for each of them too (OSA_MD_GetMaxInputNumber: 1 for both). The
# 36 bytes past the buffer break the next heap chunk; depending on the layout, that call or a later
# one of the same thread (tv.externaldevice, ...) dies in malloc and dbgfrwk's handler then waits
# on the arena lock forever: every later call to that thread times out with an empty reply. No
# configuration avoids it: types 7 and 8 are 1 and skipped whatever the tool options say, so a set
# overruns by at least 36 bytes too.
# The board is a 42LF652V: FHD table row 2, SCART, AV, component, HDMI x3.
M14*)   model=W15M ota=AFAAABAA fw=04.06.75 platform=lg1311 sound_bad_type=SOUND_ERROR_013
	capture_no_video=CAPTURE_ERROR_09 max_devices= inputs=6 reset3d='"returnValue":true'
	tool_options='"panelInch":"42","backlightType":"directM","cameraReady":"ready","headphone":"true"' ;;
# W15U (lm15u is its own platform name; MBoot's chip=LM15UB0), webOS 2 on MStar: a 65UF850V, UHD
# DVB table row 4 (ToolOpt2's video input index): HDMI x3, component, SCART, AV; edge LED, a
# camera-ready set with a headphone output (ToolOpt3, ToolOpt5). tv.capture's VIDEO without video
# answers CAPTURE_ERROR_01: a capture step before tvservice's no-signal check fails here (not
# traced; W15M and W16R answer 09 once theirs work). tvservice answers SOUND_ERROR_013 as
# W16R and as W15M on 3D. Its
# getMaximumExternalDeviceNumber is not called, as W15M's.
LM15U*) model=W15U ota=AFADABAA fw=04.06.25 platform=lm15u sound_bad_type=SOUND_ERROR_013
	capture_no_video=CAPTURE_ERROR_01 max_devices= inputs=6 reset3d='"returnValue":true'
	tool_options='"panelInch":"65","backlightType":"edgeLed","cameraReady":"ready","headphone":"true"' ;;
# W16R (msd93f2g is M2's name; MBoot's chip=M2A0, M2A1, M2B0 or M2B1, not LG's M23 of the webOS 5 boards): tvservice reads the board's EEPROM over MStar's HWI2C
# (mstar-utopia.ko). The board is a 43LH604V: non-UHD table row 7, AV, component, HDMI x3, no
# SCART; no camera, no 3D, a headphone output (W16R's RELEASE.cfg, the LH60's ToolOpt3). Its
# getMaximumExternalDeviceNumber is not called either, as W15M's. tv.capture's VIDEO gets its
# frame through the libkadaptor shim's XC status and DIP; without video it answers
# CAPTURE_ERROR_09 ("no signal state"), as W15M and a set do (the same IsVideoOnScreen check,
# here on the AV-block branch after the sync status).
# W17R (msd93xbg is M2R's name, MBoot's chip=M2RA0; matched before M2), webOS 3.5, the ATSC
# build: a 43LJ5500-UA (US), RELEASE.cfg's LJ61 defaults: direct LED, camera ready, a headphone
# output, 3D (ToolOpt5's SUPPORT_3D_IN_2D3D, so reset3DStatus succeeds); tvservice lists HDMI x2,
# component and AV (row 2 of video_external_input_adjust). tvSystemName is atsc. LG dropped
# getCalibrationImageDrawingState from tv.display ("Unknown method"), as W17H's.
M2R*)   model=W17R ota=AFAAATAA fw=06.10.60 platform=msd93xbg sound_bad_type=SOUND_ERROR_013
	capture_no_video=CAPTURE_ERROR_09 max_devices= calibration= inputs=4 tv_system=atsc
	tool_options='"panelInch":"43","backlightType":"directM","cameraReady":"ready","headphone":"true"'
	reset3d='"returnValue":true' ;;
M2[AB]*) model=W16R ota=AFAAABAA fw=05.70.50 platform=msd93f2g sound_bad_type=SOUND_ERROR_013
	capture_no_video=CAPTURE_ERROR_09 max_devices= inputs=5
	tool_options='"panelInch":"43","backlightType":"directM","cameraReady":"off","headphone":"true"'
	reset3d='"errorCode":"DISPLAY_ERROR_1002"' ;;
# W17H (lg1313 is M16P's name), the AFADABAA build: a 55UJ750V, row 1 of the one input table, AV,
# component, HDMI x4; edge LED, no 3D, no camera, a headphone output (ToolOpt3). LG
# dropped getMaximumExternalDeviceNumber from tv.externaldevice and getCalibrationImageDrawingState
# from tv.display ("Unknown method"). Its tv.capture gets a handle from LG's HAL_VT over
# lg115x.ko's display engine (/dev/lg/de0, the VTM page): GRAPHIC captures work; VIDEO without
# video answers CAPTURE_ERROR_09 ("no signal state").
# W17M (M16P lite: lxboot's chip=M16P as W17H's, so matched by its board's model, before M16P), the
# ATSC build: a 43UJ6300-UA (US), RELEASE.cfg's SJ85 defaults: edge LED, camera ready, a headphone
# output, 2D only; row 1 of the input table, AV, component, HDMI x4. tvSystemName is atsc. The rest
# as W17H's.
M16P*/43UJ6300*) model=W17M ota=AFADATAA fw=06.10.55 platform=lg1313 sound_bad_type=SOUND_ERROR_013
	capture_no_video=CAPTURE_ERROR_09 calibration= max_devices= inputs=6 tv_system=atsc
	tool_options='"panelInch":"43","backlightType":"edgeLed","cameraReady":"ready","headphone":"true"'
	reset3d='"errorCode":"DISPLAY_ERROR_1002"' ;;
M16P*)  model=W17H ota=AFADABAA fw=06.10.65 platform=lg1313 sound_bad_type=SOUND_ERROR_013
	capture_no_video=CAPTURE_ERROR_09 calibration= max_devices= inputs=6
	tool_options='"panelInch":"55","backlightType":"edgeLed","cameraReady":"off","headphone":"true"'
	reset3d='"errorCode":"DISPLAY_ERROR_1002"' ;;
# W18R (msd94bcg is M3's name, MBoot's chip=M3A0), webOS 4.4: a 32LK6100PLB, direct LED, no
# camera, no headphone; tvservice lists AV, component and HDMI x3, row 1 of its
# video_external_input_adjust (ToolOpt2's video input index 1, tv.model.videoIndex). Its
# tvservice no longer knows contiLockRatingAU, NZ and SG ("ERROR_06"), nor
# getMaximumExternalDeviceNumber and getCalibrationImageDrawingState ("Unknown method");
# reset3DStatus answers true without 3D; webOS 4's hub denies a private method to a public
# caller ("Denied method call") where earlier ones did not find it. Every capture creates a
# DILE_VT handle, whose MStar HAL_VT divides by the panel's DE window, which KADP asks of XC in
# kernel mode (MApi_SC_get_disp_de_window, MApi_XC_S_Disp_Ctrl 0x21): mstar-utopia.ko answers it
# 1920x1080 (xc_disp_ctrl); a zero window was a SIGFPE that had dbgfrwk restart the set. GRAPHIC
# captures work; VIDEO without video answers CAPTURE_ERROR_01 headless (no video window) and
# CAPTURE_ERROR_09 ("no signal state") with Live TV's no-signal screen in front.
# W19R (msd94bexm8 is M3R's name, MBoot's chip=M3RA0; matched before M3), webOS 4.10, the ATSC
# build: a 43LM5700PUA (US), direct LED, no camera, no headphone; tvservice lists AV, component and
# HDMI x3 (row 1 of video_external_input_adjust, as W18R), counted from the deviceList entries as
# W19P's (no totalDeviceNumber). tvSystemName is atsc. Its tvservice knows neither clearPlus nor
# W18R's three rating keys. The rest as W18R's.
M3R*)   model=W19R ota=AFAAATAA fw=05.40.97 platform=msd94bexm8 sound_bad_type=SOUND_ERROR_013
	capture_no_video='CAPTURE_ERROR_01 CAPTURE_ERROR_09' max_devices= calibration= inputs=5
	input_count=deviceid private_answer='Denied method call' tv_system=atsc
	no_keys='clearPlus contiLockRatingAU contiLockRatingNZ contiLockRatingSG'
	tool_options='"panelInch":"43","backlightType":"directL","cameraReady":"off","headphone":"false"'
	reset3d='"returnValue":true' ;;
M3*)    model=W18R ota=AFAAABAA fw=05.50.70 platform=msd94bcg sound_bad_type=SOUND_ERROR_013
	capture_no_video='CAPTURE_ERROR_01 CAPTURE_ERROR_09' max_devices= calibration= inputs=5 private_answer='Denied method call'
	no_keys='contiLockRatingAU contiLockRatingNZ contiLockRatingSG'
	tool_options='"panelInch":"32","backlightType":"directL","cameraReady":"off","headphone":"false"'
	reset3d='"returnValue":true' ;;
# W19P (rtd2873 is K5LP's name, U-Boot's chip=K5LPA0), webOS 4.10: a 43UM7500PLA, direct LED, no
# camera, no headphone; tvservice lists AV, component and HDMI x4 from its ToolOptionDB's input
# bytes (RELEASE.cfg's UM75 43" defaults), and its getExternalInputStatusList has no
# totalDeviceNumber or mainInputSourceId any more: the deviceList entries are counted
# (input_count). It no longer knows clearPlus besides W18R's three rating keys. tv.capture gets a
# DILE_VT handle from LG's HAL_VT over rtk.ko's scaler capture (/dev/vivtdev, vt=): GRAPHIC
# captures work; VIDEO without video answers CAPTURE_ERROR_09 ("no signal state"). The rest
# as W18R's.
K5LP*)  model=W19P ota=AFADABAA fw=05.40.97 platform=rtd2873 sound_bad_type=SOUND_ERROR_013
	capture_no_video=CAPTURE_ERROR_09 max_devices= calibration= inputs=6 input_count=deviceid
	private_answer='Denied method call'
	no_keys='clearPlus contiLockRatingAU contiLockRatingNZ contiLockRatingSG'
	tool_options='"panelInch":"43","backlightType":"directL","cameraReady":"off","headphone":"false"'
	reset3d='"returnValue":true' ;;
# Every later board, webOS 5 on (W20L on K6LP, W21P on K7LP, W21A on LM21A, the StanbyME on LM21U,
# W22H on K8HP, W22O, W23O and W24G on O22, ...): no tvservice. The TV service is legacy-broadcast,
# tv.systemproperty is tvservicemock, and tv.externaldevice, tv.display, tv.power, tv.sound and
# tv.capture are gone from the firmware (no LS2 service file; "Service does not exist"), so only what
# it still has is called (webos5). tvservicemock knows none of the tool option keys, tvSystemName,
# platformName, displayType or contiEtcChinaCP (ERROR_06). Nothing here is listed per chip: the
# firmware's model_id.json gives the model and OTA id, its name the version, and its tvconfig the boardType (below).
*)      [ "$1" ] || { echo "FAIL chip=$(cmdline chip): no firmware name given"; exit 1; }
	# the identity is what the firmware itself says: its etc/starfish/model_id.json gives the OTA id
	# (HE_DTV_W22O_AFABATAA; not the firmware collection's name, which may be another build's:
	# W22O's AFABATPU), the firmware's name only the version
	json() { sed -n "s/.*\"$2\": *\"\([A-Za-z0-9_]*\)\".*/\1/p" "$1" 2>/dev/null; }
	ota_id=$(json /etc/starfish/model_id.json otaid)
	model=$(printf %s "$ota_id" | sed -n 's/^HE_DTV_\([A-Z0-9]*\)_.*/\1/p')
	ota=${ota_id##*_} fw=$(printf %s "$1" | cut -d. -f1-3) webos5=1
	[ "$model" ] || { echo "FAIL /etc/starfish/model_id.json: no otaid HE_DTV_<model>_<OTA id> (got '$ota_id')"; exit 1; }
	# boardType is <SoC chip>_<broadcast system>_<area>, all three from the firmware's own generators
	# (docs/user/board-profiles.md) as configd gets them: hw-option-gen's SoCChipType (O22 for
	# chip=O22A0, but O22N2 on O22N2's boards, K8LPN2 on chip=K8LPNN), nyx-option-gen's
	# tvBroadcastSystem (else the board's tvregion=) and hw-option-gen's countryType, as
	# K7LP_DVB_EU, O22N_ATSC_US, K8HP_ARIB_JP
	tvc=/tmp/var/run/tvconfig
	soc=$(json $tvc/hwoption/tv.hw.json SoCChipType)
	system=$(json $tvc/nyx/tv.nyx.json tvBroadcastSystem); system=${system:-$(cmdline tvregion)}
	area=$(json $tvc/hwoption/tv.hw.json countryType)
	[ "$soc" ] && [ "$system" ] && [ "$area" ] && board_type=${soc}_${system}_$area
	# no TV service process at all where the firmware has no legacy-broadcast (N21D's webOS 24)
	ls /usr/sbin/legacy-broadcast* >/dev/null 2>&1 || no_tv=1 ;;
esac
# webOS 6's legacy-broadcast is the -atsc, -dvb or -arib its legacy-broadcast.sh runs
LB="legacy-broadcast legacy-broadcast-atsc legacy-broadcast-dvb legacy-broadcast-arib"
# no_tv: the firmware has no TV service at all (N21D's webOS 24: no tvservice pak, no legacy-broadcast)
[ "$no_tv" ] || pidof RELEASE $LB >/dev/null || echo "tvservice (RELEASE, webOS 5's legacy-broadcast) is not running"
# check NAME URL PAYLOAD EXPECTED-SUBSTRING [luna-send|luna-send-pub [LIMIT]]; spaces are ignored
# (json-c puts them after colons); LIMIT seconds (default 10) before the call is given up
check() {
	out=$($T $((${6:-10} * scale)) ${5:-luna-send} -n 1 "$2" "$3" </dev/null 2>&1)
	case $(printf %s "$out" | tr -d ' ') in
	*"$(printf %s "$4" | tr -d ' ')"*) echo "PASS $1" ;;
	*) echo "FAIL $1: $out"; fails=$((fails + 1)) ;;
	esac
}
# check_each NAME URL PAYLOAD EXPECTED...: one call (luna-send, 10 s); each EXPECTED substring must be
# in the reply, in any order (webOS 7 on, tvservicemock orders the keys as it likes)
check_each() {
	name=$1 url=$2 payload=$3; shift 3
	out=$($T $((10 * scale)) luna-send -n 1 "$url" "$payload" </dev/null 2>&1)
	for e; do
		case $(printf %s "$out" | tr -d ' ') in
		*"$(printf %s "$e" | tr -d ' ')"*) ;;
		*) echo "FAIL $name: $out"; fails=$((fails + 1)); return ;;
		esac
	done
	echo "PASS $name"
}
S=luna://com.webos.service.tv.systemproperty
# the keys of the settings app, less those this tvservice does not have (no_keys)
keys='{"keys":["tvSystemName","panelTool","logolight","_3d","isf","epa","dualView","digitalEye","clearPlus","OLED","D9","UHD","contiLangCountrySel","contiOpt1AtvCaption","contiOpt1DtvCaption","contiOpt2HardOfHearing","contiOpt2AudioDescript","contiOpt1Teletext","contiOpt1ATVSubTitle","contiOpt1DTVSubTitle","contiCustPatentInfo","contiOpt2VideoDescript","contiHotSoccerMode","contiOpt2DigitalOption","contiVideo14_9","contiVideoFullwide","contiVideoZoom2","contiLockChannelBlock","contiLockInputBlock","contiLockAppLock","contiLockParentalGuidance","contiLockRatingBR","contiEtc2HourOffUser","contiCustRemoteSVC","contiOpt2PowerOnByVoice","contiLockRatingUS","contiLockRatingCA","contiLockRatingJP","contiLockRatingAU","contiLockRatingNZ","contiLockRatingSG","contiOpt2AddAudioDescript","contiOpt1ADCC","cameraReady","headphone","supportHdmiArc","dtv","ci","netflixEsn","panelInch"]}'
for k in $no_keys; do keys=$(printf %s "$keys" | sed "s/\"$k\",//"); done
check "settings' getProperties keys all known" $S/getProperties "$keys" '"returnValue":true'
[ "$webos5" ] || check "tool options decoded" $S/getProperties '{"keys":["panelInch","backlightType","cameraReady","headphone"]}' \
	"$tool_options"
# the model and serial are the board's (modelName=, serialNum= on the command line; the serial is the VM's own)
board='"modelName":"'"$(cmdline modelName)"'","serialNumber":"'"$(cmdline serialNum)"'",'
if [ "$webos5" ]; then
	check_each "board strings" $S/getSystemInfo '{"keys":["modelName","serialNumber"]}' \
		'"modelName":"'"$(cmdline modelName)"'"' '"serialNumber":"'"$(cmdline serialNum)"'"'
	if [ "$board_type" ]; then
		check "board type $board_type" $S/getSystemInfo '{"keys":["boardType"]}' '"boardType":"'$board_type'"'
	else
		echo "FAIL board type: no broadcast system or area from $tvc (system '$system', area '$area')"
		fails=$((fails + 1))
	fi
else
	check "board strings" $S/getSystemInfo '{"keys":["modelName","serialNumber","tvSystemName","platformName"]}' \
		"$board"'"tvSystemName":"'${tv_system:-dvb}'","platformName":"'$platform'"'
fi
check "sdx identity keys" $S/getProperties \
	'{"keys":["hardwareVersion","contiLangCountry","otaId","firmwareVersion","modelName","displayType","wifi","motionRemoconType","cameraReady","contiEtcChinaCP"]}' \
	'"returnValue":true'
if [ "$webos5" ]; then
	check_each "sdx identity values" $S/getSystemInfo '{"keys":["otaId","firmwareVersion","hardwareVersion"]}' \
		'"otaId":"HE_DTV_'$model'_'$ota'"' '"firmwareVersion":"'$fw'"' '"hardwareVersion":"1"'
else
	check "sdx identity values" $S/getSystemInfo '{"keys":["otaId","firmwareVersion","hardwareVersion","displayType","contiEtcChinaCP"]}' \
		'"otaId":"HE_DTV_'$model'_'$ota'","firmwareVersion":"'$fw'","hardwareVersion":"1","displayType":"lcd_display","contiEtcChinaCP":"off"'
fi
# webOS 5's sdx answers only once its secure store gives up (no /proc/partinfo: about 53 s into the boot on KVM): its limit is the step's, not 10 s
sdx_limit=10; [ "$webos5" ] && sdx_limit=120
check "sdx platform header" luna://com.webos.service.sdx/getHttpHeaderForServiceRequest '{}' \
	'"X-Device-Platform":"'$model'"' luna-send $sdx_limit
check "sdx model header" luna://com.webos.service.sdx/getHttpHeaderForServiceRequest '{}' \
	'"X-Device-Model":"HE_DTV_'$model'_'$ota'"' luna-send $sdx_limit
check "unknown key still fails" $S/getProperties '{"keys":["dvb","nosuchkey"]}' '"errorCode":"ERROR_06"'
if [ -z "$webos5" ]; then
E=luna://com.webos.service.tv.externaldevice
[ "$max_devices" ] && check "max external devices" $E/getMaximumExternalDeviceNumber '{}' "$max_devices"
if [ "$input_count" = deviceid ]; then
	out=$($T $((10 * scale)) luna-send -n 1 $E/getExternalInputStatusList '{}' </dev/null 2>&1)
	n=$(printf %s "$out" | tr -d ' ' | grep -o '"deviceID":' | wc -l)
	[ "$n" = "$inputs" ] && echo "PASS external input list ($n inputs)" ||
		{ echo "FAIL external input list: $n inputs: $out"; fails=$((fails + 1)); }
else
	check "external input list" $E/getExternalInputStatusList '{}' '"totalDeviceNumber":'$inputs',"mainInputSourceId":"'
fi
# ATV on a new NVM, as tvservice's defaults have it; later whatever was watched last
check "current input" $E/getCurrentInput '{}' '"returnValue":true,"deviceID":"'

check "externaldevice list is private" $E/getExternalInputStatusList '{}' "${private_answer:-Unknown method}" luna-send-pub
fi
check "eim ready" luna://com.webos.service.eim/getAllInputStatus '{}' '"id":"HDMI_1"'
if [ -z "$webos5" ]; then
D=luna://com.webos.service.tv.display
check "get3DStatus" $D/get3DStatus '{}' '"status3D":{"status":false,"pattern":"2d"}'
check "get3DStatus public" $D/get3DStatus '{}' '"pattern":"2d"' luna-send-pub
check "reset3DStatus" $D/reset3DStatus '{}' "$reset3d"
check "getScreenStatus" $D/getScreenStatus '{}' '"screenStatus":[{"sinkType":"MAIN"'
[ "${calibration-yes}" ] && check "calibration" $D/getCalibrationImageDrawingState '{}' '"isDrawing":false'
check "setDimmingStatus" $D/setDimmingStatus '{"status":"dynamic"}' '"returnValue":true'
check "setDimmingStatus bad" $D/setDimmingStatus '{"status":"x"}' '"errorCode":"ERROR_06"'
fi
# LG's tvpowerd serves nop and runs its policies. Its timers depend on the settings and on what is
# on screen; with the EU defaults tvservice sets (automaticStandBy "4") there is the automatic
# standby after 4 hours without a key
check "nop getTimerInfo" luna://com.webos.service.nop/getTimerInfo '{"subscribe":true}' \
	'"returnValue":true,"timers":[{"type":"[TIMER]'
if [ -z "$webos5" ]; then
P=luna://com.webos.service.tv.power
check "tv.power turnOnAv" $P/turnOnAv '{"standbyMode":"active"}' '"returnValue":true'

check "tv.power turnOnScreen bad mode" $P/turnOnScreen '{"standbyMode":"x"}' '"errorCode":"POWER_ERROR_0004"'
check "tv.power shutDown needs a reason" $P/shutDown '{}' '"errorCode":"POWER_ERROR_0001"'
fi
check "tvpower Active" luna://com.webos.service.tvpower/power/getPowerState '{}' '"state":"Active"'
if [ -z "$webos5" ]; then
check "setInputVolume" luna://com.webos.service.tv.sound/private/setInputVolume \
	'{"inputType":"media","inputVolume":100}' '"inputType":"media"'
check "setInputVolume bad type" luna://com.webos.service.tv.sound/private/setInputVolume \
	'{"inputType":"x","inputVolume":1}' $sound_bad_type
# master volume: set to the current level again (the audio HAL applies it to pcm_output)
SND=luna://com.webos.service.tv.sound/private
v=$($T $((10 * scale)) luna-send -n 1 $SND/getVolume '{"tvSoundOutput":"tv_speaker"}' </dev/null | sed -n 's/.*"volume": *\([0-9]*\).*/\1/p')
check "tv.sound setVolume" $SND/setVolume "{\"volumeStatus\":{\"tvSoundOutput\":\"tv_speaker\",\"volume\":${v:-10}}}" \
	'"returnValue":true,"tvSoundOutput":"tv_speaker"'
# tv.capture: tvservice's own capture over the emulator's DILE_VT/DILE_GAL and libgm (WT1H's codes and file formats)
C=luna://com.webos.service.tv.capture/executeOneShot
if [ "$capture" = no ]; then
	echo "SKIP tv.capture: not on this board (its table above)"
else
	check "capture GRAPHIC BMP 320" $C '{"path":"/tmp/app-calls.bmp","method":"GRAPHIC","format":"BMP","width":320}' '"returnValue":true'
	[ "$(wc -c < /tmp/app-calls.bmp 2>/dev/null)" = 172854 ] && echo "PASS capture BMP 320x180, 24-bit" ||
		{ echo "FAIL capture BMP size $(wc -c < /tmp/app-calls.bmp 2>/dev/null)"; fails=$((fails + 1)); }
	rm -f /tmp/app-calls.bmp
	check "capture size out of range" $C '{"path":"/tmp/app-calls.png","method":"GRAPHIC","format":"PNG","width":4000,"height":3000}' '"errorCode":"CAPTURE_ERROR_03"'
	check "capture unknown format" $C '{"path":"/tmp/app-calls.gif","method":"GRAPHIC","format":"GIF"}' '"errorCode":"ERROR_06"'
	check "capture without path" $C '{"method":"GRAPHIC","format":"PNG"}' '"errorCode":"ERROR_06"'
	check "capture unwritable path" $C '{"path":"/proc/app-calls.png","method":"GRAPHIC","format":"PNG"}' '"errorCode":"CAPTURE_ERROR_06"'
fi
fi
# No video on screen (Live TV's analog channel without a signal): tvservice's own no-signal
# answer (W15M: capture_no_video above). Before any media window this is where WT1H divides by
# zero; tvservice must still be the
# same process afterwards. capture_no_video may name more than one answer (any of them passes).
p=$(pidof RELEASE $LB)
if [ "$capture" != no ] && [ -z "$webos5" ]; then
	out=$($T $((10 * scale)) luna-send -n 1 $C '{"path":"/tmp/app-calls.jpg","method":"VIDEO","format":"JPEG"}' </dev/null 2>&1)
	ok=
	for e in $capture_no_video; do
		case $(printf %s "$out" | tr -d ' ') in *"\"errorCode\":\"$e\""*) ok=1 ;; esac
	done
	[ "$ok" ] && echo "PASS capture VIDEO without video" ||
		{ echo "FAIL capture VIDEO without video: $out"; fails=$((fails + 1)); }
fi
if [ "$no_tv" ]; then
	[ -z "$(pidof RELEASE $LB)" ] && echo "PASS no TV service process (the firmware has none)" ||
		{ echo "FAIL a TV service process in a firmware without one: $(pidof RELEASE $LB)"; fails=$((fails + 1)); }
else
	[ "$(pidof RELEASE $LB)" = "$p" ] && echo "PASS tvservice still running (pid $p)" ||
		{ echo "FAIL tvservice pid $p -> $(pidof RELEASE $LB)"; fails=$((fails + 1)); }
fi
rm -f /tmp/app-calls.png /tmp/app-calls.gif /tmp/app-calls.jpg
if [ "$fails" = 0 ]; then echo "APP-CALLS PASS"; else echo "APP-CALLS FAIL ($fails)"; exit 1; fi
