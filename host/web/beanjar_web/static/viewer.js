// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
// The viewer: noVNC (Debian's, unpatched, as a library) on /vm/HOST/NAME/rfb, which the server
// relays to `beanjar vnc pipe`; the header's remote buttons and, on phones, the lite remote's
// keys over /vm/HOST/NAME/remote; the
// speaker over /vm/HOST/NAME/audio (audio.js); with the H.264 picture chosen, the picture over /vm/HOST/NAME/video (video.js) on a canvas over noVNC's, which still takes the
// input. Both carry this page's id, so the server sends noVNC no picture while the stream plays.
import RFB from "/novnc/core/rfb.js";
import { Speaker } from "/audio.js";
import { Picture, h264Unsupported } from "/video.js";
import { stopDialog } from "/stop.js";
import { drawRemote } from "/remote-draw.js";

const [, , host, name] = location.pathname.split("/").map(decodeURIComponent);
const spec = `${host}/${name}`;
const base = `/vm/${encodeURIComponent(host)}/${encodeURIComponent(name)}`;
const wsBase = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${base}`;
// pairs this page's /rfb and /video (crypto.randomUUID needs a secure context; this does not)
const pageId = Array.from(crypto.getRandomValues(new Uint8Array(12)),
  (b) => b.toString(16).padStart(2, "0")).join("");
const screenEl = document.getElementById("screen");
const status = document.getElementById("status");
const quality = document.getElementById("quality");
let rfb = null, layout = null, remoteWin = null;
let touchScreen = false; // the VM's model has a touch screen (/api/vms's touch)
// A set that turns to portrait (/api/vms's portrait, the StanbyME): the remote socket reports the
// guest's rotation setting ("rotation N", beanjar remote pipe), and the picture is shown turned as
// far as the set is, (360 - N) % 360 clockwise (beanjar/devices/rotation.py picture_turn), noVNC's canvas
// and the stream's turned by a CSS transform. While it is turned the page maps the pointer itself
// ("pointer X Y BUTTONS WxH" on the remote socket), noVNC's mapping not knowing the turn; the
// keyboard stays noVNC's. The Rotate button toggles the set between landscape (0) and portrait (270), from any other
// angle to landscape (POST rotate).
let portrait = false, rotation = 0, turn = 0;

// the titles as the GTK viewer's: the VM's name, and under it where it runs, its platform and its
// board and firmware version (as the manager's list shows them)
const where = host === "local" ? "" : ` on ${host}`;
document.title = `${name}${where} - Beanjar`;
document.getElementById("title").textContent = name;
document.getElementById("sub").textContent = host === "local" ? "" : host;
// the VM's row of /api/vms (its state, reason), asked at load and whenever the picture ends; Off
// disables the TV keys, Sound, Remote and Save Screenshot, offers Start, and hides Stop VM…
let vmRow = null;
async function loadVm() {
  try {
    const rows = await (await fetch("/api/vms")).json();
    vmRow = (Array.isArray(rows) && rows.find((r) => r.name === name && (r.host || "local") === host)) || null;
  } catch (_) {
    vmRow = null; // unknown: nothing disabled
  }
  applyVmState();
  return vmRow;
}
loadVm().then((vm) => {
  if (!vm) return;
  const m = /^(\d+(?:\.\d+)+)-HE_DTV_([A-Z0-9]+)_/.exec(vm.firmware || "");
  const board = m ? `${m[2]} ${m[1]}` : vm.firmware || "";
  const sub = [host === "local" ? "" : host, vm.platform, board].filter(Boolean).join(" \u00b7 ");
  document.getElementById("sub").textContent = sub;
  document.title = [name + where, vm.platform, board, "Beanjar"].filter(Boolean).join(" - ");
  if (vm.mitm && vm.state === "Running") watchInspector();
  touchScreen = !!vm.touch;
  showInput();
  portrait = !!vm.portrait;
  rotateButton.hidden = !portrait;
  if (portrait) remoteOpen(); // its rotation reports
});

// More's Traffic Inspector: mitmweb through the server, on its own origin (inspector.json's url:
// the inspector's port, /vm/HOST/NAME/mitm/ and the VM's grant), shown only while the VM's
// inspector is ready (`beanjar mitm show`), asked again every 15 s until it is, then every
// minute. An inspector that is off for good (no usable mitmweb: its problem says what to
// install) shows that, dimmed, in the link's place; one still starting shows nothing.
function watchInspector() {
  const check = () => fetch(`${base}/inspector.json`).then((r) => r.json()).then((s) => {
    if (s.url) inspectorLink.href = s.url;
    inspectorLink.hidden = !(s.ready && s.url);
    inspectorOff.hidden = !!s.ready || !s.problem;
    inspectorOffWhy.textContent = s.problem || "";
  }).catch(() => { inspectorLink.hidden = inspectorOff.hidden = true; })
    .finally(() => setTimeout(check, inspectorLink.hidden ? 15000 : 60000));
  check();
}

// the status over the picture; with retry, Reconnect, and Start beside it while the VM is off
function show(text, error = false, retry = false) {
  status.hidden = !text;
  status.className = error ? "error" : "";
  status.replaceChildren(document.createTextNode(text || ""));
  if (!retry) return;
  const actions = document.createElement("div");
  actions.className = "status-actions";
  if (vmRow?.state === "Off") {
    const start = document.createElement("button");
    start.className = "suggested";
    start.innerHTML = '<i class="bi" style="--i: url(/icons/play-fill.svg)"></i>Start';
    start.addEventListener("click", startVm);
    actions.append(start);
  }
  const b = document.createElement("button");
  b.innerHTML = '<i class="bi" style="--i: url(/icons/arrow-repeat.svg)"></i>Reconnect';
  b.addEventListener("click", connect);
  actions.append(b);
  status.append(actions);
}
function showOff() { show(`VM ${name} is not running`, true, true); }

// the latest start or stop of the VM once the manager has returned (null without one)
async function settled() {
  for (;;) {
    const r = await fetch(`${base}/operation`).catch(() => null);
    if (!r || r.status !== 202) return r && r.status !== 404 ? r.json().catch(() => null) : null;
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}

// Start (the off page): POST start, then, the manager done, the picture as Reconnect brings it
async function startVm() {
  show(`Starting ${name}…`);
  try {
    const r = await fetch(`${base}/start`, { method: "POST" });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || r.statusText);
    const done = await settled();
    if (done?.status === "failed") throw new Error(done.message);
  } catch (e) {
    await loadVm();
    return show(`Cannot start ${name}: ${e.message}`, true, true);
  }
  await loadVm();
  connect();
}

// More's Stop VM…: the list's dialog; the picture ends when the VM does (the disconnect below),
// a stop the manager refused shows as a notice
const stopVm = document.getElementById("stop-vm");
const stopSep = document.getElementById("stop-sep");
const notice = document.getElementById("notice");
let noticeTimer = 0;
function note(text) {
  notice.textContent = text;
  notice.hidden = false;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { notice.hidden = true; }, 10000);
}
stopVm.addEventListener("click", () => stopDialog({ spec, base, reason: vmRow?.reason,
  accepted: async () => {
    const done = await settled();
    if (done?.status === "failed") note(`Cannot stop ${name}: ${done.message}`);
    await loadVm();
    if (!rfb && vmRow?.state === "Off") showOff();
  } }));

// quality: noVNC's qualityLevel (beanjar-vnc's Tight JPEG quality 11*q+1), kept per browser
// (a slider in a popover anchored to More)
const qualityButton = document.getElementById("quality-button");
const qualityPop = document.getElementById("quality-pop");
const qualityValue = document.getElementById("quality-value");
// planes: More's Planes popover, the UI, video and cursor beanjar-vnc composes, shared by every
// client of the VM (beanjar:planes=N as clipboard text on the RFB connection, which beanjar-vnc
// takes on its --local socket alone, and answers to every page there with the planes hidden)
const planesButton = document.getElementById("planes-button");
const planesPop = document.getElementById("planes-pop");
const planeBoxes = [...planesPop.querySelectorAll("input[data-plane]")];
const compact = matchMedia("(max-width: 640px) and (orientation: portrait)");
// phones, either way up: where the lite remote shows (style.css's two queries)
const phone = matchMedia("(max-width: 640px) and (orientation: portrait), "
  + "(orientation: landscape) and (max-height: 500px)");
const rotateButton = document.getElementById("rotate"); // More's Portrait check item
const moreButton = document.getElementById("more-button");
const morePop = document.getElementById("viewer-more");
const remoteButton = document.getElementById("remote");
const remotePop = document.getElementById("remote-pop");
const remoteFrame = document.getElementById("remote-frame");
const statsButton = document.getElementById("stats-button");
const statsPanel = document.getElementById("stats-overlay");
const fullscreenButton = document.getElementById("full");
const inspectorLink = document.getElementById("inspector");
const inspectorOff = document.getElementById("inspector-off"); // its place while it is off for good
const inspectorOffWhy = document.getElementById("inspector-off-why");
const fullRemote = document.getElementById("full-remote");
const inputMenu = document.getElementById("input-menu"); // a touch screen's Remote button
const inputRemote = document.getElementById("input-remote");
const inputTouch = document.getElementById("input-touch"); // the Mouse toggle (onMouseFinger)
const shotButton = document.getElementById("shot");
const remoteWindowItem = document.getElementById("remote-window"); // More's Remote in a Window
const floatEl = document.getElementById("remote-float"); // the desktop's floating remote
const moreItems = [qualityButton, planesButton, shotButton, statsButton, rotateButton, fullRemote,
  inspectorLink, inspectorOff, remoteWindowItem, fullscreenButton, document.getElementById("stop-vm")];
const menuItems = () => moreItems.filter((item) => item.parentElement === morePop && !item.hidden && !item.disabled);

function placePopover(pop, anchor) {
  const r = anchor.getBoundingClientRect(), p = pop.getBoundingClientRect();
  const top = compact.matches ? r.top - p.height - 6 : r.bottom + 6;
  pop.style.top = `${Math.max(6, Math.min(innerHeight - p.height - 6, top))}px`;
  pop.style.left = `${Math.max(6, Math.min(innerWidth - p.width - 6, r.right - p.width))}px`;
}
function placeControls() {
  if (morePop.matches(":popover-open")) placePopover(morePop, moreButton);
  if (qualityPop.matches(":popover-open")) placePopover(qualityPop, moreButton);
  if (planesPop.matches(":popover-open")) placePopover(planesPop, moreButton);
  if (remotePop.matches(":popover-open")) placePopover(remotePop, remoteButton);
  if (inputMenu.matches(":popover-open")) placePopover(inputMenu, remoteButton);
}
function adaptiveControls() {
  // Move only Save Screenshot and Fullscreen; the same secondary actions stay in More at every size.
  for (const pop of [morePop, qualityPop, planesPop, remotePop, inputMenu]) {
    if (pop.matches(":popover-open")) pop.hidePopover();
  }
  // More's Full remote: only the phone in landscape, whose lite strip has no 123 key (portrait's sheet does)
  fullRemote.hidden = !phone.matches || compact.matches;
  // desktop: the remote floats in the page, its window is More's; phones have the lite sheet
  remoteWindowItem.hidden = phone.matches;
  if (phone.matches) setFloat(false);
  showInput();
  if (compact.matches) {
    planesButton.after(shotButton);
    inspectorLink.before(fullscreenButton);
    for (const b of [shotButton, fullscreenButton]) b.setAttribute("role", "menuitem");
  } else {
    remoteButton.before(shotButton);
    moreButton.before(fullscreenButton);
    for (const b of [shotButton, fullscreenButton]) b.removeAttribute("role");
  }
}
morePop.addEventListener("toggle", (event) => {
  const open = event.newState === "open";
  moreButton.setAttribute("aria-expanded", String(open));
  if (open) { placeControls(); qualityButton.focus(); }
});
morePop.addEventListener("keydown", (event) => {
  const items = menuItems();
  const current = items.indexOf(document.activeElement);
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    items[(current + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length].focus();
  } else if (event.key === "Escape") {
    event.preventDefault();
    morePop.hidePopover();
    moreButton.focus();
  }
});
// (not the inspector's note: a click there may be selecting its install command to copy)
for (const item of moreItems) if (item !== inspectorOff) item.addEventListener("click", () => {
  if (morePop.matches(":popover-open")) morePop.hidePopover();
});
qualityPop.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    event.preventDefault();
    qualityPop.hidePopover();
    moreButton.focus();
  }
});
planesPop.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    event.preventDefault();
    planesPop.hidePopover();
    moreButton.focus();
  }
});
planesPop.addEventListener("toggle", (e) => {
  const open = e.newState === "open";
  planesButton.setAttribute("aria-pressed", String(open));
  planesButton.setAttribute("aria-expanded", String(open));
  if (open) { placeControls(); planeBoxes[0].focus(); }
});
// beanjar-vnc's answer: the planes hidden now, for this page and every other
function showPlanes(hidden) {
  window.beanjarPlanes = hidden; // the tests
  for (const box of planeBoxes) box.checked = !(hidden & Number(box.dataset.plane));
}
function sendPlanes(text) {
  if (rfb && connected) rfb.clipboardPasteFrom(text);
}
for (const box of planeBoxes) box.addEventListener("change", () => {
  const hidden = planeBoxes.reduce((m, b) => (b.checked ? m : m | Number(b.dataset.plane)), 0);
  sendPlanes(`beanjar:planes=${hidden}`);
});
compact.addEventListener("change", adaptiveControls);
phone.addEventListener("change", adaptiveControls);
addEventListener("resize", placeControls);
new ResizeObserver(placeControls).observe(qualityPop);
adaptiveControls();

quality.value = localStorage.getItem("beanjar-web-quality") ?? "3";
qualityValue.value = quality.value;
quality.addEventListener("input", () => { qualityValue.value = quality.value; });
quality.addEventListener("change", () => {
  localStorage.setItem("beanjar-web-quality", quality.value);
  applyPicture();
});
qualityPop.addEventListener("toggle", (e) => {
  const open = e.newState === "open";
  qualityButton.setAttribute("aria-pressed", String(open));
  qualityButton.setAttribute("aria-expanded", String(open));
  if (!open) return;
  placeControls();
  qualityPop.querySelector('input[name="picture"]:checked').focus();
});

// the picture: VNC's own (noVNC, Tight JPEG) or H.264 (video.js), chosen in the same popover and
// kept per browser. With a stream noVNC stays connected and
// takes the keyboard and pointer, the stream's canvas over its canvas and the pointer passing
// through; the server withholds noVNC's update requests while the stream plays, and asks for the
// whole screen when it ends. A stream that fails falls back to VNC for this page, with a notice.
const radios = [...document.querySelectorAll('input[name="picture"]')];
const h264Radio = radios.find((r) => r.value === "h264");
const pictureNote = document.getElementById("picture-note");
const bitrate = document.getElementById("bitrate");
const bitrateValue = document.getElementById("bitrate-value");
const statsEl = document.getElementById("picture-stats");
const overlay = document.createElement("canvas");
overlay.className = "h264";
overlay.hidden = true;
screenEl.append(overlay);
let pictureMode = localStorage.getItem("beanjar-web-picture");
if (pictureMode !== "h264") pictureMode = "vnc";
let h264Why = "checking this browser";
let picture = null, pictureRetry = 0, pictureError = "", connected = false;
const rfbCount = { bytes: 0, t: performance.now(), last: 0, kbps: 0 };
window.beanjarRfbCount = rfbCount; // the RFB socket's bytes (the tests)
const mbit = (kbps) => `${(kbps / 1000).toFixed(kbps < 10000 ? 1 : 0)} Mbit/s`;
bitrate.value = localStorage.getItem("beanjar-web-bitrate") ?? "10000";
bitrateValue.value = mbit(Number(bitrate.value));
bitrate.addEventListener("input", () => { bitrateValue.value = mbit(Number(bitrate.value)); });
bitrate.addEventListener("change", () => {
  localStorage.setItem("beanjar-web-bitrate", bitrate.value);
  if (picture) { stopPicture(); applyPicture(); } // at the new bitrate
});
for (const r of radios) {
  r.addEventListener("change", () => {
    pictureMode = r.value;
    localStorage.setItem("beanjar-web-picture", pictureMode);
    applyPicture();
    if (rfb) rfb.focus();
  });
}
// the overlay exactly over noVNC's canvas (its scaling and letterboxing), turned with it
function placeOverlay() {
  const c = rfb && screenEl.querySelector("div canvas");
  if (!c) return;
  applyTurn(c);
  const r = c.getBoundingClientRect(), s = screenEl.getBoundingClientRect();
  const w = c.offsetWidth, h = c.offsetHeight; // as laid out, before the turn
  Object.assign(overlay.style, { left: `${r.left - s.left + (r.width - w) / 2}px`,
    top: `${r.top - s.top + (r.height - h) / 2}px`, width: `${w}px`, height: `${h}px`,
    transform: c.style.transform });
  if (!statsPanel.hidden) {
    statsPanel.style.left = `${Math.max(8, Math.min(r.left - s.left + 8, s.width - statsPanel.offsetWidth - 8))}px`;
    statsPanel.style.top = `${Math.max(8, Math.min(r.top - s.top + 8, s.height - statsPanel.offsetHeight - 8))}px`;
  }
}
new ResizeObserver(placeOverlay).observe(screenEl);
// the stream to play: the choice, unless this browser cannot decode it (then VNC)
function streamMode() {
  if (pictureMode === "h264" && !h264Why) return "h264";
  return "vnc";
}
function applyPicture() {
  const mode = streamMode();
  for (const r of radios) r.checked = r.value === mode;
  document.getElementById("vnc-settings").hidden = mode !== "vnc";
  document.getElementById("h264-settings").hidden = mode !== "h264";
  if (mode !== "h264" || !connected) {
    clearTimeout(pictureRetry);
    pictureRetry = 0;
    pictureError = "";
    if (picture) stopPicture();
  }
  if (mode === "h264" && connected && !picture && !pictureRetry) startPicture();
  if (rfb) rfb.qualityLevel = Number(quality.value);
  showStats();
}
function startPicture() {
  const p = new Picture(`${wsBase}/video?bitrate=${bitrate.value}&page=${pageId}`, overlay, (state, detail) => {
    if (p !== picture) return;
    if (state === "playing") {
      pictureError = "";
      showStats();
      overlay.hidden = false;
      placeOverlay();
    } else if (state === "failed") {
      stopPicture();
      pictureError = detail;
      // VNC resumes while this stream retries. The RFB session and sound stay connected.
      pictureRetry = setTimeout(() => {
        pictureRetry = 0;
        if (connected && streamMode() === "h264") startPicture();
      }, 3000);
      showStats();
    }
  });
  picture = p;
  window.beanjarPicture = p; // its stats, for a look from the console (and the tests)
  p.start();
}
function stopPicture() {
  picture.stop();
  picture = null;
  window.beanjarPicture = null;
  overlay.hidden = true;
}
// the stream's numbers, every second: H.264's from video.js, VNC's from the RFB
// socket's bytes
function showStats() {
  qualityButton.title = pictureError ? `Picture Quality: H.264 reconnecting (${pictureError}); using VNC` : "Picture Quality";
  qualityButton.classList.toggle("reconnecting", !!pictureError);
  qualityButton.setAttribute("aria-label", qualityButton.title);
  moreButton.classList.toggle("reconnecting", compact.matches && !!pictureError);
  moreButton.title = compact.matches && pictureError ? `More: H.264 reconnecting (${pictureError})` : "More";
  pictureNote.hidden = !h264Why && !pictureError;
  pictureNote.textContent = pictureError ? `H.264 reconnecting: ${pictureError}. Using VNC meanwhile.`
    : h264Why ? `H.264 is not available: ${h264Why}.` : "";
  if (statsPanel.hidden) return;
  const now = performance.now(), dt = (now - rfbCount.t) / 1000;
  if (dt >= 0.9) {
    rfbCount.kbps = (rfbCount.bytes - rfbCount.last) * 8 / dt / 1000;
    Object.assign(rfbCount, { t: now, last: rfbCount.bytes });
  }
  const s = picture && picture.stats;
  statsEl.textContent = !connected ? "Video disconnected"
    : s && s.encoder ? `H.264 · ${s.encoder.toUpperCase()} · ${s.width}×${s.height}`
      + `\n${s.fps.toFixed(1)}\u00a0fps · ${mbit(s.kbps).replace(" ", "\u00a0")}`
      + `\nDecoded ${s.decoded} · drawn ${s.drawn}`
    : picture ? "H.264 starting\u2026" : `VNC · ${mbit(rfbCount.kbps).replace(" ", "\u00a0")} received`;
}
function showAudioStats() {
  if (statsPanel.hidden) return;
  const el = document.getElementById("audio-stats");
  const ctx = speaker?.ctx, w = speaker?.stats.worklet;
  const ms = (seconds) => Number.isFinite(seconds) ? `${(seconds * 1000).toFixed(1)}\u00a0ms` : "unavailable";
  if (!ctx) { el.textContent = "Sound off"; return; }
  el.textContent = `Audio · ${ctx.sampleRate / 1000}\u00a0kHz · ${speaker.stats.format || "starting"}`
    + `\nBuffered ${ms(w?.buffered / 48000)} · target ${ms(w?.target / 48000)}`
    + `\nBrowser base ${ms(ctx.baseLatency)} · output\u00a0${ms(ctx.outputLatency)}`
    + `\nUnderruns ${w?.underruns ?? 0}`;
}
setInterval(() => { showStats(); showAudioStats(); if (picture || !statsPanel.hidden) placeOverlay(); }, 1000);
applyPicture();
h264Unsupported().then((why) => {
  h264Why = why;
  h264Radio.disabled = !!why;
  document.getElementById("h264-choice").title = why ? `H.264 is not available: ${why}` : "";
  pictureNote.hidden = !why;
  pictureNote.textContent = why ? `H.264 is not available: ${why}.` : "";
  applyPicture();
});

async function connect() {
  show("Starting VNC…");
  let r;
  try {
    r = await fetch(`${base}/vnc`, { method: "POST" });
  } catch (e) {
    return show(`beanjar-web is not reachable: ${e.message}`, true, true);
  }
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    await loadVm(); // off: Start beside Reconnect
    return show(body.error || r.statusText, true, true);
  }
  show("Connecting…");
  // the socket is the page's own, to count what it brings (the popover's VNC rate)
  const channel = new WebSocket(`${wsBase}/rfb?page=${pageId}`);
  channel.addEventListener("message", (e) => { rfbCount.bytes += e.data.byteLength || 0; });
  rfb = new RFB(screenEl, channel, { shared: true });
  window.beanjarRfb = rfb;
  rfb.scaleViewport = true;
  rfb.resizeSession = false;
  rfb.qualityLevel = Number(quality.value);
  rfb.background = "#000";
  screenEl.append(overlay); // after noVNC's own
  rfb.addEventListener("clipboard", (e) => {
    const m = /^beanjar:planes=([0-7])$/.exec(e.detail.text);
    if (m) showPlanes(Number(m[1]));
  });
  rfb.addEventListener("connect", () => {
    show("");
    rfb.focus();
    connected = true;
    for (const box of planeBoxes) box.disabled = false;
    sendPlanes("beanjar:planes?");
    applyPicture();
    applySound();
    const c = screenEl.querySelector("div canvas");
    if (c) new ResizeObserver(placeOverlay).observe(c);
  });
  rfb.addEventListener("disconnect", async (e) => {
    rfb = null;
    connected = false;
    for (const box of planeBoxes) box.disabled = true;
    applyPicture();
    applySound();
    const text = e.detail.clean ? "Disconnected." : "The connection to the VM was lost.";
    show(text, !e.detail.clean);
    // the VM stopped? (a stop still running: once it has returned; the list is a few seconds old)
    await settled();
    for (const wait of [0, 3500]) {
      await new Promise((resolve) => setTimeout(resolve, wait));
      await loadVm();
      if (rfb || vmRow?.state === "Off") break;
    }
    if (rfb) return; // reconnected meanwhile
    if (vmRow?.state === "Off") showOff(); else show(text, !e.detail.clean, true);
  });
}

// the header's remote buttons: down on press, up on release (held: a long press)
const remote = { ws: null, queue: [] };
function remoteOpen() {
  if (remote.ws && remote.ws.readyState <= 1) return;
  remote.ws = new WebSocket(`${wsBase}/remote`);
  remote.ws.addEventListener("open", () => { for (const l of remote.queue.splice(0)) remote.ws.send(l); });
  remote.ws.addEventListener("message", (e) => {
    if (e.data.startsWith("error")) console.warn(e.data);
    const m = /^rotation (0|90|180|270)$/.exec(e.data);
    if (m) setRotation(Number(m[1]));
  });
  // a set that turns: its reports again after the socket drops
  remote.ws.addEventListener("close", () => { if (portrait) setTimeout(remoteOpen, 3000); });
}
function remoteSend(line) {
  remoteOpen();
  if (remote.ws.readyState === 1) remote.ws.send(line); else remote.queue.push(line);
}

// the picture turned as the set is (portrait, above)
setRotation(0);
function setRotation(angle) {
  rotation = angle;
  window.beanjarRotation = angle; // the tests
  // a two-state toggle: landscape is 0, anything else is portrait (270 what a press gives)
  const turned = angle !== 0; // More's "Portrait" check item
  rotateButton.setAttribute("aria-pressed", String(turned));
  rotateButton.setAttribute("aria-checked", String(turned));
  const t = (360 - angle) % 360;
  if (t === turn) return;
  turn = t;
  placeOverlay();
}
// noVNC's canvas turned and scaled to fit #screen, about its centre (noVNC centres it)
function applyTurn(c) {
  if (!turn) { c.style.transform = ""; return; }
  const w = c.offsetWidth, h = c.offsetHeight, odd = turn % 180 === 90;
  const k = odd && w && h ? Math.min(screenEl.clientWidth / h, screenEl.clientHeight / w) : 1;
  c.style.transform = `rotate(${turn}deg) scale(${k})`;
}
// a point of the page in noVNC's framebuffer pixels through the turn ([x, y, inside])
function framebufferAt(clientX, clientY) {
  const c = screenEl.querySelector("div canvas");
  if (!c || !c.width || !c.height) return null;
  const r = c.getBoundingClientRect(), odd = turn % 180 === 90;
  const a = -turn * Math.PI / 180, dx = clientX - (r.left + r.width / 2), dy = clientY - (r.top + r.height / 2);
  const ux = dx * Math.cos(a) - dy * Math.sin(a), uy = dx * Math.sin(a) + dy * Math.cos(a);
  const w = odd ? r.height : r.width, h = odd ? r.width : r.height;
  const x = Math.round((ux + w / 2) * c.width / w), y = Math.round((uy + h / 2) * c.height / h);
  const inside = x >= 0 && y >= 0 && x < c.width && y < c.height;
  return [Math.min(Math.max(x, 0), c.width - 1), Math.min(Math.max(y, 0), c.height - 1), inside,
    `${c.width}x${c.height}`];
}
rotateButton.addEventListener("click", async () => {
  if (rotateButton.disabled) return;
  rotateButton.disabled = true;
  try {
    const r = await fetch(`${base}/rotate`, { method: "POST", body: String(rotation === 0 ? 270 : 0) });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) alert(`Rotate failed: ${body.error || r.statusText}`);
    else setRotation(body.rotation);
  } catch (e) {
    alert(`Rotate failed: ${e.message}`);
  } finally {
    rotateButton.disabled = false;
    if (rfb) rfb.focus();
  }
});
// the pointer while the picture is turned: caught before noVNC, sent as the tablet's position and
// buttons (left, middle, right: 1, 2, 4); a press outside the picture is dropped
let pointerButtons = 0;
function onPointer(e) {
  if (!turn || !rfb || e.pointerType === "touch") return;
  e.preventDefault();
  e.stopPropagation();
  if (!e.type.startsWith("pointer")) return; // the mouse events after them
  const at = framebufferAt(e.clientX, e.clientY);
  if (!at) return;
  const bits = (e.buttons & 1) | (e.buttons & 4 ? 2 : 0) | (e.buttons & 2 ? 4 : 0);
  if (e.type === "pointerdown" && !at[2]) return;
  if (e.type === "pointerdown") screenEl.setPointerCapture?.(e.pointerId);
  if (e.type === "pointermove" && !bits && !pointerButtons && !at[2]) return;
  if (mouseTouch && touchScreen) bits &= ~1; // the left button is the mouse's finger (below)
  pointerButtons = bits;
  remoteSend(`pointer ${at[0]} ${at[1]} ${bits} ${at[3]}`);
  if (e.type === "pointerdown") rfb.focus();
}
for (const type of ["pointerdown", "pointermove", "pointerup", "pointercancel", "mousedown",
  "mousemove", "mouseup", "click", "contextmenu", "wheel"]) {
  // the floating remote's own presses are its keys' and its drag's, not the picture's
  screenEl.addEventListener(type, (e) => floatEl.contains(e.target) || onMouseFinger(e) || onPointer(e),
    { capture: true, passive: false });
}
function codeOf(key) {
  const k = layout && layout.keys.find((k) => k.name === key);
  return k && k.code !== null ? k.code : null;
}
const remoteKeys = () => document.querySelectorAll("button.ir, #lite button[data-key]");
for (const b of remoteKeys()) {
  const isLite = !b.classList.contains("ir");
  let down = null;
  const up = () => {
    b.classList.remove("held");
    if (down === null) return;
    remoteSend(`up ${down}`);
    down = null;
    if (rfb) rfb.focus();
  };
  b.addEventListener("pointerdown", (e) => {
    const code = codeOf(b.dataset.key);
    if (code === null || down !== null) return;
    b.setPointerCapture(e.pointerId);
    down = code;
    remoteSend(`down ${code}`);
    if (isLite) {
      b.classList.add("held");
      if (navigator.vibrate) navigator.vibrate(8);
    }
  });
  b.addEventListener("pointerup", up);
  b.addEventListener("pointercancel", up);
  b.addEventListener("lostpointercapture", up);
  b.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    e.preventDefault();
    const code = codeOf(b.dataset.key);
    if (e.repeat || down !== null || code === null) return;
    down = code;
    remoteSend(`down ${code}`);
  });
  b.addEventListener("keyup", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); up(); }
  });
  b.addEventListener("blur", up);
}

// On a VM with a touch screen (the StanbyME), the picture's touches go to it, each finger a slot
// of its own (0 to 9), over the remote socket in noVNC's framebuffer pixels: "touch KIND SLOT X Y
// WxH" (beanjar remote pipe maps them to the guest's display). Caught before noVNC, which would
// make them the pointer's; a mouse stays the pointer. Elsewhere touches are noVNC's as before.
const touchSlots = new Map(); // Touch.identifier -> slot
function touchAt(t) {
  const at = framebufferAt(t.clientX, t.clientY); // through the turn
  return at && `${at[0]} ${at[1]} ${at[3]}`;
}
function onTouch(e) {
  if (!touchScreen || !rfb || floatEl.contains(e.target)) return;
  e.preventDefault();
  e.stopPropagation();
  for (const t of e.changedTouches) {
    let slot = touchSlots.get(t.identifier);
    if (e.type === "touchstart") {
      if (slot !== undefined) continue;
      const used = new Set(touchSlots.values());
      slot = [...Array(10).keys()].find((n) => !used.has(n));
      if (slot === undefined) continue; // more fingers than the panel's slots
      touchSlots.set(t.identifier, slot);
    } else if (slot === undefined) continue;
    const at = touchAt(t);
    const kind = { touchstart: "begin", touchmove: "move", touchend: "end", touchcancel: "cancel" }[e.type];
    if (at) remoteSend(`touch ${kind} ${slot} ${at}`);
    if (kind === "end" || kind === "cancel") touchSlots.delete(t.identifier);
  }
}
for (const type of ["touchstart", "touchmove", "touchend", "touchcancel"]) {
  screenEl.addEventListener(type, onTouch, { capture: true, passive: false });
}

// The Mouse toggle, on a VM with a touch screen: the mouse as the Magic Remote's pointer (as
// before: noVNC's, or the page's while the picture is turned) or, in touch mode, a finger: its
// left button's press, drag and release a touch's begin, moves and end, in a slot of its own and
// mapped as a finger's is (framebufferAt, through the turn). Hovering sends nothing (a finger can't
// hover; a right or middle press carries its own position), the wheel and the other buttons stay
// the pointer's. Kept per browser (beanjar-web-mouse: "touch" or "pointer"). Not
// shown on phones, whose fingers are the touch screen's already.
const MOUSE = "mouse"; // touchSlots' key for the mouse's finger
let mouseTouch = localStorage.getItem("beanjar-web-mouse") === "touch";
let mouseAt = null; // where the mouse's finger was last ("X Y WxH")
// A touch screen's model outside the phones gets a menu on the Remote button: Show remote, and the
// mouse as a finger (the choice kept below); else it is the plain Remote button.
function inputMode() { return touchScreen && !phone.matches; }
var inputReady = false; // remoteAttrs wants the lite remote's state, which comes later
function showInput() {
  if (inputMode()) remoteButton.setAttribute("popovertarget", "input-menu");
  else { remoteButton.removeAttribute("popovertarget"); inputMenu.hidePopover?.(); }
  if (inputReady) remoteAttrs();
}
function setMouseTouch(on) {
  if (!on && touchSlots.has(MOUSE)) { // lifted where it is
    remoteSend(`touch end ${touchSlots.get(MOUSE)} ${mouseAt}`);
    touchSlots.delete(MOUSE);
  }
  mouseTouch = on;
  localStorage.setItem("beanjar-web-mouse", on ? "touch" : "pointer");
  inputTouch.setAttribute("aria-checked", String(on));
  inputTouch.title = on ? "Mouse: touch (its left button is a finger; uncheck for the pointer)"
    : "Mouse: pointer (check to make it a finger on the touch screen)";
  inputTouch.querySelector(".bi").style.setProperty("--i", `url(/icons/${on ? "hand-index" : "mouse"}.svg)`);
}
setMouseTouch(mouseTouch);
inputTouch.addEventListener("click", () => setMouseTouch(!mouseTouch));
// the screen's pointer and mouse events in touch mode: true for those it takes (the left button's),
// which then reach neither onPointer nor noVNC
function onMouseFinger(e) {
  if (!touchScreen || !mouseTouch || !rfb || e.pointerType === "touch") return false;
  const slot = touchSlots.get(MOUSE);
  const take = () => { e.preventDefault(); e.stopPropagation(); return true; };
  const send = (kind, s, clientX, clientY) => {
    const at = touchAt({ clientX, clientY });
    if (at) mouseAt = at;
    if (mouseAt) remoteSend(`touch ${kind} ${s} ${mouseAt}`);
  };
  switch (e.type) {
  case "pointerdown": {
    if (e.button !== 0) return false;
    const at = framebufferAt(e.clientX, e.clientY);
    if (!at || !at[2]) return take(); // outside the picture: nothing
    if (slot !== undefined) return take();
    const used = new Set(touchSlots.values());
    const free = [...Array(10).keys()].find((n) => !used.has(n));
    if (free === undefined) return take(); // more fingers than the panel's slots
    touchSlots.set(MOUSE, free);
    screenEl.setPointerCapture?.(e.pointerId);
    send("begin", free, e.clientX, e.clientY);
    rfb.focus();
    return take();
  }
  case "pointermove":
    if (slot === undefined) return e.buttons ? false : take(); // a finger can't hover
    send("move", slot, e.clientX, e.clientY);
    return take();
  case "pointerup":
  case "pointercancel":
    if (slot === undefined || (e.type === "pointerup" && e.button !== 0)) return false;
    send(e.type === "pointerup" ? "end" : "cancel", slot, e.clientX, e.clientY);
    touchSlots.delete(MOUSE);
    return take();
  case "mousedown":
  case "mouseup":
  case "click": // the left button's, which noVNC would press
    return e.button === 0 ? take() : false;
  case "mousemove":
    return slot !== undefined || e.buttons & 1 || !e.buttons ? take() : false;
  }
  return false;
}

document.getElementById("shot").addEventListener("click", async () => {
  if (rfb) rfb.focus();
  const r = await fetch(`${base}/screenshot.png`);
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    return alert(`Screenshot failed: ${body.error || r.statusText}`);
  }
  const a = document.createElement("a");
  a.href = URL.createObjectURL(await r.blob());
  const t = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
  a.download = `${name}-${t}.png`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
});

const full = document.getElementById("full");
full.addEventListener("click", () => {
  if (document.fullscreenElement) document.exitFullscreen(); else screenEl.requestFullscreen();
  if (rfb) rfb.focus();
});
document.addEventListener("fullscreenchange", () => full.querySelector(".bi").style.setProperty(
  "--i", `url(/icons/${document.fullscreenElement ? "fullscreen-exit" : "fullscreen"}.svg)`));

// the speaker: muted until clicked (a browser starts audio only from a gesture), remembered per
// browser; the audio WebSocket is open only while it is on. Its tooltip names the format.
const sound = document.getElementById("sound");
const FORMAT_NAMES = {
  opus: "Opus (WebCodecs)",
  pcm: "PCM (no WebCodecs: plain HTTP away from localhost is not a secure context)",
};
let speaker = null;
let soundWanted = localStorage.getItem("beanjar-web-sound") === "on", soundUnlocked = false;
function soundShow(on, detail, waiting = false) {
  sound.classList.toggle("reconnecting", waiting);
  sound.setAttribute("aria-pressed", String(on));
  sound.querySelector(".bi").style.setProperty("--i", `url(/icons/${on ? "volume-up" : "volume-mute"}.svg)`);
  sound.title = on ? `Sound on: ${detail}` : `Sound off: click to hear the VM${detail ? ` (${detail})` : ""}`;
}
function stopSpeaker() {
  if (speaker) speaker.stop();
  speaker = null;
  window.beanjarSpeaker = null;
}
function applySound() {
  if (!connected || !soundWanted || !soundUnlocked) {
    stopSpeaker();
    soundShow(soundWanted, !connected ? "waiting for the picture to reconnect" : "click to resume", soundWanted);
    return;
  }
  if (speaker) return;
  const s = new Speaker(`${wsBase}/audio`, (text, playing) => {
    if (s !== speaker) return;
    soundShow(true, playing ? FORMAT_NAMES[text] || text : `reconnecting: ${text}`, !playing);
  });
  speaker = s;
  window.beanjarSpeaker = s; // its stats, for a look from the console (and the tests)
  soundShow(true, "starting", true);
  s.start().catch((e) => {
    if (s !== speaker) return;
    stopSpeaker();
    soundShow(true, `click to retry: ${e.message}`, true);
  });
}
sound.addEventListener("click", () => {
  if (!(soundWanted && connected && !speaker)) soundWanted = !soundWanted;
  soundUnlocked = true;
  localStorage.setItem("beanjar-web-sound", soundWanted ? "on" : "off");
  applySound();
  if (rfb) rfb.focus();
});
// The panel is passive: toggling it returns keyboard input to the VM.
function setStatsOpen(open) {
  statsPanel.hidden = !open;
  statsButton.setAttribute("aria-pressed", String(open));
  statsButton.setAttribute("aria-checked", String(open));
  localStorage.setItem("beanjar-web-stats", open ? "on" : "off");
  if (open) { showStats(); showAudioStats(); placeOverlay(); }
}
statsButton.addEventListener("click", () => {
  setStatsOpen(statsPanel.hidden);
  if (rfb) rfb.focus();
});
document.getElementById("stats-close").addEventListener("click", () => {
  setStatsOpen(false);
  if (rfb) rfb.focus();
});
setStatsOpen(localStorage.getItem("beanjar-web-stats") === "on");

// Remember intent across an RFB reconnect, but wait for a gesture on a newly opened page.
if (soundWanted) {
  soundShow(true, "click anywhere to resume", true);
  const resume = (e) => {
    removeEventListener("pointerdown", resume, true);
    removeEventListener("keydown", resume, true);
    soundUnlocked = true;
    if (!sound.contains(e.target)) applySound();
  };
  addEventListener("pointerdown", resume, true);
  addEventListener("keydown", resume, true);
}
window.addEventListener("pagehide", () => {
  connected = false;
  applySound();
  applyPicture();
});

// The lite remote on phones (style.css places it): open unless this browser closed it last. In
// portrait a bottom sheet that collapses to its grip strip; in landscape a strip on the right, gone
// when closed. Opening and closing slide it (Web Animations, which the compositor runs) and carry
// the picture along: the layout changes once, when the sheet starts to open or has closed, and
// until then a transform on #screen keeps the picture centred in the space above (or beside) the
// sheet, so noVNC rescales once per toggle, not every frame.
const lite = document.getElementById("lite");
const liteGrip = document.getElementById("lite-grip");
const liteRows = [...lite.children].filter((e) => e !== liteGrip);
const calm = matchMedia("(prefers-reduced-motion: reduce)");
const SLIDE_MS = 220, SLIDE_EASE = "cubic-bezier(.2, .8, .2, 1)"; // ease-out
let liteOpen = localStorage.getItem("beanjar-web-lite") !== "closed";
let slide = null; // the running slide's two animations
const liteLaidOpen = () => document.body.classList.contains("lite-open");
// The header's Remote button is the sheet's control on phones (the grip is pointers' only):
// expanded with it, naming what it does; on desktop a toggle for the remote panel or window.
function remoteAttrs() {
  if (phone.matches) {
    remoteButton.removeAttribute("aria-pressed");
    remoteButton.setAttribute("aria-expanded", String(liteOpen));
    remoteButton.setAttribute("aria-controls", "lite");
    remoteButton.title = liteOpen ? "Hide remote" : "Show remote";
    remoteButton.removeAttribute("aria-haspopup");
  } else if (inputMode()) {
    remoteButton.removeAttribute("aria-pressed");
    remoteButton.setAttribute("aria-haspopup", "menu");
    remoteButton.setAttribute("aria-expanded", String(inputMenu.matches(":popover-open")));
    remoteButton.setAttribute("aria-controls", "input-menu");
    remoteButton.title = "Remote and input";
    inputRemote.setAttribute("aria-checked", String(remoteShown()));
  } else {
    remoteButton.removeAttribute("aria-expanded");
    remoteButton.removeAttribute("aria-haspopup");
    remoteButton.setAttribute("aria-controls", "remote-pop");
    remoteButton.setAttribute("aria-pressed", String(remoteShown()));
    remoteButton.title = "Show or hide the TV remote";
  }
}
function remoteShown() {
  return remotePop.matches(":popover-open") || !floatEl.hidden || !!remoteWin && !remoteWin.closed;
}
function liteAttrs() {
  for (const row of liteRows) row.inert = !liteOpen; // collapsed, the keys wait behind the toolbar
  remoteAttrs();
}
document.body.classList.toggle("lite-open", liteOpen);
liteAttrs();
inputReady = true;
// the collapsed sheet's negative margin (style.css) needs its height
const liteHeight = () => { if (lite.offsetHeight) lite.style.setProperty("--lite-h", `${lite.offsetHeight}px`); };
liteHeight();
new ResizeObserver(liteHeight).observe(lite);
// how far the sheet moves between open and collapsed: all but the grip strip (portrait), its
// width (landscape; 0 while it is laid out closed)
function liteTravel() {
  if (!compact.matches) return lite.offsetWidth;
  return lite.offsetHeight - parseFloat(getComputedStyle(lite).getPropertyValue("--peek"));
}
// The sheet at offset o (0 open, travel collapsed) in the current layout: the sheet's transform,
// and #screen's, which moves and scales the picture to be centred in the space the sheet leaves
// (noVNC fits and centres it in #screen; the transform's origin is #screen's centre).
function liteFrame(o, travel) {
  const d = o - (liteLaidOpen() ? 0 : travel), w = screenEl.clientWidth, h = screenEl.clientHeight;
  const [vw, vh] = compact.matches ? [w, h + d] : [w + d, h];
  const c = screenEl.querySelector("div canvas");
  // the picture as shown: turned to portrait, its width and height swapped
  const [pw, ph] = c && turn % 180 === 90 ? [c.height, c.width] : [c?.width, c?.height];
  const fit = (W, H) => (pw && ph ? Math.min(W / pw, H / ph) : 1);
  return { sheet: `translate${compact.matches ? "Y" : "X"}(${d}px)`,
    screen: `translate(${(vw - w) / 2}px, ${(vh - h) / 2}px) scale(${fit(vw, vh) / fit(w, h)})` };
}
function placeLite(o, travel) {
  const f = liteFrame(o, travel);
  lite.style.transform = f.sheet;
  screenEl.style.transform = f.screen;
}
// where the sheet is now (from open), mid-slide or mid-drag; null at rest
function liteOffset(travel) {
  if (!slide && !lite.style.transform) return null;
  const m = new DOMMatrix(getComputedStyle(lite).transform);
  return (compact.matches ? m.m42 : m.m41) + (liteLaidOpen() ? 0 : travel);
}
function stopSlide() {
  if (!slide) return;
  for (const a of slide) { a.onfinish = null; a.cancel(); }
  slide = null;
}
// at rest: the state's own layout, no transforms
function settleLite(open) {
  stopSlide();
  lite.style.transform = screenEl.style.transform = "";
  if (liteLaidOpen() === open) return;
  document.body.classList.toggle("lite-open", open);
  if (rfb) rfb.scaleViewport = true; // rescale now, in this frame, not on noVNC's next one
}
function setLite(open, { from = null, keepFocus = false } = {}) {
  let travel = liteTravel();
  const o0 = from ?? liteOffset(travel);
  liteOpen = open;
  localStorage.setItem("beanjar-web-lite", open ? "open" : "closed");
  liteAttrs();
  if (!keepFocus && rfb) rfb.focus();
  stopSlide();
  if (!phone.matches || calm.matches) return settleLite(open);
  if (open && !liteLaidOpen()) { // lay it out open now; the sheet starts where it was
    document.body.classList.add("lite-open");
    if (rfb) rfb.scaleViewport = true;
    travel = liteTravel();
  }
  if (travel <= 0) return settleLite(open);
  const start = Math.min(travel, Math.max(0, o0 ?? (open ? travel : 0))), end = open ? 0 : travel;
  if (start === end) return settleLite(open);
  // keyframes along the way: the picture's scale is not linear in the offset when height-bound
  const frames = Array.from({ length: 7 }, (_, i) => liteFrame(start + (end - start) * i / 6, travel));
  const timing = { duration: Math.max(120, SLIDE_MS * Math.abs(end - start) / travel),
    easing: SLIDE_EASE, fill: "forwards" };
  lite.style.transform = screenEl.style.transform = "";
  slide = [lite.animate(frames.map((f) => ({ transform: f.sheet })), timing),
    screenEl.animate(frames.map((f) => ({ transform: f.screen })), timing)];
  slide[0].onfinish = () => settleLite(open);
}
// The grip (portrait): the sheet follows a finger (or mouse) dragged either way and, let go,
// settles open or collapsed by how far it went (a third of the way from where it started) or a
// flick; a tap toggles it. Keyboards and assistive technology use the Remote button instead.
let drag = null;
liteGrip.addEventListener("pointerdown", (e) => {
  if (e.button !== 0 || drag || !compact.matches) return;
  e.preventDefault(); // not a focus change or a text selection
  liteGrip.setPointerCapture(e.pointerId);
  const travel = liteTravel(), o = liteOffset(travel) ?? (liteLaidOpen() ? 0 : travel);
  stopSlide();
  placeLite(o, travel);
  drag = { id: e.pointerId, y: e.clientY, o0: o, o, travel, moved: false, trail: [[e.timeStamp, o]] };
  lite.classList.add("dragging");
});
liteGrip.addEventListener("pointermove", (e) => {
  if (!drag || e.pointerId !== drag.id) return;
  const o = Math.min(drag.travel, Math.max(0, drag.o0 + e.clientY - drag.y));
  drag.o = o;
  drag.trail = drag.trail.filter(([t]) => e.timeStamp - t < 120).concat([[e.timeStamp, o]]);
  if (Math.abs(e.clientY - drag.y) > 6) drag.moved = true;
  placeLite(o, drag.travel);
});
function liteDragEnd(e) {
  if (!drag || e.pointerId !== drag.id) return;
  // the speed over the last 120 ms or so (px/ms, down positive), none if the finger rested
  const { o, o0, travel, moved } = drag, [t0, y0] = drag.trail[0], [t1, y1] = drag.trail.at(-1);
  const v = e.timeStamp - t1 < 100 && t1 > t0 ? (y1 - y0) / (t1 - t0) : 0, d = o - o0;
  drag = null;
  lite.classList.remove("dragging");
  let open = liteOpen; // a cancelled gesture goes back
  if (e.type === "pointerup" && !moved) open = !liteOpen; // a tap
  else if (e.type === "pointerup") {
    if (v > 0.5 && d > 24) open = false;
    else if (v < -0.5 && d < -24) open = true;
    else open = o < travel * (o0 < travel / 2 ? 1 / 3 : 2 / 3);
  }
  setLite(open, { from: o });
}
liteGrip.addEventListener("pointerup", liteDragEnd);
liteGrip.addEventListener("pointercancel", liteDragEnd);
liteGrip.addEventListener("lostpointercapture", liteDragEnd);
// turned, or grown past a phone: no slide survives a change of layout
for (const q of [compact, phone]) q.addEventListener("change", () => { settleLite(liteOpen); remoteAttrs(); });
lite.addEventListener("contextmenu", (e) => e.preventDefault()); // a long press is a held key
// a key the layout draws otherwise than the usual remote (NetCast's HOME is "NetCast") gets the
// layout's words, or its glyph's icon (as remote.js draws it)
const GLYPH_ICONS = { POWER: "power", INPUT: "box-arrow-in-right", SEARCH: "search",
  MUTE: "volume-mute", HOME: "house", GEAR: "gear", PLUS: "plus-lg", MINUS: "dash-lg",
  CH_UP: "caret-up-fill", CH_DOWN: "caret-down-fill", UP: "caret-up-fill", DOWN: "caret-down-fill",
  LEFT: "caret-left-fill", RIGHT: "caret-right-fill", STOP: "stop-fill", PLAY: "play-fill",
  PAUSE: "pause-fill", PREV: "skip-start-fill", NEXT: "skip-end-fill" };
function relabel(b) {
  const k = layout.keys.find((k) => k.name === b.dataset.key);
  if (!k || ((k.glyph || "") === (b.dataset.glyph || "") && (k.text || "") === (b.dataset.text || ""))) return;
  const icon = b.querySelector(".bi");
  if (k.text) {
    const t = document.createElement("b");
    t.className = "lite-text";
    t.textContent = k.text;
    b.replaceChildren(t);
    b.setAttribute("aria-label", k.text.replace(/\s+/g, " "));
  } else if (icon && GLYPH_ICONS[k.glyph]) {
    icon.style.setProperty("--i", `url(/icons/${GLYPH_ICONS[k.glyph]}.svg)`);
  }
}

// Desktop keeps its independent popup; portrait embeds the same remote page above the toolbar.
function closeRemotePanel() {
  remotePop.hidePopover();
  remoteButton.focus();
}
function openRemotePanel() {
  if (!remotePop.matches(":popover-open")) remotePop.showPopover();
}
// on phones the header's remote button shows or hides the lite remote; the whole remote is its
// 123 key's and More's
fullRemote.addEventListener("click", openRemotePanel);
document.getElementById("lite-full").addEventListener("click", openRemotePanel);
function toggleRemoteWindow() {
  if (remoteWin && !remoteWin.closed) return remoteWin.close();
  const w0 = layout ? layout.width : 188, h0 = layout ? layout.height : 652;
  const s = Math.min(1.25, (window.screen.availHeight || 900) * 0.9 / h0);
  remoteWin = window.open(`/remote.html?vm=${encodeURIComponent(spec)}`,
    `beanjar-remote-${host}-${name}`.replace(/[^A-Za-z0-9_-]/g, "_"),
    `popup,width=${Math.round(w0 * s)},height=${Math.round(h0 * s)}`);
}
remoteButton.addEventListener("click", (e) => {
  // a keyboard's click (no pointer, detail 0) leaves the focus on the button
  if (phone.matches) return setLite(!liteOpen, { keepFocus: e.detail === 0 });
  if (!inputMode()) setFloat(floatEl.hidden, { keepFocus: e.detail === 0 }); // else the menu (popovertarget)
});
inputRemote.addEventListener("click", () => { inputMenu.hidePopover(); setFloat(floatEl.hidden); });
remoteWindowItem.addEventListener("click", () => { toggleRemoteWindow(); remoteAttrs(); });

// The desktop's remote floats over the picture (in #screen, so fullscreen keeps it), as the GTK
// remote window does: its own body the only frame, a close control on its chin, moved by a press
// on the body between the keys once it has gone THRESHOLD px (the GTK remote's drag threshold); a
// press on a key is that key's however it wobbles, and the close control never drags. It stays in
// the picture's area MARGIN px from its edges, snaps to an edge within SNAP px, and is kept per
// browser as its distance from the nearer edges (beanjar-web-remote-float), so it stays in its
// corner as the window changes; top right at first. A window too short for it scales it down.
// Presses on it never take the focus: the PC's keys stay the TV's (noVNC's).
const floatPanel = floatEl.querySelector(".remote-panel");
const floatClose = document.getElementById("float-close");
const FLOAT_KEY = "beanjar-web-remote-float";
const MARGIN = 12, SNAP = 24, THRESHOLD = 8;
let floatDrawn = false;
function sizeFloat() {
  const b = layout.body;
  const s = Math.min(1, (screenEl.clientHeight - 2 * MARGIN) / b.h, (screenEl.clientWidth - 2 * MARGIN) / b.w);
  const k = Math.max(0.25, s);
  Object.assign(floatEl.style, { width: `${b.w * k}px`, height: `${b.h * k}px` });
  Object.assign(floatPanel.style, { transform: `scale(${k})`, left: `${-b.x * k}px`, top: `${-b.y * k}px` });
  // the chin: between the lowest key and the body's bottom edge
  const lowest = Math.max(b.y, ...layout.keys.map((key) => key.y + key.h));
  floatClose.style.top = `${((lowest + b.y + b.h) / 2 - b.y) * k}px`;
  floatEl.dataset.scale = k.toFixed(3); // the tests
}
function placeFloat(x, y, save) {
  const maxX = Math.max(MARGIN, screenEl.clientWidth - floatEl.offsetWidth - MARGIN);
  const maxY = Math.max(MARGIN, screenEl.clientHeight - floatEl.offsetHeight - MARGIN);
  x = Math.max(MARGIN, Math.min(maxX, x));
  y = Math.max(MARGIN, Math.min(maxY, y));
  if (x - MARGIN < SNAP) x = MARGIN; else if (maxX - x < SNAP) x = maxX;
  if (y - MARGIN < SNAP) y = MARGIN; else if (maxY - y < SNAP) y = maxY;
  floatEl.style.left = `${x}px`;
  floatEl.style.top = `${y}px`;
  if (!save) return;
  const w = screenEl.clientWidth, h = screenEl.clientHeight;
  const right = x + floatEl.offsetWidth / 2 > w / 2, bottom = y + floatEl.offsetHeight / 2 > h / 2;
  localStorage.setItem(FLOAT_KEY, JSON.stringify({ right, bottom,
    dx: Math.round(right ? w - x - floatEl.offsetWidth : x), dy: Math.round(bottom ? h - y - floatEl.offsetHeight : y) }));
}
function restoreFloat() {
  if (floatEl.hidden || !layout) return;
  sizeFloat();
  let st = null;
  try { st = JSON.parse(localStorage.getItem(FLOAT_KEY) || "null"); } catch (_) { /* a default spot */ }
  const w = screenEl.clientWidth, h = screenEl.clientHeight;
  if (!st) return placeFloat(w, 0, false);
  placeFloat(st.right ? w - st.dx - floatEl.offsetWidth : st.dx, st.bottom ? h - st.dy - floatEl.offsetHeight : st.dy, false);
}
function setFloat(open, { keepFocus = false } = {}) {
  if (open && !layout) return; // nothing to draw yet
  if (open && !floatDrawn) {
    drawRemote(floatPanel, layout, remoteSend);
    floatDrawn = true;
  }
  if (floatEl.hidden === !open) return;
  floatEl.hidden = !open;
  restoreFloat();
  remoteAttrs();
  if (!keepFocus && rfb) rfb.focus();
}
new ResizeObserver(restoreFloat).observe(screenEl);
let floatPress = null;
floatEl.addEventListener("pointerdown", (e) => {
  e.preventDefault(); // the focus stays where it was
  if (e.button !== 0 || floatPress || e.target.closest(".rk, #float-close")) return;
  floatEl.setPointerCapture(e.pointerId);
  floatPress = { id: e.pointerId, x: e.clientX, y: e.clientY, left: floatEl.offsetLeft, top: floatEl.offsetTop, moved: false };
});
floatEl.addEventListener("pointermove", (e) => {
  const p = floatPress;
  if (!p || e.pointerId !== p.id) return;
  const dx = e.clientX - p.x, dy = e.clientY - p.y;
  if (!p.moved && Math.hypot(dx, dy) < THRESHOLD) return;
  p.moved = true;
  floatEl.classList.add("dragging");
  placeFloat(p.left + dx, p.top + dy, false);
});
function floatDragEnd(e) {
  const p = floatPress;
  if (!p || e.pointerId !== p.id) return;
  floatPress = null;
  floatEl.classList.remove("dragging");
  if (p.moved) placeFloat(floatEl.offsetLeft, floatEl.offsetTop, true);
}
floatEl.addEventListener("pointerup", floatDragEnd);
floatEl.addEventListener("pointercancel", floatDragEnd);
floatEl.addEventListener("lostpointercapture", floatDragEnd);
floatClose.addEventListener("click", () => setFloat(false));
inputMenu.addEventListener("toggle", (event) => {
  remoteAttrs();
  if (event.newState === "open") { placeControls(); inputRemote.focus(); }
});
inputMenu.addEventListener("keydown", (event) => {
  const items = [inputRemote, inputTouch], i = items.indexOf(document.activeElement);
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    items[(i + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length].focus();
  } else if (event.key === "Escape") {
    event.preventDefault();
    inputMenu.hidePopover();
    remoteButton.focus();
  }
});
remotePop.addEventListener("toggle", (event) => {
  const open = event.newState === "open";
  remoteAttrs();
  if (open) {
    // the frame as wide as the remote's body is at its height (remote.js fits the body to it)
    if (layout?.body) remotePop.style.setProperty("--remote-ratio", `${layout.body.w} / ${layout.body.h}`);
    remoteFrame.src = `/remote.html?vm=${encodeURIComponent(spec)}`;
    placeControls();
    document.getElementById("remote-close").focus();
  } else remoteFrame.removeAttribute("src"); // end its input WebSocket when hidden
});
document.getElementById("remote-close").addEventListener("click", closeRemotePanel);
remotePop.addEventListener("keydown", (event) => {
  if (event.key === "Escape") { event.preventDefault(); closeRemotePanel(); }
});
addEventListener("message", (event) => {
  if (event.origin === location.origin && event.source === remoteFrame.contentWindow &&
      event.data === "beanjar-close-remote") closeRemotePanel();
});
window.addEventListener("pagehide", () => { if (remoteWin) remoteWin.close(); });
setInterval(remoteAttrs, 300); // a remote window closed on its own

// a key the layout has no code for is disabled, and every remote control while the VM is off
function applyVmState() {
  const off = vmRow?.state === "Off";
  for (const b of remoteKeys()) b.disabled = off || (!!layout && codeOf(b.dataset.key) === null);
  for (const b of [sound, remoteButton, shotButton, fullRemote, document.getElementById("lite-full")]) {
    b.disabled = off;
  }
  if (off) for (const pop of [remotePop, inputMenu]) if (pop.matches(":popover-open")) pop.hidePopover();
  if (off) setFloat(false);
  stopVm.hidden = stopSep.hidden = !vmRow || off;
}

fetch(`${base}/layout.json`).then((r) => r.json()).then((l) => {
  if (l.error) throw new Error(l.error);
  layout = l;
  applyVmState();
  for (const b of lite.querySelectorAll("button[data-key]")) relabel(b);
}).catch((e) => console.warn(`no remote layout: ${e.message}`));
connect();
