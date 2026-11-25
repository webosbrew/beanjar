# Boot a global firmware as another region's model

A firmware is built for one broadcast standard, and its `etc/starfish/model_id.json` says which:
its `region` is `ARIB`, `ATSC`, `DVB` or `GLOBAL`, the value LG's own nyxDeviceInfo module and
libdile_tvregion read (`targets.firmware_broadcast`). The VM then boots that standard's board (the
soc file's `broadcast_boards`, `guest/targets/soc/<chip>.toml`), and nothing needs choosing. Its
region paks, where it has them, are named for the same standard (`otncabi-arib.pak`,
`tvservice-dvb.pak`), but a `super.pak` firmware's are not (W25G's, W25O's and W25H's Japanese
queue builds), and those names only pick the paks. Firmwares before jhericurl have no `model_id.json`;
their chips have one board.

A global build (`GLOBAL`) names no standard. The same image ships on models of
several regions, and the board command line decides which one the TV is: its model, hwopt
country digit, country group and ToolOpt feed the firmware's own generators. Without a choice
the VM boots the chip's default board.

**Prefer the US model.** When the firmware's OTA ID ships on US retail models (the models DB lists
US models for it), the chip's default board is a US model of that firmware: ATSC, the US hwopt
country digit and configd's USA RMM group (the soc file's `board_cmdline`, `board-atsc.cmdline`). The European
model it booted before is `board-dvb.cmdline`. A chip whose OTA IDs have no US model keeps its one
`board.cmdline`: the DVB-only lines (WT1H to W20H, W23A) and the Japanese one (W20L).

To boot such a firmware as another region's model, set the broadcast standard in the VM's `vm.toml` while the
VM is off:

```toml
[firmware]
directory = "firmware"
broadcast = "dvb"
```

The value is one of `dvb`, `atsc` or `arib`. The launcher then boots that standard's board, the
same way it boots a firmware that names the standard: `boot.conf`'s `BOARD_CMDLINE`, the EEPROM's
factory records and the memory and display sizes all follow it. The standard is worked out once per
launch (`targets.board_broadcast`): the firmware's own standard, else `firmware.broadcast`, else
the chip's default (`targets.default_broadcast`, `atsc` where the default is the US board). A
standard the chip has no board for is refused, and so is one that differs from the firmware's.

Change the standard only with fresh TV state (`beanjar wipe <vm>`). The EEPROM's factory records and
the country picked in the first-use setup were made for the old board, and they would override
the new one. This includes a VM made before its chip's default became the US board: it boots the
US board line over its European EEPROM and settings until it is wiped, or until its `vm.toml` says
`broadcast = "dvb"`. `beanjar config unset <vm> broadcast` returns a VM to the default.

Measured with W24G spacebuns 02.68.80.01 under TCG on 2026-11-03. A fresh VM on the US board
reports model OLED55C4PUA, country USA, country group US (`langSelUS`), LG services country US,
locale en-US, configd RMM group 2 US and broadcast system ATSC, and shows the English Home. A VM
first booted with `broadcast = "dvb"` and then started on the US board did not run first use again.
Its command line and nyx said US and ATSC, but the EEPROM's factory model stayed OLED55C44LA, the
country GBR, the RMM group EU and `boardType` `O22N2_ATSC_EU`. After `beanjar wipe` it came up as the
fresh US VM. On this firmware the first boot of a fresh state restarts the set once (on either
board): its google-home-controller switches to LG's QA2 server and eula-service reboots, as on a
set of this build. The VM starts again by itself, and the second boot keeps the switch and boots
normally. Spacebuns' `getSystemInfo` `boardType` is empty on the US
board, where the European board gives `O22N2_DVB_EU`.

## Choose the TV's country

A board is a model of one country group: its `countryGrp=` is the `grpCode` of one RMM group of
the firmware's configd tables (`etc/configd/data/rmm/<group>/<country>/tv.rmm.json`), US 2, EU 4,
JP 18. Without a choice the TV takes the group's default country (US: USA; EU: GBR). The `country`
setting picks another country of the same group, while the VM is off:

```sh
beanjar config set tv1 country=CAN
beanjar config unset tv1 country
```

The value is the country's ISO 3166 alpha-3 code. The choices come from the firmware: the
countries of the board's group in `etc/configd/data/rmmCountryGroup.json` (W22O 13.30's US board:
`CAN`, `MEX`, `USA`; its European board 37 countries). A country of another group is refused, with
the board's countries and the group the country is in; one of the other broadcast standard (the
file's `groupDVB` or `groupATSC` block) says so, and needs that standard's board first
(`broadcast=dvb country=DEU`). `beanjar config set` checks the value against the firmware before
it saves anything, and every start checks it again.

webOS 5 (W20) and later only. Older firmwares' RMM records have no `grpCode` (W16R to W19P), or
there are no RMM tables at all (WT1H, W15M), so nothing in the firmware ties the board to a group,
and `country` is refused there: `the country setting needs webOS 5 or later`.

What the VM does with it is what a set of that country has:

- A new TV state (a new VM, or after `beanjar wipe`) gets the country in the EEPROM, the
  lowlevelstorage factory DB's `countryName`, as a factory programs it. devicereset copies it into
  the settings country at every boot, and the normal layer's first-use replay takes the smart
  service country and the time zone from it.
- A state that has another country gets it at the next start through LG's settings, as the
  settings app changes it (`emulator-country`): the settings country, which devicereset writes
  back to lowlevelstorage, the smart service country where first use has stored one, and the time
  zone. This happens once per value: a later change in the TV's own settings stays until the
  setting changes again.
- With LG's services mocked, the mock tells the TV it is in that country, as LG's server does from
  the TV's address, so the TV raises no "Changing the country will cause your TV to restart"
  prompt.

`unset` leaves a TV state's country as it is; `beanjar wipe` gives the board's default back. The
sources of the country lists and the stores each release line keeps the country in are in
[the country setting's research record](../research/country-setting.md).

Measured with W22O number1 13.30.56.01 on an x86-64 host (x86-64 guest kernel, host GPU) on
2026-11-25: a new VM with `country=CAN` booted with lowlevelstorage `countryName` CAN, settings
country CAN, smart service country CA/CAN and time zone America/Toronto, and showed Home with no
prompt; the same state set to `MEX` came up MEX in settings, lowlevelstorage, the smart service
country (MX/MEX) and the time zone (America/Mexico_City), in one boot with no prompt, and kept MEX
after another restart.

## Boards

| Chip | Firmware | `atsc` (default) | `dvb` | `arib` |
| --- | --- | --- | --- | --- |
| k7lp | W21P | 65UP8000PUR | 43UP77009LB | |
| k8lp | W22P | 75NANO75UQA | 55NANO756QA | |
| k8ap | W22L | 32LQ630BPUA | 32LQ570B6LA | |
| k8hp | W22H | 65QNED85UQA | | 55QNED85JQA, by the firmware |
| k8hpp | W23H | 86QNED80URA | 86UR80006LA | |
| k8lpn | W23P | 75UR8000AUA | 43UR8050PSB (Australia) | |
| k8lpn2 | W24P | 43UT8000AUA | 43UT80006LA | |
| kf23f | W23L | 32LR655BPUA | 32LR570B6LA (India) | |
| k24 | W24H | 55QNED85TUA | 55QNED85T6C | |
| k24n | W25H | 55QNED85AUA | 55QNED85A6C | OLED55B5PJA, by the firmware (queue) |
| k25lp | W25P | 43UA7500ZUA | 43UA75006LA | |
| lm21an | W22A | 70UQ8000AUB | 43UQ80006LB | |
| m23 | W23M | 55UR9000PUA | 55UR91006LA | |
| o22 | W22O | OLED55C2PUA | OLED55C21LA, global C2 | OLED55C2PJA, by the firmware |
| o22n | W23O | OLED77C3PUA | OLED55C36LA | |
| o22n2 | W24G | OLED55C4PUA | OLED55C44LA | |
| o22n3 | W25G | OLED55C5PUA | OLED55C54LA | OLED55C5PJA, by the firmware (queue) |
| o24 | W24O | OLED55G4SUB | OLED55G45LW | |
| o24n | W25O | OLED55G5WUA | OLED55G54LW | OLED55G5PJB, by the firmware (queue) |
| o26 | W26O | OLED55G6WUA | OLED55G64LW | |

W21A's `lm21a` board was a US model (65UP7560AUD) already and stays its only board.

W22H's global firmwares (AFADATAA, AFABATPU) ship on US models; its Japanese ones (AFADJAAA,
AFABJAPU) say ARIB and boot the Japanese QNED85. Its US board comes from that
firmware's own generators directly (65QNED85UQA on 04.60.85, 23.25.75 and
43.22.73, which agree), not from a European line: no European board is derived for it. Its
US board's tuner has the K8 chips' European values, as W22P's, until the US tuner is derived.

LM21U's boards are the StanbyME 27ART10AKPL (N21D) as a UK set (DVB, GBR group 4), one per
firmware line: `board.cmdline` for webOS 6 (`lm21u-kisscurl`, N21D 04.44.16) and
`board-ombre.cmdline` for webOS 24 (`lm21u-ombre`). N21D's OTA ID ships only on Korean and UK
StanbyMEs, no US model, so each keeps one board; each line comes from its own firmware's tables
(27ART10AKPL with `countryType=EU panelResolution=FHD ddrSize=2G
graphicResolution=1920x1080`), and the two give the same values. W21U, the chip's 2021 OLED and NANO/QNED TV
line, is not the StanbyME's firmware and has no board here.

M16P lite's board (`m16plite-dreadlocks2`, W17M 06.10.55) is a 43UJ6300-UA, a US set (ATSC, countryGrp 2, continent 10): W17M's only OTA ID in the collection is the ATSC build (AFADATAA), which ships on US, Canadian, Latin American, Philippine and Korean models, so it has one board. Its RELEASE.cfg is a Korean SJ85, so the region values are the US group's of the same tables (`host/tools/research/model-config.py` with `--set szCountryGroupCode=US`); its DDR is the straps' default 1.5 GB, as W17H's board.

M16PP's board (`m16pp-goldilocks`, W18H 05.50.70) is a 55SK8000PUA, a US set (ATSC, countryGrp 2, continent 10): the collection has W18H's ATSC build (AFADATAA), whose OTA ID the models DB lists for US and Canadian models, so it has one board. Its RELEASE.cfg is a Korean SK85, so the region values are the US group's of the same tables (`host/tools/research/model-config.py` with `--set szCountryGroupCode=US`). Its lxboot reads the 16 model-option straps from a SAR ADC and has no strap tables the tool reads, so `modelopt=` is derived by hand from the bootloader's strap meanings (DDR 1.5 GB, a UD FRC V-by-One panel); `board.cmdline` says which strap is which.

M16PP lite's board (`m16pplite-goldilocks`, W18M 05.50.60) is a 43UK6300PJF, a Japanese set (ARIB, countryGrp 18, continent 5): the only W18M build held is the ARIB one (AFADJAAA), which the models DB lists for Japanese models only, so it has one board. Its RELEASE.cfg is a Korean SK85, so the region values are the JP group's of the same tables; the tuner column (S and dual) is the JP row's first, since the retail strap is not in the firmware; `modelopt=` is M16PP's.

M16P3's board (`m16p3-goldilocks2`, W19H 05.50.00) is a 55SM9000PUA, a US set (ATSC, countryGrp 2, continent 10): W19H's builds in the EPK archive are ATSC (AFADATAA) and ARIB; the models DB lists the ATSC OTA ID for US, Canadian and Mexican models, so it has one board, the US one. Its RELEASE.cfg is a Korean SM85, so the region values are the US group's of the same tables (`host/tools/research/model-config.py` with `--set szCountryGroupCode=US`). Its lxboot prints six ToolOpt words; its strap tables were not decoded, and since the kernel reads only the panel straps, `modelopt=` is M16PP's.

O18's board (`o18-goldilocks2`, W19O 05.50.00) is an OLED65C9PUA, a US set (ATSC, countryGrp 2, continent 10): W19O's builds in the EPK archive are ATSC (AFABATAA) and ARIB; the models DB lists the ATSC OTA ID for US and Canadian OLED C9, E9 and W9 models, so it has one board, the US one. Its RELEASE.cfg is a Korean 65" C9, the model's series, with the region values of the US group (`host/tools/research/model-config.py` with `--set szCountryGroupCode=US`); its DDR is 3 GB, the only size W19O's lxboot knows. Its kernel reads no model-option straps; `modelopt=` is M16P3's.

O18K's board (`o18k-goldilocks2`, W19K 05.50.00) is a 75SM9970PUA, the US 8K NanoCell the models DB lists for W19K's ATSC OTA ID (AFADATAA, US and Canada), so it has one board. W19K's RELEASE.cfg is W19O's file, a Korean 65" C9, so only the model's inch, tool type and direct LED backlight replace its values (`host/tools/research/model-config.py` with `--set eBackLightType=BL_DIRECT_L`); the 8K path (hwopt's FRC type, the Realtek codec host) is not chosen, so it runs as a UHD set.

M2R's board (`m2r-dreadlocks2`, W17R 06.10.60) is a 43LJ5500-UA, a US set (ATSC, countryGrp 2, continent 10): W17R's only OTA ID in the collection is the ATSC build (AFAAATAA), which ships on US, Canadian, Latin American, Philippine and Korean models, so it has one board. Its RELEASE.cfg is a Korean LJ61, so the region values are the US group's of the same tables (`host/tools/research/model-config.py` with `--set szCountryGroupCode=US`), and its DDR is 1.25 GB, as M2's board.

M3R's board (`m3r-goldilocks2`, W19R 05.40.97) is a 43LM5700PUA, a US set (ATSC, countryGrp 2, continent 10): W19R's only OTA ID in the collection is the ATSC build (AFAAATAA), which ships on US, Canadian and Mexican models, so it has one board. Its RELEASE.cfg is a Korean LM63, so the region values are the US group's of the same tables (`host/tools/research/model-config.py` with `--set szCountryGroupCode=US`).

LM18A's board (`lm18a-goldilocks`, W18A 05.50.70) is a 43UK6300PUE, a US set (ATSC, countryGrp 2, continent 10): the only W18A build held is the ATSC one (AFADATAA), which the models DB lists for US models (UK6200 to UK7700), so it has one board. Its RELEASE.cfg is a Korean UK75, so the region values are the US group's of the same tables (`host/tools/research/model-config.py` with `--set szCountryGroupCode=US`). Its MBoot reads `modelopt=` from six SAR ADC levels and three GPIO pins, not M3's strap loop, so `modelopt=` and `hwopt=` are derived by hand from the bootloader's decoder (a UD V-by-One panel, the internal demodulator, no EWBS); `board.cmdline` says which level is which.

O20N's board (`o20n-kisscurl`, W21O 03.70.05) is an OLED65G1PUA, the US 65" G1 (ATSC, countryGrp 2): W21O's global build (AFABATAA) is the one the models DB lists for the US C1 and G1 models, so it boots the US board. Its line comes from the firmware's own generators (`host/tools/research/model-config.py`, VALIDATE "W21O"): a V-by-One UHD OLED panel at 120 Hz, a 1920x1080 graphic plane, DDR 3 GB (the lower of the two sizes its lxboot knows), and lowlevelstorage's default ToolOpt with the G1's tool type and 65". `chip=O20A0` is lxboot's form ("O20" and a revision); A0 is a virtual revision.

E60N's board (`e60n-kisscurl`, W21K 03.70.25) is a 65QNED99UPA, the US 65" QNED99 (ATSC, countryGrp 2): the models DB's only US models of W21K's global build (AFADATAA) are it and the 75NANO99UPA. Its line comes from the firmware's own generators (VALIDATE "W21K"): a V-by-One 8K LCD panel at 120 Hz, DDR 4 GB (the lower of the two sizes its lxboot knows) and lowlevelstorage's default ToolOpt with the QNED99's tool type and 65". Its graphic plane is 3840x2160: W21K's compositor, built with LG's partial update, draws nothing on a 1920x1080 plane. `chip=E60A0`, as O20N's.

LM15U's board (`lm15u-beehive`, W15U 04.06.25) is a 65UF850V as a German set (DVB, the
nContinentIdx 50 area): W15U's only OTA ID is the European DVB one (AFADABAA), so it has no US
or ARIB board. Its fields come from that firmware's MBoot and tvservice tables, each named in the
board file's comments.

mtka5lr's board (`mtka5lr-beehive`, W15L 03.31.00, MediaTek MT5882) is a 49LF590V as a German set (DVB, the nContinentIdx 50 area): W15L's only OTA ID is the European DVB one (AFAAABAA), so it has no US or ARIB board. Its RELEASE.cfg is the model's own series (INCH_49, TOOL_LF59); the values are `host/tools/research/model-config.py`'s for it. The EPK has no bootloader, so the DDR strap (1.5 GB) and the chip revision (A0) are defaults, named as such in the board file.

The boards are in `guest/layers/soc/<chip>/rootfs/etc/emulator/`. Region digits and ToolOpt words are
specific to each firmware, so each board comes from its own firmware's data. A US board is the
European board's line with three region values changed, each read from that firmware:

- hwopt digit 0, the area option: the digit the firmware's hw-option-gen decodes as `countryType`
  US and nyx-option-gen as ATSC (5 on every chip above). Every other tv.hw field decodes as on the
  European line, except the tuner flags the country sets.
- `countryGrp=`: the `grpCode` of configd's USA RMM group (`etc/configd/data/rmm/US/USA/tv.rmm.json`,
  2 on every chip above). libdile_boardinfo alone gives the generic North America group 1.
- `ToolOpt=`: LLS's factory default record generated for this line (it follows the hwopt country
  digit), with the US model's `panelInch` and `toolType` from the firmware's factory tables
  (`factorymanager.db.tooloption.json`, factorymanager's enums or `optionTable.json`).

`tvregion=ATSC` where the bootloader prints one. The US model is one the models DB lists for the
OTA ID whose series the firmware's tables name.
The W21P, W22L, W22P and W23L US boards were reproduced field for field from their firmware's tables.
