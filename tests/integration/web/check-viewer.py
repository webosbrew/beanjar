#!/usr/bin/env python3
# SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""PC-only viewer session check: headless Chromium, fake QEMU and the fake manager.
Real RFB, Opus/PCM and H.264 streams; test code closes individual browser sockets.
Nothing connects to a VM or an installed web service. Prints measured states and pass/fail.
"""

import asyncio
import base64
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path

try:
    import aiohttp
except ImportError:
    print("python3-aiohttp is not installed: skipped")
    sys.exit(77)

TOP = Path(__file__).resolve().parents[3]
VNC = sys.argv[1]
CHROME = shutil.which("chromium")
if not CHROME:
    print("chromium is not installed: skipped")
    sys.exit(77)
processes, logs = [], []


def start(name, args, env=None):
    log = open(OUT / (name + ".log"), "w")
    logs.append(log)
    p = subprocess.Popen(
        args, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True
    )
    processes.append(p)
    return p


async def until(check):
    end = time.monotonic() + 15
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        await asyncio.sleep(0.1)
    raise AssertionError("wait timed out")


class CDP:
    def __init__(self, ws):
        self.ws, self.id = ws, 0

    async def cmd(self, method, **params):
        self.id += 1
        ident = self.id
        await self.ws.send_json({"id": ident, "method": method, "params": params})
        while True:
            msg = await asyncio.wait_for(self.ws.receive_json(), 20)
            if msg.get("id") == ident:
                assert "error" not in msg, msg
                return msg.get("result", {})

    async def js(self, expression):
        r = await self.cmd(
            "Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True
        )
        assert "exceptionDetails" not in r, r
        return r["result"].get("value")

    async def wait(self, expression):
        for _ in range(150):
            try:
                if await self.js(expression):
                    return
            except AssertionError:
                pass  # the page is not there yet
            await asyncio.sleep(0.1)
        print("browser status:", await self.js("document.body.innerText"), flush=True)
        raise AssertionError(expression)


async def checks(tmp):
    start(
        "fake",
        ["/usr/bin/python3", str(TOP / "tests/fakes/fake-qemu.py"), tmp, "--audio", "--animate"],
    )
    await until(lambda: Path(tmp, "ready").exists())
    Path(tmp, "animate").touch()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    vnc = start(
        "vnc",
        [
            VNC,
            f"unix:path={tmp}/dbus.sock",
            "--listen",
            "127.0.0.1",
            "--port",
            str(port),
            "--local",
            tmp + "/rfb-local.sock",
            "--audio-local",
            tmp + "/audio-local.sock",
            "--video-local",
            tmp + "/video-local.sock",
            "--video-encoder",
            "x264",
            "--size",
            "640x360",
            "--no-media",
            "--name",
            "tv",
        ],
    )
    await until(lambda: "beanjar-vnc: ready" in (OUT / "vnc.log").read_text())
    env = dict(os.environ, FAKE_DIR=tmp, FAKE_VNC_PID=str(vnc.pid), PYTHONDONTWRITEBYTECODE="1")
    start(
        "web",
        [
            str(TOP / "host/web/bin/beanjar-web"),
            "--port",
            "0",
            "--auth",
            "none",
            "--beanjar",
            "/usr/bin/python3 " + str(TOP / "tests/fakes/fake-beanjar.py"),
        ],
        env,
    )
    url = await until(lambda: re.search(r"http://127.0.0.1:\d+", (OUT / "web.log").read_text()))
    url = url.group()
    assert not url.endswith(":8059")
    profile = OUT / "profile"
    if profile.exists():
        shutil.rmtree(profile)
    start(
        "chromium",
        [
            CHROME,
            "--headless",
            "--mute-audio",
            "--autoplay-policy=no-user-gesture-required",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--disable-background-networking",
            "--blink-settings=primaryHoverType=2,availableHoverTypes=2,primaryPointerType=4,availablePointerTypes=4",
            "--remote-debugging-port=0",
            "--user-data-dir=" + str(profile),
            "about:blank",
        ],
    )
    await until(lambda: (profile / "DevToolsActivePort").exists())
    debug = "http://127.0.0.1:" + (profile / "DevToolsActivePort").read_text().splitlines()[0]
    async with aiohttp.ClientSession() as session:
        tabs = await (await session.get(debug + "/json/list")).json()
        async with session.ws_connect(tabs[0]["webSocketDebuggerUrl"]) as ws:
            c = CDP(ws)
            await c.cmd("Page.enable")
            await c.cmd(
                "Emulation.setDeviceMetricsOverride",
                width=1100,
                height=800,
                deviceScaleFactor=1,
                mobile=False,
            )
            await c.cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                source="""
              window.testSockets = [];
              const OriginalWebSocket = window.WebSocket;
              window.WebSocket = new Proxy(OriginalWebSocket, {
                construct(Target, args) { const ws = new Target(...args); window.testSockets.push(ws); return ws; }
              });
              localStorage.setItem('beanjar-web-picture', 'h264');
              localStorage.setItem('beanjar-web-sound', 'off');
              window.audioFrames = []; window.audioTimestamps = [];
              if (location.search.includes('pcm-fallback')) {
                window.AudioDecoder = undefined;
                const Context = window.AudioContext;
                window.numericRejected = 0;
                window.AudioContext = new Proxy(Context, {construct(Target, [opts]) {
                  if (typeof opts.latencyHint === 'number') {
                    numericRejected++; throw new TypeError('numeric hints unsupported');
                  }
                  const ctx = new Target(opts);
                  Object.defineProperty(ctx, 'outputLatency', {value: undefined});
                  return ctx;
                }});
              } else {
                const Decoder = window.AudioDecoder;
                window.AudioDecoder = new Proxy(Decoder, {construct(Target, [opts]) {
                  const output = opts.output;
                  const dec = new Target({...opts, output(d) { audioFrames.push(d.numberOfFrames); output(d); }});
                  const decode = dec.decode.bind(dec);
                  dec.decode = chunk => {audioTimestamps.push(chunk.timestamp); decode(chunk);};
                  return dec;
                }});
              }
            """,
            )
            await c.cmd("Page.navigate", url=url + "/vm/local/tv/")
            await c.wait(
                'document.querySelector("#status").hidden && document.querySelector("#screen div canvas")?.width > 0'
            )
            await c.wait('!document.querySelector("#h264-choice input").disabled')
            assert await c.js("document.querySelector('#stats-overlay').hidden")
            assert await c.js(
                "!document.querySelector('#quality-pop #picture-stats, #quality-pop #audio-stats')"
            )
            await toolbar_checks(c)
            await planes_checks(c)
            await lite_checks(c, tmp)
            await c.js(
                "document.querySelector('#more-button').click(); document.querySelector('#stats-button').click()"
            )
            assert await c.js(
                "!document.querySelector('#stats-overlay').hidden && document.querySelector('#audio-stats').textContent === 'Sound off'"
            )
            assert await c.js(
                "document.querySelector('#stats-button').getAttribute('aria-pressed') === 'true'"
            )
            await c.js('document.querySelector("#sound").click()')
            await c.wait(
                "window.beanjarSpeaker?.stats.worklet.played > 0 && window.beanjarPicture?.stats.drawn > 5"
            )
            print(
                "media started:",
                await c.js(
                    "({audio:beanjarSpeaker.stats.messages,played:beanjarSpeaker.stats.worklet.played,video:beanjarPicture.stats.drawn})"
                ),
                flush=True,
            )
            await c.wait(
                "audioFrames.length > 50 && document.querySelector('#audio-stats').textContent.includes('target')"
            )
            codec = await c.js(
                "({frames:[...new Set(audioFrames)],steps:[...new Set(audioTimestamps.slice(1).map((t,i)=>t-audioTimestamps[i]))],base:beanjarSpeaker.ctx.baseLatency,output:beanjarSpeaker.ctx.outputLatency,rate:beanjarSpeaker.ctx.sampleRate})"
            )
            assert codec["frames"] == [480] and codec["steps"] == [10000], codec
            print("PASS: Chromium WebCodecs decodes 10 ms Opus; context:", codec, flush=True)
            adaptive = await c.js("""(async()=>{
              const source = await (await fetch('/audio-worklet.js')).text();
              return new Function(`let currentFrame=0; const sampleRate=48000;
                let Processor;
                class AudioWorkletProcessor {constructor(){this.port={postMessage(){}};}}
                function registerProcessor(_name, value){Processor=value;}
                ${source}
                const p=new Processor();
                const push=n=>p.port.onmessage({data:{l:new Float32Array(n).fill(.25),r:new Float32Array(n).fill(-.25)}});
                const tick=()=>{const l=new Float32Array(128),r=new Float32Array(128);p.process([],[[l,r]]);currentFrame+=128;return [l,r];};
                push(1920); tick();
                const initial=p.target;
                for(let i=0;i<20;i++)tick();
                const grown=p.target, unders=p.stats.underruns;
                for(let i=0;i<20;i++)tick();
                const waitingUnders=p.stats.underruns;
                push(grown);
                let maxError=0;
                let earlyTarget;
                for(let i=0;i<4200;i++) {
                  if(i===3749)earlyTarget=p.target;
                  push(128);const out=tick();
                  for(let j=0;j<128;j++)maxError=Math.max(maxError,Math.abs(out[0][j]-.25),Math.abs(out[1][j]+.25));
                }
                const shrunk=p.target, queued=p.count, steadyUnders=p.stats.underruns;
                for(let i=0;i<20;i++) {
                  push(p.target);
                  for(let j=0;j<80;j++)tick();
                }
                const capped=p.target;
                push(60000);
                return {initial,grown,unders,waitingUnders,earlyTarget,shrunk,queued,steadyUnders,maxError,capped,cutback:p.count,skips:p.stats.skips};
              `)();
            })()""")
            assert adaptive["initial"] == 1920 and adaptive["grown"] == 2880, adaptive
            assert (
                adaptive["unders"] == adaptive["waitingUnders"] == adaptive["steadyUnders"] == 1
            ), adaptive
            assert (
                adaptive["earlyTarget"] == 2880
                and adaptive["shrunk"] == 2640
                and adaptive["queued"] < 2880
            ), adaptive
            assert adaptive["maxError"] < 0.00001 and adaptive["capped"] == 7680, adaptive
            assert adaptive["cutback"] == adaptive["capped"] and adaptive["skips"] > 0, adaptive
            print(
                "PASS: adaptive buffer grows, holds while priming, shrinks after steady delivery and caps:",
                adaptive,
                flush=True,
            )
            readout = await c.js("document.querySelector('#audio-stats').textContent")
            for label in (
                "48\u00a0kHz",
                "Buffered",
                "target",
                "Browser base",
                "output",
                "Underruns",
            ):
                assert label in readout, readout
            await c.wait(
                "document.querySelector('#picture-stats').textContent.includes('fps') && document.querySelector('#picture-stats').textContent.includes('Mbit/s')"
            )
            await c.js(
                "document.querySelector('#more-button').click(); document.querySelector('#quality-button').click()"
            )
            assert await c.js(
                "!document.querySelector('#quality-pop').textContent.includes('Buffered') && !document.querySelector('#quality-pop').textContent.includes('fps')"
            )
            await c.js("document.querySelector('#quality-pop').hidePopover()")
            await screenshots(c)
            await remote_icons(c, session, debug)
            await c.js(
                "document.querySelector('#stats-close').click(); window.statsMutations=0; window.statsObserver=new MutationObserver(records=>statsMutations+=records.length);statsObserver.observe(document.querySelector('#stats-overlay'),{childList:true,subtree:true,characterData:true})"
            )
            await asyncio.sleep(2.2)
            assert await c.js(
                "document.querySelector('#stats-overlay').hidden && statsMutations === 0 && localStorage.getItem('beanjar-web-stats') === 'off' && document.querySelector('#stats-button').getAttribute('aria-pressed') === 'false'"
            )
            await c.js(
                "statsObserver.disconnect();document.querySelector('#more-button').click();document.querySelector('#stats-button').click()"
            )
            assert await c.js("localStorage.getItem('beanjar-web-stats') === 'on'")
            print(
                "PASS: stats toggle, close, sound-off placeholder, video/audio values; no text updates while closed; Quality contains only controls",
                flush=True,
            )
            await c.js(
                "window.oldSpeaker=beanjarSpeaker;window.oldPicture=beanjarPicture;window.oldRfb=testSockets.find(w=>new URL(w.url).pathname.endsWith('/rfb')&&w.readyState===1);oldRfb.close(4001,'test RFB drop')"
            )
            await c.wait('!document.querySelector("#status").hidden')
            await asyncio.sleep(0.5)
            state = await c.js(
                "({text:document.querySelector('#status').textContent,audioContext:oldSpeaker.ctx?.state||null,videoStopped:oldPicture.stopped,open:testSockets.filter(w=>/\\/(audio|video)(\\?|$)/.test(new URL(w.url).pathname)&&w.readyState===1).length})"
            )
            print("RFB dropped:", state, flush=True)
            assert state["audioContext"] is None and state["videoStopped"] and state["open"] == 0, (
                state
            )
            count = await c.js("testSockets.length")
            await asyncio.sleep(3.3)
            assert await c.js("testSockets.length") == count, (
                "media reconnected while RFB was disconnected"
            )
            assert await c.js(
                "testSockets.filter(w=>/\\/(audio|video)$/.test(new URL(w.url).pathname)).every(w=>w.readyState === 3)"
            ), "old media sockets did not close"
            await c.js('document.querySelector("#status button").click()')
            await c.wait(
                'document.querySelector("#status").hidden && window.beanjarSpeaker !== window.oldSpeaker && window.beanjarSpeaker?.stats.messages > 5 && window.beanjarPicture !== window.oldPicture && window.beanjarPicture?.stats.drawn > 5'
            )
            print(
                "PASS: RFB drop stops both media streams, neither retries until Reconnect; all three return",
                flush=True,
            )
            await c.js(
                "window.currentRfb=testSockets.find(w=>new URL(w.url).pathname.endsWith('/rfb')&&w.readyState===1);window.audioSocket=beanjarSpeaker.ws;window.audioMessages=beanjarSpeaker.stats.messages;window.videoFrames=beanjarPicture.stats.drawn;audioSocket.close(4001,'test audio drop')"
            )
            await c.wait(
                "window.beanjarSpeaker?.ws?.readyState === 1 && beanjarSpeaker.ws !== window.audioSocket && beanjarSpeaker.stats.messages > window.audioMessages + 5"
            )
            assert await c.js(
                'document.querySelector("#status").hidden && currentRfb.readyState === 1 && beanjarPicture.stats.drawn > window.videoFrames'
            )
            print(
                "PASS: audio-only drop retries quietly; RFB stays open and video frames increase",
                await c.js(
                    "({audio:beanjarSpeaker.stats.messages,played:beanjarSpeaker.stats.worklet.played,video:beanjarPicture.stats.drawn})"
                ),
                flush=True,
            )
            await c.js(
                "window.failedPicture=beanjarPicture;window.audioMessages=beanjarSpeaker.stats.messages;failedPicture.ws.close(4001,'test video drop')"
            )
            await c.wait(
                "window.beanjarPicture && beanjarPicture !== window.failedPicture && beanjarPicture.stats.drawn > 5"
            )
            assert await c.js(
                'document.querySelector("#status").hidden && currentRfb.readyState === 1 && beanjarSpeaker.stats.messages > window.audioMessages'
            )
            print(
                "PASS: video-only drop retries quietly with VNC fallback; RFB and audio stay open",
                await c.js(
                    "({audio:beanjarSpeaker.stats.messages,played:beanjarSpeaker.stats.worklet.played,video:beanjarPicture.stats.drawn})"
                ),
                flush=True,
            )
            await c.cmd("Page.navigate", url=url + "/vm/local/tv/?pcm-fallback")
            await c.wait("document.querySelector('#status')?.hidden")
            assert await c.js(
                "!document.querySelector('#stats-overlay').hidden && document.querySelector('#stats-button').getAttribute('aria-pressed') === 'true'"
            )
            print("PASS: stats visibility persists across navigation", flush=True)
            await c.js("document.querySelector('#sound').click()")
            await c.wait(
                "beanjarSpeaker?.stats.worklet.played > 24000 && document.querySelector('#audio-stats').textContent.includes('unavailable')"
            )
            fallback = await c.js(
                "({format:beanjarSpeaker.format,hint:beanjarSpeaker.stats.latencyHint,rejected:numericRejected,played:beanjarSpeaker.stats.worklet.played,readout:document.querySelector('#audio-stats').textContent})"
            )
            assert (
                fallback["format"] == "pcm"
                and fallback["hint"] == "interactive"
                and fallback["rejected"] == 1
            ), fallback
            print(
                "PASS: PCM playback, rejected numeric hint and unavailable outputLatency:",
                fallback,
                flush=True,
            )
            await rotation_checks(c, tmp, url)
        await check_heartbeat(session, url)


async def rotation_checks(c, tmp, url):
    """A set that turns to portrait (its list row's portrait): the remote socket's "rotation N"
    turns noVNC's canvas (360 - N) % 360 clockwise, a finger and a click on the turned picture
    land on the panel's own pixels (the guest display's), and Rotate turns the set a quarter
    clockwise (beanjar sensor set rotate with the next setting)."""
    log = Path(tmp, "input.log")
    Path(tmp, "portrait").touch()
    Path(tmp, "rotation").write_text("rotation=90\n")
    await asyncio.sleep(3.2)  # the server's list (its portrait) is up to 3 s old
    try:
        await c.cmd("Page.navigate", url=url + "/vm/local/tv/")
        await c.wait(
            'document.querySelector("#status").hidden && document.querySelector("#screen div canvas")?.width > 0'
            " && window.beanjarRotation === 90"
        )
        await asyncio.sleep(0.3)
        state = await c.js("""(()=>{const c=document.querySelector('#screen div canvas');
          const r=c.getBoundingClientRect();return {transform:c.style.transform,w:r.width,h:r.height,
          button:!document.querySelector('#rotate').hidden};})()""")
        assert "rotate(270deg)" in state["transform"] and state["h"] > state["w"], state
        assert state["button"], state
        # the panel's point (fx, fy) of 1920x1080 on the page: turned 270 clockwise, the panel's
        # top-left corner is at the picture's bottom-left
        at = """((fx, fy) => {const c=document.querySelector('#screen div canvas');
          const r=c.getBoundingClientRect(), k=r.height/1920;
          return [r.left + fy*k, r.bottom - fx*k];})"""
        log.write_text("")
        x, y = await c.js(f"{at}(480, 270)")
        await c.cmd("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
        await c.cmd(
            "Input.dispatchTouchEvent", type="touchStart", touchPoints=[{"x": x, "y": y, "id": 3}]
        )
        await c.cmd("Input.dispatchTouchEvent", type="touchEnd", touchPoints=[])
        await c.cmd("Emulation.setTouchEmulationEnabled", enabled=False)
        await until(lambda: "MultiTouch.SendEvent 2 " in log.read_text())
        begin = next(
            l.split()
            for l in log.read_text().splitlines()
            if l.startswith("MultiTouch.SendEvent 0")
        )
        tx, ty = float(begin[3]), float(begin[4])
        assert abs(tx - 480) <= 4 and abs(ty - 270) <= 4, begin
        # a click: the tablet's position and the left button, not noVNC's mapping
        log.write_text("")
        x, y = await c.js(f"{at}(1440, 810)")
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            await c.cmd(
                "Input.dispatchMouseEvent", type=kind, x=x, y=y, button="left",
                buttons=1 if kind == "mousePressed" else 0, clickCount=1,
            )  # fmt: skip
        await until(lambda: "Mouse.Release" in log.read_text())
        lines = log.read_text().splitlines()
        pos = [l.split() for l in lines if l.startswith("Mouse.SetAbsPosition")]
        assert pos and all(
            abs(float(p[1]) - 1440) <= 4 and abs(float(p[2]) - 810) <= 4 for p in pos
        ), lines
        assert any(l.startswith("Mouse.Press 0") for l in lines), lines
        print(
            "PASS: rotation 90 turns the picture 270 clockwise; a finger and a click on it land at "
            f"the panel's {tx:g},{ty:g} and {pos[-1][1]},{pos[-1][2]}",
            flush=True,
        )
        # the Mouse toggle: in touch mode a tap on the turned picture is a finger's, mapped the same
        log.write_text("")
        await c.js("document.querySelector('#input-touch').click()")
        assert await c.js(
            "localStorage.getItem('beanjar-web-mouse') === 'touch' && "
            "document.querySelector('#input-touch').getAttribute('aria-checked') === 'true'"
        )
        x, y = await c.js(f"{at}(480, 270)")
        await mouse_drag(c, x, y, 0, 0)
        got = await touch_lines(log)
        assert [g[1] for g in got] == ["0", "2"], got
        assert abs(float(got[0][3]) - 480) <= 4 and abs(float(got[0][4]) - 270) <= 4, got
        assert not any(l.startswith("Mouse.Press") for l in log.read_text().splitlines())
        print(
            "PASS: in touch mode a click on the turned picture is a tap at the panel's "
            f"{float(got[0][3]):g},{float(got[0][4]):g}, no pointer button",
            flush=True,
        )
        await preview_checks(c, url, tmp, "90", "rotate(270deg)")
        rotate_calls = lambda: [
            l.rsplit(" ", 1)[1]
            for l in Path(tmp, "calls.log").read_text().splitlines()
            if " rotate --json local/tv " in l or l.startswith("rotate --json local/tv ")
        ]
        assert (
            await c.js("document.querySelector('#rotate').getAttribute('aria-checked')") == "true"
        )
        await c.js("document.querySelector('#rotate').click()")
        await c.wait("window.beanjarRotation === 0 && !document.querySelector('#rotate').disabled")
        assert rotate_calls()[-1] == "0", rotate_calls()
        assert await c.js("document.querySelector('#screen div canvas').style.transform === ''")
        assert (
            await c.js("document.querySelector('#rotate').getAttribute('aria-checked')") == "false"
        )
        # landscape -> portrait (270) -> landscape
        await c.js("document.querySelector('#rotate').click()")
        await c.wait(
            "window.beanjarRotation === 270 && !document.querySelector('#rotate').disabled"
        )
        assert rotate_calls()[-1] == "270", rotate_calls()
        assert (
            await c.js("document.querySelector('#rotate').getAttribute('aria-checked')") == "true"
        )
        assert "rotate(90deg)" in await c.js(
            "document.querySelector('#screen div canvas').style.transform"
        )
        await c.js("document.querySelector('#rotate').click()")
        await c.wait("window.beanjarRotation === 0 && !document.querySelector('#rotate').disabled")
        assert rotate_calls()[-3:] == ["0", "270", "0"], rotate_calls()
        print(
            "PASS: Portrait (in More) toggles landscape and portrait: 90 -> 0, 0 -> 270 -> 0, "
            "aria-checked follows, picture upright at 0",
            flush=True,
        )
        await asyncio.sleep(5.2)  # the preview's picture is refreshed every 5 s
        await preview_checks(c, url, tmp, "0", None)
        await mouse_checks(c, tmp, url)
        await c.cmd("Page.reload")
        await c.wait(
            'document.querySelector("#status").hidden && document.querySelector("#screen div canvas")?.width > 0'
            " && !document.querySelector('#rotate').hidden"
        )
        await layout_shots(c)
    finally:
        Path(tmp, "portrait").unlink()
        Path(tmp, "rotation").unlink()


async def layout_shots(c):
    """BEANJAR_VIEWER_ARTIFACTS: the viewer of the rotating touch-screen set in the desktop, phone
    portrait and phone landscape layouts, its bar and then its More menu, for a person to judge."""
    dest = os.environ.get("BEANJAR_VIEWER_ARTIFACTS")
    if not dest:
        return
    for name, width, height in (("desktop", 1100, 800), ("phone-portrait", 412, 915),
                                ("phone-landscape", 844, 390)):  # fmt: skip
        await c.cmd(
            "Emulation.setDeviceMetricsOverride", width=width, height=height,
            deviceScaleFactor=1, mobile=name != "desktop",
        )  # fmt: skip
        await c.cmd("Emulation.setEmulatedMedia", features=[])
        await asyncio.sleep(0.6)
        for step in ("bar", "more", "input"):
            if step == "more":
                await c.js("document.querySelector('#more-button').click()")
            if step == "input":
                await c.js("document.querySelector('#viewer-more')?.hidePopover?.()")
                await c.js("document.querySelector('#remote').click()")
                if await c.js("!document.querySelector('#input-menu')"):
                    break  # before the merged control: nothing more to show
            await asyncio.sleep(0.5)
            result = await c.cmd("Page.captureScreenshot", format="png")
            (Path(dest) / f"viewer-{name}-{step}.png").write_bytes(base64.b64decode(result["data"]))
        await c.js(
            "for (const p of document.querySelectorAll('[popover]:popover-open')) p.hidePopover()"
        )
    await c.cmd("Emulation.clearDeviceMetricsOverride")
    await asyncio.sleep(0.6)


async def preview_checks(c, url, tmp, rotation, transform):
    """The VM list's preview of the portrait set: the thumbnail says the guest's rotation, the page
    turns the picture by it inside the card's 16:9 box (none at 0), and goes back to the viewer."""
    async with aiohttp.ClientSession() as session:
        for _ in range(12):  # a stale picture is served while a new one is taken
            async with session.get(url + "/vm/local/tv/screenshot.png?thumbnail=1") as r:
                assert r.status == 200, r.status
                if r.headers.get("X-Beanjar-Rotation") == rotation:
                    break
            await asyncio.sleep(0.5)
        else:
            raise AssertionError(dict(r.headers))
    await c.cmd("Page.navigate", url=url + "/")
    await c.wait(
        "(() => {const i = document.querySelector('.vm-card[data-spec=\"local/tv\"] img.preview');"
        " return !!i && !i.hidden && i.naturalWidth > 0})()"
    )
    got = await c.js("""(() => {
      const i = document.querySelector('.vm-card[data-spec="local/tv"] img.preview');
      const b = i.parentElement.getBoundingClientRect(), r = i.getBoundingClientRect();
      return {rotation: i.dataset.rotation, transform: getComputedStyle(i).transform,
        box: [b.width, b.height], rect: [r.width, r.height, r.left - b.left, r.top - b.top],
        fit: getComputedStyle(i).objectFit};})()""")
    assert got["rotation"] == rotation and got["fit"] == "contain", got
    dest = os.environ.get("BEANJAR_VIEWER_ARTIFACTS")
    if dest:
        result = await c.cmd("Page.captureScreenshot", format="png")
        (Path(dest) / f"list-rotation-{rotation}.png").write_bytes(base64.b64decode(result["data"]))
    if transform:
        m = got["transform"]
        assert m.startswith("matrix("), got
        a, b = (float(v) for v in m[7:-1].split(",")[:2])
        import math

        turned = round(math.degrees(math.atan2(b, a))) % 360
        want = int(transform[7:-4])
        assert turned == want, (turned, want, got)
        # the turned element fills the 16:9 box exactly: the portrait picture is as tall as it
        assert (
            abs(got["rect"][0] - got["box"][0]) < 2 and abs(got["rect"][1] - got["box"][1]) < 2
        ), got
        assert abs(got["rect"][2]) < 2 and abs(got["rect"][3]) < 2, got
    else:
        assert got["transform"] == "none", got
    print(
        f"PASS: the list's preview of a set reporting rotation {rotation} "
        + (f"is turned {transform} and fits its box" if transform else "is not turned"),
        flush=True,
    )
    await c.cmd("Page.navigate", url=url + "/vm/local/tv/")
    await c.wait(
        'document.querySelector("#status").hidden && document.querySelector("#screen div canvas")?.width > 0'
        f" && window.beanjarRotation === {rotation}"
    )
    await asyncio.sleep(0.3)


async def mouse_drag(c, x, y, dx, dy, button="left", steps=5):
    """A press at x, y, a drag by dx, dy in steps, a release, as a mouse's."""
    bit = {"left": 1, "middle": 4, "right": 2}[button]
    await c.cmd("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
    await c.cmd(
        "Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button=button, buttons=bit,
        clickCount=1,
    )  # fmt: skip
    for i in range(1, steps + 1 if dx or dy else 1):
        await c.cmd(
            "Input.dispatchMouseEvent", type="mouseMoved", x=x + dx * i / steps,
            y=y + dy * i / steps, button=button, buttons=bit,
        )  # fmt: skip
    await c.cmd(
        "Input.dispatchMouseEvent", type="mouseReleased", x=x + dx, y=y + dy, button=button,
        buttons=0, clickCount=1,
    )  # fmt: skip


async def touch_lines(log):
    """The MultiTouch.SendEvent calls once the last one ends a touch, split."""
    await until(lambda: "MultiTouch.SendEvent 2 " in log.read_text())
    await asyncio.sleep(0.3)
    return [l.split() for l in log.read_text().splitlines() if l.startswith("MultiTouch.")]


async def mouse_checks(c, tmp, url):
    """The Mouse toggle on the upright picture (a VM with a touch screen; rotation_checks left it
    on): kept per browser across a reload; a drag is a swipe (begin, moves, end in one slot), the
    right button still the pointer's; toggled back, a click is the pointer's left button."""
    log = Path(tmp, "input.log")
    await c.cmd("Page.reload")
    await c.wait(
        'document.querySelector("#status").hidden && document.querySelector("#screen div canvas")?.width > 0'
        " && document.querySelector('#remote').getAttribute('popovertarget') === 'input-menu'"
    )
    await asyncio.sleep(0.3)
    assert await c.js(
        "document.querySelector('#input-touch').getAttribute('aria-checked') === 'true' && "
        "document.querySelector('#input-touch .bi').style.getPropertyValue('--i').includes('hand-index')"
    ), "the touch mode was not kept"
    box = """(()=>{const c=document.querySelector('#screen div canvas'), r=c.getBoundingClientRect();
      return [r.x, r.y, r.width, r.height, c.width, c.height];})()"""
    bx, by, bw, bh, fw, fh = await c.js(box)
    log.write_text("")
    await mouse_drag(c, bx + bw * 0.25, by + bh * 0.5, bw * 0.25, 0)
    got = await touch_lines(log)
    kinds = [g[1] for g in got]
    assert kinds[0] == "0" and kinds[-1] == "2" and kinds.count("1") >= 3, got
    assert len({g[2] for g in got}) == 1, got
    # beanjar remote pipe scales noVNC's framebuffer (fw x fh) to the guest's 1920x1080 display
    assert abs(float(got[0][3]) - 1920 * 0.25) <= 8 and abs(float(got[0][4]) - 540) <= 8, got
    assert abs(float(got[-1][3]) - 1920 * 0.5) <= 8, got
    assert not any(l.startswith("Mouse.Press") for l in log.read_text().splitlines())
    print(
        f"PASS: kept per browser; in touch mode a drag is a swipe ({kinds.count('1')} moves, slot "
        f"{got[0][2]}, {float(got[0][3]):g},{float(got[0][4]):g} to {float(got[-1][3]):g}), "
        f"no pointer button (framebuffer {fw}x{fh})",
        flush=True,
    )
    log.write_text("")
    for fx in (0.3, 0.4, 0.6):  # a finger can't hover: no button held, nothing sent
        await c.cmd("Input.dispatchMouseEvent", type="mouseMoved", x=bx + bw * fx, y=by + bh * 0.4)
    await asyncio.sleep(0.5)
    assert log.read_text().strip() == "", log.read_text()
    await mouse_drag(c, bx + bw * 0.5, by + bh * 0.5, 0, 0, button="right")
    await until(lambda: "Mouse.Release 2" in log.read_text())
    lines = log.read_text().splitlines()
    assert "Mouse.Press 2" in lines and not any(l.startswith("MultiTouch.") for l in lines), lines
    at = [l.split() for l in lines if l.startswith("Mouse.SetAbsPosition")]
    assert at and abs(int(at[0][1]) - 1920 * 0.5) <= 8 and abs(int(at[0][2]) - 540) <= 8, lines
    print(
        f"PASS: in touch mode hovering sends nothing; a right click is the pointer's at {at[0][1]},{at[0][2]}",
        flush=True,
    )
    await c.js("document.querySelector('#input-touch').click()")
    assert await c.js(
        "localStorage.getItem('beanjar-web-mouse') === 'pointer' && "
        "document.querySelector('#input-touch').getAttribute('aria-checked') === 'false'"
    )
    log.write_text("")
    await mouse_drag(c, bx + bw * 0.5, by + bh * 0.5, 0, 0)
    await until(lambda: "Mouse.Release 0" in log.read_text())
    lines = log.read_text().splitlines()
    assert "Mouse.Press 0" in lines and not any(l.startswith("MultiTouch.") for l in lines), lines
    print(
        "PASS: in touch mode the right button stays the pointer's; toggled back (kept as "
        "'pointer'), a click is the pointer's left button",
        flush=True,
    )
    # the Remote button of a touch screen's model is a menu: Show remote, and the mouse as a finger
    await c.js("document.querySelector('#remote').click()")
    await c.wait("document.querySelector('#input-menu').matches(':popover-open')")
    items = await c.js("""[...document.querySelectorAll('#input-menu button')].map(b =>
      [b.id, b.querySelector('.control-label').textContent, b.getAttribute('aria-checked'),
       b.getBoundingClientRect().width > 0])""")
    assert items == [
        ["input-remote", "Show remote", "false", True],
        ["input-touch", "Mouse as a finger", "false", True],
    ], items
    assert await c.js("document.querySelector('#remote').getAttribute('aria-haspopup')") == "menu"
    await c.js("document.querySelector('#input-touch').click()")
    assert await c.js(
        "localStorage.getItem('beanjar-web-mouse') === 'touch' && "
        "document.querySelector('#input-menu').matches(':popover-open')"
    )
    await c.cmd("Input.dispatchKeyEvent", type="keyDown", key="Escape", code="Escape")
    await c.cmd("Input.dispatchKeyEvent", type="keyUp", key="Escape", code="Escape")
    await c.wait("!document.querySelector('#input-menu').matches(':popover-open')")
    await c.js("document.querySelector('#input-touch').click()")  # back to the pointer
    assert await c.js("localStorage.getItem('beanjar-web-mouse') === 'pointer'")
    print(
        "PASS: the Remote button of a touch screen's model opens Show remote and the mouse "
        "as a finger; the choice is kept, Escape closes it",
        flush=True,
    )


async def toolbar_checks(c):
    dest = os.environ.get("BEANJAR_VIEWER_ARTIFACTS")
    if dest:
        Path(dest).mkdir(parents=True, exist_ok=True)
    await c.cmd(
        "Browser.setDownloadBehavior", behavior="allow", downloadPath=str(OUT / "downloads")
    )
    for name, width, height in (("desktop", 1100, 800), ("portrait", 412, 915)):
        await c.cmd(
            "Emulation.setDeviceMetricsOverride",
            width=width,
            height=height,
            deviceScaleFactor=1,
            mobile=False,
        )
        await asyncio.sleep(0.2)
        visible = await c.js("""[...document.querySelectorAll('header .end > button')]
          .filter(b=>b.getBoundingClientRect().width>0).map(b=>b.id)""")
        assert visible == (
            ["sound", "shot", "remote", "full", "more-button"]  # a touch screen's VM
            if name == "desktop"
            else ["sound", "remote", "more-button"]
        ), visible

        # a touch screen's Remote button is the input menu outside the phones, the plain button on them
        assert await c.js("document.querySelector('#remote').getAttribute('popovertarget')") == (
            "input-menu" if name == "desktop" else None
        )

        async def capture(suffix):
            if dest:
                result = await c.cmd("Page.captureScreenshot", format="png")
                (Path(dest) / f"toolbar-{name}-{suffix}.png").write_bytes(
                    base64.b64decode(result["data"])
                )

        await capture("closed")
        await c.js("document.querySelector('#more-button').click()")
        await c.wait(
            "document.querySelector('#more-button').getAttribute('aria-expanded') === 'true'"
        )
        entries = await c.js("""[...document.querySelectorAll('#viewer-more > button')]
          .filter(b=>!b.hidden).map(b=>({id:b.id,label:b.querySelector('.control-label').textContent,icon:!!b.querySelector('.bi')}))""")
        # Save Screenshot and Fullscreen are the header's on desktop, More's in portrait; Remote in
        # a Window desktop's only; Stop VM… last (the fake VM runs)
        if name == "portrait":
            expected = [
                "quality-button",
                "planes-button",
                "shot",
                "stats-button",
                "full",
                "stop-vm",
            ]
        else:
            expected = [
                "quality-button",
                "planes-button",
                "stats-button",
                "remote-window",
                "stop-vm",
            ]
        assert [e["id"] for e in entries] == expected, entries
        labels = {
            "quality-button": "Quality",
            "planes-button": "Planes",
            "shot": "Save Screenshot",
            "stats-button": "Stats for nerds",
            "full": "Fullscreen",
            "remote-window": "Remote in a Window",
            "stop-vm": "Stop VM…",
        }
        assert [e["label"] for e in entries] == [labels[i] for i in expected], entries
        if name != "portrait":
            assert await c.js(
                "document.querySelector('#shot').nextElementSibling.id === 'remote'"
                " && document.querySelector('#shot').parentElement.classList.contains('end')"
            )
        assert all(e["icon"] for e in entries), entries
        await capture("open")
        await c.js("document.querySelector('#quality-button').click()")
        await c.wait(
            "document.querySelector('#quality-pop').matches(':popover-open') && !document.querySelector('#viewer-more').matches(':popover-open')"
        )
        assert await c.js("""(()=>{const p=document.querySelector('#quality-pop').getBoundingClientRect();
          return p.width>0 && p.x>=0 && p.right<=innerWidth && p.y>=0 && p.bottom<=innerHeight &&
          document.querySelector('#quality-button').getAttribute('aria-expanded')==='true';})()""")
        await c.cmd("Input.dispatchKeyEvent", type="keyDown", key="Escape", code="Escape")
        await c.cmd("Input.dispatchKeyEvent", type="keyUp", key="Escape", code="Escape")
        await c.wait("!document.querySelector('#quality-pop').matches(':popover-open')")
        assert await c.js("document.activeElement.id === 'more-button'")
        for enabled in (True, False):
            await c.js(
                "document.querySelector('#more-button').click();document.querySelector('#stats-button').click()"
            )
            await c.wait("!document.querySelector('#viewer-more').matches(':popover-open')")
            assert await c.js(
                f"document.querySelector('#stats-button').getAttribute('aria-checked') === '{str(enabled).lower()}' && document.querySelector('#stats-overlay').hidden === {str(not enabled).lower()}"
            )
            assert await c.js("document.querySelector('#screen').contains(document.activeElement)")
        # names have one-second resolution: two shots within a second are one file
        for old in (OUT / "downloads").glob("tv-*.png"):
            old.unlink()
        await c.js(
            ("document.querySelector('#more-button').click();" if name == "portrait" else "")
            + "document.querySelector('#shot').click()"
        )
        await c.wait("!document.querySelector('#viewer-more').matches(':popover-open')")
        assert await c.js("document.querySelector('#screen').contains(document.activeElement)")
        await until(lambda: list((OUT / "downloads").glob("tv-*.png")))
        for shot in (OUT / "downloads").glob("tv-*.png"):
            assert shot.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        await c.js("document.querySelector('#more-button').click()")
        await c.wait("document.querySelector('#viewer-more').matches(':popover-open')")
        await c.cmd("Input.dispatchKeyEvent", type="keyDown", key="ArrowUp", code="ArrowUp")
        assert await c.js(f"document.activeElement.id === '{expected[-1]}'")
        await c.cmd("Input.dispatchKeyEvent", type="keyDown", key="Escape", code="Escape")
        await c.wait("!document.querySelector('#viewer-more').matches(':popover-open')")
        await c.js("document.querySelector('#more-button').click()")
        await c.wait("document.querySelector('#viewer-more').matches(':popover-open')")
        await c.cmd(
            "Input.dispatchMouseEvent",
            type="mousePressed",
            x=width / 2,
            y=height / 4,  # the picture, above a phone's lite remote
            button="left",
            clickCount=1,
        )
        await c.cmd(
            "Input.dispatchMouseEvent",
            type="mouseReleased",
            x=width / 2,
            y=height / 4,  # the picture, above a phone's lite remote
            button="left",
            clickCount=1,
        )
        await c.wait(
            "!document.querySelector('#viewer-more').matches(':popover-open') && document.querySelector('#more-button').getAttribute('aria-expanded') === 'false'"
        )
        print(
            f"PASS: {name} toolbar order, shared menu, Quality, stats, PNG download, focus and dismissal",
            flush=True,
        )
    await c.cmd(
        "Emulation.setDeviceMetricsOverride",
        width=1100,
        height=800,
        deviceScaleFactor=1,
        mobile=False,
    )
    await asyncio.sleep(0.2)


async def planes_checks(c):
    """More's Planes: the page learns the planes beanjar-vnc hides (none), its UI check item hides
    the UI for every client (beanjar:planes=2, beanjar-vnc logs it, the picture turns black where
    the UI was blue), and checked again shows it; the popover's note says it is shared."""
    dest = os.environ.get("BEANJAR_VIEWER_ARTIFACTS")
    await c.wait("window.beanjarPlanes === 0")
    # blue UI, below fake-qemu's noise band and left of its white box
    at = await c.js("""(()=>{const r=document.querySelector('#screen div canvas').getBoundingClientRect();
      return [Math.round(r.x+r.width*0.15), Math.round(r.y+r.height*0.6)];})()""")
    before = await pixel(c, *at)
    await c.js(
        "document.querySelector('#more-button').click();document.querySelector('#planes-button').click()"
    )
    await c.wait("document.querySelector('#planes-pop').matches(':popover-open')")
    assert await c.js(
        "[...document.querySelectorAll('#planes-pop input')].every(b=>b.checked && !b.disabled)"
    )
    assert "everyone watching this VM" in await c.js(
        "document.querySelector('#planes-pop .pop-hint').textContent"
    )
    if dest:
        shot = await c.cmd("Page.captureScreenshot", format="png")
        (Path(dest) / "planes-popover.png").write_bytes(base64.b64decode(shot["data"]))
    await c.js("document.querySelector('#planes-pop input[data-plane=\"2\"]').click()")
    await c.wait("window.beanjarPlanes === 2")
    await until(lambda: "planes hidden 2" in (OUT / "vnc.log").read_text())
    for _ in range(50):
        hidden = await pixel(c, *at)
        if max(hidden) < 16:
            break
        await asyncio.sleep(0.1)
    if dest:
        shot = await c.cmd("Page.captureScreenshot", format="png")
        (Path(dest) / "planes-ui-hidden.png").write_bytes(base64.b64decode(shot["data"]))
    await c.js("document.querySelector('#planes-pop input[data-plane=\"2\"]').click()")
    await c.wait("window.beanjarPlanes === 0")
    for _ in range(50):
        after = await pixel(c, *at)
        if max(abs(a - b) for a, b in zip(after, before)) < 16:
            break
        await asyncio.sleep(0.1)
    await c.cmd("Input.dispatchKeyEvent", type="keyDown", key="Escape", code="Escape")
    await c.cmd("Input.dispatchKeyEvent", type="keyUp", key="Escape", code="Escape")
    await c.wait("!document.querySelector('#planes-pop').matches(':popover-open')")
    ok = (
        max(hidden) < 16 and max(abs(a - b) for a, b in zip(after, before)) < 16 and before[2] > 150
    )
    print(f"{'PASS' if ok else 'FAIL'}: Planes: UI shown {before}, hidden {hidden}, shown {after}",
          flush=True)  # fmt: skip
    assert ok


SETTLED = (
    "!document.getAnimations().some(a=>['lite','screen'].includes(a.effect?.target?.id))"
    " && !document.querySelector('#lite').style.transform"
    " && !document.querySelector('#screen').style.transform"
)


async def lite_checks(c, tmp):
    """The phones' lite remote: shown by default, keys at least 48 px, a touch on a key sends its
    IR code down and up, the header's remote button collapses (portrait: to its grip strip) or
    hides (landscape) it and opens it again, remembered; that button carries aria-expanded."""
    log = Path(tmp, "input.log")

    async def touch(sel, kind):
        # a D-pad wedge's box is the whole pad: aim at its arrow
        x, y = await c.js(f"""(()=>{{const b=document.querySelector("{sel}");
          const r=(b.querySelector('.bi')||b).getBoundingClientRect();return [r.x+r.width/2,r.y+r.height/2];}})()""")
        points = [] if kind == "touchEnd" else [{"x": x, "y": y, "id": 1}]
        await c.cmd("Input.dispatchTouchEvent", type=kind, touchPoints=points)

    state_js = """(()=>{const keys=[...document.querySelectorAll('#lite button:not(#lite-grip)')]
      .filter(b=>b.getBoundingClientRect().width>0), r=document.querySelector('#lite').getBoundingClientRect(),
      g=document.querySelector('#lite-grip');
      return {shown:r.width>0, width:Math.round(r.width), keys:keys.length, top:Math.round(r.top),
        open:document.body.classList.contains('lite-open'),
        screenBottom:Math.round(document.querySelector('#screen').getBoundingClientRect().bottom),
        inert:document.querySelector('#lite .lite-row.sys').inert,
        expanded:document.querySelector('#remote').getAttribute('aria-expanded'),
        controls:document.querySelector('#remote').getAttribute('aria-controls'),
        title:document.querySelector('#remote').title,
        linked:document.querySelector('header .linked').getBoundingClientRect().width,
        pressed:document.querySelector('#remote').getAttribute('aria-pressed'),
        grip:g.getBoundingClientRect().height,
        small:keys.filter(b=>{const r=b.getBoundingClientRect();return r.width<48||r.height<48;})
          .map(b=>b.getAttribute('aria-label')),
        disabled:keys.filter(b=>b.disabled).map(b=>b.dataset.key),
        fullRemote:!document.querySelector('#full-remote').hidden};})()"""
    await c.cmd("Emulation.setTouchEmulationEnabled", enabled=True)
    for name, width, height, keys in (("portrait", 412, 915, 17), ("landscape", 844, 390, 11)):
        await c.cmd(
            "Emulation.setDeviceMetricsOverride",
            width=width,
            height=height,
            deviceScaleFactor=1,
            mobile=False,
        )
        await asyncio.sleep(0.3)
        state = await c.js(state_js)
        assert state["shown"] and state["linked"] == 0 and state["pressed"] is None, state
        assert state["controls"] == "lite" and state["title"] == "Hide remote", state
        assert state["keys"] == keys and not state["small"] and not state["disabled"], state
        assert (
            state["fullRemote"] == (name == "landscape")
            and not state["inert"]
            and state["expanded"] == "true"
        ), state
        if name == "landscape":
            assert 220 <= state["width"] <= 230, state
        log.write_text("")
        for key in ("HOME", "UP"):
            await touch(f"#lite [data-key={key}]", "touchStart")
            await c.wait(
                f"document.querySelector('#lite [data-key={key}]').classList.contains('held')"
            )
            await asyncio.sleep(0.05)
            await touch(f"#lite [data-key={key}]", "touchEnd")
            await c.wait(
                f"!document.querySelector('#lite [data-key={key}]').classList.contains('held')"
            )
        sent = ["IR.Down 0x7c", "IR.Up 0x7c", "IR.Down 0x40", "IR.Up 0x40"]
        await until(
            lambda: [l for l in log.read_text().splitlines() if l.startswith("IR.")] == sent
        )
        await c.js("document.querySelector('#remote').click()")
        await c.wait(SETTLED)
        state = await c.js(state_js)
        assert not state["open"] and state["linked"] > 0 and state["pressed"] is None, state
        assert state["inert"] and state["expanded"] == "false", state
        assert state["title"] == "Show remote", state
        if name == "portrait":
            # collapsed to the grip strip on the toolbar (56 px), the picture down to it
            assert state["shown"] and state["top"] == height - 56 - PEEK, state
            assert state["screenBottom"] == state["top"], state
        else:
            assert not state["shown"], state
        assert await c.js("localStorage.getItem('beanjar-web-lite') === 'closed'")
        await c.js("document.querySelector('#remote').click()")
        await c.wait(SETTLED)
        state = await c.js(state_js)
        assert state["open"] and state["shown"] and state["linked"] == 0, state
        assert state["title"] == "Hide remote" and not state["inert"], state
        assert await c.js("localStorage.getItem('beanjar-web-lite') === 'open'")
        print(
            f"PASS: {name} lite remote shown, {keys} keys >= 48 px, HOME and UP sent as IR, "
            "header remote button "
            + ("collapses it to its grip strip" if name == "portrait" else "hides it")
            + " and opens it",
            flush=True,
        )
        if name == "portrait":
            await sheet_checks(c, state_js, height)
        else:
            await slide_checks(c, "landscape")
    # the VM's touch screen (its list row's touch): a finger on the picture is a touch, a slot of
    # QEMU's MultiTouch through beanjar-vnc's Display1.Touch, never the pointer's button
    log.write_text("")
    box = """(()=>{const r=document.querySelector('#screen div canvas').getBoundingClientRect();
      return [r.x+r.width/2, r.y+r.height/4];})()"""
    x, y = await c.js(box)
    await c.cmd(
        "Input.dispatchTouchEvent", type="touchStart", touchPoints=[{"x": x, "y": y, "id": 7}]
    )
    await c.cmd(
        "Input.dispatchTouchEvent", type="touchMove", touchPoints=[{"x": x + 40, "y": y, "id": 7}]
    )
    await c.cmd("Input.dispatchTouchEvent", type="touchEnd", touchPoints=[])
    await until(lambda: "MultiTouch.SendEvent 2 " in log.read_text())
    touches = [l.split()[1:3] for l in log.read_text().splitlines() if l.startswith("MultiTouch.")]
    assert touches == [["0", "0"], ["1", "0"], ["2", "0"]], touches
    assert not any(l.startswith("Mouse.Press") for l in log.read_text().splitlines())
    print(
        "PASS: a finger on the picture is the touch screen's (begin, move, end in slot 0)",
        flush=True,
    )
    await c.cmd("Emulation.setTouchEmulationEnabled", enabled=False)
    await c.cmd(
        "Emulation.setDeviceMetricsOverride",
        width=1100,
        height=800,
        deviceScaleFactor=1,
        mobile=False,
    )
    await asyncio.sleep(0.2)
    assert await c.js(
        "document.querySelector('#lite').getBoundingClientRect().width === 0 && "
        "document.querySelector('#full-remote').hidden"
    )
    # back on desktop the touch screen's Remote button is the input menu's again
    assert await c.js(
        "(()=>{const r=document.querySelector('#remote');return !r.hasAttribute('aria-pressed')"
        " && r.getAttribute('aria-expanded')==='false' && r.getAttribute('aria-haspopup')==='menu'"
        " && r.getAttribute('aria-controls')==='input-menu' && r.title==='Remote and input';})()"
    )


async def pixel(c, x, y):
    """The page's colour (r, g, b) at CSS pixel x, y: a 1 x 1 screenshot. A one-pixel PNG row has
    no neighbours, so every filter type leaves its bytes as they are."""
    shot = await c.cmd(
        "Page.captureScreenshot",
        format="png",
        clip={"x": x, "y": y, "width": 1, "height": 1, "scale": 1},
    )
    png, pos, data = base64.b64decode(shot["data"]), 8, b""
    while pos < len(png):
        n = int.from_bytes(png[pos : pos + 4], "big")
        kind, body = png[pos + 4 : pos + 8], png[pos + 8 : pos + 8 + n]
        if kind == b"IHDR":
            assert body[:8] == (1).to_bytes(4, "big") * 2 and body[8] == 8, body
        elif kind == b"IDAT":
            data += body
        pos += 12 + n
    return tuple(zlib.decompress(data)[1:4])


# portrait's collapsed sheet: the strip left above the toolbar (border and grip), and how far above
# the sheet's top the grip still takes a touch (style.css's ::before)
PEEK, GRIP_REACH = 21, 27
# the picture's centre against the centre of the space the sheet leaves it (both as drawn, with
# transforms): [dx, dy]
CENTRED = """(()=>{const p=document.querySelector('#screen div canvas').getBoundingClientRect(),
  s=document.querySelector('#screen').getBoundingClientRect(), l=document.querySelector('#lite').getBoundingClientRect(),
  h=document.querySelector('header.bar').getBoundingClientRect(), portrait=innerWidth<innerHeight,
  [x0,y0,x1,y1]=portrait?[0,h.bottom,innerWidth,l.top]:[0,h.bottom,l.width?l.left:innerWidth,innerHeight];
  return [p.x+p.width/2-(x0+x1)/2, p.y+p.height/2-(y0+y1)/2];})()"""
FREEZE = """(t=>{const as=document.getAnimations().filter(a=>['lite','screen'].includes(a.effect?.target?.id));
  for(const a of as){a.pause();a.currentTime=t;} return as.length;})"""


async def slide_checks(c, name):
    """The Remote button's slides: two animations (sheet and picture) that end in the state's own
    layout; mid-slide, the picture centred in the space the sheet leaves; none with reduced motion."""
    for _ in range(2):
        await c.js("document.querySelector('#remote').click()")
        assert await c.js(f"{FREEZE}(110)") == 2
        dx, dy = await c.js(CENTRED)
        assert abs(dx) <= 1 and abs(dy) <= 1, (name, dx, dy)
        await c.js("document.getAnimations().forEach(a=>a.play())")
        await c.wait(SETTLED)
        dx, dy = await c.js(CENTRED)
        assert abs(dx) <= 1 and abs(dy) <= 1, (name, dx, dy)
    await c.cmd(
        "Emulation.setEmulatedMedia",
        features=[{"name": "prefers-reduced-motion", "value": "reduce"}],
    )
    for want in ("false", "true"):
        await c.js("document.querySelector('#remote').click()")
        assert await c.js(
            f"({SETTLED}) && document.body.classList.contains('lite-open') === {want}"
        ), (name, want)
    await c.cmd("Emulation.setEmulatedMedia", features=[])
    print(
        f"PASS: {name} lite remote slides open and closed with the picture centred beside it "
        "mid-slide, ending in its layout; no slide with reduced motion",
        flush=True,
    )


async def sheet_checks(c, state_js, height):
    """Portrait's sheet: black outside its rounded top corners open and collapsed; one grip, not a
    control (no role, name or focus: the Remote button is), its peek strip PEEK px; dragged either
    way, also by the touch area just above the collapsed strip, the sheet follows and the picture
    with it, let go it settles by distance or flick; a tap toggles it; Enter and Space on the
    Remote button toggle it, the focus staying there."""
    toolbar = height - 56

    async def corners():
        left, top, right = await c.js(
            "(()=>{const r=document.querySelector('#lite').getBoundingClientRect();"
            "return [r.left, r.top, r.right];})()"
        )
        for x in (left + 2, right - 3):
            assert await pixel(c, x, top + 2) == (0, 0, 0), (x, await pixel(c, x, top + 2))
        assert await pixel(c, left + 60, top + 8) == (0x23, 0x23, 0x26)
        return top

    open_top = await corners()
    # open, the grip also takes the 8 px gap under it, not the keys below
    gap = await c.js("""(()=>{const g=document.querySelector('#lite-grip'), r=g.getBoundingClientRect(),
      k=document.querySelector('#lite .lite-row.sys').getBoundingClientRect(), x=innerWidth/2;
      return [k.top-r.bottom, document.elementFromPoint(x,r.bottom+7)===g,
        document.elementFromPoint(x,k.top+1)?.closest('.lite-row.sys')!==null,
        document.elementFromPoint(x,r.top-1)!==g];})()""")
    assert gap == [8, True, True, True], gap
    grip = await c.js("""(()=>{const h=document.querySelector('#lite-grip'),
      pseudo=(e)=>['::before','::after'].map(p=>getComputedStyle(e,p).content).filter(v=>v!=='none'&&v!=='normal'&&v!=='""');
      h.focus();
      const tabbable=[...document.querySelectorAll('button,a[href],input,select,[tabindex]')]
        .filter(e=>e.tabIndex>=0);
      return {tag:h.tagName, kids:[...h.children].map(k=>k.className), text:h.textContent.trim(),
        pseudo:[h,...h.children].flatMap(pseudo), role:h.getAttribute('role'),
        label:h.getAttribute('aria-label'), expanded:h.getAttribute('aria-expanded'),
        hidden:h.getAttribute('aria-hidden'), tabIndex:h.tabIndex, attr:h.hasAttribute('tabindex'),
        focused:document.activeElement===h, inTabOrder:tabbable.includes(h),
        height:h.getBoundingClientRect().height, touchAction:getComputedStyle(h).touchAction};})()""")
    assert grip == {
        "tag": "DIV",
        "kids": ["grip"],
        "text": "",
        "pseudo": [],
        "role": None,
        "label": None,
        "expanded": None,
        "hidden": "true",
        "tabIndex": -1,
        "attr": False,
        "focused": False,
        "inTabOrder": False,
        "height": PEEK - 1,
        "touchAction": "none",
    }, grip
    travel = await c.js("document.querySelector('#lite').offsetHeight") - PEEK
    collapsed_top = toolbar - PEEK

    async def drag(dy, pause=0.3, gap=0.02, steps=10, above=0):
        # above: this many px above the grip's top edge (the collapsed strip's extra touch area)
        x, y = await c.js(
            "(()=>{const r=document.querySelector('#lite-grip').getBoundingClientRect();"
            f"return [r.x+r.width/2, {'r.y-' + str(above) if above else 'r.y+r.height/2'}];}})()"
        )
        await c.cmd(
            "Input.dispatchTouchEvent", type="touchStart", touchPoints=[{"x": x, "y": y, "id": 1}]
        )
        for step in range(1, steps + 1):
            await c.cmd(
                "Input.dispatchTouchEvent",
                type="touchMove",
                touchPoints=[{"x": x, "y": y + dy * step / steps, "id": 1}],
            )
            await asyncio.sleep(gap)
        await asyncio.sleep(pause)
        moved = await c.js(
            "(()=>{const l=document.querySelector('#lite');"
            "return [l.classList.contains('dragging'), Math.round(l.getBoundingClientRect().top)];})()"
        )
        centred = await c.js(CENTRED)
        await c.cmd("Input.dispatchTouchEvent", type="touchEnd", touchPoints=[])
        return moved, centred

    async def settled(open):
        await c.wait(SETTLED)
        state = await c.js(state_js)
        assert state["open"] == open and state["expanded"] == str(open).lower(), state
        assert state["top"] == round(open_top if open else collapsed_top), state
        assert (state["linked"] == 0) == open, state
        assert state["title"] == ("Hide remote" if open else "Show remote"), state
        stored = await c.js("localStorage.getItem('beanjar-web-lite')")
        assert stored == ("open" if open else "closed"), stored
        dx, dy = await c.js(CENTRED)
        assert abs(dx) <= 1 and abs(dy) <= 1, (open, dx, dy)

    # a short drag down: the sheet and the picture follow the finger, then spring back open
    (dragging, top), (dx, dy) = await drag(60)
    assert dragging and top == round(open_top) + 60 and abs(dx) <= 1 and abs(dy) <= 1, (top, dy)
    await settled(True)
    # dragged down past a third of the way: collapsed to the peek strip
    await drag(travel * 0.5)
    await settled(False)
    await corners()
    # collapsed, the strip is PEEK px and the grip catches a finger up to GRIP_REACH px above the
    # sheet's top (the letterbox, not the picture); just beyond that, the picture gets the touch
    hit = await c.js(f"""(()=>{{const l=document.querySelector('#lite').getBoundingClientRect(),
      g=document.querySelector('#lite-grip'), x=innerWidth/2,
      at=(y)=>{{const e=document.elementFromPoint(x,y);return e===g?'grip':e?.closest('#screen')?'screen':e?.id||e?.tagName;}},
      p=document.querySelector('#screen div canvas').getBoundingClientRect();
      return {{peek:{toolbar}-l.top, top:at(l.top+1), reach:at(l.top-{GRIP_REACH}+1),
        beyond:at(l.top-{GRIP_REACH}-1), clear:l.top-{GRIP_REACH}-p.bottom}};}})()""")
    assert hit["peek"] == PEEK and hit["top"] == hit["reach"] == "grip", hit
    assert hit["beyond"] == "screen" and hit["clear"] > 0, hit
    # a drag up from that area above the strip opens it
    await drag(-travel * 0.5, above=GRIP_REACH - 4)
    await settled(True)
    await drag(travel * 0.5)
    await settled(False)
    # a short drag up from the strip follows the finger and springs back
    (dragging, top), (dx, dy) = await drag(-60)
    assert dragging and top == collapsed_top - 60 and abs(dy) <= 1, (top, dy)
    await settled(False)
    # dragged up past a third of the way: open
    await drag(-travel * 0.5)
    await settled(True)
    # quick flicks, shorter than a third, down then up
    assert 80 < travel / 3, travel
    await drag(80, pause=0, gap=0, steps=3)
    await settled(False)
    await drag(-80, pause=0, gap=0, steps=3)
    await settled(True)
    # a tap toggles it
    for want in (False, True):
        await drag(0, pause=0, steps=0)
        await settled(want)
    # so do Enter and Space on the Remote button, which keeps the focus
    await c.js("document.querySelector('#remote').focus()")
    for key, code, vk, text, want in (
        ("Enter", "Enter", 13, "\r", False),
        (" ", "Space", 32, " ", True),
    ):
        for kind in ("keyDown", "keyUp"):
            await c.cmd(
                "Input.dispatchKeyEvent",
                type=kind,
                key=key,
                code=code,
                windowsVirtualKeyCode=vk,
                text=text if kind == "keyDown" else "",
            )
        await settled(want)
        assert await c.js("document.activeElement.id === 'remote'")
    print(
        f"PASS: portrait lite remote is a sheet: black outside its corners open and collapsed, "
        f"a {PEEK} px peek; its grip is not focusable, follows a drag either way (collapsed, from "
        f"up to {GRIP_REACH} px above the strip) with the picture centred above, springs back from "
        "a short drag, settles by distance and flick; a tap toggles it, and Enter and Space on "
        "the Remote button",
        flush=True,
    )
    await slide_checks(c, "portrait")


async def float_checks(c):
    """The floating remote under real mouse events: a press on a key is the key's (no move, the
    focus kept), a wobble on the body under the 8 px threshold moves nothing, a drag on the body
    moves it, snapped to the edge it ends near and kept in localStorage, and the close control
    closes it."""
    rect = "(()=>{const r=document.querySelector('#remote-float').getBoundingClientRect();return [r.x,r.y,r.width,r.height]})()"

    async def mouse(points):
        x0, y0 = points[0]
        await c.cmd("Input.dispatchMouseEvent", type="mouseMoved", x=x0, y=y0)
        await c.cmd("Input.dispatchMouseEvent", type="mousePressed", x=x0, y=y0, button="left",
                    buttons=1, clickCount=1)  # fmt: skip
        for x, y in points[1:]:
            await c.cmd("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y, button="left",
                        buttons=1)  # fmt: skip
        x1, y1 = points[-1]
        await c.cmd("Input.dispatchMouseEvent", type="mouseReleased", x=x1, y=y1, button="left",
                    buttons=0, clickCount=1)  # fmt: skip
        await asyncio.sleep(0.2)

    await c.js("localStorage.removeItem('beanjar-web-remote-float')")
    start = await c.js(rect)
    screen = await c.js(
        "(()=>{const r=document.querySelector('#screen').getBoundingClientRect();return [r.x,r.y,r.width,r.height]})()"
    )
    # top right of the picture, 12 px in
    assert (
        abs(start[0] + start[2] - (screen[0] + screen[2] - 12)) < 1
        and abs(start[1] - screen[1] - 12) < 1
    ), (start, screen)
    focus = await c.js("document.activeElement === document.querySelector('#screen canvas')")
    key = await c.js("""(()=>{const k=[...document.querySelectorAll('#remote-float .rk')]
      .find(e=>e.title.startsWith('ENTER')).getBoundingClientRect();return [k.x+k.width/2,k.y+k.height/2]})()""")
    await mouse([key] + [(key[0] - 6 * i, key[1] + 3 * i) for i in range(1, 11)])
    assert await c.js(rect) == start, "a press on a key moved the remote"
    if focus:
        assert await c.js("document.activeElement === document.querySelector('#screen canvas')")
    body = (start[0] + 4, start[1] + start[3] / 2)
    await mouse(
        [body, (body[0] + 3, body[1] + 2), (body[0] + 5, body[1] - 1), (body[0] + 2, body[1] + 4)]
    )
    assert await c.js(rect) == start, "a wobble under the threshold moved the remote"
    assert await c.js("localStorage.getItem('beanjar-web-remote-float')") is None
    await mouse([body] + [(body[0] - 2000 * i / 10, body[1] + 2000 * i / 10) for i in range(1, 11)])
    moved = await c.js(rect)
    assert (
        abs(moved[0] - screen[0] - 12) < 1
        and abs(moved[1] + moved[3] - (screen[1] + screen[3] - 12)) < 1
    ), (moved, screen)
    saved = await c.js("JSON.parse(localStorage.getItem('beanjar-web-remote-float'))")
    assert saved == {"right": False, "bottom": True, "dx": 12, "dy": 12}, saved
    close = await c.js(
        "(()=>{const r=document.querySelector('#float-close').getBoundingClientRect();return [r.x+r.width/2,r.y+r.height/2]})()"
    )
    await mouse([close, (close[0] + 20, close[1])])
    assert await c.js(rect) == moved, "a press on the close control moved the remote"
    await mouse([close])
    assert await c.js("document.querySelector('#remote-float').hidden")
    await c.js("localStorage.removeItem('beanjar-web-remote-float')")
    print(
        "PASS: the floating remote: a key's press stays the key's, a wobble does not drag, a drag on the body moves it to the corner and is kept, close closes it",
        flush=True,
    )


async def remote_icons(c, session, debug):
    dest = os.environ.get("BEANJAR_VIEWER_ARTIFACTS")

    async def capture(browser, name):
        if dest:
            result = await browser.cmd("Page.captureScreenshot", format="png")
            (Path(dest) / f"{name}.png").write_bytes(base64.b64decode(result["data"]))

    async def check_keys(browser, frame="window"):
        result = await browser.js(f"""(async()=>{{
          const doc={frame}.document;
          const keys=[...doc.querySelectorAll('.rk')];
          const icons=[...doc.querySelectorAll('.rk .bi')];
          const urls=[...new Set(icons.map(i=>i.style.getPropertyValue('--i').match(/url\\((.*?)\\)/)[1]))];
          return {{keys:keys.length,icons:icons.length,
            named:icons.every(i=>i.parentElement.title && i.parentElement.getAttribute('aria-label') && i.getAttribute('aria-hidden')==='true'),
            masks:icons.every(i=>{frame}.getComputedStyle(i).maskImage.includes('/icons/')),
            served:(await Promise.all(urls.map(u=>fetch(u).then(r=>r.ok)))).every(Boolean),
            power:keys.find(k=>k.getAttribute('aria-label')==='POWER')?.querySelector('.bi').style.getPropertyValue('--i'),
            back:keys.find(k=>k.getAttribute('aria-label')==='BACK')?.querySelector('.bi')?.style.getPropertyValue('--i') ?? null,
            left:keys.find(k=>k.getAttribute('aria-label')==='LEFT')?.querySelector('.bi').style.getPropertyValue('--i'),
            numbers:keys.filter(k=>/^\\d$/.test(k.textContent)).length}};
        }})()""")
        assert result["keys"] == 48 and result["icons"] == 19, result
        assert result["named"] and result["masks"] and result["served"], result
        assert result["power"] == "url(/icons/power.svg)" and result["numbers"] == 10, result
        assert result["back"] is None, result  # text keys keep their labels
        assert result["left"] == "url(/icons/chevron-left.svg)", result
        return result

    await c.js("document.querySelector('#stats-close').click()")
    for name, width, height in (("desktop", 1100, 800), ("portrait", 412, 915)):
        await c.cmd(
            "Emulation.setDeviceMetricsOverride",
            width=width,
            height=height,
            deviceScaleFactor=1,
            mobile=False,
        )
        await asyncio.sleep(0.2)
        assert await c.js(
            "getComputedStyle(document.querySelector('#remote .bi')).maskImage.includes('/tv-remote.svg')"
        )
        await capture(c, f"toolbar-{name}")
        # on phones the header's remote button is the lite remote's; its 123 key opens this one
        opener = "#input-remote" if name == "desktop" else "#lite-full"
        if name == "desktop":  # a touch screen's Remote button is a menu: Show remote opens it
            await c.cmd(
                "Runtime.evaluate",
                expression="document.querySelector('#remote').click()",
                userGesture=True,
            )
            await asyncio.sleep(0.3)
        await c.cmd(
            "Runtime.evaluate",
            expression=f"document.querySelector('{opener}').click()",
            userGesture=True,
        )
        if name == "desktop":
            # Show remote: the remote floating over the picture, drawn in the page
            await c.wait(
                "document.querySelectorAll('#remote-float:not([hidden]) .rk').length === 48"
            )
            result = await check_keys(c)
            await capture(c, f"remote-float-{name}")
            await float_checks(c)
            # More's Remote in a Window: the popup remote
            await c.js("document.querySelector('#more-button').click()")
            await c.cmd(
                "Runtime.evaluate",
                expression="document.querySelector('#remote-window').click()",
                userGesture=True,
            )
            popup = None
            for _ in range(150):
                tabs = await (await session.get(debug + "/json/list")).json()
                popup = next((t for t in tabs if "/remote.html?" in t["url"]), None)
                if popup:
                    break
                await asyncio.sleep(0.1)
            assert popup, tabs
            async with session.ws_connect(popup["webSocketDebuggerUrl"]) as ws:
                remote = CDP(ws)
                await remote.cmd(
                    "Emulation.setDeviceMetricsOverride",
                    width=width,
                    height=height,
                    deviceScaleFactor=1,
                    mobile=False,
                )
                await remote.wait("document.querySelectorAll('.rk').length === 48")
                result = await check_keys(remote)
                await asyncio.sleep(0.2)
                await capture(remote, f"remote-{name}")
                await remote.js("window.close()")
        else:
            await c.wait(
                "document.querySelector('#remote-frame').contentWindow.document.querySelectorAll('.rk').length === 48"
            )
            result = await check_keys(c, "document.querySelector('#remote-frame').contentWindow")
            await asyncio.sleep(0.2)
            await capture(c, f"remote-{name}")
            await c.js("document.querySelector('#remote-close').click()")
        print(
            f"PASS: {name} remote icons and accessible names; system SVGs served; numbers preserved:",
            result,
            flush=True,
        )
    await c.cmd(
        "Emulation.setDeviceMetricsOverride",
        width=1100,
        height=800,
        deviceScaleFactor=1,
        mobile=False,
    )
    await c.js(
        "document.querySelector('#more-button').click();document.querySelector('#stats-button').click()"
    )


async def screenshots(c):
    dest = os.environ.get("BEANJAR_VIEWER_ARTIFACTS")
    await c.wait(
        "beanjarPicture.stats.fps > 0 && !document.querySelector('#picture-stats').textContent.includes('0.0\u00a0fps')"
    )
    path = Path(dest) if dest else None
    if path:
        path.mkdir(parents=True, exist_ok=True)
    for name, width, height in (("desktop", 1100, 800), ("portrait", 412, 915)):
        await c.cmd(
            "Emulation.setDeviceMetricsOverride",
            width=width,
            height=height,
            deviceScaleFactor=1,
            mobile=False,
        )
        await asyncio.sleep(0.2)
        if name == "portrait":
            await c.js("document.querySelector('#more-button').click()")
            await c.wait("document.querySelector('#viewer-more').matches(':popover-open')")
            assert await c.js(
                "document.querySelector('#viewer-more').contains(document.querySelector('#stats-button'))"
            )
            await c.js("document.querySelector('#stats-button').click()")
            assert await c.js(
                "document.querySelector('#stats-overlay').hidden && !document.querySelector('#viewer-more').matches(':popover-open')"
            )
            await c.js("document.querySelector('#more-button').click()")
            await c.js("document.querySelector('#stats-button').click()")
            assert await c.js("!document.querySelector('#stats-overlay').hidden")
        await asyncio.sleep(0.2)
        box = await c.js(
            "(()=>{const r=document.querySelector('#stats-overlay').getBoundingClientRect();return {x:r.x,y:r.y,right:r.right,bottom:r.bottom};})()"
        )
        assert (
            box["x"] >= 0 and box["y"] >= 0 and box["right"] <= width and box["bottom"] <= height
        ), box
        assert await c.js(
            "(()=>{const p=document.querySelector('#stats-overlay'),r=document.querySelector('#picture-stats').getBoundingClientRect();return !p.contains(document.elementFromPoint(r.x+2,r.y+2)) && !p.contains(document.activeElement);})()"
        ), "stats intercepted VM input"
        assert await c.js(
            "(()=>{const b=document.querySelector('#stats-close'),r=b.getBoundingClientRect();return b.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));})()"
        ), "close button is not reachable"
        assert await c.js(
            "(()=>{const p=document.querySelector('#stats-overlay').getBoundingClientRect(),v=document.querySelector('#screen canvas.h264').getBoundingClientRect();return Math.abs(p.x-v.x-8)<1 && Math.abs(p.y-v.y-8)<1;})()"
        ), "stats are not anchored to the video"
        assert await c.js(
            "document.querySelector('#stats-overlay').scrollWidth <= document.querySelector('#stats-overlay').clientWidth"
        ), "stats overflow horizontally"
        if path:
            result = await c.cmd("Page.captureScreenshot", format="png")
            (path / f"stats-{name}.png").write_bytes(base64.b64decode(result["data"]))
        print(
            f"PASS: {name} stats fit {width}x{height}; text passes input through, close is reachable:",
            box,
            flush=True,
        )
    await c.cmd(
        "Emulation.setDeviceMetricsOverride",
        width=1100,
        height=800,
        deviceScaleFactor=1,
        mobile=False,
    )


async def check_heartbeat(session, url):
    started = time.monotonic()
    async with session.ws_connect(url + "/vm/local/tv/rfb", autoping=False) as ws:
        # Leave the RFB greeting unanswered: no application traffic in either direction.
        while True:
            msg = await asyncio.wait_for(ws.receive(), 25)
            if msg.type == aiohttp.WSMsgType.PING:
                await ws.pong(msg.data)
                elapsed = time.monotonic() - started
                assert elapsed < 25, elapsed
                print(
                    f"PASS: idle RFB WebSocket received server ping at {elapsed:.2f} s; pong sent",
                    flush=True,
                )
                return
            assert msg.type == aiohttp.WSMsgType.BINARY, msg


(TOP / "scratch").mkdir(exist_ok=True)
with (
    tempfile.TemporaryDirectory(prefix="check-viewer-", dir=TOP / "scratch") as artifact_dir,
    tempfile.TemporaryDirectory(prefix="bjviewer-") as tmp,
):
    OUT = Path(artifact_dir)
    try:
        asyncio.run(checks(tmp))
    except Exception:
        for name in ("fake", "vnc", "web", "chromium"):
            path = OUT / (name + ".log")
            if path.exists():
                print(name, path.read_text()[-3500:], flush=True)
        raise
    finally:
        for p in reversed(processes):
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
                    p.wait()
        for p in reversed(processes):
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for f in logs:
            f.close()
        print("recorded parent PIDs reaped:", [p.pid for p in processes], flush=True)
