# NetCast-line boot check

`scripts/regress` checks webOS (bootd, Luna, tvservice); the NetCast-line targets have none of
that, so they get their own check: their stock GUI must come up. `run.py` boots one VM of a NetCast 4.5
(`lm14-netcast45`) or SimpleSmart (`m2-simplesmart`) firmware through the beanjar CLI, headless,
and passes only when the stock dashboard is drawn and follows the remote:

```sh
tests/regress/netcast/run.py --firmware /srv/epk/LM14_ATSC_US_URSA9_..._V04.11.18_usb_V2_SECURED.epk
tests/regress/netcast/run.py --firmware <SimpleSmart .epk> --timeout 3600
```

`--firmware` takes what `beanjar create --firmware` does (a `.epk`, extracted paks or a collection
name). It needs the target's guest build published (`build/components/release/<release>`; NetCast
4.5's is set up with `meson/cross/lgtv-sdk.ini`, `scripts/build.sh guest` does this for the
configured firmware's target) and Pillow.

Each step is PASS or FAIL with its time from the start (`build/regress/netcast/<name>/result.json`,
the screenshots beside it, and the guest's `webos.log`):

| Step | Passes when |
|---|---|
| create, start | `beanjar create --link off --audio none --tag scratch`, `beanjar start`, `beanjar vnc start` (the remote's front end) succeed, and the VM's platform is NetCast or SimpleSmart |
| gal | `webos.log` has our GAL's `[libgal] Init` (NetCast 4.5) or `[halgal] Init` (SimpleSmart): the firmware initialised its graphics (`PM_GX_Initialize` in procmgr) |
| ready | `run/micom-status` ends in `ready=1`: the firmware's power stack read the micom |
| home | a screenshot is judged the dashboard, as the host's accelerator is expected to show it (below). When the screen is black, HOME (`0x7c`) is pressed again; every `--settle` seconds (30) a screenshot |
| locale | the country group the browser reports (lb6wk's `locale string` in `webos.log`) is the firmware's own, `RELEASE.cfg`'s `szCountryGroupCode`. A NetCast 4.5 EEPROM with another group's byte drew an EU build's UI in Swedish, with a stray glyph in its labels, on group KR; the pixels cannot tell, the group can |
| nav | after RIGHT, the screenshot is still the dashboard and differs from the last (NetCast 4.5 under TCG: SKIP) |

The judge (`judge()`) reduces a screenshot to 320x180 (nearest neighbour) and counts its non-black
pixels (luma 16 and above) and its distinct colours. What each host is expected to show (`EXPECT`):

- **KVM** (the Pi): NetCast 4.5's populated FX HOME, the stock dashboard the bring-up accepted:
  about 0.57 non-black and 6400 colours; RIGHT moves its focus (about 9800 pixels change). The
  check asks for 0.25 and 2000.
- **TCG** (an x86 PC): FX HOME's grey tile skeleton, 0.56 non-black and about 100 colours, with no
  text or pictures: fxui's home logs D-Bus reply timeouts and services not yet there (sdpif) and
  never gets as far as its preferences and SmartShare list, which it does under KVM. This was measured alike at the commit before the presenter regression (4cfa3060) and with
  its fix, for 30 minutes after HOME, on a first and a second boot. So under TCG the check asks only
  for 0.25 non-black (the dashboard is drawn) and skips nav. A black screen (0) and the MyApps tray
  (0.12) still fail there. Since the LM14 NVM EEPROM is modelled (the software I2C bus, whose
  slow failures had held RELEASE back), TCG too draws the populated dashboard: ready in about 70 s,
  the dashboard at 0.57 non-black and 4900 to 6400 colours (all five LM14 builds, 2026-11-21);
  the thresholds above are unchanged so far.

- **SimpleSmart**, under both: its HOME ribbon over black, 0.28 non-black and about 600 colours;
  RIGHT moves its focus. The check asks for 0.15 and 300.

`tests/unit/regress/test_netcast_check.py` checks the judge.

Run it on both. The two hosts' GPUs differ: the PC's virtio-gpu-gl connector lists 35 modes without
EDID, the Pi's vhost-user-gpu 27. The presenter that kept at most 32 (fixed in `gop-present.c`)
left the PC black and the Pi working, so only the TCG run caught that regression: there the check
fails at home with a black screen (non-black 0).

Everything up to nav has `--timeout` seconds (2400). Under KVM a first boot reaches the GAL in about
90 s, ready in 120 s and the dashboard in 140 s; under TCG (NetCast 4.5, with the EEPROM modelled)
the GAL in under a minute, ready in about 70 s and the dashboard in about 3.5 minutes. The VM is stopped and deleted at the end (`--keep`
keeps it, stopped). Like every VM test, it runs alone on its host.
