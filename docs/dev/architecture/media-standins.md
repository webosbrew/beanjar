# Media stand-ins

The emulator replaces vendor GStreamer decoder and sink elements while retaining LG's
pipeline services. These elements hand compressed media to the common host transport;
[video](video.md) describes session ownership, timing and presentation.

## Replacement boundary

| Family | Targets | Element boundary |
|---|---|---|
| LG115x | `h13-afro`, `m14-beehive` | `vdecsink`, `adecsink` |
| LG1313 | `m16p-dreadlocks2` | `omx_lxvideodec` / `lxvideodec`, `omx_lxvideosink` / `lxvideosink`, `audio_dec`, `audio_sink` |
| MStar | `m2-dreadlocks`, `m3-goldilocks` | OMX decoder factories, `mvsink`, `masink`, `omxaudiosink` |
| MStar | `lm15u-beehive` | OMX decoder factories, `mvsink`, `omxaudiosink` (W16R's set over GLib 2.34, `mstar-lm15u`; no `masink`); for NDL's Esplayer, MStar's OpenMAX IL core itself |
| Realtek | `k5lp-goldilocks2`, `k6lp-jhericurl`, `k7lp-kisscurl` | OMX decoder factories, `dvovideosink`, `rtkalsasink`; `rvscale` where the firmware has it (K7, K8) |

`libpf`, `lpsink`, `decproxy` and umediaserver stay stock. A stand-in must be discoverable through
the same factory names, ranks, caps and class metadata that those clients use. The actual
factory set is selected by the target's build; the table is not an assertion that every stock
plugin or secure variant is replaced.

## Control and timing contract

- Preserve the properties and event shapes the stock pipeline reads and writes, including
  boxed `resource-info`. Resource assignment is distinct from successful decoding.
- Emit the video-information message understood by that release. Older clients use
  `video-info`; later clients use `media-info`. Source placement matters where libpf drops
  messages originating under `decproxy`.
- Retain normal GStreamer preroll, state, flush, EOS and query behavior. `ASYNC_DONE` participates
  in the stock load/seek completion path; completing a method call alone is insufficient.
- Preserve a single pipeline clock owner and the applicable audio context. Forward timestamps
  and state to the shared media transport rather than inventing an independent video clock.
- Honour a sink's `sync` set to false. LG's player factory sets it on both sinks of NDL
  DirectMedia (with the video sink's `low-delay`), whose apps pass timestamps in units of their
  own, and the stock sinks then show a frame and play a sample as it comes. The stand-ins send
  such video untimed, which the host shows at once, and stamp such audio from its arrival on,
  each buffer after the one before (`mediaport.c`).
- Underrun signals and frame counters must reflect their implemented semantics; exposing a
  property is not evidence that a hardware event was reproduced.

These are compatibility obligations at the selected boundary. The
per-family factories, types, caps and messages are in the sources named below. They describe what stock clients expect, which must be
distinguished from what each stand-in currently implements.

## Decoding for the guest

A stand-in decoder normally passes the compressed stream through to our own sink, which sends it
to the host. Some pipelines end in a consumer that needs real pixels instead: LG's camera player
(`libStarfishCameraPlayer`) decodes the camera's MJPEG with `appsrc ! omxmjpegdec ! ... ! appsink`
and hands the appsink's buffers to Chromium. For those the decoder asks the host to decode
and pushes the pictures it gets back (`include/webos-media.h`, "Decoding for the guest").

- **Which consumer needs pixels.** On the first buffer the decoder walks downstream from its src
  pad: through ghost and proxy pads and the src pads of elements in between, to every sink that is
  reached. If every one of them is ours (a type whose ancestry is `EsSink-*`, essink.c), the
  passthrough stays. If any other sink is reached (an element flagged as a sink, or one without
  src pads), the decoder decodes on the host. A missing link leaves the passthrough and asks
  again on the next buffer. Video and audio decoders both do this (audio below). Through an element the walk takes the
  src pads its sink pad links to inside it (the pad's internal links), not all of them: playsink's
  `streamsynchronizer` carries video, audio and text side by side, and on W18R an HLS stream with
  WebVTT subtitles reached LG's text sink (`tsinkbin`, an appsink) through its text pad, so the
  video went raw to `mvsink`, which cannot show it, and stayed on its first picture.
- **The format.** The decoder takes the formats of its own src template (system memory first),
  keeps NV12 and YUY2, and picks the first that downstream accepts. N21D's camera player gets
  YUY2; W22H's capsfilter asks for NV12.
- **The layout.** Each picture carries a GstVideoMeta with the plane offsets and strides. The
  camera player's new-sample handler reads plane 1 only from that meta, and without it the
  chroma is garbage. libgstvideo is linked from the firmware; the layer compiles against
  GStreamer core's headers only, so the two functions are declared locally.
- **Memory.** On a VM run as a systemd unit the pictures sit in the VM's media pool (the
  transport is in video.md, "Decoding for the guest", and `include/webos-media.h`, "The pool").
  The decoder claims its port on `/dev/lg/webos.media-pool` (the `media-pool` module), and the
  helper answers DECODE with POOL: the session's control extent, where a picture ring and a
  release ring sit. Pictures arrive on the ring with the doorbell's interrupt; each names its slot
  by pool offset, and the buffer maps that slot's dma-buf (EXPORT). Freeing the buffer puts the
  slot on the release ring. Without the device, or when the pool is full, pictures come inline
  on the port. A `memory:DMABuf` caps feature is accepted, but the memory under it is ordinary
  mapped memory: the camera player only maps buffers (`gst_buffer_map`; no
  `gst_dmabuf_memory_get_fd`, no EGL import).
- **Audio.** An audio decoder whose consumer is not ours asks for `WEBOS_MEDIA_S16LE` and pushes
  `audio/x-raw` S16LE, interleaved, stereo at 48000 Hz, the samples coming back inline on its
  port (no pool). W24G spacebuns' playbin picks `pulsesink` (rank 266) over LG's audio sink
  (rank 128), so a web app's AAC reached a sink that needs PCM and the pipeline failed until the
  decoder decoded on the host. LG's own audio decoders push PCM whatever follows them.

**`rvscale` (K7, K8).** W22H's camera player builds its PTZ pipeline
`... omxmjpegdec ! capsfilter NV12 ! videoconvert ! videocrop ! rvscale ! capsfilter
video/x-raw(memory:DMABuf),WxH ! appsink`. The stock `rvscale` opens the SoC scaler in its instance
init, and `se_open` asserts (se_drv.c:167) there, which aborted WebAppMgr's video-capture process.
The stand-in keeps the stock factory, rank (none), pad templates and metadata. At equal sizes it
passes the buffer through (the shared slot included). Otherwise it scales NV12 bilinearly in
software into system memory with a GstVideoMeta. Scaling on the host was not chosen: `videocrop`
sits between the decoder and `rvscale` and changes the crop at run time for PTZ, so the host would
have to take over the crop too. In the measured W22H runs the preview did not scale (640x480 in,
640x480 asked for). It is built where the firmware has `libgstrvscale.so`.

## The direct decoder and audio output

NDL DirectMedia's first API (`NDL_DirectVideoOpen`, `PlayWithCallback`, `NDL_DirectAudioOpen`,
`Play`), the only one on W17H, W18R and W19P, and LG's `libcgl` do not use GStreamer. They run
over LG's `libdile_vdec_direct` and `libdile_audio_direct`, which stay stock: thin layers that keep
the one handle of each HAL below them and pass every call to it. The stand-ins are those HALs,
bsppart's `libhal_vdec_direct` and `libhal_audio_direct` (`src/libhal_direct`, on `m16p`, `m3` and
`k5lp`), which take the stock library's place and NEED the stock file. Their streams go to the
host on the same media ports as the sinks', through `mediaport.c` built without GStreamer
(`MEDIAPORT_CORE`), on the guest's monotonic clock.

- **Video.** The stock decoder runs free: no timestamp reaches it, and it shows each picture as
  it is decoded. The stand-in sends each access unit untimed, which the host shows at once, on the
  channel DILE gives `HAL_VDEC_DIRECT_SetDEId` (the VDEC port the resource manager handed NDL), so
  it reaches the scaler window of that port. The fourcc picks H.264, HEVC or MPEG-2 (NDL's first API
  opens H.264 only). A buffer's ID given to `PlayWithCallback` comes back through the callback from
  the stand-in's own thread once the buffer has been sent, as the stock HAL's thread calls it per
  decoded picture. One stream at a time, as the stock HAL.
- **Audio.** DILE turns its sampling-frequency enum into Hz for `Start`. The stand-in stamps each
  buffer after the one before; a buffer that finds nothing queued starts the low-delay window's
  lower bound after its arrival (`SetNoDelayParam`, NDL's default 100 ms), where LG's DSP starts
  playing. `Write` always answers OK, as the stock HAL does on a decoder index, and sends everything.
  `GetBufferInfo` reports a 256 KiB ring holding what is still to play by those stamps, so it
  empties in real time. A mixer index (4 to 11) takes PCM only, as the stock one.

`NDL_DirectVideoSetArea` takes coordinates in the app's resolution (its `appinfo.json`
`resolution`, 1920x1080 by default), which NDL scales by the board's graphic resolution over that,
and tvservice by the panel over the graphic resolution: on a UHD K5LP set (graphic plane
1920x1080) panel pixels come out twice as large.

## The OpenMAX IL core (W15U)

On W15U (`lm15u`) NDL's Esplayer (`libndl-directmedia2.so.1`) is neither GStreamer nor the direct
HALs: it drives MStar's OpenMAX IL core, `/usr/lib/openmax/libOMX.MStar.so.1`, directly, with
decoders and renderers and no tunnels, and keeps time itself. The stand-in replaces that file
(`src/omx_core`, `omx_core` in the soc table); gst-omx, its other user, is the soc layer's own and
loads no core.

- **Components.** The roles the client asks for (`video_decoder.avc`, `.hevc`, `.mpeg2`,
  `audio_decoder.aac`, `.mp3`, `.ac3`, `iv_renderer.yuv.overlay`, `audio_renderer.local`) under the
  stock component names. A decoder copies each access unit into an output buffer unchanged; the
  client hands it to the renderer by index, which sends it to the host: video untimed on the
  channel of the resource info (`0x7F000003`), audio stamped from its arrival as the direct
  audio output's.
- **What the client reads back.** The video renderer sends the vendor event `0x7F000002` (size,
  frame rate, aspect, scan, from the stream's sequence header) once it knows them: the client
  passes it to ACB's `setMediaVideoData`, without which tvservice keeps the window muted. A
  buffer's `nFilledLen` is left as given: the client reads it back after `EmptyThisBuffer` returns
  and takes 0 for a failed write ("write.b buffer:N, error").
- **The window above it.** The sample apps clear an SDL 2.0.2 window to transparent; Mali's
  window buffers on W15U are RGBA only (`gl_vendor_window_rgba`), so the video shows under it.

## Validation

The [TV feature suite](../guides/build.md#tv-feature-suite) exercises the actual
guest pipeline and host decoding. Static interface evidence, a synthetic element test and a
successful stock playback case establish different things; keep those distinctions in results.
`tests/regress/virtcam/run-camera.py NAME --app` checks the camera app's decode path end to end.
