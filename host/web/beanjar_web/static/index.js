// SPDX-FileCopyrightText: Mariotaku <git@mariotaku.me> and the Beanjar contributors
// SPDX-License-Identifier: GPL-2.0-or-later
// VM cards, grouped by host. Only visible Running cards with beanjar-vnc get small previews, and
// Off cards their last screen;
// the server coalesces screenshot requests from several pages. No framework or build step.
// The header filters the cards by name, tag or host, and puts running VMs first in each host's
// section (kept per browser); each card's buttons are its state's actions: Start, or Open and Stop.
import { stopDialog, reasonLine } from "/stop.js";

const main = document.getElementById("vms");
const icon = (name) => `<i class="bi" style="--i: url(/icons/${name}.svg)"></i>`;
const DOT = { Running: "ok", Starting: "warn", Stopping: "warn", Error: "bad" };
const requesting = new Set();
const failures = new Map(); // HOST/NAME: a start the server did not take, shown on its card
let timer, refreshing = false, rows = null, listError = null;
const filter = document.getElementById("filter");
const runningFirst = document.getElementById("running-first");
const previews = new Map();
const observer = new IntersectionObserver((entries) => {
  for (const entry of entries) {
    const preview = previews.get(entry.target.dataset.spec);
    if (!preview || preview.card !== entry.target) continue;
    preview.visible = entry.isIntersecting;
    if (preview.visible) updatePreview(preview);
  }
});

async function updatePreview(preview) {
  if (!preview.card.isConnected || !preview.visible || document.hidden || !preview.eligible || preview.pending || Date.now() - preview.updated < 5000) return;
  if (preview.once && preview.updated) return;
  preview.pending = true;
  preview.controller = new AbortController();
  try {
    const response = await fetch(`${preview.base}/screenshot.png?thumbnail=1`, { signal: preview.controller.signal });
    if (!response.ok || response.status === 204) return;
    const blob = await response.blob();
    if (blob.type !== "image/png" || !preview.eligible) return;
    const old = preview.url;
    preview.url = URL.createObjectURL(blob);
    preview.rotation = response.headers.get("X-Beanjar-Rotation") || "0";
    preview.image.dataset.rotation = preview.rotation;
    preview.image.src = preview.url;
    preview.image.hidden = false;
    preview.placeholder.hidden = true;
    if (old) URL.revokeObjectURL(old);
  } catch (_) {
    // The placeholder stays when a VM stops, a host disappears, or a page hides.
  } finally {
    preview.pending = false;
    preview.updated = Date.now();
  }
}
setInterval(() => { for (const preview of previews.values()) updatePreview(preview); }, 1000);
document.addEventListener("visibilitychange", () => {
  for (const preview of previews.values()) {
    if (document.hidden) preview.controller?.abort();
    else updatePreview(preview);
  }
});

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

// gui.fw_label: "<version>-..._DTV_<board>_..." as "<board> <version>"
function fwLabel(fw) {
  const [version, ...rest] = (fw || "").split("-");
  const parts = rest.join("-").split("_");
  const i = parts.indexOf("DTV");
  return i >= 0 && i < parts.length - 1 && /^[\d.]+$/.test(version) ? `${parts[i + 1]} ${version}` : fw || "";
}
const mem = (m) => (m >= 1024 && m % 1024 === 0 ? `${m / 1024} GiB` : `${m} MiB`);
function duration(s) {
  s = Math.floor(s);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  const mm = String(m).padStart(2, "0"), ss = String(s % 60).padStart(2, "0");
  return d ? `${d}d ${h}:${mm}` : h ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
}

function card(vm) {
  const host = vm.host || "local";
  const spec = `${host}/${vm.name}`;
  const base = `/vm/${encodeURIComponent(host)}/${encodeURIComponent(vm.name)}`;
  const pending = vm.operation?.status === "pending";
  const state = pending ? (vm.operation.action === "start" ? "Starting" : "Stopping") : vm.state;
  const open = state === "Running" && vm.display === "dbus";
  const r = el("article", "vm-card" + (open ? " open" : ""));
  r.dataset.spec = spec;
  const indicatorState = vm.operation?.status === "failed" ? "Error" : state;
  const indicatorDetail = vm.operation?.status === "failed" ? vm.operation.message : vm.detail;
  const stateDetail = indicatorState + (indicatorDetail ? ` (${indicatorDetail})` : "");
  r.setAttribute("aria-label", `${vm.name}: ${stateDetail}`);
  let link;
  if (open) {
    link = el("a", "card-link");
    link.href = `${base}/`;
    link.setAttribute("aria-label", `Open ${spec}`);
    link.title = vm.viewer ? "Open (the GTK viewer holds it now: the page will say so)" : "Open the viewer";
    r.append(link);
  } else {
    r.title = state === "Running" ? `No picture: display ${vm.display || "none"}` : state;
  }
  const thumbnail = el("div", "thumbnail" + (state === "Off" ? " dim" : ""));
  if (indicatorState !== "Off") {
    const dot = el("span", "state-dot " + (DOT[indicatorState] || "off"));
    dot.setAttribute("role", "img");
    dot.setAttribute("aria-label", stateDetail);
    dot.title = stateDetail;
    (link || thumbnail).append(dot);
  }
  const placeholder = el("div", "placeholder");
  placeholder.innerHTML = icon("tv");
  if (state === "Running") placeholder.append(el("span", "", "No preview"));
  thumbnail.append(placeholder);
  const image = el("img", "preview");
  image.alt = `TV picture of ${spec}`;
  image.hidden = true;
  thumbnail.append(image);
  let preview = previews.get(spec);
  if (!preview) {
    preview = { updated: 0, pending: false, url: null };
    previews.set(spec, preview);
  }
  // Off: the last screen the manager kept, dimmed under the Start button (fetched once a state)
  const off = state === "Off";
  if (preview.off !== off) { preview.updated = 0; preview.off = off; }
  Object.assign(preview, { card: r, base, image, placeholder, visible: false, eligible: (open && vm.vnc) || off, once: off });
  if (preview.eligible && preview.url) {
    image.dataset.rotation = preview.rotation || "0";
    image.src = preview.url;
    image.hidden = false;
    placeholder.hidden = true;
  }
  r.append(thumbnail);
  const content = el("div", "card-info");
  const head = el("div", "card-head");
  head.append(el("h4", "name", vm.name));
  const actions = el("div", "card-actions");
  head.append(actions);
  content.append(head);
  const activity = [vm.platform, state !== "Off" && vm.uptime ? duration(vm.uptime) : ""];
  if (state === "Running" && vm.vnc) activity.push("VNC");
  if (state === "Running" && vm.viewer) activity.push("GTK viewer open");
  const details = el("div", "details", activity.filter(Boolean).join(" · "));
  details.title = [vm.platform, fwLabel(vm.firmware), vm.mem ? mem(vm.mem) : "", vm.smp ? `${vm.smp} CPUs` : ""].filter(Boolean).join(" · ");
  content.append(details);
  if (vm.tags?.length) {
    const tags = el("div", "tags");
    for (const tag of vm.tags) tags.append(el("span", "tag", tag));
    content.append(tags);
  }
  if (vm.reason && state !== "Off") {
    // why it runs (beanjar status): stop asks before it overrides it
    const reason = el("div", "reason");
    reason.innerHTML = icon("pin-angle");
    reason.append(vm.reason.text);
    reason.title = reasonLine(vm.reason);
    content.append(reason);
  }
  const operation = vm.operation;
  // Routine success is the new state. Retain refusals and the manager's explicit stop fallback.
  const result = operation?.status === "failed" ? operation.message :
    operation?.status === "succeeded" && operation.action === "stop" ?
      operation.message.split("\n").filter((line) => line.includes("syncing its disks and stopping it")).join("\n") : "";
  if (state === "Error" && vm.detail && vm.detail !== result) {
    const error = el("span", "operation error", vm.detail);
    error.setAttribute("role", "status");
    content.append(error);
  }
  if (result) {
    const message = el("span", "operation" + (operation.status === "failed" ? " error" : ""), result);
    message.setAttribute("role", "status");
    content.append(message);
  }
  if (failures.has(spec)) {
    const message = el("span", "operation error", failures.get(spec));
    message.setAttribute("role", "status");
    content.append(message);
  }
  r.append(content);
  // the state's actions, labelled, in the info row (no menu): Start; Open and Stop
  if (state === "Off") {
    const start = el("button", "card-action", "Start");
    start.insertAdjacentHTML("afterbegin", icon("play-fill"));
    start.setAttribute("aria-label", `Start ${spec}`);
    start.disabled = requesting.has(spec);
    start.addEventListener("click", () => startVm(spec, base, start));
    actions.append(start);
  } else if (state === "Running") {
    if (open) {
      const view = el("a", "card-action", "Open");
      view.href = `${base}/`;
      view.insertAdjacentHTML("afterbegin", icon("box-arrow-up-right"));
      view.setAttribute("aria-label", `Open ${spec}`);
      view.title = link.title;
      actions.append(view);
    }
    const stop = el("button", "card-action icon destructive");
    stop.innerHTML = icon("power");
    stop.title = "Stop…";
    stop.setAttribute("aria-label", `Stop ${spec}…`);
    stop.setAttribute("aria-haspopup", "dialog");
    stop.disabled = requesting.has(spec);
    stop.addEventListener("click", () => {
      failures.delete(spec);
      // taken: the card's buttons off until the list shows it Stopping
      stopDialog({ spec, base, reason: vm.reason, accepted: async () => {
        requesting.add(spec);
        render();
        try { await refresh(); } finally { requesting.delete(spec); render(); }
      } });
    });
    actions.append(stop);
  }
  return r;
}

// Start: the server takes it at once (202), the card follows the operation; a refusal of the
// request itself (another operation pending, the server gone) stays on the card until the next try
async function startVm(spec, base, button) {
  requesting.add(spec);
  failures.delete(spec);
  button.disabled = true;
  try {
    const response = await fetch(`${base}/start`, { method: "POST" });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || response.statusText);
  } catch (e) {
    failures.set(spec, `Cannot start: ${e.message}`);
  } finally {
    requesting.delete(spec);
    await refresh();
  }
}

function status(iconName, title, text) {
  const page = el("div", "status-page");
  const i = el("span", "big");
  i.innerHTML = icon(iconName);
  page.append(i, el("h2", "", title), el("p", "", text));
  main.replaceChildren(page);
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  clearTimeout(timer);
  try {
    const r = await fetch("/api/vms");
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || r.statusText);
    rows = body;
    listError = null;
  } catch (e) {
    rows = null;
    listError = e.message;
  }
  refreshing = false;
  timer = setTimeout(refresh, !rows ? 10000 : rows.some((vm) => vm.operation?.status === "pending") ? 2000 : 10000);
  render();
}

// what the filter matches: the name, the tags, the host (this machine's as "local" and as its
// section's title)
function matches(vm, words) {
  const hay = [vm.name, vm.host || "local", vm.host ? "" : "this machine", ...(vm.tags || [])]
    .map((t) => t.toLowerCase());
  return words.every((w) => hay.some((t) => t.includes(w)));
}

function render() {
  if (!rows) {
    observer.disconnect();
    return status("exclamation-triangle", "Cannot List the VMs", `beanjar list failed: ${listError}`);
  }
  if (!rows.length)
    return status("tv", "No Virtual Machines",
      "Create a VM from a webOS firmware file (.epk) or extracted firmware with the Beanjar window or beanjar create.");
  const words = filter.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const shown = rows.filter((vm) => matches(vm, words));
  if (!shown.length) {
    observer.disconnect();
    return status("search", "No Matching VMs", `No VM's name, tag or host matches “${filter.value.trim()}”.`);
  }
  const hosts = new Map();
  for (const vm of shown) {
    const h = vm.host || "local";
    if (!hosts.has(h)) hosts.set(h, []);
    hosts.get(h).push(vm);
  }
  const first = runningFirst.getAttribute("aria-pressed") === "true";
  const order = [...hosts.keys()].sort((a, b) => (a === "local" ? -1 : b === "local" ? 1 : a.localeCompare(b)));
  const sections = order.map((h) => {
    const sec = el("section");
    const head = el("h3");
    head.innerHTML = icon(h === "local" ? "pc-display" : "hdd-network");
    head.append(h === "local" ? "This Machine" : h);
    const list = el("div", "vm-grid");
    let vms = hosts.get(h);
    // running first, then starting or stopping, then off: the manager's order otherwise (a stable sort)
    const rank = (vm) => (vm.state === "Running" ? 0 : vm.state === "Off" ? 2 : 1);
    if (first) vms = [...vms].sort((a, b) => rank(a) - rank(b));
    for (const vm of vms) list.append(card(vm));
    sec.append(head);
    sec.append(list);
    return sec;
  });
  observer.disconnect();
  main.replaceChildren(...sections);
  const present = new Set(rows.map((vm) => `${vm.host || "local"}/${vm.name}`));
  for (const [spec, preview] of previews) {
    if (!present.has(spec)) {
      preview.controller?.abort();
      if (preview.url) URL.revokeObjectURL(preview.url);
      previews.delete(spec);
    } else if (preview.card.isConnected) observer.observe(preview.card);
  }
}

document.getElementById("refresh").addEventListener("click", refresh);
filter.addEventListener("input", render);
function setRunningFirst(on) {
  runningFirst.setAttribute("aria-pressed", String(on));
  runningFirst.title = on ? "Running VMs first: click for the manager's order" : "Show running VMs first";
  localStorage.setItem("beanjar-web-running-first", on ? "on" : "off");
}
setRunningFirst(localStorage.getItem("beanjar-web-running-first") !== "off");
runningFirst.addEventListener("click", () => {
  setRunningFirst(runningFirst.getAttribute("aria-pressed") !== "true");
  render();
});
// Log Out, with --auth password
fetch("/api/session")
  .then((r) => r.json())
  .then((s) => { document.getElementById("logout").hidden = s.auth !== "password"; })
  .catch(() => {});
refresh();
