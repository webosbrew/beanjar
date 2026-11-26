# Your own broadcast on the tuner

A VM whose board has Beanjar's tuner (the boards whose SoC loads `dtv_soc`) gets an antenna:
physical channels with a signal, which the set scans and watches as off-air or cable TV. By
default the antenna carries a small built-in lineup of the board's broadcast standard: three US
ATSC channels on the air and one on cable, one Japanese ISDB-T channel with a BML data broadcast
on an ARIB board, or a European DVB-T multiplex and a DVB-C one on a DVB board. You can give a VM
your own broadcast in two ways:

- a **lineup file**: a TOML file that describes the physical channels, their services, the
  programme guide, each service's picture and sound and, on ARIB, its data broadcast pages.
  Beanjar makes the transport streams from it with TSDuck and ffmpeg.
- a **transport stream** of yours (`.ts`) on one physical channel, played on a loop.

Both are kept in the VM's state for its next starts. The commands:

```sh
beanjar io tuner list tv                        # the channels, which are mapped, what is tuned
beanjar io tuner lineup show tv                 # the lineup in effect, as TOML (--json: JSON)
beanjar io tuner lineup show tv > mine.toml     # a start for your own
beanjar io tuner lineup set tv mine.toml        # check it, copy it and its files into the VM
beanjar io tuner map tv 20 21 C78               # put a signal on RF 20 and 21, and cable C78
beanjar io tuner map tv 30 --stream rec.ts      # RF 30 plays rec.ts, looped
beanjar io tuner unmap tv 30                    # off again; the copy of rec.ts is removed
beanjar io tuner lineup reset tv                # back to the built-in lineup
```

An RF is named by its channel number, or by C and the number for a cable channel: `7` is air
channel 7, `C7` cable channel 7. Both may be in one lineup and mapped together; on a US set they
even share their frequency (177 MHz), and the set tells them apart by how it tunes (8VSB on the
air, QAM on cable). The channel plans are the sets' own:

| Board | Air | Cable |
| --- | --- | --- |
| ATSC (US) | 2 to 36 | C1 to C135, the US cable plan's standard (STD) frequencies |
| ARIB (Japan) | UHF 13 to 62 | none |
| DVB (Europe) | VHF 6 to 12 and UHF 21 to 69, 8 MHz apart (21 is 474 MHz) | C1 to C106, 50 MHz and on in 8 MHz steps (C38 is 346 MHz) |

Then scan for channels on the set (Settings, Channels, Channel Tuning, Auto Tuning) and watch
them in Live TV. A scan finds cable channels in its cable pass, so let that pass run. The streams of a mapped channel are made when you map it (or set the lineup),
so errors show at once; this needs `tsp` and `tstabcomp` (TSDuck) and `ffmpeg` on the machine
that makes them.

A VM on another host (`HOST/NAME`) works the same way: the streams are made on this machine, the
lineup's files and your stream stay here, and the tuned channel is fed to the host over the ssh
connection while the VM runs. The host keeps the lineup file and the map, so `lineup show` and
`list` work there too; run `map` again after the VM has started to start the feed.

## The lineup file

One file describes one broadcast standard's antenna. `broadcast` must be the VM's board's
standard (`atsc`, `arib` or `dvb`, as `beanjar io tuner list` shows it); a file of another
standard is refused. The fields use each standard's own terms. Paths in the file are relative to the file
and stay inside its folder; `lineup set` copies the file and everything it names into the VM, so
the VM no longer depends on your copy. When you edit the file, set it again.

The streams beanjar makes from a lineup are cached on the machine that makes them. `lineup set`
and `lineup reset` remove the cached streams of lineups no VM of the library (nor a VM on another
host that this machine feeds) has any more, an earlier edit's included; the built-in lineups'
streams stay. A removed stream is made again when a lineup of that content comes back.

The built-in lineups are complete examples: `beanjar io tuner lineup show NAME` prints the one in
effect, and they are `host/core/beanjar/lineups/atsc.toml`, `arib.toml` and `dvb.toml` in the
source.

### ATSC (US)

```toml
broadcast = "atsc"
timezone = "America/Chicago"   # the programme cycle repeats from midnight here
loop_seconds = 10              # how long the picture and sound run before they repeat

[video]                        # optional: the built-in lineup's values
size = "704x480"
fps = "30000/1001"
bitrate = 1500000

[[rf]]
channel = 20                   # the physical channel (2 to 36; on cable 1 to 135)
tsid = 2020

[[rf.services]]
major = 20
minor = 1
name = "KBJR"                  # the short name, at most 7 characters
long_name = "Beanjar Rivers"
source_id = 2001               # unique in the lineup: the guide's key
picture = { video = "media/intro.mp4" }
sound = { file = "media/intro.mp4" }
label = true                   # draw "20.1 KBJR" over your picture too
events = [
  { title = "Morning News", minutes = 60, text = "The day's news." },
  { title = "Cartoons", minutes = 30 },
]
```

- `rf`: `channel`, `tsid`, and its `services` (1 to 16); `medium`, `air` (the default) or
  `cable`; a cable one's `modulation`, `qam256` (the default) or `qam64`. A cable channel's
  virtual channels go out in a cable virtual channel table (CVCT) with that modulation, an air
  one's in the terrestrial one (TVCT). A virtual channel is given once on the air and once on
  cable at most; `source_id`s are unique in the whole lineup.

  ```toml
  [[rf]]
  channel = 78                 # RF C78, 549 MHz
  medium = "cable"
  tsid = 1078
  ```
- A service: `major`, `minor`, `name`, `source_id` are needed; `long_name` (default the name),
  `program` (the MPEG program number, default 1, 2, ... in order), `picture`, `sound`, `label`,
  `captions` and `events`.
- `captions`: `{ file = FILE, language = "eng" }`, closed captions from a Scenarist (`.scc`)
  caption file, the format caption editors and converters write (pycaption, Subtitle Edit, ...).
  They go out as CEA-608 CC1 in the pictures' user data (ATSC A/53), with the caption service
  descriptor in the PMT and the guide. They repeat with the picture, so they must fit in
  `loop_seconds`. `language` defaults to `eng`. The built-in 7.1 has a pair of test captions
  (`lineups/captions-eng.scc` in the source). A US set finds, decodes and draws them once its
  captions are on (the system setting `caption`/`captionEnable`); drawing is checked on W23P
  (K8LPN) so far.

### ARIB (Japan, ISDB-T)

```toml
broadcast = "arib"
loop_seconds = 5

[[rf]]
channel = 30                   # UHF 13 to 62
network_id = 0x7FD1
network_name = "ビーンジャー"
tsid = 0x7FD1
area_code = 0x5A5
remote_key = 3                 # the remote's button: the set numbers the service 031
ts_name = "ビーンジャー３"

[[rf.services]]
service_id = 0x0408
name = "ビーンジャー３"
provider = "ビーンジャー"
picture = { image = "art/card.png" }
sound = { tone = 1000 }
bml = "pages"                  # a folder with startup.bml: the data broadcast (the d button)
events = [
  { title = "データ放送", minutes = 60, text = "自作のBMLページ。" },
]
```

- `rf`: `channel`, `network_id`, `tsid`, `area_code`, `remote_key` are needed; `network_name`
  and `ts_name` default to the first service's name; its `services` (1 to 8).
- A service: `service_id` and `name` are needed; `provider` (default the network name),
  `picture`, `sound`, `label`, `bml` and `events`. The set's three-digit number for a service is
  the remote key times ten plus the service's number in its TS (`service_id`'s low three bits)
  plus one.
- `bml`: a folder whose files become the service's data carousel module, `startup.bml` first;
  the set shows it when you press d. Files: `.bml` (sent as EUC-JP BML documents, as broadcasts
  write them), `.png`, `.jpg`, `.mng`, `.aiff`; no subfolders, up to about 1 MB. A document links
  to another as `/40/0000/name.bml` (component tag 0x40, module 0). Only BML: Hybridcast and AIT
  are not made.

### DVB (Europe, DVB-T and DVB-C)

```toml
broadcast = "dvb"
timezone = "Europe/Berlin"     # the programme cycle repeats from midnight here
country = "DEU"                # the TOT's local time offset is this country's (ISO 3166 alpha-3)
loop_seconds = 10

[video]
size = "720x576"
fps = "25"

[[rf]]
channel = 21                   # UHF 21, 474 MHz
system = "t"                   # t: DVB-T on the air, c: DVB-C on cable
onid = 0xFF01                  # original_network_id
tsid = 0x0101
network_name = "Mein Netz"

[[rf.services]]
service_id = 0x2001
name = "Mein Kanal"
lcn = 1                        # the logical channel number: the set's programme number
provider = "Ich"
picture = "smptebars"
sound = { tone = 1000, language = "deu" }
events = [
  { title = "Testbild", minutes = 60, text = "Farbbalken." },
]

[[rf]]
channel = 38                   # cable C38, 346 MHz
system = "c"
onid = 0xFF02
tsid = 0x0238
modulation = "qam256"          # qam64 or qam256 (the default)
symbol_rate = 6900000          # the default
```

- `rf`: `channel`, `onid`, `tsid` and its `services` (1 to 8) are needed; `system` (`t`, the
  default, or `c`), `network_id` (default the `onid`), `network_name` (default the first
  service's name), and on cable `modulation` and `symbol_rate`. DVB-T2 is not made yet.
- A service: `service_id`, `name` and `lcn` are needed; `provider` (default the network name),
  `picture`, `sound`, `label` and `events`. A logical channel number is given once among the
  DVB-T services and once among the DVB-C ones.
- The tables are the ones a European set scans and fills its guide from: NIT with the delivery
  descriptor (terrestrial or cable) and the logical channel numbers, SDT, EIT present/following
  and the schedule of the next day, TDT and TOT (a W21P DVB board fills its guide only from the
  schedule).

### Pictures, sounds and events

- `picture`: a test pattern's name (`testsrc2`, `testsrc`, `smptebars`, `smptehdbars`,
  `pal75bars`, `pal100bars`, `rgbtestsrc`, `yuvtestsrc`, `colorspectrum`, `gradients`,
  `zoneplate`), `{ image = FILE }` or `{ video = FILE }`. An image or video is scaled into the
  lineup's video size, letterboxed; a video is looped or cut to `loop_seconds`. Default
  `testsrc2`.
- `sound`: `{ tone = HZ }` or `{ file = FILE }`, an audio or video file whose first audio is used,
  looped or cut likewise. Default a 1000 Hz tone. A list of up to three gives the service more
  sound tracks, each in its own language, which the set lists in its audio language menu:
  `sound = [{ tone = 440 }, { tone = 554, language = "spa" }]`. Each track has a `language` (an
  ISO 639-2 code); the first one's defaults to `eng` on ATSC and DVB and `jpn` on ARIB, the others
  must name theirs. The built-in 7.1 has an English and a Spanish track.
- `label`: whether the service's name (ATSC: `20.1 KBJR`; DVB: `1 Mein Kanal`) is drawn in the
  middle of the picture.
  Default `true` on a test pattern and `false` on an image or video of yours.
- `events`: the programme cycle, one event or more, each `title`, `minutes` and an optional
  `text`; it repeats from midnight (ATSC and DVB: in `timezone`; ARIB: Japan time). ATSC sends
  the guide of the next 12 hours, DVB of the next day, ARIB the present and following event.
- ffmpeg encodes everything: MPEG-2 video, AC-3 audio (ATSC), AAC (ARIB) or MPEG-1 layer II
  (DVB).

## A transport stream of yours

`beanjar io tuner map NAME RF --stream FILE` takes exactly one RF. The file is copied into the
VM, so it may be large; `unmap` removes the copy, and mapping the RF again without `--stream`
goes back to the lineup's stream. The RF may be any channel of the band, also one the lineup does
not have.

The stream is played as it is, at the rate its PCR gives, without its null packets. Beanjar never
rewrites its tables, except the clock: the ATSC STT (PID 0x1FFB), or the ARIB and DVB TDT and
TOT (PID 0x14), are set to now as they are sent. On a DVB board a cable RF (`C38`) tunes DVB-C and
any other DVB-T. At the loop point each PID starts again at a PES or
section start, the continuity counters run on and the first PCR after the wrap carries the
discontinuity indicator, so the set keeps the picture.

These stop `map`:

- the file is not a whole number of 188-byte packets, or a packet has no sync byte (a 192-byte
  M2TS or BDAV recording needs remuxing to TS first: `ffmpeg -i in.m2ts -c copy -map 0 out.ts`);
- it is scrambled in its first seconds: a recording protected by B-CAS or another conditional
  access system. Beanjar never descrambles; give it a free-to-air stream;
- it has no PAT, or no PCR.

These are warnings; the stream is mapped anyway:

- its NIT or PSIP is of another standard than the board's: the set may find no channel in it;
- its delivery descriptor names another frequency than the RF's;
- it has no clock tables (STT; TDT and TOT).

A stream for a US board should carry ATSC PSIP (MGT, TVCT or on cable CVCT, STT, EIT), one for a
Japanese board the ISDB-T SI (NIT with the terrestrial delivery descriptor, SDT, EIT, TOT), one
for a European board the DVB SI (NIT with the terrestrial or cable delivery descriptor, SDT, EIT
with the schedule, TDT and TOT); TSDuck can make or fix each. A recording of a real broadcast is yours to use: only free-to-air ones play.
