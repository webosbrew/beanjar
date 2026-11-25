# Your own broadcast on the tuner

A VM whose board has Beanjar's tuner (the boards whose SoC loads `dtv_soc`) gets an antenna:
physical channels with a signal, which the set scans and watches as off-air TV. By default the
antenna carries a small built-in lineup of the board's broadcast standard: three US ATSC
channels, or one Japanese ISDB-T channel with a BML data broadcast on an ARIB board. You can give
a VM your own broadcast in two ways:

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
beanjar io tuner map tv 20 21                   # put a signal on RF 20 and 21
beanjar io tuner map tv 30 --stream rec.ts      # RF 30 plays rec.ts, looped
beanjar io tuner unmap tv 30                    # off again; the copy of rec.ts is removed
beanjar io tuner lineup reset tv                # back to the built-in lineup
```

Then scan for channels on the set (Settings, Channels, Channel Tuning, Auto Tuning) and watch
them in Live TV. The streams of a mapped channel are made when you map it (or set the lineup),
so errors show at once; this needs `tsp` and `tstabcomp` (TSDuck) and `ffmpeg` on the machine
that makes them.

A VM on another host (`HOST/NAME`) works the same way: the streams are made on this machine, the
lineup's files and your stream stay here, and the tuned channel is fed to the host over the ssh
connection while the VM runs. The host keeps the lineup file and the map, so `lineup show` and
`list` work there too; run `map` again after the VM has started to start the feed.

## The lineup file

One file describes one broadcast standard's antenna. `broadcast` must be the VM's board's
standard (`atsc` or `arib`, as `beanjar io tuner list` shows it); a file of the other standard is
refused. The fields use each standard's own terms. Paths in the file are relative to the file
and stay inside its folder; `lineup set` copies the file and everything it names into the VM, so
the VM no longer depends on your copy. When you edit the file, set it again.

The streams beanjar makes from a lineup are cached on the machine that makes them. `lineup set`
and `lineup reset` remove the cached streams of lineups no VM of the library (nor a VM on another
host that this machine feeds) has any more, an earlier edit's included; the built-in lineups'
streams stay. A removed stream is made again when a lineup of that content comes back.

The built-in lineups are complete examples: `beanjar io tuner lineup show NAME` prints the one in
effect, and they are `host/core/beanjar/lineups/atsc.toml` and `arib.toml` in the source.

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
channel = 20                   # the physical channel (2 to 36)
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

- `rf`: `channel`, `tsid`, and its `services` (1 to 16).
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
  ISO 639-2 code); the first one's defaults to `eng` on ATSC and `jpn` on ARIB, the others must
  name theirs. The built-in 7.1 has an English and a Spanish track.
- `label`: whether the service's name (ATSC: `20.1 KBJR`) is drawn in the middle of the picture.
  Default `true` on a test pattern and `false` on an image or video of yours.
- `events`: the programme cycle, one event or more, each `title`, `minutes` and an optional
  `text`; it repeats from midnight (ATSC: in `timezone`; ARIB: Japan time). ATSC sends the guide
  of the next 12 hours, ARIB the present and following event.
- ffmpeg encodes everything: MPEG-2 video, AC-3 audio (ATSC) or AAC (ARIB).

## A transport stream of yours

`beanjar io tuner map NAME RF --stream FILE` takes exactly one RF. The file is copied into the
VM, so it may be large; `unmap` removes the copy, and mapping the RF again without `--stream`
goes back to the lineup's stream. The RF may be any channel of the band, also one the lineup does
not have.

The stream is played as it is, at the rate its PCR gives, without its null packets. Beanjar never
rewrites its tables, except the clock: the ATSC STT (PID 0x1FFB), or the ARIB TDT and TOT (PID
0x14), are set to now as they are sent. At the loop point each PID starts again at a PES or
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

A stream for a US board should carry ATSC PSIP (MGT, TVCT, STT, EIT), one for a Japanese board
the ISDB-T SI (NIT with the terrestrial delivery descriptor, SDT, EIT, TOT); TSDuck can make or
fix either. A recording of a real broadcast is yours to use: only free-to-air ones play.
