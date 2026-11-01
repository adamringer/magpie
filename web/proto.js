// Transport-agnostic client for the Clip's USB data protocol v1 (docs/PHONE_USB.md).
// Mirrors tools/phone/usbclient.py. No browser-only APIs except globalThis.crypto (also present in Node).
//
// Transport = { write(Uint8Array): Promise<void>, read(maxLen): Promise<Uint8Array> }
// read() resolves with the next packet(s) (at least one byte); it may return more than maxLen (whole packets).

export const MAGIC = 0x3150474d; // "MGP1"
export const HDR_LEN = 16;
export const RESP_FLAG = 0x8000;
export const MAX_PAYLOAD = 65536;
export const MAX_READ = MAX_PAYLOAD - 4;
export const MAX_LIST = 500;
export const MAX_DELETE = 500;

export const CMD = { STATUS: 1, LIST: 2, READ: 3, HASH: 4, DELETE: 5, TIME: 6, SETTIME: 7 };

export const NAME_RE = /^[0-9]{8}-[0-9]{6}\.(jpg|snap|snap\.time)$/;

const ERRNO = { 1: "EPERM", 2: "ENOENT", 5: "EIO", 12: "ENOMEM", 13: "EACCES", 16: "EBUSY", 22: "EINVAL", 28: "ENOSPC", 30: "EROFS" };

export class ProtocolError extends Error {}
export class DeviceError extends Error {
  constructor(status, cmd) {
    const name = ERRNO[-status] || "E" + -status;
    super(`command 0x${(cmd ?? 0).toString(16).padStart(4, "0")} failed: status ${status} (${name})`);
    this.status = status;
    this.cmd = cmd;
    this.errnoName = name;
  }
}

const enc = new TextEncoder();
const dec = new TextDecoder();

export function packHeader(cmd, seq, len) {
  const b = new Uint8Array(HDR_LEN);
  const v = new DataView(b.buffer);
  v.setUint32(0, MAGIC, true);
  v.setUint16(4, cmd, true);
  v.setUint16(6, 0, true);
  v.setUint32(8, seq >>> 0, true);
  v.setUint32(12, len, true);
  return b;
}

export function concat(parts) {
  let n = 0;
  for (const p of parts) n += p.length;
  const out = new Uint8Array(n);
  let o = 0;
  for (const p of parts) {
    out.set(p, o);
    o += p.length;
  }
  return out;
}

export function hex(buf) {
  return [...new Uint8Array(buf)].map((x) => x.toString(16).padStart(2, "0")).join("");
}

export async function sha256Hex(data) {
  return hex(await globalThis.crypto.subtle.digest("SHA-256", data));
}

export function randomSeq() {
  return globalThis.crypto.getRandomValues(new Uint32Array(1))[0];
}

// Battery level 1-4 from the raw value.
export function batteryLevel(raw) {
  if (raw == null) return null;
  if (raw > 235) return 4;
  if (raw > 225) return 3;
  if (raw > 220) return 2;
  return 1;
}

export function stemOf(name) {
  const i = name.indexOf(".");
  return i < 0 ? name : name.slice(0, i);
}

// Group file entries by day (YYYYMMDD); returns [{day, files, size}] ascending.
export function groupByDay(files) {
  const map = new Map();
  for (const f of files) {
    const day = f.name.slice(0, 8);
    let g = map.get(day);
    if (!g) map.set(day, (g = { day, files: [], size: 0 }));
    g.files.push(f);
    g.size += f.size;
  }
  return [...map.values()].sort((a, b) => (a.day < b.day ? -1 : a.day > b.day ? 1 : 0));
}

export class Client {
  // opts: seq (initial), wallMs() -> epoch ms (float), monoMs() -> monotonic ms (float)
  constructor(transport, opts = {}) {
    this.t = transport;
    this.seq = (opts.seq ?? randomSeq()) >>> 0;
    this.wallMs = opts.wallMs || (() => Date.now());
    this.monoMs = opts.monoMs || (() => performance.now());
    this.buf = new Uint8Array(0);
  }

  // -- framing ---------------------------------------------------------------------------------------------

  _append(chunk) {
    if (!chunk || chunk.length === 0) return;
    this.buf = this.buf.length === 0 ? chunk : concat([this.buf, chunk]);
  }

  // Make sure at least `n` bytes are buffered.
  async _fill(n) {
    while (this.buf.length < n) {
      this._append(await this.t.read(Math.min(MAX_PAYLOAD, n - this.buf.length)));
    }
  }

  _take(n) {
    const out = this.buf.subarray(0, n);
    this.buf = this.buf.subarray(n);
    return out;
  }

  async _readResponse() {
    await this._fill(HDR_LEN);
    const v = new DataView(this.buf.buffer, this.buf.byteOffset, HDR_LEN);
    const magic = v.getUint32(0, true);
    const cmd = v.getUint16(4, true);
    const seq = v.getUint32(8, true);
    const len = v.getUint32(12, true);
    if (magic !== MAGIC) throw new ProtocolError("bad response magic 0x" + magic.toString(16));
    if (len > MAX_PAYLOAD) throw new ProtocolError(`response len ${len} exceeds ${MAX_PAYLOAD}`);
    await this._fill(HDR_LEN + len);
    this._take(HDR_LEN);
    return { cmd, seq, body: this._take(len) };
  }

  _checkAndSplit(cmd, seq, r) {
    if (r.cmd !== ((cmd | RESP_FLAG) & 0xffff)) {
      throw new ProtocolError(`response cmd 0x${r.cmd.toString(16)}, expected 0x${(cmd | RESP_FLAG).toString(16)}`);
    }
    if (r.seq !== seq) throw new ProtocolError(`response seq ${r.seq}, expected ${seq}`);
    if (r.body.length < 4) throw new ProtocolError(`response payload of ${r.body.length} bytes has no status word`);
    const status = new DataView(r.body.buffer, r.body.byteOffset, 4).getInt32(0, true);
    if (status < 0) throw new DeviceError(status, cmd);
    if (status > 0) throw new ProtocolError("positive status " + status);
    return r.body.subarray(4);
  }

  async _send(cmd, payload) {
    if (payload.length > MAX_PAYLOAD) throw new RangeError(`request payload ${payload.length} bytes exceeds ${MAX_PAYLOAD}`);
    const seq = this.seq;
    this.seq = (this.seq + 1) >>> 0;
    await this.t.write(concat([packHeader(cmd, seq, payload.length), payload]));
    return seq;
  }

  // Send one request, return the result bytes after the status. Throws DeviceError on status < 0.
  async request(cmd, payload = new Uint8Array(0)) {
    const seq = await this._send(cmd, payload);
    let r;
    for (let i = 0; i < 8; i++) {
      // skip stale responses (an earlier seq) left over from an aborted exchange
      r = await this._readResponse();
      const behind = (seq - r.seq) >>> 0;
      if (r.seq === seq || !(behind > 0 && behind < 0x80000000)) break;
    }
    return this._checkAndSplit(cmd, seq, r);
  }

  async _json(cmd, obj) {
    const payload = obj === undefined ? new Uint8Array(0) : enc.encode(JSON.stringify(obj));
    const raw = await this.request(cmd, payload);
    try {
      return JSON.parse(dec.decode(raw));
    } catch (e) {
      throw new ProtocolError("result is not valid JSON: " + e.message);
    }
  }

  // Resync on connect. WebUSB reads have no timeout, so we cannot drain. Instead send STATUS and scan the byte
  // stream for its response header (magic, cmd 0x8001, our seq, sane len) at any offset, discarding what comes
  // before (a stale partial or complete response from a client that died mid-exchange).
  async resync() {
    this.buf = new Uint8Array(0);
    const seq = await this._send(CMD.STATUS, new Uint8Array(0));
    const want = new Uint8Array(12);
    const wv = new DataView(want.buffer);
    wv.setUint32(0, MAGIC, true);
    wv.setUint16(4, CMD.STATUS | RESP_FLAG, true);
    wv.setUint16(6, 0, true);
    wv.setUint32(8, seq, true);
    let scanned = 0;
    let found = -1;
    while (found < 0) {
      for (let i = scanned; i + HDR_LEN <= this.buf.length; i++) {
        let ok = true;
        for (let k = 0; k < 12; k++) {
          if (this.buf[i + k] !== want[k]) {
            ok = false;
            break;
          }
        }
        if (!ok) continue;
        const len = new DataView(this.buf.buffer, this.buf.byteOffset + i + 12, 4).getUint32(0, true);
        if (len >= 4 && len <= MAX_PAYLOAD) {
          found = i;
          break;
        }
      }
      if (found >= 0) break;
      // Everything except the last 15 bytes can no longer start a header.
      const keep = Math.min(this.buf.length, HDR_LEN - 1);
      this.buf = this.buf.subarray(this.buf.length - keep);
      scanned = 0;
      this._append(await this.t.read(512));
    }
    this._take(found);
    const r = await this._readResponse();
    return JSON.parse(dec.decode(this._checkAndSplit(CMD.STATUS, seq, r)));
  }

  // -- commands --------------------------------------------------------------------------------------------

  status() {
    return this._json(CMD.STATUS);
  }
  list(after = "", max = 200) {
    return this._json(CMD.LIST, { after, max });
  }
  async read(name, offset, length) {
    const data = await this.request(CMD.READ, enc.encode(JSON.stringify({ name, offset, len: length })));
    if (data.length > length) throw new ProtocolError(`READ returned ${data.length} bytes, asked for ${length}`);
    return data;
  }
  hash(name) {
    return this._json(CMD.HASH, { name });
  }
  deleteRaw(files) {
    return this._json(CMD.DELETE, { files });
  }
  time() {
    return this._json(CMD.TIME);
  }
  settime(utcMs) {
    return this._json(CMD.SETTIME, { utc_ms: Math.trunc(utcMs) });
  }

  // -- helpers ---------------------------------------------------------------------------------------------

  async listAll(page = MAX_LIST) {
    const files = [];
    let after = "";
    for (;;) {
      const r = await this.list(after, page);
      const got = r.files;
      files.push(...got);
      if (!r.more) return files;
      if (got.length === 0) throw new ProtocolError("LIST says more but returned an empty page");
      if (got[got.length - 1].name <= after) throw new ProtocolError("LIST page is not ascending after " + after);
      after = got[got.length - 1].name;
    }
  }

  // One attempt: READ in 65532-byte chunks, compare with HASH. Never throws DeviceError (reported in `error`).
  async _downloadAttempt(name, progress) {
    const res = { name, ok: false, size: 0, sha256: null, deviceSha256: null, data: null, error: null };
    try {
      const chunks = [];
      let size = 0;
      for (;;) {
        const data = await this.read(name, size, MAX_READ);
        if (data.length === 0) break;
        chunks.push(data.slice()); // own copy: the transport buffer may be reused
        size += data.length;
        if (progress) progress(name, size, data.length);
      }
      const all = chunks.length === 1 ? chunks[0] : concat(chunks);
      res.size = size;
      res.sha256 = await sha256Hex(all);
      const dev = await this.hash(name);
      res.deviceSha256 = dev.sha256;
      if (dev.sha256 !== res.sha256 || dev.size !== size) {
        res.error = `hash mismatch: device ${dev.sha256} (${dev.size} bytes), received ${res.sha256} (${size} bytes)`;
      } else {
        res.ok = true;
        res.data = all;
      }
    } catch (e) {
      if (!(e instanceof DeviceError)) throw e; // transport / protocol errors end the whole session
      res.error = e.message;
      res.deviceError = true;
    }
    return res;
  }

  // Download + verify one file; a hash mismatch is retried up to `retries` more times. result.attempts = tries.
  async downloadFile(name, { progress, retries = 2 } = {}) {
    if (!NAME_RE.test(name)) return { name, ok: false, size: 0, sha256: null, deviceSha256: null, data: null, error: "invalid file name", attempts: 0 };
    let res;
    for (let attempt = 1; attempt <= retries + 1; attempt++) {
      res = await this._downloadAttempt(name, progress);
      res.attempts = attempt;
      if (res.ok || res.deviceError) break;
    }
    return res;
  }

  // DELETE with known hashes, in batches of at most 500. files: [{name, sha256}]. Returns [{name, ok, err, sha256}].
  async deleteFiles(files) {
    const out = [];
    for (let i = 0; i < files.length; i += MAX_DELETE) {
      const batch = files.slice(i, i + MAX_DELETE).map((f) => ({ name: f.name, sha256: f.sha256 }));
      const hashes = new Map(batch.map((f) => [f.name, f.sha256]));
      const r = await this.deleteRaw(batch);
      for (const x of r.results) out.push({ name: x.name, ok: !!x.ok, err: x.err ?? null, sha256: hashes.get(x.name) ?? null });
    }
    return out;
  }

  // HASH each file on the Clip, then DELETE with exactly those hashes (like usbclient.delete_verified).
  async deleteVerified(names) {
    const failed = new Map();
    const todo = [];
    for (const n of names) {
      try {
        todo.push({ name: n, sha256: (await this.hash(n)).sha256 });
      } catch (e) {
        if (!(e instanceof DeviceError)) throw e;
        failed.set(n, { name: n, ok: false, err: e.message, sha256: null });
      }
    }
    const done = new Map((await this.deleteFiles(todo)).map((r) => [r.name, r]));
    return names.map((n) => failed.get(n) || done.get(n)).filter(Boolean);
  }

  async timeSample() {
    const w0 = this.wallMs();
    const m0 = this.monoMs();
    const r = await this.time();
    const rttMs = this.monoMs() - m0;
    const hostMid = w0 + rttMs / 2;
    return { hostSendMs: w0, rttMs, clipUtcMs: r.clip_utc_ms, offsetMs: r.clip_utc_ms - hostMid, reply: r };
  }

  // TIME `rounds` times; the sample with the smallest round trip gives the offset (Clip minus host, ms).
  async measureOffset(rounds = 8) {
    const samples = [];
    for (let i = 0; i < Math.max(1, rounds); i++) samples.push(await this.timeSample());
    const best = samples.reduce((a, b) => (b.rttMs < a.rttMs ? b : a));
    return { rounds: samples.length, bestRttMs: best.rttMs, offsetMs: best.offsetMs, samples, best };
  }

  async syncClock(rounds = 8) {
    const m = await this.measureOffset(rounds);
    const utcMs = Math.round(this.wallMs() + m.bestRttMs / 2);
    const reply = await this.settime(utcMs);
    return { rounds: m.rounds, bestRttMs: m.bestRttMs, offsetBeforeMs: m.offsetMs, sentUtcMs: utcMs, reply };
  }
}
