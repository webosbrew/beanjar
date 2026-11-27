# beanjar-vnc: the TV picture over VNC

`beanjar-vnc` (`host/vnc/`, Debian package `beanjar-vnc`) serves one VM's TV picture as a VNC
server. It exists for headless hosts such as the Pi, where watching a VM used to mean the GTK
viewer inside a Wayland desktop captured whole by WayVNC, all on the CPU (the Pi 5 has no video
encoder). Here only the composed TV picture is encoded, only when it changes, one server per VM.

```sh
beanjar vnc start tv1               # vnc://127.0.1.N:5900, N the VM's slot; with link lan also
                                    # vnc://<its LAN address>:5900
beanjar vnc start tv1 --size 1920x1080 --fps 60
beanjar vnc start tv1 --port 5901   # 5900 is taken (the Pi desktop's wayvnc holds *:5900)
beanjar vnc start tv1 --native-cursor  # LG's cursor as the clients' own, not in the picture
beanjar vnc start tv1 --listen 0.0.0.0  # link nat: on every address of this host
beanjar vnc stop tv1
beanjar vnc password show tv1       # print the VM's password, to copy it into a client
beanjar vnc password set tv1        # choose it: a prompt, or stdin's line (or set FILE)
beanjar vnc password reset tv1      # a new random one
```

`beanjar vnc start` prints where it is (the LAN address with link lan, and this host's) and the file
with its password, `identity/vnc.password` in the VM folder. `beanjar show NAME` shows the same under
`vnc` while it runs, and the manager window shows a VNC group in the VM's summary, after the
traffic inspector, with Copy Address and Copy Password. It is started on demand, never with the
VM, and stopping the VM stops it.

## Design

- **The viewer's core composes.** The program links `libbeanjar-viewer-core.so` (the `beanjar`
  package) and uses the same scene as the GTK viewer: scanout, video windows with the decoded media
  streams under the OSD, and LG's cursor. Nothing of the frame path is duplicated.
- **Core additions** ([viewer](viewer.md#the-c-core-api-coreh), `WV_ABI` 4, then 5 for the cursor below):
  `wv_frame(pixels, w, h)` composes offscreen at a chosen size and reads it into a caller buffer
  (RGBA, top row first: the scene is drawn into one framebuffer, then drawn flipped into a second,
  so `glReadPixels` needs no CPU copy). The "frame changed" notification is the existing `redraw`
  callback. It already fires on everything that changes the picture (a scanout, the cursor, a video
  window, a decoded picture, the pointer), and repeats from the 4 ms poll until a render has
  consumed it. `wv_frame` is such a render; `wv_screenshot` is not.
- **No display server.** GLES 2 on Mesa's surfaceless EGL platform (`EGL_MESA_platform_surfaceless`,
  `EGL_KHR_surfaceless_context`). On the Pi that is V3D's render node. Under WSL it is Mesa's d3d12
  on a Windows GPU ([GL](#gl)), llvmpipe with `--gl-adapter software`.
- **One thread, no busy polling.** neatvnc runs on aml. aml's fd (epoll: sockets, timers, the
  encoder workers' completions) is a `g_unix_fd_add` source on the core's GLib main context;
  each wakeup is `aml_poll(0)` + `aml_dispatch`. Encoding runs in aml's worker threads.
- **Frames on change, rate-limited.** `redraw` marks the picture dirty; a frame is made when a client
  is connected, at most `--fps` per second (default the VM's panel's refresh:
  [Frame rate](#frame-rate)). A frame whose pixels did not change is dropped
  ([Unchanged frames](#unchanged-frames)). Each frame goes into an `nvnc_fb_pool`
  buffer (XBGR8888, which is RGBA's byte order) and is fed whole; neatvnc's damage refinery
  hashes tiles and sends only what changed. neatvnc accepts no client before it has a buffer, so
  the first frame (black until the first scanout) is made at start.
- **Size.** The VNC desktop is a fixed `--size` (default 1280x720), the TV picture letterboxed
  into it as in the viewer. It is also the core's logical view, so pointer coordinates need no
  scaling.
- **Address.** It listens on the VM's own loopback address, `127.0.1.N`, port 5900, like every other
  port of the VM ([network](network.md#host-addresses-and-ports)). `beanjar vnc start` passes the
  address (`--listen`). There is no default: a bare `127.0.0.1` would collide between VMs.
- **The LAN, with link lan.** Like the traffic inspector, VNC is then also on the VM's own LAN
  address, port 5900. `beanjar vnc start` adds `--unix SOCKDIR/vnc.sock` (the VM's port-bridge socket
  directory, `NETNS_RUNDIR`), a second RFB listener behind the same password, and the LAN
  namespace's relay, started with the VM, serves that socket on the LAN address
  (`netns.lan_relay_pairs`; a connection while VNC is not running is closed). Each LAN VM has its
  own address, so 5900 there never collides; `--port` moves only this host's listener, and
  `--listen` is refused (both addresses are the VM's own).
- **The local socket.** `beanjar vnc start` also adds `--local run/rfb-local.sock`: RFB on a unix
  socket in the VM's run directory, without RFB authentication (security type None alone, whatever
  the password), created owner-only (mode 0600, under a umask of 0177 from its bind on). The
  file's permissions are the access control: only the VM's owner (and root) can connect. It is for
  this host's own front ends: `beanjar vnc pipe [HOST/]NAME` relays stdin and stdout to it, which
  is how the [web viewer](web-viewer.md) reaches a VM's picture, on this host or another one over
  the manager's ssh. beanjar-vnc removes it at exit, the manager after a stop.
- **Audio.** `beanjar vnc start` also adds `--audio-local run/audio-local.sock`, the VM's sound on
  a second owner-only socket (below).

## GL

Under WSL (`/dev/dxg`) Mesa 25's surfaceless EGL is llvmpipe unless `GALLIUM_DRIVER` names d3d12.
beanjar-vnc sets `GALLIUM_DRIVER=d3d12` itself there, before
EGL and any thread (`main.c` `gl_driver`), unless `GALLIUM_DRIVER` or `LIBGL_ALWAYS_SOFTWARE` is set
already, and logs the choice (`beanjar-vnc: GL: ...`) before the core's `GL` line.
`--gl-adapter NAME` picks the GPU whose name contains NAME (`MESA_D3D12_DEFAULT_ADAPTER_NAME`, e.g.
`NVIDIA`, `Intel`) whatever the environment says; without it, d3d12's default adapter, which is also
QEMU's (the integrated GPU here). `--gl-adapter software` is llvmpipe, on any host. Through the
manager: `beanjar vnc start NAME --gl-adapter NVIDIA`. Elsewhere (the Pi, a Linux host with a
render node) nothing is set.

The core's GL is GLES 2 with `GL_EXT_texture_format_BGRA8888`, which d3d12 has (GLES 3.1 on the
RTX 4070 Ti, 3.0 on the UHD 770). d3d12 has no `EGL_EXT_image_dma_buf_import`, so the media's
pictures arrive as planes in memory (`EGL dma-buf import no`) and are uploaded as luminance
textures; QEMU's scanout comes as before through Unix.Map. One driver cost needed a change: on
the RTX, a 1920x1080 `glTexSubImage2D` from client memory takes about 0.5 s (memcpy in the
driver), against 3 ms through a mapped pixel unpack buffer (`glMapBufferRange`, GLES 3), so where
the renderer is D3D12 the core uploads the scanout through one (`core.c` `tex_upload`; on the UHD
770 that is 5 ms against 3, on llvmpipe it is not used). The cursor and the video planes stay
direct: a 1920x1080 luminance plane takes 4 ms on the RTX. The GTK viewer uses the same core,
so it would take that path too if it ran on D3D12.

Measured 2026-10-25 on the x86 PC (WSL, Mesa 25.0.7) on `w22o-number1` (W22O, webOS 23, TCG, 4
vCPUs) at 1280x720, a headless Chromium page from a beanjar-web of the checkout (the user's
closed) and the home screen's app row navigated with RIGHT and LEFT every 0.3 s; beanjar-vnc
restarted for each GL, 30 s in H.264 mode (NVENC), then 30 s in VNC mode, two 10 s windows in
each; three rounds, in the orders llvmpipe, RTX, UHD; UHD, RTX, llvmpipe; default (UHD), llvmpipe,
RTX. CPU in % of one core:

| | llvmpipe | d3d12, RTX 4070 Ti | d3d12, UHD 770 |
|---|---|---|---|
| beanjar-vnc, H.264 mode | 34.9, 38.1; 35.5, 34.9; 33.0, 33.1 | 6.2, 6.2; 6.9, 6.5; 5.4, 5.3 | 5.8, 6.0; 5.2, 5.7; 4.3, 4.4 |
| beanjar-vnc, VNC mode | 34.4, 34.3; 33.2, 33.0; 32.2, 32.1 | 6.1, 6.0; 6.4, 6.4; 5.0, 5.0 | 5.9, 5.8; 5.9, 6.4; 4.3, 4.5 |
| its main thread | 4 to 6 | 4 to 7 | 4 to 6 |
| QEMU | 76 to 97 | 78 to 144 | 72 to 211 |
| frames a second, H.264 / VNC | 9.9, 8.3, 7.9 / 16.0, 12.2, 12.4 | 9.9, 6.8, 8.1 / 14.6, 11.0, 12.4 | 8.6, 8.3, 8.6 / 11.4, 4.9, 13.8 |

With llvmpipe the rest of the process is its 24 worker threads at about 2% each. The frame rates
(the page's H.264 pictures drawn, noVNC's updates) follow the TCG guest and the navigation, not
the GL; QEMU's CPU is as noisy with each and shows no effect. Per composed frame, timed in the
first two rounds with a temporary `glFinish` before `wv_frame`'s `glReadPixels` (means over 30
frames, ms): llvmpipe drew in 1.7, finished in 1.1 and read back in 0.4, and
uploaded a scanout in 3.3; d3d12 drew in under 0.1 (submission), finished in 1.3 and 2.0 (RTX),
1.9 and 5.8 (UHD, which QEMU's virgl also renders on), read back in 2.0 to 3.1, and uploaded in 4.9
to 7.0 through the buffer. Before the buffer, on the RTX an upload took 510 ms: 1 to 2 frames a
second, the main thread at 57 to 82%. Pictures (`beanjar screenshot`, and the page in both modes)
were the same on all three; on fake-qemu, check-vnc's frames match viewer-headless's on d3d12 and
on llvmpipe, and a video window (fake-media's test clip, `--no-osd`) was drawn the same on all three.
On d3d12 a client's first frame takes longer, 20 to 30 ms on the UHD 770 with a video window (13
to 18 ms without): d3d12's first drawing of each kind; later ones are as above.

## Audio

With `--audio-local PATH` beanjar-vnc serves the VM's sound, the guest's audio and the decoded
media audio mixed, on an owner-only (0600) unix socket, made as the local RFB socket is and removed
at exit. `beanjar vnc audio-pipe [HOST/]NAME` relays stdio to it, as `vnc pipe` does to RFB, and
the [web viewer](web-viewer.md#sound)'s speaker plays it. Without the option nothing is listened to
(`no-audio`) and the media is decoded against the core's clock, as before.

**The stream** (`host/vnc/audio.h`). A client sends one request line, `opus` or `pcm`, and gets a
16-byte header, all little-endian: `BJAU`, u32 16 (the header's length), u32 48000 (Hz), u16 2
(channels), u16 format (1 PCM, 2 Opus). Then:

- **PCM**: signed 16-bit little-endian stereo frames, interleaved: 192 kB/s, about 1.5 Mbit/s.
- **Opus**: libopus, 48 kHz stereo, 10 ms frames (480 samples), 128 kbit/s, restricted low
  delay (`OPUS_APPLICATION_RESTRICTED_LOWDELAY`, CELT-only). One core mix block fills a
  packet; no second block is accumulated. Each packet is a u16 length and the packet, so the stream is framed. A new Opus client
  after none starts a fresh encoder state.

Anything else (`flac`, no line within 5 s, a line over 31 bytes) is closed without a header.

**The mix** (`core/pcm-out.c`, the core's `wv_audio_sink`, [viewer](viewer.md#the-c-core-api-coreh)).
A thread of the core is the audio clock, every 10 ms on CLOCK_MONOTONIC: it takes the media mix
(`media_fill`, media.c's mixer with the virtual AMP's volume and mute, the same samples the GTK
viewer's media device plays), adds the guest's audio and hands 480 frames of 48 kHz s16 stereo to
beanjar-vnc. That clock replaces media's own clock thread (`media-silent`): it runs whether or not
anyone listens, so the media helper is paced and video sync is unchanged; without listeners only
the guest's conversion, the mix and the hand-over are skipped. The GTK viewer is untouched: it
keeps its two SDL devices (the guest's in its own format, the media's), mixed by the desktop's
sound server.

The guest's audio comes from QEMU's AudioOutListener (`-audiodev dbus`, 44.1 kHz s16 stereo) on
QEMU's timing, not this clock's. Its dedicated listener thread/context keeps reception
independent of rendering ([ownership](viewer-audio.md#listener-ownership)).
`audio.c` hands each Write, after its counters and WAV, to a tap;
pcm-out converts it to float with the guest's SetVolume gain and mute, resamples it to 48 kHz by
linear interpolation (enough for this version; no filter), and queues it in a FIFO. The clock
starts taking from it once 40 ms are queued; past 150 ms (a burst after a stall, or QEMU's clock
drifting from the host's) it drops back to 60 ms, and run dry it pads the block with silence and
primes again, so latency never grows. At exit `audio out:` counts blocks, guest frames, drops and
underruns.

**Clients.** Each format is encoded once per block for all its clients; Opus only while an Opus
client is connected. Sends never wait: each client socket has a 48 kB send buffer (about 200 ms of
PCM), a block that does not fit is dropped for that client (whole blocks or packets only, so its
stream stays framed), and a client that takes nothing for 2 s is disconnected. At exit
`beanjar-vnc: audio clients=` counts them, the packets, the Opus bitrate, the drops and the slow
disconnects.

**Why PCM stays.** Browsers decode Opus with WebCodecs' `AudioDecoder`, which exists only in a
secure context: https, or `http://127.0.0.1` and `localhost`, which is how beanjar-web is usually
opened, but not a plain-HTTP LAN address. Raw PCM needs no decoder anywhere; it costs about 1.5
Mbit/s against Opus's 0.13, which matters across ssh from another host, hence Opus where it can
be decoded.

## Video

With `--video-local PATH` beanjar-vnc also serves the composed picture, the frames its RFB clients
get, cursor included, as H.264 on an owner-only (0600) unix socket, made and removed as
the audio one is. `beanjar vnc start` passes `run/video-local.sock` when beanjar-vnc is built with it (its
`--help` names the option); `beanjar vnc video-pipe [HOST/]NAME` relays stdio to it, and the
[web viewer](web-viewer.md#the-h264-picture) decodes it with WebCodecs. neatvnc's own H.264 (RFB
encoding 50) needs GBM frames and a VA-API or V4L2 encoder that imports them;
beanjar-vnc gives it GBM frames where
a V4L2 encoder makes H.264 from RGB ([GBM frames](#gbm-frames)).

**The stream** (`host/vnc/video.h`). A client sends one request line: `h264`, optionally followed
by `bitrate=N` (kbit/s, the most the stream may take: 100 to 50000; default 10000, sized for 720p60). It gets a
32-byte header, all little-endian: `BJVI`, u32 32 (the header's length), u16 width, u16 height,
u16 fps (`--fps`, the most a second), u16 codec (1: H.264), u32 bitrate (kbit/s), and the
encoder's name in 12 bytes, NUL-padded (`nvenc`, `vaapi`, `v4l2`, `x264`). Then units, each a
u32 length, u32 flags (1: keyframe) and u64 timestamp (microseconds since beanjar-vnc started)
before its bytes: an access unit, Annex-B, no B-frames, SPS and PPS before every IDR. Anything
else (`vp8`, `jpeg`, a bitrate out of range, no line within 5 s) is closed without a header.

**Encoders** (libavcodec, and V4L2 for `v4l2`; `--video-encoder auto|nvenc|vaapi|v4l2|x264`; auto
takes the first that opens, in this order, and keeps it):

| Encoder | Input | Settings |
|---|---|---|
| `h264_nvenc` | the RGBX frame as it is (`rgb0`), converted on the GPU | preset p1, tune ll, VBR at `cq` 23, Main, `zerolatency`, `delay` 0, `forced-idr` |
| `h264_vaapi` (the first `/dev/dri/renderD*`) | NV12 [converted on the GPU](#rgb-to-yuv-on-the-gpu) (libswscale where it cannot), uploaded to a VA surface | VBR at half the bitrate, Main, `async_depth` 1 |
| `v4l2` (`host/vnc/h264v4l2.c`: the first `/dev/video*` that is a stateful mem2mem H.264 encoder taking 32-bit RGB; RK3399's VEPU2 with the hantro-vpu DKMS module) | the RGB as it is: a GBM frame's dma-buf imported (`XBGR32`), or RGBX copied into its buffer (`RGBX32`); the encoder converts (full-range BT.709, signalled in the VUI) | CBR at the bitrate (the encoder writes no filler, so a still picture costs little), GOP of ten seconds, keyframes forced on demand (`V4L2_CID_MPEG_VIDEO_FORCE_KEY_FRAME`), SPS and PPS before each keyframe (the driver's), one frame at a time |
| `libx264` | NV12 [converted on the GPU](#rgb-to-yuv-on-the-gpu) (libswscale's yuv420p where it cannot) | ultrafast, zerolatency (sliced threads), Constrained Baseline, CRF 23, QP at least 10 |

All of them: the requested bitrate as the maximum rate with a VBV of half a second, constant
quality below it (VA-API: VBR), no B-frames, a GOP of ten seconds' worth of frames with keyframes forced on demand, BT.601 limited
range signalled as the conversion (NVENC's, the GPU pass's and libswscale's) makes it. The bitrate is a cap, not a
target: with CBR NVENC spent the whole budget on every unit, so a still screen re-encoded once a
second cost 25 kB a unit (200 kbit/s at 6 Mbit/s) and LG's cursor moving over a still picture
3.6 Mbit/s; at constant quality the still screen's units are about 60 bytes. libx264 164 with sliced
threads codes every slice after the first wrong under VBV at a QP below 10 (a flat picture at
20 Mbit/s decoded grey), hence the QP floor. On the WSL PC NVENC works without `/dev/dri`
(`libnvidia-encode.so.1` in `/usr/lib/wsl/lib`). The Pi 5 has no H.264 encoder, so x264. The
`v4l2` encoder's `/dev/video*` need the user running beanjar-vnc in group `video` (Debian's 0660
`root:video`); otherwise none is found and auto goes on to x264.

**RGB to YUV on the GPU** (`host/vnc/yuv.c`). x264 and VA-API take YUV 4:2:0, which libswscale
made from each RGB frame on the encoder thread until 2026-10-28; now beanjar-vnc's own GL context
makes it, so the CPU it took is left to the VM. After a frame is composed at the output size, one
GLES 2 pass samples it (without GBM frames from a texture of beanjar-vnc's own that the scene is
composed into, then read back for neatvnc and the [unchanged frames'](#unchanged-frames)
comparison as before; with [GBM frames](#gbm-frames) the frame's own texture) and writes NV12 into
an RGBA target a quarter of the width wide and one and a half times the height high, four bytes in
each pixel. One `glReadPixels` reads it into a buffer of the encoder thread's pool: 1.5 bytes a
pixel where the RGBX copy it replaces was 4, rows padded to a multiple of 4 bytes. x264 takes it as
NV12, the chroma layout it keeps internally, and VA-API uploads it to a VA surface
(`av_hwframe_transfer_data`) as it did swscale's: on WSL Mesa's d3d12 cannot export the planes as
dma-bufs, so there is no upload-free path there. The conversion is the
one swscale made: BT.601 limited range, chroma the mean of two columns over four rows weighted 1,
3, 3, 1 (`SWS_BILINEAR`'s siting), edges repeated, the picture cut to even width and height as the
encoders' size is. On the PC it matches swscale within 1 code value; on the Pi it is closer to the
standard than the system's swscale, whose Raspberry Pi fast path takes chroma from one pixel of
four. NVENC (RGB
in) and `v4l2` (the GBM frame's dma-buf) are unchanged; with `--video-encoder nvenc` or `v4l2` the
pass is not set up.

Each encoder's opening line says which conversion it gets: `, RGB to NV12 on the GPU`,
`, RGB to YUV by swscale` or `, RGB to YUV in NVENC`. The frame handed on for a new client
precedes the encoder's opening, so it is RGB and swscale converts it; once open, the encoder thread
asks the main loop for a fresh frame, which comes as NV12. libswscale stays as the fallback. At the
start the pass is compiled and checked on a 14x6 test picture against the same conversion done in
double precision on the CPU (at most 1 code value apart); without high-precision floats in fragment
shaders, with a shader that does not compile or link, or with a larger difference, beanjar-vnc logs
`video: no RGB to NV12 on the GPU (the reason): swscale converts` and the encoders open as before
(x264 on swscale's yuv420p). A read back that fails later logs `RGB to NV12 on the GPU failed:
swscale from now on`: the open encoder converts the RGB frames to NV12 with swscale, and later ones
open as without the GPU. `BEANJAR_VNC_SWSCALE=1` in the environment forces the fallback; it exists
for its test (`check-vnc-video`) and for comparisons, not as a setting. At exit
`video: N frames converted to NV12 on the GPU` counts them.

Measured 2026-10-28 through beanjar-vnc itself (`tests/integration/vnc/bench-vnc.py`: fake-qemu `--animate`,
one video client, CPU of all its threads per unit):
on the WSL PC x264 went from 6.5 to 5.0 ms a frame at 720p and from 14.8 to 10.2 at 1080p, VA-API
from 5.9 to 4.0 and from 9.3 to 5.6, at the same 30 frames a second. On the Pi 5, composing into
memory as it does by default, nothing is saved (x264 720p 21.8 and 21.2 ms): the RGB read back
for neatvnc and the comparison stays, and on V3D the NV12 read back costs the main loop about what
swscale cost the encoder thread; at 1080p the frame rate fell from 22.7 to 20.7 with the main
loop waiting on both read backs. With `--frames gbm`, no RGB read back, the Pi took 16.8 ms a
frame at 720p.

**No JPEG stream.** The socket also served whole-frame JPEGs (codec 2) from 2026-10-27, for
hosts without a hardware H.264 encoder:
on the RK3399 Hantro's JPEG encoder with RGA converting ahead of it through dma-bufs made 78.8
frames a second at 1280x720 for 4.1 ms of CPU each, libjpeg-turbo 97.1 for 10.3 ms. It was
removed the same week, with the web viewer's MJPEG choice: the RK3399's VEPU2 makes H.264 with
the hantro-vpu DKMS module (neatvnc's Open H.264 and the `v4l2` encoder above), so every host that
matters has H.264, and a browser without WebCodecs (plain HTTP from another machine) stays on
noVNC's Tight, which needs no stream of its own.

**VA-API on WSL.** There is no `/dev/dri`
until `sudo modprobe vgem` (beanjar never loads it), and the VA driver is Mesa's d3d12 over vgem's
render node, which needs `LIBVA_DRIVER_NAME=d3d12`, `GALLIUM_DRIVER=d3d12` and, to choose the GPU,
`MESA_D3D12_DEFAULT_ADAPTER_NAME`. beanjar-vnc's own GL is chosen after it, with its own adapter
([GL](#gl)), so the VA device does not take these from its environment: with
`--video-encoder vaapi`, where `/dev/dxg` exists, it opens the VA device at start, before EGL and
before any thread, with the three variables set (those not already set; the adapter from
`--vaapi-adapter NAME`, a part of the GPU's name such as `NVIDIA` or `Intel`, else Mesa's default),
unsets them again, and keeps the device for every encoder it opens. Through the manager:
`beanjar vnc start NAME --video-encoder vaapi --vaapi-adapter NVIDIA`. Elsewhere (a Linux host with
a VA driver of its own) the device is opened with the encoder, as before. `auto` stays NVENC, then
VA-API, then x264, and on WSL its VA-API attempt has no d3d12 variables, so it fails there and x264
follows; auto never opens a GPU device at start, and nothing loads vgem.

The render node must be readable and writable by the user running beanjar-vnc. systemd-udevd
does run under WSL and Debian's rules give vgem's nodes `video` (`card0`) and `render`
(`renderD128`, 0660), so the PC's standing setup is vgem loaded at boot
(`/etc/modules-load.d/vgem.conf`, one line `vgem`) and the user in `render` and `video`. Right
after a `modprobe` the nodes can show `root:root 0600` until udev has handled them; the first test
used an ACL instead (`setfacl -m u:USER:rw /dev/dri/renderD128 /dev/dri/card0`), which goes with
the module. With the node readable, Mesa's EGL also tries vgem
first and logs `failed to create dri2 screen` before settling on its software winsys (d3d12 or
llvmpipe), which is harmless.

Measured 2026-10-25 on the x86 PC (WSL, Mesa 25.0.7), RTX 4070 Ti (`--vaapi-adapter NVIDIA`):
`check-vnc-video` with `BEANJAR_TEST_VAAPI_ADAPTER=NVIDIA` got the header and 29 units that ffmpeg
decoded to fake-qemu's picture (`avc1.4D4C1E`, Main); on `w22o-number1` 76 units in 5 s from
`beanjar vnc video-pipe` decoded without an error, and the web viewer (headless Chromium 156)
showed "VAAPI" and drew 17.7 frames a second from 8 kbit/s on a nearly still YouTube page, with no
RFB bytes underneath. The same PC's Intel UHD 770 is no use for this: with Windows driver
31.0.101.3222 the d3d12 VA driver offers it only video processing, and with 32.0.101.7092 it lists
H.264 `EncSlice` but `h264_vaapi` hangs at the first frame (Mesa waits on the encode fence without
a timeout).

**When it encodes.** The encoder runs on a thread of its own, which owns the clients. It is
opened for the first client and closed after the last (`video encoder closed: no clients`). A video client counts as a client
for rendering, so frames are composed for it with no RFB client connected; each composed frame is
copied (`video_server_frame`), or converted on the GPU and read back
([above](#rgb-to-yuv-on-the-gpu)), to a thread of its own, which encodes the latest one, so a slow
encoder skips frames rather than hold up the main loop. That copy is the only one
before the encoder: the frames' buffers come from an `AVBufferPool`, and NVENC gets its frame as
a reference to the thread's buffer (`buf[0]` set), so `avcodec_send_frame` takes it without the
copy libavcodec makes of a frame it does not own (`av_frame_ref` on one without `buf[0]`: a new
buffer and `av_frame_copy`). A buffer goes back to the pool once the slot, the thread and
libavcodec have all let it go (NVENC keeps a frame it cannot upload yet, for want of a free input
surface, until a later call), so none is written while libavcodec still reads it. On fake-qemu
`--animate` at 1280x720 with NVENC, `av_frame_copy` ran 135 times in 5 s before (once a unit)
and not at all after; the encoder thread took 4.7, 6.8 and 5.4% of a core before, 3.7 and 5.0%
after (the rest of beanjar-vnc, llvmpipe's composing, is the bulk and noisy). On `w22o-number1` (as under [Paused RFB
clients](#paused-rfb-clients)) the encoder thread took 3.1, 3.0 and 2.3, 2.3% before, 2.0, 2.1
and 1.7, 1.8% after. A frame is encoded when the
picture changed, up to `--fps`, and the last one again once a second when it did not, so a still
screen costs a unit a second (about 60 bytes with NVENC at 720p). One encoder serves all the
clients: a request with another bitrate reopens it, the latest request's bitrate applying to all.

**Clients.** A new client is sent nothing until a keyframe, forced for it at once (the others get
it too). Sends never wait: each socket has a 256 kB send buffer; a unit that does not fit is
dropped for that client, and so is every unit after it until a keyframe, which is forced (at most
every 250 ms) once its socket has room again, so its stream never misses a reference. A client
that takes nothing for 3 s is disconnected. The thread polls the clients, so one that closes is
noticed at once. At exit `beanjar-vnc: video clients=` counts the clients and refusals, the
encoder, its openings, units, keyframes (forced ones too), bytes, drops and slow disconnects.

Without libavcodec, libavutil and libswscale (`libavcodec-dev`, `libswscale-dev`) beanjar-vnc is
built without `--video-local` (meson warns; `--help` does not list it).

## GBM frames

Reading the composed picture back to the CPU was the RK3399's cap:
the 1280x720 `glReadPixels` takes 15 ms, 11.6 of them on the CPU, and on the main thread, which
also handles QEMU's display calls, so the next guest frame waited behind it. With `--frames gbm`
the scene is composed straight into GBM buffers and nothing is read back (`host/vnc/frames.c`):

- **The buffers.** Eight linear XRGB8888 buffer objects (bytes B, G, R, X) on the first render node
  whose buffers the EGL context can render into, each imported as an EGL image and made a
  framebuffer, each exported once as a dma-buf. Their height is rounded up to 16 rows, since
  V4L2 encoders read whole macroblock rows (neatvnc queues `stride * ALIGN(height, 16)` bytes); neatvnc gets a view of the same memory at the
  picture's height (`gbm_bo_import`), so its desktop keeps the picture's size.
- **The render** (`wv_frame_fbo`, `WV_ABI` 12): the core's scene composed into a free buffer
  (drawn into its own target, then flipped into the buffer, as `wv_frame` does before its read
  back). An EGL native fence (`EGL_ANDROID_native_fence_sync`) is watched on the main loop, and
  only when it signals is the frame handed on; one render is in flight at a time. The main
  thread only submits GL work.
- **Handed on, not copied.** Frames are reference-counted (`host/vnc/frame.h`); a buffer is reused
  only when nobody holds it. neatvnc gets a frame of its own for the buffer object each time
  (`nvnc_fb_from_gbm_bo`), whose cleanup lets the buffer go once neatvnc's display has a newer one
  and its encoders are done. Its Open H.264 encoder (V4L2) imports the dma-buf; Tight reads the
  buffer through a mapping. The video socket holds a reference until its encoder is done or a
  newer frame replaces the one waiting (`video_server_gbm_frame`).
- **The answer to QEMU.** The core answers `ScanoutDMABUF` and `UpdateDMABUF` when the fence of
  its copy into the cache signals, not after a `glFinish` (any front end with the extension).
- **When.** `--frames auto`, the default, takes GBM frames where a V4L2 encoder makes H.264 from
  `XBGR32` (RK3399 with the hantro-vpu DKMS module) and reads frames back elsewhere, as before:
  there a CPU read of a GPU buffer (write-combined on panfrost) has nothing to gain over the read
  back. `--frames gbm` or `memory` forces either; without a GBM device or a dma-buf import it
  falls back to the read back with the reason in the log.
- **neatvnc.** The V4L2 encoder needs [the private build](https://github.com/mariotaku/beanjar-vendor/blob/main/neatvnc/README.md)'s patches to
  give the right colours and quality.

### Unchanged frames

A frame whose pixels are all the same as the last one handed on is dropped: neither neatvnc nor
the video socket gets it. QEMU's damage rectangles cannot tell: the guest flips between buffers,
so nearly every frame comes as a new `ScanoutDMABUF` without one (on wt1h-afro 5803 of 6249). So
the composed frame is compared with the last one in 16x16 tiles. With GBM frames, and without
them while no RGB is read back, that is on the GPU (`tilediff.c`): one pass marks each 4x4 block
with a difference, a second marks each tile with a marked block, and the tiles are read back,
80x45 bytes at 1280x720. With GBM frames it is drawn before the render's fence and read once that
signals (`frames.c` `frames_diff`). Without GBM frames, a frame read back whole as RGB anyway is
compared by `memcmp` with the last one read back ([Read backs](#read-backs)): on the Pi the GPU
passes before a whole read back cost an RFB mix more than the `memcmp` they would save. A first version drew one fragment a tile reading
its 256 pixel pairs; on the Pi's V3D that held 1080p to 12 frames a second, the two passes keep its
rate. The changed tiles are neatvnc's damage (its refinery then hashes only those, and Tight
encodes only those), unless neatvnc does not have the last frame (a frame held for paused
clients, or the first): then the whole frame. A new RFB client, a video client starting or
resuming always gets a frame (`force`). `vnc_unchanged` in the log counts the dropped ones.

Measured on the RK3399 (wt1h-afro, the launcher row navigated, alternating builds in one
session; on the JPEG stream, since removed): with the comparison the stream carried only changed frames, at 5.4 to 6.0 Mbit/s
instead of 8.4 to 10.8. The comparison costs the main thread about 2.2 ms a render (0.9 to draw,
1.3 to read; main thread 27 % against 22 to 24 %). A Tight client got 16.7 updates a second of
16.0 KiB against 16.0 of 19.6 KiB. On the little cores it also costs changed frames: 17.0 a
second against 19.1 without it (the table below), which the [CPU placement](#cpu-placement)
more than wins back.

The steps of the RK3399 read-back cap like for like (2026-10-27, the netboot base's shared root, the same build of
the board's manager, the same 15 s of RIGHT/LEFT presses on the launcher row, the JPEG stream (since removed), three
rounds in rotating order; frames a second as means, CPU in % of one core):

| | units a second | changed frames a second | gap p50 | beanjar-vnc main thread | QEMU main thread |
|---|---|---|---|---|---|
| before (read back, `glFinish`) | 12.1 | 8.5 | 84 ms | 52 | 33 |
| answer at a fence | 12.2 | 8.3 | 84 ms | 54 | 33 |
| GBM frames, no read back | 29.4 | 19.1 | 35 ms | 24 | 51 |
| unchanged frames dropped | 17.0 | 17.0 | 70 ms | 28 | 49 |
| and on the big cores | 20.8 | 20.8 | 40 ms | 16 | 36 |

Tight reads the mapped GBM buffer although that is slower per byte than cached memory: on the
RK3399 a Tight client got 17.7 and 18.7 updates a second with GBM frames, 10.8 and 11.5 with
`--frames memory` (the read back), in alternating runs of the same motion. So GBM frames stay
when a Tight client is connected; there is no read back for it.

CPU access to a 1280x720 frame on the RK3399 (panfrost, Mesa 25.0.7; means of 30, ms wall / CPU):

| | A72 (core 4) | A53 (core 1) |
|---|---|---|
| a GBM buffer's mapping, copied out (`gbm_bo_map` or the dma-buf's `mmap`) | 6.4 / 6.3 | 26.5 / 26.5 |
| `glReadPixels` from a texture (the read back) | 14.2 / 9.4 | 35.1 / 30.5 |
| `glReadPixels` from a linear GBM buffer's framebuffer | 42.8 / 41.5 | 136.9 / 136.1 |
| `memcpy` of cached memory | 1.4 | 1.8 |

### Read backs

Without GBM frames (`--frames memory`, the default where no V4L2 encoder is found: the PC, the Pi)
the scene is composed into one of two textures of beanjar-vnc's own, the other holding the last
frame handed on. A frame is read back only for the consumers that take it:

- RGB, into a buffer of neatvnc's pool, while an RFB client wants pictures (one that beanjar-web
  has not paused, or the first frame, before "ready") or the video socket's encoder takes RGB
  (NVENC, `v4l2`, swscale's fallback, or no encoder open yet). Only the rows that changed are read
  back: each buffer of neatvnc's pool keeps the frame it holds (a serial in the buffer's userdata),
  and the rows changed in the last 16 frames handed on are kept, so a buffer is brought up to date
  with the band from the first changed row to the last since its frame, or read whole when its
  frame is older or it is new. The frame is compared on the GPU first, which gives that band.
  When the last changed frame changed more than half the rows (video playing, a page scrolled),
  the frame is instead read back whole and compared on the CPU with the last RGB frame, which
  stops at a difference: the read back would wait for the GPU comparison otherwise.
- With no RGB consumer nothing is read back for the comparison: it runs on the GPU between the
  two textures ([Unchanged frames](#unchanged-frames)).
- NV12, for a changed frame, while the video socket's encoder takes NV12 from the GPU (x264,
  VA-API: [RGB to YUV on the GPU](#rgb-to-yuv-on-the-gpu)).

So a video client alone (beanjar-web's H.264 picture, its RFB client paused) costs the GPU
comparison and one NV12 read back a changed frame, no RGB one; with no consumer nothing is composed
at all ([idle](#idle-without-clients)). A paused client that resumes gets the frame held for it if
one was read back, else a whole frame composed and read back at once. `rgb_reads` in the counters
counts the RGB read backs, `rgb_rows` the rows they read, `video_nv12` the NV12 read backs.

### CPU placement

On hosts whose cores differ in capacity (`/sys/devices/system/cpu/cpu*/cpu_capacity`; RK3399: four
Cortex-A53 at 381, two Cortex-A72 at 1024), `beanjar vnc start` starts beanjar-vnc on the biggest
cores (`taskset`, so every thread it makes inherits them) and moves QEMU's main thread there
(the display and virgl; the threads QEMU made before keep theirs, the vCPU threads included)
(`host/core/beanjar/runtime/cpus.py`). Where the cores are alike nothing is changed.

Measured on the RK3399 with unchanged frames dropped (the JPEG stream, since removed; changed frames a second;
three rounds of 12, 12 and 20 s in rotating order; CPU in % of one core):

| | frames a second | beanjar-vnc main thread | QEMU main thread |
|---|---|---|---|
| as scheduled (mostly on the A53s) | 22.4, 17.3, 17.0 | 25 to 27 | 47 to 48 |
| beanjar-vnc and QEMU's main thread on the A72s | 18.9, 20.9, 20.2 | 15 to 16 | 34 to 35 |
| the same, the vCPUs moved to the A53s | 20.2, 21.7, 19.3 | 15 to 16 | 36 |

The picture's rate follows the guest's distinct frames during the motion more than the host now;
on the big cores both threads need a third less CPU, which is headroom. Moving the vCPUs to the
little cores bought nothing measurable, so they are left as they are.

### Toward 60 fps

`--fps` is 60 by default; unchanged
frames are dropped, so a still screen costs no more than at 30. Each guest frame reaches the core
as a `ScanoutDMABUF` (the guest flips to another buffer; QEMU does not wait for the answer)
followed by an `UpdateDMABUF` (its flush; QEMU holds the guest's GL, `graphic_hw_gl_block`,
until the answer). The core copied the guest's 1920x1080 buffer into its cache on both, so twice
a frame; it now copies on the update only, and on a draw if a scanout comes with no update
(`refresh`).

Measured 2026-10-27 on the RK3399 boards ([Counters](#counters), `fps60.py`-style rounds: an RFB
client asking for Open H.264 updates back to back, the launcher row navigated with RIGHT/LEFT,
15 s each, the two builds alternating; means):

| | guest frames/s | copies a frame | QEMU's wait, ms an update (share of time) | render ms | client updates/s |
|---|---|---|---|---|---|
| w22o-queue (webOS 26), two copies (3 rounds) | 23.8 | 2.0 | 12.4 (30%) | 14.3 | 22.1 |
| w22o-queue, one copy (3 rounds) | 27.2 | 1.1 | 5.8 (16%) | 12.6 | 25.1 |
| wt1h-afro (webOS 1), two copies (2 rounds) | 21.3 | 2.0 | 9.8 (21%) | 12.5 | 20.5 |
| wt1h-afro, one copy (2 rounds) | 21.5 | 1.0 | 4.2 (9%) | 11.7 | 20.5 |

The wait QEMU sees per frame halves on both. On w22o-queue the guest then draws more frames; on
wt1h-afro it does not, so there the guest's own rate (about 21 a second in this motion) is the
limit. With no client at all (updates answered without a copy) w22o-queue's guest made 29 to 37
frames a second in the same motion: the guest, not the host, stops short of 60 there. Every
stage after the hand-off keeps up: renders and the client's updates follow the guest's frames.

A draw between a scanout and its update (the main loop runs a render in between) used to copy
the scanout then, and the update copied it again. Now a scanout waits up to 50 ms for its update
(`SCANOUT_WAIT_US`): a draw meanwhile shows the last frame, and the scanout is drawn as it is if
no update comes.

**Without the guest** (`check-vnc-rate`, `tests/integration/vnc/check-rate.py`): `fake-qemu.py --dmabuf`
flips three pre-filled 1920x1080 GBM buffers at 60 frames a second, a `ScanoutDMABUF` and an
`UpdateDMABUF` each, and holds the next frame until the update is answered, as QEMU does; an RFB
client on the `--local` socket asks for updates back to back. On board 1 (2026-10-27, wt1h-afro
running idle beside it, everything on the A72s, 8 s rounds alternating):

| | source frames/s | copies/s | renders/s (changed) | client updates/s (Open H.264) | answer, ms | render ms |
|---|---|---|---|---|---|---|
| one copy a frame, the extra copy on a draw (2 rounds) | 57.3, 55.0 | 67.5, 67.3 | 51.8 (47.7), 50.7 (44.1) | 47.6, 44.1 | 8.8, 9.7 | 8.4, 8.3 |
| no extra copy (2 rounds) | 55.7, 54.7 | 55.7, 54.9 | 53.0 (53.0), 53.2 (53.2) | 53.0, 53.2 | 9.9, 10.1 | 8.2, 8.1 |
| the extra copy on a draw, source at 30 (one run) | 29.9 | 33.7 | 34.4 (29.9) | 30.0 | 4.8 | 9.8 |
| no client, source at 60 | 60.0 | 0 | 0 | | 0.4 | |

Without a client the source makes its 60 (the answer without a copy takes 0.4 ms here, 3.2 ms
as the source sees it, the D-Bus round trip). With one, the host passes about 53 of 60: the
copy of the 1080p frame (the answer, about 10 ms at 60 against 4.8 at 30) and the render into
the 720p GBM frame (about 8 ms) share the Mali-T860, and together take more than the 16.7 ms
a frame has.

**The copy at the output's size** (2026-10-27): the core made its cache of the guest's frame at the
guest's size (1920x1080), and the compose then scaled the cache to the output. The cache is now
made at the size the front end draws at when that is smaller than the scanout (`cache_want`: the
picture's `fit()` rectangle, 1280x720 for the default stream, the window's for the desktop
viewer), so the one copy QEMU waits for is also the downscale, with the same linear filter. The
compose then draws the cache 1:1. The cache follows the output: `wv_render` and the frame
renders (`wv_frame`, `wv_frame_fbo`) set the output size, and the cache is reallocated and copied
again from the source (the dmabuf as it is, or the uploaded texture) at the next draw when that
size or the scanout's changes, so the frame that follows is complete. A window as large as the
guest keeps the full copy. Cursor, hotspot, video planes and `no-osd` work in guest coordinates
scaled by the output rectangle, never by the cache, so they are unchanged; an UpdateDMABUF's
rectangle was never used (the whole frame is copied). `Display1.Screenshot` (and the guest's
capture) at a size above the cache's makes the cache that large for the one draw, then restores
it: `beanjar screenshot` at its default 1280x720 is the stream's cache as it is, and
`--size 1920x1080` has the guest's full detail (a copy of its own, from the dmabuf as it is then),
at the price of two extra copies; the `osd-alpha` dump needs the full size and gets it. The
counters `cache_w`, `cache_h` and `recopies` are in the stats line.

Picture: the 720p frame of the launcher on wt1h-afro, old and new core, differs by at most one
level in 0.001 of the mean (PSNR 77.9 dB, no pixel over 8; two shots of one build differ by 0);
the 1920x1080 screenshot is identical. Not softer. `check-vnc` checks that a 1920x1080
screenshot halved is the stream's frame and that the stream's frames follow it.

Board 1 (RK3399, 2026-10-27), the builds alternating, one measurement at a time:

| | answer, ms an update (share of time) | render ms | guest updates/s | client updates/s |
|---|---|---|---|---|
| wt1h-afro launcher motion, copy at 1080p (3 rounds, 15 s) | 3.15 (9.7%) | 9.9 | 30.8 | 24.3 |
| same, copy at 720p (3 rounds) | 2.55 (7.8%) | 9.5 | 30.4 | 23.0 |
| steady 60 source, copy at 1080p (2 runs, 8 s) | 9.6 | 9.2 | 50.2 | 49.8 |
| same, copy at 720p (2 runs) | 8.8 | 9.3 | 49.8 | 49.7 |

The wait QEMU sees fell by about 0.6 ms (19%) with the real guest and by 0 to 0.8 ms with the
steady source, much less than the 2.25 times fewer pixels written suggest, and between runs of one
build the steady source's answer moves by as much (6.6 to 10.4 ms): in that table only the real
guest's difference is outside the noise. The rates do not move. Why, from a measurement-only
build of each core (timestamps around the steps, `glFinish` between them; the GPU timer query
`GL_EXT_disjoint_timer_query` is not exposed by panfrost on the T860; steady 60 fps source on
board 1, Open H.264 client, means of 120-update windows, microseconds):

| step | old (copy at 1080p) | new (copy at 720p) |
|---|---|---|
| (a) importing the dmabuf (EGLImage, texture; per `ScanoutDMABUF`, on the main thread, before the update; QEMU does not wait for it) | 1100 to 1750 | 1250 to 2600 |
| submitting the copy (CPU) | 350 to 450 | 350 to 450 |
| flushing and creating the fence | 570 to 800 | 570 to 720 |
| (b) the GPU still busy with this context's previous work (the last 720p render and flip) when the update arrives (`glFinish` before the copy) | 2840 | 2840 |
| (c) the copy itself after that, submission to fence | 4160 (10 windows) | 3950 (11 windows) |
| the same copy drawn into a 16x9 target (fixed cost of a job and its fence; sparse reads) | | 2250 |
| (d) the whole wait for the fence, as the guest runs it (b and c overlapping) | 8400 to 9200 | 7900 to 9100 |

So of the 8.4 to 9.2 ms QEMU waits for the fence, about 2.8 ms is the previous frame's render
and flip still on the GPU (b, unchanged), about 2.2 ms is a fixed cost of any GPU job and its
fence on this board, and the copy's own work above that is about 1.9 ms at 1080p and 1.7 ms at
720p. The copy reads the whole 8.3 MB source in both builds; only its write shrinks, from 8.3 to
3.7 MB, which is all the new build saves. The rest of the wait (about 2 ms plain over the
sum of the parts) is the guest's and QEMU's own GPU work sharing the Mali-T860, which these
timestamps cannot separate. The 720p render, with the copy, is what the queue holds: the
H.264 encoder is the separate VEPU block and has no place in the GPU queue; it shares only
memory bandwidth with the GPU.

Whether that bandwidth matters: with a Tight client (no Open H.264, so the VEPU is idle) the
steady source made 52 to 55 frames a second against 49 to 50 with H.264, and the fence wait was
6.1 to 8.5 ms against 7.9 to 9.2, in both builds (copy at 1080p: 52.0 and 53.1; at 720p: 54.5 and
54.8; H.264: 48.6, 48.7 and 49.9, 49.4). The Tight path also renders differently (a read back, no
GBM buffer), so this suggests contention from the encoder's traffic but does not prove it. The
client's updates a second: 49 to 50 with H.264 (53 of 60 in the earlier run), 50.5 to 51 with
Tight, 23 to 24 with the launcher motion (the guest's 30 a second is the limit).

What would shorten the wait further: not the copy's size but what surrounds it: that the
previous frame's render and flip are still queued when the update arrives, and the fixed cost of
each GPU job. Neither is touched by this change.

## Frame rate

A frame is made at most `--fps` times a second; without `--fps`, at the VM's panel's refresh
(the core's `panel_hz`, from `vm-info.toml`, [viewer](viewer.md#output-rate)), whatever it is: 60
on a 60 Hz panel, 120, 144 or 240 on such panels (`--fps` takes up to 1000). The times follow the
core's pacing rule (`pace.h`: a grid of the period, no burst after a pause; the core's own pacing
is off, `output-hz` 0, and its poll runs at the panel's `pace_poll_ms`), and only changed pictures
are made, so the guest's UI, which draws at most 60 frames a second, still gives at most 60, and
more come only while a video plane changes that often. No frame is repeated to fill a rate. The
video stream's header has the rate as its u16 (its high byte was not written before: a rate above
255 would have been cut).

**Above 60 only while sustained** (`rate.c`). The host must do a frame's work in a frame's time,
8.3 ms at 120, 6.9 at 144, 4.2 at 240. Each frame's main loop work is timed: composing, the read
backs and their comparison (memory frames), or from the GBM render's start to its fence and the
hand-off (`gbm_ready`, with the NV12 conversion); and the encoder thread times each new picture it
encodes (`video_server_encoded`). Both are averaged over windows of 60 frames after the first 30
(the GL's and the encoder's first frames are slow on every host); a window whose mean of either is
over the frame's time counts against the rate, one within it clears the count, and two in a row
lower it for the rest of the session to what the slower mean allows with a tenth to spare
(1 s / (1.1 x the mean), lower than before, never below 60, the UI's own rate), logged:

```
beanjar-vnc: 144 fps not sustained (a frame's main loop work 2.0 ms, encoding 7.5 ms, against 6.9): 121 fps from now on
```

The new rate is measured the same way and may be lowered again; 60 is never measured. The
encoders opened later and the video clients that come later get the new rate in their header;
neatvnc's own encodings (Tight, Open H.264) run in its worker threads, are not timed, and take the
frames they can. The stats line has `fps=` (the rate now) and `frame_main_us=`,
`frame_encode_us=` (the last window's means in us; 0 and -1 before one, and -1 for the encoding of
a window with nothing encoded).

Measured 2026-11-12 at 1280x720 with one video client (`beanjar vnc video-pipe`, `bitrate=20000`)
and the same clips as the [viewer's](viewer.md#output-rate), 20 s from two stats lines, the media
from the `present` reports around them (about 25 s):

| | panel | clip | encoder, frames | rate | main loop, encoding (ms a frame) | frames, units a second | received, shown |
|---|---|---|---|---|---|---|---|
| x86 PC, TCG, W22O | 120 Hz | 120 fps | NVENC, memory | 120 kept | 3.5, 4.9 | 110.1, 109.3 | 2905, 2774 |
| the same | 120 Hz | 60 fps | NVENC, memory | 120 kept | 3.6, 5.1 | 63.2, 60.5 | 1481, 1477 |
| Pi 5, KVM, W22O | 120 Hz | 120 fps | x264, memory | 120 to 60 | 15.6, 9.0 | 47.7, 47.7 | 2986, 1154 |
| the same | 120 Hz | 120 fps | x264, `--frames gbm` | 120 to 60 | 17.8, 7.7 | 42.7, 42.7 | 2994, 1032 |
| the same | 120 Hz | 60 fps | x264, memory | 60 | | 54.0, 54.0 | 1496, 1291 |
| Pi 5, KVM, W22P | 60 Hz | 120 fps | x264, memory | 60 | | 39.0, 38.9 | 2869, 934 |
| the same | 60 Hz | 60 fps | x264, memory | 60 | | 55.1, 55.0 | 1495, 1336 |

The PC keeps 120 (its TCG guest delivers about 114 pictures a second); the Pi 5, which has no
H.264 encoder, does not: its main loop alone needs about 16 ms a frame with the guest's virgl on
the same V3D, so it falls back to 60 and makes 40 to 55 frames a second.

## Idle without clients

With no RFB client (TCP, WebSocket, `--unix` or `--local`) and no video client, beanjar-vnc does no
frame work (`update_idle` in `main.c`): nothing is composed, read back, converted or encoded, and
the core is told so (`wv_idle`, core ABI 11). QEMU's `UpdateMap` and `UpdateDMABUF` are then
acknowledged without the copy into the cache texture (the core's `skipped` counter); the picture
is brought up to date when it is drawn next, from the shared surface (the whole of it uploaded) or
the dmabuf (copied as it is then): for a new client's first frame, or `Display1.Screenshot`, which
composes one frame on demand. Scanouts, LG's cursor, the IR and media channels, the video windows
and pcm-out's clock (which the media's A/V sync relies on) go on as before, so the guest sees no
difference; an audio client alone keeps the idle.

- **2D updates.** QEMU sends `Scanout`/`Update` with the pixels in the D-Bus message when it
  cannot share memory with the listener. The core keeps its own copy of that surface (the shadow,
  `width * 4` per row, filled from every 2D `Scanout` and `Update`, idle or not: one `memcpy` of the
  rectangle); while idle an `Update` is only copied there and counted in `skipped`, and the shadow
  is uploaded whole when the picture is drawn next. A `Scanout` (a new size or format) is still
  uploaded at once; an `Update` in another format than the shadow's drops the shadow, and updates
  are then uploaded as they come, as before.
- **Decoded media.** Nothing to change: a video window's decoded picture is uploaded (or its
  dmabuf imported) only when the window is drawn (`draw_stream` in `media.c`), and
  `media_update` picks the picture due and releases the one before whether or not it was drawn.
  While idle the pictures are picked on time and released to the helper, none uploaded (its
  report: `shown=0`), so the guest's pipeline and the A/V sync see no difference; a new client's
  first frame or a screenshot draws the current one.

The first client of either kind ends it at once: an RFB client's connection schedules a frame
(neatvnc may send its held, older frame first, then that one), a video client's request a
keyframe. RFB clients without video clients cost no H.264 work (frames are copied for the encoder
only while one is connected), and video clients alone do not feed neatvnc, nor do they with RFB
clients that are all paused (below).

Measured 2026-10-25 on the x86 PC (WSL, TCG), W22O webOS 23 (`w22o-number1`) at 1280x720, the home
screen's category row navigated with RIGHT and LEFT every 0.3 s throughout, beanjar-vnc's CPU (all
threads, % of one core) over two 10 s windows each, a headless Chromium page from beanjar-web as
the client:

| State | Before | After |
|---|---|---|
| no client | 47, 47 | 2.0, 2.1 |
| one noVNC page (Tight JPEG q3) | 79, 83 (19 updates/s) | 108, 118 (35 updates/s); 118, 111 (35/s) |
| the page with H.264 (NVENC; noVNC at q0) | 116, 108 (30 updates/s) | 111, 116 (31/s); 111, 106 (29/s) |

Idle went from 47% of a core to 2%. With the 2D shadow (above) the same VM idled at 1.8 and 1.9%
(2.1 and 2.0 just before, with the YouTube page open in its browser): it uses the shared surface
(`maps=1 skipped=1380` at the stop), so the 2D change does not apply to it; QEMU there shares
memory with the listener, and 2D updates are only the fallback. With a client the guest drew faster after the change (the
host's CPU no longer spent on an unwatched beanjar-vnc goes to TCG), so per update it costs less
(about 4.2% of a core per update a second in VNC before, 3.2% after) though the totals rose.

## Against WayVNC on the Pi

What watching a VM costs the Pi 5 through beanjar-vnc, against the setup it replaces: the GTK
viewer on the desktop (labwc, 1920x1080) with WayVNC capturing the whole desktop. Measured
2026-11-25 (22:25 to 22:38 UTC) on the Pi 5 (KVM), W22O 43.21.89.01 (webOS 26, 2560 MiB, nat and
mitm), the checkout's build of a47c617, nothing else of ours running. The same RFB client for both,
`tests/lib/rfbclient.py` with noVNC's encodings (Tight JPEG, continuous updates); beanjar-vnc on
its `--local` socket at its default 1280x720, `--fps 60` or `--fps 30`; WayVNC as a second
instance like the desktop's (`wayvnc` 0.10.2, neatvnc 1.0.4, `--gpu`, its default 30 fps limit),
but on a Unix socket without authentication, so with no TLS; the viewer window at its default
size. "Playing" is a looping 1280x720 60 fps H.264 test pattern in the video test app
(`tests/regress/video/app`, a 960x540 window); "idle" is Home with nothing moving (a key pressed
before each window keeps the screen on). 20 s windows, three setups in turn, two rounds in
alternating order. CPU in % of one core (all threads; the host's is all cores busy, other
users' services included):

| | client updates/s | watching it | QEMU | media-helper | host |
|---|---|---|---|---|---|
| playing, beanjar-vnc at 60 | 57.5, 57.8 | 72.0, 75.0 (main thread 57, 60) | 55.7, 60.0 | 42.5, 44.2 | 236, 235 |
| playing, beanjar-vnc at 30 | 30.0, 30.0 | 40.3, 38.9 (main thread 32, 31) | 48.2, 49.2 | 38.2, 36.9 | 186, 180 |
| playing, viewer and WayVNC | 28.0, 28.4 | 66.8, 67.7: WayVNC 34.1, 33.8, viewer 26.6, 27.3, labwc 6.1, 6.6 | 55.9, 55.7 | 44.0, 43.8 | 240, 232 |
| idle, beanjar-vnc at 60 | 0 | 1.3, 1.3 | 22.2, 23.1 | 0.6, 0.5 | 37, 38 |
| idle, beanjar-vnc at 30 | 0 | 1.4, 1.3 | 23.5, 26.8 | 0.5, 0.5 | 38, 42 |
| idle, viewer and WayVNC | 1.3, 1.2 | 3.7, 3.6: WayVNC 1.0, 1.0, viewer 2.4, 2.3, labwc 0.3, 0.3 | 23.0, 24.8 | 0.6, 0.6 | 43, 39 |

At the same 30 updates a second beanjar-vnc takes about 40% of a core where the viewer, WayVNC
and the compositor take 67%, and the host as a whole is about 50 points lower; at 60 it sends
twice the updates for 72 to 75%, a little more than the old setup at 30. Idle, both are small. beanjar-vnc renders
on V3D there, not llvmpipe (`GL OpenGL ES 3.1 Mesa 26.2.2; V3D 7.1.7.0` in its log).

## Thumbnails

beanjar-web's cards ask for a 384x216 picture of each running VM every 5 s while they are on
screen (`beanjar screenshot NAME - --frame 384x216`, [the web viewer](web-viewer.md)), through the
`Screenshot` call that beanjar-vnc's core serves ([Screenshots from a
script](viewer.md#screenshots-from-a-script)). beanjar-vnc answers a call smaller than its
desktop each way itself (`wv_screenshot_hook`, `WV_ABI` 13); other sizes go to the core's own
screenshot as before.

- **From a frame on the GPU, never the guest's buffer at QEMU's call.** While frames are composed
  for a client, the thumbnail is drawn from the last one composed (`mtex`). With no client
  (nothing composed, [idle](#idle-without-clients)) the scene is composed once for it into a
  texture of its own, which brings the core's cache up to date from the guest's surface after
  QEMU's updates were already answered, so QEMU never waits for a thumbnail.
- **Small, with several samples a pixel.** `thumb.c` draws it letterboxed into the requested size,
  each pixel the mean of 4x4 bilinear samples over its footprint (no aliasing of text or fine
  lines), and reads back only that: 384x216x4 bytes, written as the PPM the manager converts.
- **Again only after a change.** The core counts the scene's changes (`wv_counter` `changes`: what
  marks it dirty, and updates skipped while idle). While that, the requested size and the
  desktop's are the same as for the last thumbnail, the call is answered with its bytes, no GPU
  work.
- **After the frame, on request.** A call that needs the GPU waits until the frame due is
  composed and handed on (or runs at once while none is due), at low priority on the main loop.
  Nothing is done while nobody asks. LG's cursor is not in a thumbnail where beanjar-vnc sends it
  as the clients' cursor (`--native-cursor`): the frames it is drawn from have none.

When the VM stops the manager keeps its last thumbnail (any screenshot of 640x360 or less,
`vms/NAME/thumbnail.png`), and beanjar-web's Off card shows it dimmed under the Start button
(`beanjar screenshot NAME - --last`).

Measured with `tests/integration/vnc/bench-thumbnail.py` (fake-qemu `--animate`, 5 calls a second for 10 s;
2026-10-28). "Made" is the counter `thumbnail_us` a thumbnail, the main thread's time to compose if
need be, draw and read back; the call's time includes waiting for the frame due:

| Machine | Desktop | Cached, call | Idle, made / call | With a Tight client, made / call | Frames a second with the client, without / with calls |
|---|---|---:|---:|---:|---:|
| PC / D3D12 | 1280x720 | 0.70 ms | 5.10 / 5.41 ms | 1.36 / 7.06 ms | 29.4 / 29.4 |
| PC / D3D12 | 1920x1080 | 0.67 ms | 8.14 / 8.44 ms | 1.86 / 9.77 ms | 29.3 / 29.4 |
| Pi 5 / V3D | 1280x720 | 0.76 ms | 10.44 / 10.73 ms | 2.99 / 15.20 ms | 29.4 / 29.3 |
| Pi 5 / V3D | 1920x1080 | 0.75 ms | 17.56 / 19.02 ms | 6.71 / 34.71 ms | 29.3 / 29.2 |

The idle cost is mostly the compose and the upload of fake-qemu's shared surface; a cached call
costs the bus round trip and writing 249 KB. The frame rate beside a client did not change.

## Paused RFB clients

While a web viewer page plays the H.264 stream, its noVNC stays connected for input, and
beanjar-web withholds its update requests ([No RFB picture under
H.264](web-viewer.md#no-rfb-picture-under-h264)). neatvnc (0.9.1 and 1.0.3) shows no client's
pending requests or continuous updates in its API, and it runs its damage refinery (hashing
every tile) on every frame fed while any client tracks damage, sent or not. So beanjar-web says
so: on the paused connection it sends a ClientCutText of exactly `beanjar:rfb-pause`, and of
exactly `beanjar:rfb-resume` first when it resumes (no NUL, no line end). beanjar-vnc takes these
on the `--local` listener alone (`nvnc_set_cut_text_fn` is set there only; a marker over TCP,
WebSocket or `--unix` does nothing), and never as clipboard text: it has no clipboard, and any
added later must filter the markers out first.

With every RFB client paused, frames are still composed and read back for the video clients, but
none is fed to neatvnc; the latest is held instead. On a resume the held frame is fed at once with
the whole frame as damage, in the marker's callback, before the non-incremental request beanjar-web
sends after it, so the page's first picture is the current one; a fresh frame follows. A closed
connection is no longer paused. With paused clients and no video client the core idles as with no
client. Each change logs `RFB client paused|resumed (N of M paused, vnc_feeds=F)`, and
`vnc_feeds=` at exit counts the frames fed to neatvnc (`vnc_frames=`: those composed).

Measured 2026-10-25 on the x86 PC (WSL, TCG), `w22o-number1` at 1280x720, a headless Chromium page
from beanjar-web in H.264 mode (NVENC) and the home screen's app row navigated with RIGHT and LEFT
every 0.3 s, beanjar-vnc's threads (% of one core, two 10 s windows each, two rounds in opposite
orders): its main thread, where neatvnc's refinery runs, took 7.5, 7.7 and 6.5, 6.4 with the
feed (with the NVENC change), and 5.7, 5.7 and 5.3, 5.6 without; `vnc_feeds` stayed at 13
through each 30 s pause. The whole process (48 to 98%, most of it llvmpipe composing, and moving
with the guest's frame rate, 15 to 26 a second over the rounds) is too noisy to show either change.

## Planes

The core leaves out the planes beanjar-vnc hides (`wv_planes`, [viewer.md](viewer.md)): the video
windows, the UI or LG's cursor. beanjar-vnc composes one picture for every client, so they are
hidden for all of them: `--hide-planes video,ui,cursor` at start, and the web viewer's More, Planes
 while it runs. On the `--local` listener alone, as the pause markers, a ClientCutText of
exactly `beanjar:planes=N` (N 0 to 7, the `WV_PLANE_*` bits: 1 video, 2 UI, 4 cursor) sets them
and `beanjar:planes?` asks; either is answered with a ServerCutText `beanjar:planes=N` to every
`--local` client (`nvnc_send_cut_text` on that listener), so every page shows the choice. Other
listeners never see these; a standard VNC client's clipboard is not touched. A change logs
`planes hidden N (video on|off, UI on|off, cursor on|off)`. With `--native-cursor` a hidden
cursor is the transparent one (`wv_cursor` gives none). The bus's `Screenshot` calls still have
every plane: a thumbnail is then composed afresh rather than taken from the last frame, and the
planes are hidden again after it. `tests/integration/vnc/check-planes.py` checks the markers with two
clients, and both kinds of screenshot.

## Ports and conflicts

`beanjar vnc start NAME` takes `--listen ADDRESS` (link nat only) and `--port N`, before or after the
name; the defaults are the VM's `127.0.1.N` and 5900. `--port 0` takes any free port: the manager
binds port 0 on the address to learn one (`hostprocs.free_port`) and passes it on as an ordinary `--port`,
so `run/vnc.json`, `show` and the start message report the real number. beanjar-web starts VNC that
way, since it uses only the local unix sockets and the TCP listener is for a person's viewer; with
link lan the LAN address keeps 5900, which is in the LAN namespace and cannot collide with the
host's. The
ports are per invocation, not stored: the per-VM address already keeps VMs apart, so only a
listener outside beanjar collides, and that is a property of the host, not of the VM.

A listener on all addresses covers every address, so the Pi desktop's wayvnc on `*:5900` takes
port 5900 on `127.0.1.N` too. Before starting the server, the manager looks for a TCP listener on
the same port at the same address or on all addresses (`/proc/net/tcp`, `tcp6`;
`hostprocs.port_conflict`), and refuses with its name:

```
port 5900 is taken: wayvnc (pid 1234) listens on *:5900; choose another with beanjar vnc start tv1 --port N
```

A process of another user is named as such (its file descriptors are not readable). The manager
then waits for beanjar-vnc's `ready` line, writes where it listens to `run/vnc.json` (what
`show` and the window read), and prints it. If beanjar-vnc exits first, its last message is the error (for example
its own bind error, `Address already in use`, which also suggests `--port`).

## Authentication and the LAN

`beanjar vnc start` always sets a password: the VM's own, kept with its identity so it can be saved in a
client. It is the owner-only (0600) `identity/vnc.password` in the VM folder, one line, and
beanjar-vnc reads it from there (`--password-file`) at its start; there is no copy in `run/`. It
is never in argv or in beanjar-vnc's environment. So there is no passwordless VNC through the
manager on any address, loopback included; the one way in without the password is the owner-only
local socket (above), which needs the VM owner's own account.

- **Made once.** The first `beanjar vnc start` or `beanjar vnc password show` makes it at random: 20
  characters of `23456789abcdefghjkmnpqrstuvwxyz` (about 99 bits), none that reads like another,
  no symbol a keyboard layout moves, so it can be typed off the screen. VNC restarts, VM restarts
  and reboots keep it; a file that is empty or unusable is made anew.
- **`beanjar show NAME`** says where it is (`vnc_password_file`); `vnc password show NAME`
  prints the password alone, for copying; `set` reads a new one from a prompt (twice) when stdin is a terminal, else from
  stdin's line, or `set FILE` from a one-line file (`-` is stdin), never from argv; `reset`
  makes a new random one. Only `vnc password show` prints it; `show` and `beanjar vnc start` only
  name the file.
- **What a password may be.** 1 to 255 characters of printable ASCII (space to `~`), no space at
  either end. neatvnc's RSA-AES (`src/auth/rsa-aes.c`, 0.9.1 and 1.0.3) reads the password after a
  length byte and copies it into a C string, so 255 bytes at most and no NUL; TigerVNC refuses to
  send more than 255 (`CSecurityRSAAES::writeCredentials`). The line end ends the file's line, and
  the manager's Copy Password strips the line. Non-ASCII would reach neatvnc as the client's
  encoding of it, which clients do not agree on, so it is refused, with tabs and control
  characters.
- **A running beanjar-vnc keeps the password it started with.** `set` and `reset` say so and
  name the restart (`beanjar vnc stop NAME; beanjar vnc start NAME`); it is not restarted for you,
  since `beanjar vnc start`'s options of that start are not kept.
- **Identity.** `beanjar identity reset` makes it anew with the SSH keys (another TV, another
  password); `beanjar wipe` blanks state disks only and keeps it; `delete` removes it with the VM.

**The user name.** neatvnc's RSA-AES always asks for a user name and a password (subtype 1,
`RFB_RSA_AES_CRED_SUBTYPE_USER_AND_PASS`). beanjar-vnc ignores the name: any is accepted, the
empty one too, and only a failed attempt logs it. That is the least to type: no client needs a
particular one.

- **TigerVNC** (1.13+, `RA2_256`/`RA2`): it shows a dialog with Username and Password, after one
  asking to confirm the server key's fingerprint. Leave Username empty or type anything (the VM's
  name, say), and put the password in **Password**: the Username field has the focus, and a
  password pasted there is sent as the user name with an empty password, which is refused ("Invalid
  username or password"). Type a user name if you want "Keep password for reconnect", which
  TigerVNC applies only when both are non-empty. Unattended: `VNC_USERNAME=x VNC_PASSWORD=...
  vncviewer HOST::PORT`; `VNC_USERNAME` must be set (empty is enough), or `VNC_PASSWORD` is not
  used. `-passwd`/`PasswordFile` cannot be used at all: TigerVNC reads it only for security types
  that ask no user name, and it holds a `vncpasswd`-obfuscated (DES) 8-byte password, not a plain
  file.
- **noVNC**: cannot log in. It speaks RSA-AES only as `RA2ne` (type 6, `core/rfb.js`), which neatvnc does not
  offer, and neatvnc has no RSA-AES over WebSocket (below), so `beanjar vnc start` has no listener for it.
  The [web viewer](web-viewer.md) runs noVNC over the local socket instead.
- **macOS Screen Sharing**: cannot log in. It offers VNC (DES) and Apple's own authentication,
  neither of which beanjar-vnc enables (no classic VNC authentication, no Apple DH: below); not
  measured.
- **RealVNC Viewer**: any user name and the password.

With `--password-file`, beanjar-vnc requires authentication on every
connection: `nvnc_enable_auth` with `NVNC_AUTH_REQUIRE_AUTH | NVNC_AUTH_REQUIRE_ENCRYPTION`, which
leaves neatvnc's RSA-AES security types alone (RSA-AES-256 and RSA-AES, 129 and 5; no None, no Apple
DH, no VeNCrypt since no TLS certificate is set). The password is compared in constant time; the
user name clients send is ignored. Classic VNC (DES) authentication is not offered.

- **Loopback** (`127.0.0.0/8`, `::1`, as numbers) without a password works when beanjar-vnc is
  run by hand: no authentication.
- **Any other address** (`0.0.0.0`, `::`, a LAN address, or any host name, `localhost` included),
  and `--unix`, require the password: without it beanjar-vnc refuses to start, before it binds
  anything.
- **`--local PATH`** never authenticates, with a password or without one: it is created mode 0600,
  and only processes of its owner reach it.
- **RSA key.** None is configured: neatvnc generates one at the first RSA-AES handshake
  (`rsa_aes_send_public_key`, logging "An RSA key has not been set"), kept for the process's life.
  The fingerprint a client shows therefore changes each time beanjar-vnc starts.
- **Clients.** TigerVNC 1.13+ (`RA2`, `RA2_256`), RealVNC Viewer; see the user name above.

beanjar-vnc needs neatvnc 1.0: the private build (1.0.4, beanjar-vendor's, [its README](https://github.com/mariotaku/beanjar-vendor/blob/main/neatvnc/README.md))
links nettle, hogweed and gmp (`HAVE_CRYPTO`) and export
`nvnc_enable_auth` and `nvnc_set_rsa_creds`; the callback resolves an `nvnc_auth_creds` with
`nvnc_auth_creds_accept` or `_reject`. Debian trixie's 0.9.1 sent the server's RSA-AES challenge
without the ciphertext's leading zero bytes, so about one login in 128 to 256 (depending on the
client key) failed; 1.0 sends it at the key's length.

**No WebSocket with a password.** neatvnc cannot do RSA-AES over its WebSocket listener, in 0.9.1,
1.0.3 and upstream master alike. After the key exchange, `on_rsa_aes_challenge`
(`src/auth/rsa-aes.c`) calls `stream_upgrade_to_rsa_eas(client->net_stream, …)`
(`src/stream/rsa-aes.c`), which casts the stream to `struct stream_rsa_aes` and replaces its `impl`
with the TCP one. On a WebSocket stream (`struct stream_ws`, `src/stream/ws/ws.c`) that cast
overwrites `on_event`, `ws_state` and the frame header with the RSA-AES fields and drops the
WebSocket framing, so the server's hash never reaches the client as a frame (measured: the
handshake stalls), and the stream's state is corrupt. So beanjar-vnc refuses `--websocket` with a
password, saying why, and `beanjar vnc start`, which always has one, offers no WebSocket listener. Use a
VNC client on the RFB port.

## In a browser (noVNC)

The managed way is the [web viewer](web-viewer.md) (`beanjar-web`): noVNC in its own page,
carried over a WebSocket of the web server to `beanjar vnc pipe`, that is to the local socket.

Directly, only beanjar-vnc run by hand, on loopback without a password. `--websocket N` opens a second
listener on the same address that speaks RFB over WebSocket
(neatvnc's `nvnc_open_websocket`), so noVNC connects to beanjar-vnc directly, without websockify.
A display belongs to one neatvnc server, so the websocket
listener has its own display, fed the same frames; clients of both count together.

neatvnc 0.9.1 refuses a WebSocket handshake that offers any subprotocol other than `chat`.
noVNC 1.6 offers none by default (`wsProtocols: []`), and that works.

noVNC 1.6 asks for Open H.264 (encoding 50) where WebCodecs decodes a real H.264 frame (Chromium
does; Electron builds without the codec do not, and get Tight). With GBM frames and the RK3399's
V4L2 encoder the web viewer's VNC mode then gets H.264. Its quality slider is noVNC's quality
level, which neatvnc hands to the encoder as constant quality: at the viewer's default, 3, flat
areas show faint vertical streaks that 9 does not have (the video socket's CBR stream does not
show them either). The private neatvnc build therefore holds the encoder's QP at 18 or better
([beanjar-vendor's neatvnc](https://github.com/mariotaku/beanjar-vendor/blob/main/neatvnc/README.md), patch 02), so the default gives the quality of level 6 while
Tight keeps the JPEG quality of level 3. On a 960x600 screenshot a JPEG is 30.5 kB at level 3
(quality 34), 41.3 kB at 6 and 128.6 kB at 9.

noVNC itself is Debian's `novnc` package, served as static files, for example:

```sh
python3 -m http.server --bind 127.0.1.N 6081 --directory /usr/share/novnc
# then http://127.0.1.N:6081/vnc.html?host=127.0.1.N&port=6080&path=&autoconnect=1&resize=scale
```

## Input

- **Keys.** Clients with QEMU's extended key events send the key itself; neatvnc hands it over as a
  Linux evdev code. Other clients send X keysyms, which `host/vnc/keysym.c` maps to the US-layout
  PC key that types them (shifted symbols to their key; the client sends Shift). Either way the key
  goes to `wv_key_evdev`, the core's PC key map: Super (and Home) is the remote's Home, Escape is
  Back, the volume and mute keys go over IR, everything else to the virtio keyboard.
- **Pointer.** Every pointer event is `wv_motion`. Buttons 1 to 3 are `wv_button` 0 to 2, on their
  edges. Wheel buttons 4 and 5 are one `wv_wheel` step per press, as the GTK viewer's discrete
  scroll. A client's held buttons are released when it leaves; with the last client the core
  also releases held keys and the pointer leaves the picture, so LG's cursor returns to the
  guest's position.
- **From a script.** `org.beanjar.Display1.Pointer` on the VM's bus (`beanjar remote click`), served
  by the core as for the viewer ([viewer](viewer.md#the-pointer-from-a-script)), in display pixels.
- **The cursor**: see below.

## The cursor

By default LG's cursor is composed into the picture where the VNC pointer is, for every client
(one server serves one picture). It looks as on the TV, translucency included, and scales with
the picture in any client; each pointer move costs a frame, and the cursor trails by a round trip.

`--native-cursor` (`beanjar vnc start NAME --native-cursor`) sends it to the clients as their own cursor
instead (`nvnc_set_cursor`), and the client draws it at its pointer: no frame per pointer move, no
lag. The core gives it (`wv_cursor`) and draws the scene
without it (`no-cursor`), so a pointer move or libim moving the cursor is no change of the
picture. It is opt-in because of the mask (below): every 1-bit rendering of webOS 26's pointer
looked worse than the composed one, and the released neatvnc has nothing else.

- **Shape.** The picture webos.cursor carries, cropped to its visible pixels (alpha above 0) and
  the hotspot, then scaled like the TV picture into the desktop, as the composed cursor is. LG's
  buffers can be larger than what they show (256x256 or 255x255 with the hotspot at 0,0 from the
  KMS path), so the crop matters. Scaling is pixman's, on premultiplied alpha.
- **Size.** The RFB rectangle takes any size, but clients do not: TigerVNC drops the connection
  over 256 in either dimension (`CMsgReader.h` `maxCursorSize`), and noVNC sets a CSS cursor
  (a data URI), which Chromium and Firefox ignore over 128x128. A cursor larger than 128 in either
  dimension is shrunk to fit 128x128, keeping its proportions and scaling the hotspot. That only
  happens in desktops from about 2900 pixels wide: LG's cursors are small.
- **Hidden.** A hidden LG cursor (or one with nothing visible) is a transparent 1x1 cursor, so the
  client shows no pointer of its own over the picture, as on the TV.
- **Updates.** A cursor is sent when it differs from the last one sent (pixels, size, hotspot), not
  per frame, pointer move or heartbeat: the core's serial changes with the picture, hotspot,
  visibility or scale, and beanjar-vnc compares the result before `nvnc_set_cursor` (neatvnc 0.9.1
  resends on every call; 1.0.3 compares pixels itself). neatvnc sends the current one to a client
  that connects later. Each change is logged, with LG's own geometry, e.g.
  `beanjar-vnc: cursor 40x48 hotspot 4,1 (LG's: 60x85, visible 57x71 at 2,7, hotspot 9,9, 40x57 in
  the desktop)`; `vnc_cursors=` at exit counts them.
- **The mask.** neatvnc 0.9.1 and 1.0.4 offer only the plain Cursor pseudo-encoding (-239):
  colours and a 1-bit mask, a pixel opaque when its alpha is above half (`extract_alpha_mask`).
  A true-colour cursor needs VMware's alpha cursor (0x574d5664 type 1: noVNC, TigerVNC) or
  CursorWithAlpha (-314: TigerVNC), which upstream neatvnc has neither of. LG's newer pointers
  depend on alpha: webOS 26's is a white outline (alpha 255) around a black fill and a soft shadow
  at alpha 64 to 127, so through the mask only the outline is left, nearly invisible over light
  pictures (seen on W22O queue's Home). Cutting at a lower alpha makes the fill solid black
  instead; neither looks like the TV, which is why composed is the default. WT1H's pointer is
  opaque and looks as composed apart from hard edges.
- **The alpha cursor.** A neatvnc patch for beanjar-vendor (not in its releases yet) adds VMware's
  cursor pseudo-encoding, type 1: each pixel's R, G, B and straight alpha bytes, whatever the
  client's pixel format. A client that asks for it gets the cursor that way,
  any other the Cursor pseudo-encoding as before. noVNC 1.7.0 asks for VMware's before Cursor and
  decodes it in `_handleVMwareCursor`; TigerVNC asks for CursorWithAlpha, then VMware's, and reads
  type 1 as it is. The patched `neatvnc.h` defines `NVNC_HAS_VMWARE_ALPHA_CURSOR`, and beanjar-vnc
  built against it drops the noVNC colour swap (below), which applies to the Cursor pseudo-encoding
  only. Against a neatvnc without it, beanjar-vnc is unchanged. check-vnc covers both: a cursor at
  alpha 64 arrives at alpha 64 through VMware's encoding, and is masked out entirely through Cursor.
- **neatvnc 0.9.1's mask overflow.** Its `cursor_encode` reserves `(width*height+7)/8` bytes for
  the mask but writes `(width+7)/8` a row, past the buffer unless the width is a multiple of 8. It
  crashed beanjar-vnc ("double free or corruption") on LG's 38-pixel cursor. Fixed in 1.0
  (upstream fc853b8); against 0.9 beanjar-vnc pads the cursor's width to a multiple of 8 with
  transparent columns.
- **noVNC's colours.** noVNC (1.7.0 and master) decodes a Cursor's pixels as BGRX in
  `core/rfb.js` `_handleCursor` ("Decode from BGRX pixels"), though the pixel format it asks for is
  RGBX (red-shift 0) and its raw decoder reads RGBX: it shows red and blue swapped (WT1H's red
  pointer came out blue). Without the alpha cursor, the WebSocket listener's clients are noVNC, so
  that listener's cursor has red and blue swapped beforehand; another client over WebSocket sees
  them swapped, and noVNC on the local socket (the web viewer) sees them swapped. VMware's cursor
  has no such bug, so with the alpha cursor nothing is swapped.

Measured 2026-10-12, LG's cursors (all on a 1920x1080 UI screen), and what beanjar-vnc sends in
its default 1280x720 desktop with `--native-cursor`:

| Release | LG's arrow | Sent | Appearance |
|---|---|---|---|
| WT1H afro (PC, TCG) | 60x85, visible 57x71 at 2,7, hotspot 9,9 | 40x48, hotspot 4,1 (0.9: 40 is a multiple of 8) | grow-in on an 80x95 buffer (hotspot 12,11): 4 frames, then the arrow; 5 updates, 1 more when it hides |
| W22O queue, webOS 26 (Pi, KVM) | 60x85, visible 50x50 at 3,7, hotspot 9,9 | 33x34, hotspot 4,1 | grow-in on 80x95: 3 frames, then the arrow; 4 updates |

Clients:

- **TigerVNC 1.15** (FLTK 1.3 with Xcursor) shows the cursor at the size sent, at its pointer. It
  has no desktop scaling, so nothing to scale with. It hides its own pointer for a transparent
  cursor (`AlwaysCursor` off by default). With pointer moves and frame requests over several
  seconds, a client received the cursor once (2 updates: the hidden one at start, then the arrow).
- **noVNC 1.7.0** (headless Chromium 154, `vnc_lite.html`, `scale=true`) sets a CSS cursor on its
  canvas (`style.cursor = url(data:...) x y, default`): no canvas fallback outside touch devices.
  The cursor is not scaled with the picture: with the 1280x720 desktop shown at 756x425 the cursor
  stayed 40x48, so it looks larger relative to the picture (an upstream noVNC limitation).
  The composed default scales with the picture there.

## Not in version 1

- Audio as an RFB extension: noVNC and the other clients have none worth using (QEMU's audio
  pseudo-encoding is QEMU's own), so audio is the separate `--audio-local` socket above.
- Use beside the GTK viewer: both front ends connect to QEMU's single-client chardevs (cursor,
  video, IR, media, capture). The manager refuses `vnc` while a viewer is open, and `view` while
  beanjar-vnc runs.
- A second display client for screenshots is not needed: beanjar-vnc answers `beanjar screenshot`
  as `org.beanjar.Display1` on the VM's bus like the GTK viewer ([viewer](viewer.md#screenshots-from-a-script)), with
  LG's cursor in the picture even with `--native-cursor`, at the size asked, not `--size`.
- TLS (VeNCrypt): no certificate is configured; RSA-AES already encrypts the session.
- A persistent RSA key (`nvnc_set_rsa_creds`): the key is generated per start.
- Authentication for the WebSocket listener (see above).
- Rate limiting of failed passwords: each is logged (`authentication failed`), nothing more.

## Counters

At exit beanjar-vnc prints its counters and the core's. On `SIGUSR1` it prints, and goes on, one
line for a measurement to take before and after a run and difference:

```
beanjar-vnc: stats t_us=... updates=... scanouts=... copies=... reply_us=... reply_max_us=...
  skipped=... renders=... unchanged=... render_us=... feeds=... video_units=... video_nv12=...
  rgb_reads=... rgb_rows=... thumbnails=... thumbnails_cached=... thumbnail_us=... cache_w=...
  cache_h=... recopies=... fps=... frame_main_us=... frame_encode_us=...
```

(one line in the log). `t_us`: the monotonic clock. `updates`: QEMU's `UpdateDMABUF` calls, one
per guest frame it hands over (the guest's flush); `scanouts`: its `ScanoutDMABUF` calls (a
flip to another buffer: on the guests measured, one per frame too); `copies`: the guest's
buffer copied into the core's cache. `reply_us` and `reply_max_us`: how long QEMU waited for the
updates' answers in all and at most, from the call's dispatch here to its answer (the copy's
fence), during which it holds the guest's GL (`graphic_hw_gl_block`); QEMU does not wait for
`ScanoutDMABUF`. `skipped`: updates acknowledged without a copy while idle. `renders`: frames
composed; `unchanged`: of those, the ones dropped ([Unchanged frames](#unchanged-frames));
`render_us`: the GBM renders' time from submission to their fence. `feeds`: frames fed to
neatvnc. `video_units`: units sent on the video socket; `video_nv12`: frames converted to NV12 on
the GPU for them ([RGB to YUV on the GPU](#rgb-to-yuv-on-the-gpu)); `rgb_reads`: frames read back
as RGB ([Read backs](#read-backs)), `rgb_rows` the rows they read. `thumbnails`: thumbnails made,
`thumbnails_cached`: answered from the last one, `thumbnail_us`: making them
([Thumbnails](#thumbnails)).

## Build and packaging

`host/vnc/meson.build` builds it against the private neatvnc 1.0, aml 1.0 and FFmpeg (the
prebuilt trees of `packaging/vendor.toml`, docs/dev/guides/build.md, "The prebuilt programs") when
`opus` is found (`libopus-dev`, in `beanjar-build-deps-host`), and publishes
`build/vnc/beanjar-vnc`, whose run path finds the libraries in `build/neatvnc` and `build/ffmpeg`.
Without opus it warns and skips the program. It links libavcodec, libavutil and libswscale for
[video](#video), and also libavfilter, which only libneatvnc uses: libneatvnc's own run path is
its directory, so each FFmpeg library it needs must already be loaded from the private copy, or
the dynamic linker would take Debian's. `packaging/debian/build.py` makes
`beanjar-vnc_<version>_<arch>.deb`: `/usr/bin/beanjar-vnc` with run path `/usr/lib/beanjar/viewer`,
`/usr/lib/beanjar/neatvnc/lib` and `/usr/lib/beanjar/ffmpeg/lib`, and libneatvnc and libaml in
`/usr/lib/beanjar/neatvnc/lib`, depending on `beanjar (= <version>)`, which ships the FFmpeg, and
on its own libraries (dpkg-shlibdeps: `libopus0`, `libpixman-1-0`, `libegl1`, `libglib2.0-0t64`,
`libgbm1`, ...) and neatvnc's (its `BUILDINFO`: GnuTLS, nettle, GMP, libjpeg-turbo, ...).
The host lane that packages must therefore have built it.

## Tests

- `check-vnc-keysym`: the keysym map.
- `check-vnc`: against `tests/fakes/fake-qemu.py`. The composed default runs twice, with the
  default GL (D3D12 required where `/dev/dxg` is and the environment does not choose) and with
  `--gl-adapter software` (llvmpipe), each frame matching the core's and its GL line checked. It renders the reference picture with
  viewer-headless at 960x540, then reads a raw frame from beanjar-vnc and requires it to match.
  It sends Super and "a" as keysyms, Volume Up as a QEMU extended key event, then a move, a click
  and a wheel click, and finds each in fake-qemu's input log (IR Home and Volume Up, keyboard 30,
  tablet 600,400, buttons 0 and 3). With vncdotool installed it also checks a vncdotool capture.
  Over the `--websocket` listener, with a minimal WebSocket client that offers no subprotocol as
  noVNC does, it checks a frame, a key, and the cursor with red and blue swapped.
  By default the frame must match the core's picture with the cursor, and no VNC cursor is sent;
  with `--native-cursor` the frames must match the picture without it.
  A client that asks for the Cursor pseudo-encoding gets LG's cursor once, scaled like the picture,
  and no further update over pointer moves, frame requests and the guest's unchanged heartbeats; a
  padded cursor (fake-qemu's `padded`) arrives cropped with its hotspot moved, a hidden one as a
  transparent 1x1, and `vnc_cursors=` counts exactly those. In a 2880x1620 desktop a 300x360
  cursor (`large`) arrives shrunk to 128 high.
  With `--password-file` and a small RSA-AES client of its own (pycryptodomex, the client side
  of the handshake above), it checks that only RSA-AES is offered, that a wrong password is refused
  over both types, and that the right one gets a matching frame and a key through.
  `--local` beside them offers None alone, has mode 0600, and gets a matching frame and a key
  through; both unix sockets are gone after exit. It checks the
  refusals: `--websocket` with a password, `0.0.0.0` without one, and a port another socket holds.
  Audio, against a second fake-qemu with `--audio` (a 1 kHz sine at -12 dBFS, 44.1 kHz, written on
  QEMU's timing): viewer-headless's `--audio-wav` records it unchanged (the GTK viewer's path);
  `--audio-local` has mode 0600; `pcm` brings the header and the tone at 48 kHz (frequency by
  zero crossings, RMS within 1.5 dB) at 192000 bytes a second; `opus` brings about 50 packets a
  second that libopus (through ctypes, `tests/lib/audiostream.py`) decodes to the tone; both at
  once each get theirs beside a client that never reads, which is disconnected; `flac` is refused;
  SIGTERM removes the socket.
- `check-vnc-video` (`tests/integration/vnc/check-video.py`): `--video-local` with x264 against fake-qemu
  `--animate` (a noise band redrawn every 33 ms while a file says so): mode 0600; the header; a
  keyframe with SPS, PPS and an IDR first; a still picture at 2 to 10 units in 2.5 s; a busy one at
  about the frame rate; every unit decoded by ffmpeg (`tests/lib/videostream.py`) to fake-qemu's
  picture (the white box, the blue around it); a second client's keyframe within 0.5 s, the first
  client getting it too; a client that stops reading for 1.5 s at 20 Mbit/s resuming at a keyframe
  with a stream ffmpeg decodes without an error; a client that never reads disconnected while
  another keeps its frame rate; bad requests (`jpeg` among them) refused; the encoder closed with
  the last client; the counters. Then NVENC (`--video-encoder nvenc`) and VA-API (`--video-encoder vaapi`, with
  `--vaapi-adapter $BEANJAR_TEST_VAAPI_ADAPTER` when that is set), each skipped where it does not
  open: its header and a stream, the noise band moving for a second, decoded to the same picture.
  Then the colours, at an odd size (643x361, coded 642x360, NV12 rows padded to 644 bytes):
  fake-qemu `--bars` (ten flat colour bars) through x264 and VA-API (where it opens), each with the
  GPU conversion (the opening line says so and frames are converted) and with swscale
  (`BEANJAR_VNC_SWSCALE=1`); the decoded Y, U and V in the middle of each bar must be within 3 code
  values of the bar's colour in BT.601 limited range, and the largest difference of each run is
  printed. Skipped (77) without ffmpeg or without `--video-local`.
- `check-vnc-thumbnail` (`tests/integration/vnc/check-thumbnail.py`): `Screenshot` calls at 384x216 against
  fake-qemu `--animate` at 1280x720: with no client a P6 of the picture (the white box, the blue
  around it), made once; asked again unchanged, the same bytes from the cache; with the top quarter
  changing a new one, its noise averaged (the rows' standard deviation under 30, where one sample
  a pixel gives 37 to 74); beside an RFB client, made from its frames; at the desktop's size the
  core's own screenshot; with beanjar-vnc gone `org.beanjar.Display1` has no owner and the
  headless viewer gives the picture. Then a GL guest (fake-qemu `--dmabuf`) that draws only after
  beanjar-vnc's first render, with no client, with memory and with GBM frames: its updates are
  acknowledged without a copy, yet `Screenshot` at 384x216 and 1280x720 gives the picture; and
  after a client saw one frame and left, the frame the guest drew while idle, not the client's,
  with QEMU's longest wait unchanged (skipped without a render node).
- `check-vnc-damage` (`tests/integration/vnc/check-damage.py`): an RFB client on `--local` taking Raw
  rectangles (`tests/lib/rfbclient.py`, which draws them into its own framebuffer) against
  fake-qemu `--animate --bar` (a grey band of 20 rows moving down, only its rows updated): while
  it moves, every picture the client holds is fake-qemu's picture with exactly one band, and the RGB
  read backs take at most a third of the rows on average (`rgb_rows`); paused
  (beanjar-web's marker) while an x264 video client streams, frames are composed but none is read
  back as RGB (`rgb_reads`) nor sent to the paused client, and once resumed its pictures are again
  the picture with one band; stopped, the client's framebuffer and a new client's first frame are
  fake-qemu's picture, within 8 in each channel (V3D blends the box's edge pixels a little).
- `check-vnc-rate` (`tests/integration/vnc/check-rate.py`): fake-qemu `--dmabuf` at 60 frames a second
  (GBM buffers on the first render node, each update's answer awaited), beanjar-vnc and an RFB
  client on `--local`; prints the rate of every stage as JSON (`--seconds`, `--rate`, `--size`,
  `--out`, `--client none` for options) and passes when frames reached the client. Skipped (77)
  without a render node or a libgbm that makes buffers (the WSL PC).
- `check-vnc-idle` (`tests/integration/vnc/check-idle.py`): fake-qemu `--animate` (a noise band written into a
  shared surface, `UpdateMap` every 33 ms) with no client: updates skipped, not copied; an RFB
  client on the local socket gets the band within 0.5 s; idle again after it, a
  `Display1.Screenshot` composed on demand is up to date; a video client's keyframe within 0.5 s
  with the band; the counters (`skipped`, `frames`, `vnc_frames` not grown for video alone). All
  of it twice: the band in a shared surface, then in 2D `Update` messages (fake-qemu `--2d`).
  Then, with the media-helper and ffmpeg, a clip through `tests/fakes/fake-media.py` into a video
  window: in 7 s idle about 150 pictures picked, none uploaded (`shown=0`), then drawn for an RFB
  client, none dropped late. That step composes with llvmpipe (`--gl-adapter software`): on
  d3d12 the first frame with a video window takes 20 to 30 ms, and now and then drops a picture.
- `check-vnc-pipe` (`tests/integration/vnc/check-pipe.py`): the manager's pipes, `vnc audio-pipe` in both
  formats and `vnc video-pipe` (a header and a keyframe first) included.
  vncdotool must not offer the extended key event pseudo-encoding: neatvnc answers the first
  update request with that pseudo-rectangle alone, and vncdotool discards such an update and waits
  forever.
- `test_manager.py`'s `test_vnc_start_stop`: the command line, the reported addresses and ports (also
  given after the name), a held port named with its process, the refusal off loopback without a
  password, no WebSocket listener with one, the exclusion with the viewer, `stop` ending it, and
  the password kept across `vnc stop`, `stop` and a running beanjar-vnc's change. `test_vnc_password`:
  made at first use, `show`, `set` from stdin and a file, the refusals, `reset`;
  `test_new_identity` and `test_wipe`: a new password and the same one.

Measured on the x86 PC (2026-10-10, llvmpipe): the test clip through `tests/fakes/fake-media.py`
and the real media-helper, captured with vncdotool two seconds apart with `--no-osd`, showed the
clip's frames 161 and 212 in its video window.

Measured 2026-10-12, TigerVNC 1.15 (Debian trixie) with `RA2_256`: on the PC against the stand-in
display (neatvnc 0.9.1), and from the PC over the LAN to the Pi (neatvnc 1.0.3) serving a running
W22O VM on `--listen 0.0.0.0 --port 5901`. In both, a wrong password was refused ("Invalid username
or password" from 0.9.1, "wrong password" from 1.0.3), and the right one got the picture and keys
(on the Pi a Shift press woke the TV's screen-off notice). `beanjar vnc start` with the default ports on
the Pi named the desktop's wayvnc listener on `*:5900`. The VM's loopback listener without a
password worked through an SSH tunnel with security type None.

Measured 2026-10-14, TigerVNC 1.15 (Debian trixie) under Xvfb against the stand-in display (neatvnc
0.9.1), the dialogs answered with xdotool: a 24-character random password got in typed into the
Password field (with a user name) and with `VNC_USERNAME` (empty) and `VNC_PASSWORD`; typed into
the Username field it was refused as "Invalid username or password", the log naming the password
as the user; with `-passwd`, a plain file or a `vncpasswd`-obfuscated one, TigerVNC ignored the
file and showed the Username and Password dialog. So a password that "does not work" in TigerVNC is
most likely in the wrong field or given with `-passwd`; the old random passwords
(`secrets.token_urlsafe`, with `-`, `_`, `l`, `I`, `0`, `O`) were also easy to mistype.

Measured 2026-10-15, noVNC 1.6.0 through a temporary LAN websockify 0.12.0 frontend
to the Pi's running US W22O queue VM: the browser connected at 1280x720 with
`quality=3&compression=2` and displayed live video. A temporary counter around
noVNC's Tight decoder `_jpegRect` counted 567 completed JPEG rectangles in a
three-second observation; this counts rectangles, not frames. The user reported
that playback worked well. The endpoint, staged tools and temporary processes
were removed afterward; the restored normal VNC listener offered RSA-AES types
129 and 5, with no unauthenticated type.

When a temporary frontend is supervised by a systemd unit, restore normal VNC
outside that unit after stopping it. A replacement started by the supervisor's
exit handler remains in its cgroup and can be killed during the unit's shutdown.
