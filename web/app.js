import { Client, DeviceError, NAME_RE, batteryLevel, groupByDay, stemOf } from "./proto.js";
import { readZip } from "./zipread.js";
import { ZipWriter } from "./zip.js";
import { FILTERS, WebUsbTransport, isClip, isSupported } from "./webusb.js";

const $ = (id) => document.getElementById(id);
const ZIP_LIMIT = 256 * 1024 * 1024; // start a new zip beyond 256 MiB: each zip is held in memory until saved
const STORE_KEY = "magpie.downloaded";
const enc = new TextEncoder();

const state = {
  device: null,
  transport: null,
  client: null,
  connected: false,
  busy: false,
  files: [],
  days: [],
  selected: new Set(), // days
  savedNames: [], // zip names saved by the current download (for the completion banner)
  verified: new Map(), // name -> {name, sha256, size, clipChecked}: passed zip verification, deletable from the Clip
};

// -- helpers -------------------------------------------------------------------------------------------------

function fmtBytes(n) {
  if (n == null) return "-";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  if (n < 1024 ** 3) return (n / 1024 ** 2).toFixed(1) + " MB";
  return (n / 1024 ** 3).toFixed(2) + " GB";
}

function fmtDay(d) {
  return `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}`;
}

// Completion banner: hard to miss (big tick, vibration, title), cleared when the next download starts.
function showDone(info) {
  const el = $("done");
  if (!info) {
    el.hidden = true;
    document.title = "Magpie";
    return;
  }
  const mb = (info.bytes / 1048576).toFixed(1);
  el.textContent = "";
  const h = Object.assign(document.createElement("strong"), { textContent: "\u2714 Download complete" });
  const p1 = Object.assign(document.createElement("p"), {
    textContent: `${info.files} files, ${mb} MB, saved as: ${info.names.join(", ")}` + (info.failed ? ` (${info.failed} failed, see below)` : ""),
  });
  const p2 = Object.assign(document.createElement("p"), { textContent: "Next: Verify saved zip, then Delete becomes available." });
  el.append(h, p1, p2);
  el.classList.toggle("warn", info.failed > 0);
  el.hidden = false;
  document.title = "\u2714 Magpie";
  try { if (navigator.vibrate) navigator.vibrate([150, 80, 150]); } catch (_) { /* not allowed: ignore */ }
  el.scrollIntoView({ behavior: "smooth", block: "center" });
}

function say(text, warn = false) {
  const m = $("message");
  m.textContent = text;
  m.hidden = !text;
  m.classList.toggle("warn", warn);
}

function loadHistory() {
  try {
    return JSON.parse(localStorage.getItem(STORE_KEY) || "{}") || {};
  } catch (_) {
    return {};
  }
}

function saveHistory(entries) {
  try {
    const h = loadHistory();
    for (const e of entries) h[e.name] = e.sha256;
    localStorage.setItem(STORE_KEY, JSON.stringify(h));
  } catch (_) {}
}

function setBusy(b) {
  state.busy = b;
  render();
}

// -- connection ----------------------------------------------------------------------------------------------

function setConnState(text, on) {
  const c = $("conn");
  c.textContent = text;
  c.className = "badge " + (on ? "on" : "off");
}

async function teardown(msg) {
  const t = state.transport;
  state.connected = false;
  state.transport = null;
  state.client = null;
  state.device = null;
  state.files = [];
  state.days = [];
  state.selected = new Set();
  state.status = null;
  setConnState("Not connected", false);
  if (t) t.close().catch(() => {});
  if (msg) say(msg, true);
  render();
}

async function attach(device) {
  if (state.connected || state.busy) return;
  setBusy(true);
  setConnState("Connecting...", false);
  say("");
  try {
    const transport = await WebUsbTransport.open(device);
    const client = new Client(transport);
    state.device = device;
    state.transport = transport;
    state.client = client;
    const st = await client.resync();
    state.connected = true;
    setConnState("Connected", true);
    await refresh(st);
  } catch (e) {
    await teardown("Could not connect: " + (e.message || e));
  } finally {
    setBusy(false);
  }
}

async function onConnectClick() {
  if (state.connected) return;
  try {
    const device = await navigator.usb.requestDevice({ filters: FILTERS });
    await attach(device);
  } catch (e) {
    if (e.name !== "NotFoundError") say("Connect failed: " + (e.message || e), true); // NotFoundError = user cancelled
  }
}

function handleLinkError(e) {
  // Any transport/protocol error means the stream cannot be trusted any more: drop the session.
  const gone = !state.device || !state.connected;
  teardown(gone ? "The Clip was disconnected. Plug it in and tap Connect to start again." : "USB error: " + (e.message || e) + ". Reconnect to try again.");
}

// -- data ----------------------------------------------------------------------------------------------------

async function refresh(status) {
  const c = state.client;
  const st = status || (await c.status());
  state.files = await c.listAll();
  state.days = groupByDay(state.files);
  const hist = loadHistory();
  state.selected = new Set(state.days.filter((d) => !d.files.every((f) => f.name in hist)).map((d) => d.day));
  state.status = st;
  state.offsetMs = null;
  render();
  const m = await c.measureOffset(8);
  state.offsetMs = m.offsetMs;
  render();
}

function render() {
  const connected = state.connected;
  $("connect").disabled = connected || state.busy;
  $("connect").textContent = connected ? "Connected" : "Connect";
  $("statusCard").hidden = !connected || !state.status;
  $("filesCard").hidden = !connected;
  $("zipPick").disabled = state.busy;
  renderDelete();
  if (!connected) return;

  const st = state.status;
  if (st) {
    $("stPhotos").textContent = st.photos;
    $("stSnaps").textContent = `${st.snaps} (${st.snap_times} with .time)`;
    $("stBytes").textContent = fmtBytes(st.bytes);
    $("stFree").textContent = fmtBytes(st.free_kb * 1024);
    const lvl = batteryLevel(st.battery_raw);
    $("stBattery").textContent = st.battery_raw == null ? "unknown" : `${st.battery_raw} (level ${lvl} of 4)`;
    $("stCharging").textContent = st.charging == null ? "unknown" : st.charging ? "yes" : "no";
    const off = state.offsetMs;
    $("stOffset").textContent = off == null ? "measuring..." : `${(off / 1000).toFixed(2)} s`;
    $("syncClock").classList.toggle("attn", off != null && Math.abs(off) > 2000);
    $("syncClock").disabled = state.busy;
  }

  const hist = loadHistory();
  const ul = $("days");
  ul.textContent = "";
  for (const d of state.days) {
    const li = document.createElement("li");
    const label = document.createElement("label");
    label.className = "check";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = state.selected.has(d.day);
    cb.disabled = state.busy;
    cb.addEventListener("change", () => {
      cb.checked ? state.selected.add(d.day) : state.selected.delete(d.day);
      render();
    });
    const txt = document.createElement("span");
    txt.textContent = fmtDay(d.day);
    label.append(cb, txt);
    if (d.files.every((f) => f.name in hist)) {
      const done = document.createElement("span");
      done.className = "done";
      done.textContent = "downloaded";
      label.append(done);
    }
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = `${d.files.length} files, ${fmtBytes(d.size)}`;
    label.append(meta);
    li.append(label);
    ul.append(li);
  }
  if (state.days.length === 0) {
    const li = document.createElement("li");
    li.className = "muted";
    li.textContent = "No files on the Clip.";
    ul.append(li);
  }
  const sel = selectedFiles();
  $("selAll").checked = state.days.length > 0 && state.selected.size === state.days.length;
  $("selAll").disabled = state.busy || state.days.length === 0;
  $("selSummary").textContent = `${sel.length} files, ${fmtBytes(sel.reduce((a, f) => a + f.size, 0))} selected`;
  $("download").disabled = state.busy || sel.length === 0;
}

function renderDelete() {
  const list = [...state.verified.values()];
  const n = list.length;
  $("deleteCard").hidden = n === 0 && $("deleteResults").hidden;
  const size = fmtBytes(list.reduce((a, f) => a + f.size, 0));
  $("deleteText").textContent = n
    ? `${n} files are intact in the zip(s) you checked${state.connected ? "" : ". Connect the Clip to delete them"}. Only these can be deleted from the Clip.`
    : "Nothing to delete. Verify a saved zip first.";
  $("delete").textContent = `Delete ${n} verified files from the Clip (${size})`;
  $("delete").disabled = state.busy || n === 0 || !state.connected;
}

function selectedFiles() {
  return state.days.filter((d) => state.selected.has(d.day)).flatMap((d) => d.files);
}

// -- download ------------------------------------------------------------------------------------------------

class Disconnected extends Error {}

function saveZip(parts, name) {
  const url = URL.createObjectURL(new Blob(parts, { type: "application/zip" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10 * 60 * 1000);
}

// Build and save one zip from verified results. Deleting from the Clip is only offered after the saved zip
// has been read back (see onVerifyZips).
function flushZip(batch) {
  if (batch.length === 0) return [];
  const sorted = [...batch].sort((a, b) => (a.name < b.name ? -1 : 1));
  const zip = new ZipWriter();
  let sums = "";
  for (const r of sorted) {
    zip.addFile(r.name, r.data, new Date((r.mtime || 0) * 1000));
    sums += `${r.sha256}  ${r.name}\n`;
  }
  zip.addFile("SHA256SUMS", enc.encode(sums));
  const name = `magpie_${stemOf(sorted[0].name)}_to_${stemOf(sorted[sorted.length - 1].name)}.zip`;
  saveZip(zip.finish(), name);
  state.savedNames.push(name);
  return sorted.length;
}

async function onDownload() {
  const files = selectedFiles();
  if (files.length === 0 || state.busy || !state.connected) return;
  const c = state.client;
  const totalBytes = files.reduce((a, f) => a + f.size, 0);
  const failed = [];
  let batch = [];
  let batchBytes = 0;
  let doneFiles = 0;
  let doneBytes = 0;
  let savedZips = 0;
  const t0 = performance.now();
  setBusy(true);
  state.savedNames = [];
  showDone(null);
  $("progress").hidden = false;
  $("failed").hidden = true;
  $("failed").textContent = "";
  $("deleteResults").hidden = true;
  say("");

  const showProgress = (partial = 0) => {
    const bytes = doneBytes + partial;
    const secs = Math.max(0.001, (performance.now() - t0) / 1000);
    $("bar").max = Math.max(1, totalBytes);
    $("bar").value = bytes;
    $("progressText").textContent = `${doneFiles} of ${files.length} files, ${(bytes / 1048576).toFixed(1)} of ${(totalBytes / 1048576).toFixed(1)} MB, ${(bytes / 1048576 / secs).toFixed(2)} MB/s`;
  };
  showProgress();

  let aborted = null;
  try {
    for (const f of files) {
      if (!state.connected) throw new Disconnected();
      const r = await c.downloadFile(f.name, { progress: (_n, size) => showProgress(size) });
      if (!state.connected) throw new Disconnected();
      doneFiles++;
      doneBytes += f.size;
      if (!r.ok) {
        failed.push(`${f.name}: ${r.error}${r.attempts > 1 ? ` (after ${r.attempts} attempts)` : ""}`);
        showProgress();
        continue;
      }
      if (batch.length && batchBytes + r.size > ZIP_LIMIT) {
        flushZip(batch);
        savedZips++;
        batch = [];
        batchBytes = 0;
      }
      batch.push({ ...r, mtime: f.mtime });
      batchBytes += r.size;
      showProgress();
    }
  } catch (e) {
    aborted = e;
  }

  // Save whatever was verified, even after an interruption.
  try {
    if (batch.length) {
      flushZip(batch);
      savedZips++;
    }
  } catch (e) {
    failed.push("Could not build the zip: " + (e.message || e));
  }
  batch = [];

  if (aborted) {
    const gone = aborted instanceof Disconnected || !state.connected;
    const saved = savedZips ? ` ${savedZips} zip(s) with the files verified so far were saved.` : "";
    if (gone) await teardown("The Clip was disconnected during the download, so it stopped." + saved + " Plug it in, tap Connect and start again." + (savedZips ? " To delete the saved files from the Clip, use Verify saved zip." : ""));
    else await teardown("Download stopped by a USB error: " + (aborted.message || aborted) + "." + saved + " Reconnect to start again.");
  } else {
    say(`Downloaded ${doneFiles - failed.length} of ${files.length} files in ${savedZips} zip(s).` + (savedZips ? " Saved. To delete these from the Clip, tap Verify saved zip and pick the file(s)." : ""), failed.length > 0);
    if (savedZips) showDone({ files: doneFiles - failed.length, bytes: doneBytes, names: state.savedNames, failed: failed.length });
  }
  if (failed.length) {
    const ul = $("failed");
    ul.textContent = "";
    ul.append(...failed.map((t) => Object.assign(document.createElement("li"), { textContent: t })));
    ul.hidden = false;
  }
  setBusy(false);
}

// -- delete --------------------------------------------------------------------------------------------------

async function onDelete() {
  const list = [...state.verified.values()];
  if (!list.length || state.busy || !state.connected) return;
  const total = fmtBytes(list.reduce((a, f) => a + f.size, 0));
  if (!confirm(`Delete ${list.length} files (${total}) from the Clip?\n\nMake sure the zip is saved where you want to keep it. This cannot be undone.`)) return;
  setBusy(true);
  const ul = $("deleteResults");
  try {
    const res = await state.client.deleteFiles(list);
    const okNames = new Set(res.filter((r) => r.ok).map((r) => r.name));
    for (const n of okNames) state.verified.delete(n);
    ul.textContent = "";
    for (const r of res) {
      const li = document.createElement("li");
      li.className = r.ok ? "ok" : "";
      li.textContent = `${r.ok ? "deleted" : "NOT deleted"}: ${r.name}${r.ok ? "" : " (" + (r.err || "failed") + ")"}`;
      ul.append(li);
    }
    ul.hidden = false;
    say(`${okNames.size} of ${list.length} files deleted from the Clip.`, okNames.size !== list.length);
    await refresh();
  } catch (e) {
    if (e instanceof DeviceError) say("The Clip refused: " + e.message, true);
    else handleLinkError(e);
  } finally {
    setBusy(false);
  }
}

// -- verify saved zip ----------------------------------------------------------------------------------------

async function onVerifyZips(ev) {
  const picked = [...ev.target.files];
  ev.target.value = ""; // allow picking the same file again
  if (!picked.length || state.busy) return;
  setBusy(true);
  const out = $("verifyResults");
  out.textContent = "";
  out.hidden = false;
  $("vprogress").hidden = false;
  const add = (text, cls = "") => {
    const li = document.createElement("li");
    li.textContent = text;
    li.className = cls;
    out.append(li);
  };
  let intact = 0;
  let bad = 0;
  const good = [];
  try {
    for (let k = 0; k < picked.length; k++) {
      const f = picked[k];
      const r = await readZip(f, {
        onProgress: (done, total, name) => {
          $("vbar").max = Math.max(1, total);
          $("vbar").value = done;
          $("vtext").textContent = `Zip ${k + 1} of ${picked.length} (${f.name}): ${(done / 1048576).toFixed(1)} of ${(total / 1048576).toFixed(1)} MB, ${name}`;
        },
      });
      if (r.fatal) {
        add(`${f.name}: cannot be read: ${r.fatal}`);
        bad++;
        continue;
      }
      for (const e of r.entries) {
        if (e.name === "SHA256SUMS") continue;
        if (!e.ok) {
          bad++;
          continue;
        }
        const item = { name: e.name, sha256: e.sha256, size: e.size, clipChecked: false };
        if (NAME_RE.test(e.name)) good.push({ item, zip: f.name });
        else add(`${f.name}: ${e.name} is intact but not a Clip file name, ignored`);
      }
      for (const p of r.problems) add(`${f.name}: ${p}`);
    }
    // Compare with the Clip's own HASH when connected.
    for (const { item, zip } of good) {
      if (state.connected) {
        try {
          const h = await state.client.hash(item.name);
          if (h.sha256 !== item.sha256 || h.size !== item.size) {
            add(`${zip}: ${item.name} differs from the file on the Clip, not eligible for deletion`);
            bad++;
            continue;
          }
          item.clipChecked = true;
        } catch (e) {
          if (!(e instanceof DeviceError)) throw e;
          add(`${item.name}: no longer on the Clip (${e.errnoName})`, "ok");
          intact++;
          saveHistory([item]);
          continue;
        }
      }
      intact++;
      saveHistory([item]);
      state.verified.set(item.name, item);
    }
    const head = document.createElement("li");
    head.className = bad ? "" : "ok";
    head.textContent = `${intact} files intact${bad ? `, ${bad} with problems` : ""}.` + (state.connected ? "" : " The Clip is not connected, so they were not compared with it; connect to delete them.");
    out.prepend(head);
  } catch (e) {
    if (e instanceof DeviceError) say("The Clip refused: " + e.message, true);
    else handleLinkError(e);
  } finally {
    $("vprogress").hidden = true;
    setBusy(false);
  }
}

// -- clock ---------------------------------------------------------------------------------------------------

async function onSyncClock() {
  if (state.busy || !state.connected) return;
  setBusy(true);
  try {
    const r = await state.client.syncClock(8);
    const rep = r.reply;
    say(`Clip clock set (offset was ${(r.offsetBeforeMs / 1000).toFixed(2)} s, best round trip ${r.bestRttMs.toFixed(1)} ms, RTC ${rep.rtc}).`, rep.rtc !== "ok");
    state.status = await state.client.status();
    const m = await state.client.measureOffset(8);
    state.offsetMs = m.offsetMs;
  } catch (e) {
    if (e instanceof DeviceError) say("The Clip refused: " + e.message, true);
    else handleLinkError(e);
  } finally {
    setBusy(false);
  }
}

// -- service worker ------------------------------------------------------------------------------------------

function initServiceWorker() {
  const off = $("offline");
  if (!("serviceWorker" in navigator)) {
    off.textContent = "Offline support is not available in this browser";
    return;
  }
  let updating = false;
  const showUpdate = (reg) => {
    $("update").hidden = false;
    $("update").onclick = () => {
      updating = true;
      if (reg.waiting) reg.waiting.postMessage("SKIP_WAITING");
    };
  };
  const ready = () => (off.textContent = "Ready offline");
  navigator.serviceWorker.register("sw.js").then((reg) => {
    if (reg.waiting && navigator.serviceWorker.controller) showUpdate(reg);
    else if (reg.active) ready();
    reg.addEventListener("updatefound", () => {
      const w = reg.installing;
      if (!w) return;
      w.addEventListener("statechange", () => {
        if (w.state === "installed" && navigator.serviceWorker.controller) showUpdate(reg);
        else if (w.state === "activated") ready();
      });
    });
  }).catch((e) => {
    off.textContent = "Offline support failed: " + (e.message || e);
  });
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (updating) location.reload();
  });
}

// -- init ----------------------------------------------------------------------------------------------------

function init() {
  initServiceWorker();
  $("connect").addEventListener("click", onConnectClick);
  $("download").addEventListener("click", onDownload);
  $("delete").addEventListener("click", onDelete);
  $("zipPick").addEventListener("change", onVerifyZips);
  $("syncClock").addEventListener("click", onSyncClock);
  $("selAll").addEventListener("change", (e) => {
    state.selected = new Set(e.target.checked ? state.days.map((d) => d.day) : []);
    render();
  });
  if (!isSupported()) {
    $("unsupported").hidden = false;
    $("connect").disabled = true;
    return;
  }
  navigator.usb.addEventListener("connect", (e) => {
    if (isClip(e.device) && !state.connected) attach(e.device);
  });
  navigator.usb.addEventListener("disconnect", (e) => {
    if (e.device === state.device || (state.device && isClip(e.device))) {
      teardown(state.busy ? null : "The Clip was disconnected. Plug it in to continue.");
    }
  });
  navigator.usb.getDevices().then((devs) => {
    const d = devs.find(isClip);
    if (d) attach(d);
  });
  render();
}

init();
