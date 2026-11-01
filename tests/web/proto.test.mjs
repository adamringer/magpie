// web/proto.js against an in-memory fake of the device side of docs/PHONE_USB.md (mirrors tests/test_usbclient.py).
import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import {
  Client, MAGIC, HDR_LEN, CMD, NAME_RE, ProtocolError, DeviceError, packHeader, concat, batteryLevel, groupByDay,
} from "../../web/proto.js";

const PACKET = 512;
const EINVAL = 22, ENOENT = 2;
const enc = new TextEncoder();
const dec = new TextDecoder();
const sha = (d) => createHash("sha256").update(d).digest("hex");

class VClock {
  constructor() { this.ms = 0; this.wall0 = 1_800_000_000_000; }
  mono() { return this.ms; }
  wall() { return this.wall0 + this.ms; }
  advance(ms) { this.ms += ms; }
}

class FakeDevice {
  constructor(files = {}, { clock = new VClock(), clipOffsetMs = 0 } = {}) {
    this.files = { ...files };
    this.clock = clock;
    this.clipOffsetMs = clipOffsetMs;
    this.pending = new Uint8Array(0);
    this.requests = []; // {cmd, seq, payload}
    this.corruptOnce = new Set(); // "name@offset"
    this.corruptAlways = new Set(); // name
    this.tamper = null;
    this.timeDelays = [];
    this.otherDelay = [0, 0];
    this.backDelayMs = 0;
    this.settimeHostMs = null;
    this.settimeUtcMs = null;
    this.reads = 0;
  }
  preload(bytes) { this.pending = concat([this.pending, bytes]); }

  async write(data) {
    const [magic, cmd, seq, length] = (() => {
      const v = new DataView(data.buffer, data.byteOffset, data.byteLength);
      return [v.getUint32(0, true), v.getUint16(4, true), v.getUint32(8, true), v.getUint32(12, true)];
    })();
    const payload = data.slice(HDR_LEN);
    assert.equal(payload.length, length);
    this.requests.push({ cmd, seq, payload });
    let outMs, backMs;
    if (cmd === CMD.TIME && this.timeDelays.length) [outMs, backMs] = this.timeDelays.shift();
    else [outMs, backMs] = this.otherDelay;
    this.clock.advance(outMs);
    let resp = this.handle(magic, cmd, seq, length, payload);
    if (this.tamper) resp = this.tamper(resp);
    this.backDelayMs = backMs;
    this.preload(resp);
  }

  async read(maxLen) {
    if (this.pending.length === 0) throw new Error("fake: nothing to read");
    if (this.backDelayMs) { this.clock.advance(this.backDelayMs); this.backDelayMs = 0; }
    this.reads++;
    const n = Math.min(PACKET, this.pending.length); // one packet per read
    const out = this.pending.slice(0, n);
    this.pending = this.pending.subarray(n);
    return out;
  }

  static frame(cmd, seq, status, result = new Uint8Array(0)) {
    const body = new Uint8Array(4 + result.length);
    new DataView(body.buffer).setInt32(0, status, true);
    body.set(result, 4);
    return concat([packHeader(cmd | 0x8000, seq, body.length), body]);
  }

  nowMs() { return Math.floor(this.clock.wall()) + this.clipOffsetMs; }

  handle(magic, cmd, seq, length, payload) {
    if (magic !== MAGIC || length > 65536 || cmd < 1 || cmd > 7) return FakeDevice.frame(cmd, seq, -EINVAL);
    let st, res;
    try {
      const req = payload.length ? JSON.parse(dec.decode(payload)) : null;
      [st, res] = this[`cmd${cmd}`](req);
    } catch (e) {
      return FakeDevice.frame(cmd, seq, -EINVAL);
    }
    if (!(res instanceof Uint8Array)) res = enc.encode(JSON.stringify(res));
    return FakeDevice.frame(cmd, seq, st, res);
  }

  valid(n) { return typeof n === "string" && NAME_RE.test(n); }
  names() { return Object.keys(this.files).sort(); }

  cmd1() {
    return [0, { proto: 1, version: "fake", clip_utc_ms: this.nowMs(), uptime_s: 12.5, rtt: null,
      photos: this.names().filter((n) => n.endsWith(".jpg")).length, snaps: 0, snap_times: 0,
      bytes: this.names().reduce((a, n) => a + this.files[n].length, 0), free_kb: 1000, battery_raw: null, charging: null }];
  }
  cmd2(req) {
    const after = req.after ?? "", mx = req.max ?? 200;
    if (!(mx >= 1 && mx <= 500)) return [-EINVAL, new Uint8Array(0)];
    const names = this.names().filter((n) => n > after);
    return [0, { files: names.slice(0, mx).map((n) => ({ name: n, size: this.files[n].length, mtime: 1700000000 })), more: names.length > mx }];
  }
  cmd3(req) {
    const { name, offset, len } = req;
    if (!this.valid(name) || len > 65536 - 4 || offset < 0 || len < 0) return [-EINVAL, new Uint8Array(0)];
    if (!(name in this.files)) return [-ENOENT, new Uint8Array(0)];
    let data = this.files[name].slice(offset, offset + len);
    const key = `${name}@${offset}`;
    if (data.length && (this.corruptOnce.has(key) || this.corruptAlways.has(name))) {
      this.corruptOnce.delete(key);
      data[0] ^= 0xff;
    }
    return [0, data];
  }
  cmd4(req) {
    if (!this.valid(req.name)) return [-EINVAL, new Uint8Array(0)];
    if (!(req.name in this.files)) return [-ENOENT, new Uint8Array(0)];
    const d = this.files[req.name];
    return [0, { name: req.name, size: d.length, sha256: sha(d) }];
  }
  cmd5(req) {
    if (req.files.length > 500) return [-EINVAL, new Uint8Array(0)];
    const results = [];
    for (const f of req.files) {
      if (!this.valid(f.name)) results.push({ name: f.name, ok: false, err: "EINVAL" });
      else if (!(f.name in this.files)) results.push({ name: f.name, ok: false, err: "ENOENT" });
      else if (sha(this.files[f.name]) !== f.sha256) results.push({ name: f.name, ok: false, err: "hash mismatch" });
      else { delete this.files[f.name]; results.push({ name: f.name, ok: true }); }
    }
    return [0, { results }];
  }
  cmd6() { return [0, { clip_utc_ms: this.nowMs(), mono_ns: this.clock.ms * 1e6, rtt: null }]; }
  cmd7(req) {
    const before = this.nowMs();
    this.settimeHostMs = this.clock.wall();
    this.settimeUtcMs = req.utc_ms;
    this.clipOffsetMs = req.utc_ms - Math.floor(this.clock.wall());
    return [0, { before_ms: before, after_ms: this.nowMs(), rtc: "ok", rtt_cal: null }];
  }
}

function makeFiles(n = 5, size = 3000) {
  const out = {};
  for (let i = 0; i < n; i++) {
    const d = new Uint8Array(size + i * 777);
    for (let j = 0; j < d.length; j++) d[j] = (i * 7 + j) % 251;
    out[`20261031-1015${String(i).padStart(2, "0")}.jpg`] = d;
  }
  return out;
}

function mk(files, opts = {}) {
  const clock = new VClock();
  const dev = new FakeDevice(files ?? makeFiles(), { clock, ...opts });
  const c = new Client(dev, { seq: 1, wallMs: () => clock.wall(), monoMs: () => clock.mono() });
  return { dev, c, clock };
}

// -- framing -------------------------------------------------------------------------------------------------

test("header layout", () => {
  const h = packHeader(1, 2, 3);
  assert.equal(h.length, 16);
  assert.equal(dec.decode(h.subarray(0, 4)), "MGP1");
  assert.equal(HDR_LEN, 16);
});

test("framing round trip and seq echo", async () => {
  const { dev, c } = mk();
  const r1 = await c.status();
  const r2 = await c.status();
  assert.equal(r1.proto, 1);
  assert.equal(r2.version, "fake");
  assert.deepEqual(dev.requests.map((r) => [r.cmd, r.seq]), [[1, 1], [1, 2]]);
  assert.equal(c.seq, 3);
});

test("seq wraps", async () => {
  const { dev, c } = mk();
  c.seq = 0xffffffff;
  await c.status();
  await c.status();
  assert.deepEqual(dev.requests.map((r) => r.seq), [0xffffffff, 0]);
});

test("large response is reassembled from packets", async () => {
  const d = new Uint8Array(76800).map((_, i) => i % 256);
  const { c } = mk({ "20261031-101500.jpg": d });
  const got = await c.read("20261031-101500.jpg", 0, 65532);
  assert.deepEqual(got, d.subarray(0, 65532));
});

for (const size of [PACKET - 20, PACKET - 19, 2 * PACKET - 20, 100, 0]) {
  test(`read sizes around packet boundaries: ${size}`, async () => {
    const n = "20261031-101500.jpg";
    const { dev, c } = mk({ [n]: new Uint8Array(2560).map((_, i) => i % 256) });
    assert.deepEqual(await c.read(n, 0, size), dev.files[n].subarray(0, size));
  });
}

test("mismatched magic, cmd, seq raise ProtocolError", async () => {
  const flip = (i, bytes) => (r) => { const o = r.slice(); o.set(bytes, i); return o; };
  const u16 = (v) => { const b = new Uint8Array(2); new DataView(b.buffer).setUint16(0, v, true); return b; };
  const u32 = (v) => { const b = new Uint8Array(4); new DataView(b.buffer).setUint32(0, v, true); return b; };
  for (const tamper of [flip(0, enc.encode("XGP1")), flip(4, u16(0x8002)), flip(4, u16(0x0001)), flip(8, u32(99))]) {
    const { dev, c } = mk();
    dev.tamper = tamper;
    await assert.rejects(c.status(), ProtocolError);
  }
});

test("truncated response never completes; extra bytes break the next response", async () => {
  let { dev, c } = mk();
  dev.tamper = (r) => r.subarray(0, r.length - 3);
  await assert.rejects(c.status(), /nothing to read/);
  ({ dev, c } = mk());
  dev.tamper = (r) => concat([r, enc.encode("xx")]);
  await c.status(); // the first response is fine
  await assert.rejects(c.status(), ProtocolError); // leftover "xx" + next response: bad magic
});

test("negative status raises DeviceError with errno name", async () => {
  const { c } = mk();
  await assert.rejects(c.hash("20261031-999999.jpg"), (e) => e instanceof DeviceError && e.status === -ENOENT && e.errnoName === "ENOENT");
  await assert.rejects(c.hash("../etc/passwd"), (e) => e instanceof DeviceError && e.errnoName === "EINVAL" && e.status === -22);
});

test("oversized request is refused locally", async () => {
  const { dev, c } = mk();
  await assert.rejects(c.request(CMD.STATUS, new Uint8Array(65537)), RangeError);
  assert.equal(dev.requests.length, 0);
});

test("stale response with an earlier seq is skipped", async () => {
  const { dev, c } = mk();
  c.seq = 50;
  dev.preload(FakeDevice.frame(1, 49, 0, enc.encode('{"proto":1,"version":"stale"}')));
  const r = await c.status();
  assert.equal(r.version, "fake");
});

// -- list ----------------------------------------------------------------------------------------------------

test("list after and max", async () => {
  const { dev, c } = mk(makeFiles(5));
  const names = dev.names();
  let r = await c.list("", 2);
  assert.deepEqual(r.files.map((f) => f.name), names.slice(0, 2));
  assert.equal(r.more, true);
  r = await c.list(names[1], 10);
  assert.deepEqual(r.files.map((f) => f.name), names.slice(2));
  assert.equal(r.more, false);
  assert.deepEqual(JSON.parse(dec.decode(dev.requests[0].payload)), { after: "", max: 2 });
});

test("listAll across pages and empty", async () => {
  const files = {};
  for (let i = 0; i < 23; i++) files[`20261031-${String(i).padStart(6, "0")}.snap`] = new Uint8Array([1]);
  let { dev, c } = mk(files);
  assert.deepEqual((await c.listAll(5)).map((f) => f.name), dev.names());
  assert.equal(dev.requests.length, 5);
  assert.equal((await c.listAll()).length, 23);
  ({ c } = mk({}));
  assert.deepEqual(await c.listAll(), []);
});

// -- resync --------------------------------------------------------------------------------------------------

function junk(n, seed = 1) {
  const b = new Uint8Array(n);
  for (let i = 0; i < n; i++) b[i] = (i * 31 + seed) & 0xff;
  return b;
}

test("resync with nothing stale", async () => {
  const { c } = mk();
  const st = await c.resync();
  assert.equal(st.version, "fake");
});

test("resync skips a stale partial frame (tail of a response, then a header fragment)", async () => {
  const { dev, c } = mk();
  c.seq = 0x12345678;
  const partialHeader = FakeDevice.frame(3, 7, 0, junk(100)).subarray(0, 10);
  dev.preload(concat([junk(1300, 5), partialHeader]));
  const st = await c.resync();
  assert.equal(st.version, "fake");
  assert.equal(c.seq, 0x12345679);
  const again = await c.status(); // stream still in sync afterwards
  assert.equal(again.proto, 1);
});

test("resync skips a stale complete response and a partial one before it", async () => {
  const { dev, c } = mk();
  c.seq = 0xfffffffe;
  const stale = FakeDevice.frame(3, 0xfffffff0, 0, junk(3000, 9));
  const staleStatus = FakeDevice.frame(1, 0xfffffffd, 0, enc.encode('{"proto":1,"version":"stale"}'));
  dev.preload(concat([junk(777, 3), stale, staleStatus]));
  const st = await c.resync();
  assert.equal(st.version, "fake");
  assert.equal((await c.status()).version, "fake");
});

test("resync finds the header even if it straddles packet boundaries", async () => {
  for (const pad of [496, 497, 500, 511, 512, 513]) {
    const { dev, c } = mk();
    c.seq = 4242;
    dev.preload(junk(pad, pad));
    assert.equal((await c.resync()).version, "fake", `pad ${pad}`);
  }
});

// -- download ------------------------------------------------------------------------------------------------

test("download returns verified data in 65532-byte chunks", async () => {
  const files = makeFiles(3, 70000);
  const { dev, c } = mk(files);
  for (const n of dev.names()) {
    const r = await c.downloadFile(n);
    assert.ok(r.ok);
    assert.deepEqual(r.data, files[n]);
    assert.equal(r.sha256, sha(files[n]));
    assert.equal(r.size, files[n].length);
    assert.equal(r.attempts, 1);
  }
  const reads = dev.requests.filter((r) => r.cmd === CMD.READ).map((r) => JSON.parse(dec.decode(r.payload)));
  assert.equal(Math.max(...reads.map((r) => r.len)), 65532);
  assert.equal(reads[0].offset, 0);
});

test("download of an empty file", async () => {
  const { c } = mk({ "20261031-101500.snap.time": new Uint8Array(0) });
  const r = await c.downloadFile("20261031-101500.snap.time");
  assert.ok(r.ok);
  assert.equal(r.size, 0);
});

test("download with a corrupted chunk once succeeds on retry", async () => {
  const files = makeFiles(2, 5000);
  const { dev, c } = mk(files);
  const a = dev.names()[0];
  dev.corruptOnce.add(`${a}@0`);
  const r = await c.downloadFile(a);
  assert.ok(r.ok);
  assert.equal(r.attempts, 2);
  assert.deepEqual(r.data, files[a]);
});

test("download corrupted every time is reported failed after 3 attempts", async () => {
  const files = makeFiles(2, 5000);
  const { dev, c } = mk(files);
  const [a, b] = dev.names();
  dev.corruptAlways.add(a);
  const r = await c.downloadFile(a);
  assert.equal(r.ok, false);
  assert.equal(r.attempts, 3);
  assert.equal(r.data, null);
  assert.match(r.error, /mismatch/);
  assert.notEqual(r.sha256, r.deviceSha256);
  assert.equal(r.deviceSha256, sha(files[a]));
  assert.ok((await c.downloadFile(b)).ok);
});

test("download reports device errors (no retry) and invalid names (never sent)", async () => {
  const { dev, c } = mk(makeFiles(1));
  const r1 = await c.downloadFile("20261031-999999.jpg");
  assert.ok(!r1.ok && /ENOENT/.test(r1.error) && r1.attempts === 1);
  const r2 = await c.downloadFile("../evil.jpg");
  assert.ok(!r2.ok && /invalid/.test(r2.error));
  assert.equal(dev.requests.length, 1);
});

// -- delete --------------------------------------------------------------------------------------------------

test("deleteFiles with correct hash, and refusal on wrong hash", async () => {
  const { dev, c } = mk(makeFiles(3));
  const [a, b, d] = dev.names();
  const hb = (await c.hash(b)).sha256;
  const r = await c.deleteFiles([{ name: a, sha256: "0".repeat(64) }, { name: b, sha256: hb }]);
  assert.deepEqual(r.map((x) => x.ok), [false, true]);
  assert.ok(a in dev.files && !(b in dev.files) && d in dev.files);
});

test("deleteVerified hashes first and reports missing files", async () => {
  const { dev, c } = mk(makeFiles(3));
  const [a, b, d] = dev.names();
  const res = await c.deleteVerified([a, "20261031-999999.jpg", d]);
  assert.deepEqual(res.map((r) => [r.name, r.ok]), [[a, true], ["20261031-999999.jpg", false], [d, true]]);
  assert.deepEqual(dev.names(), [b]);
  assert.deepEqual(dev.requests.map((r) => r.cmd), [CMD.HASH, CMD.HASH, CMD.HASH, CMD.DELETE]);
});

test("deleteFiles batches of 500", async () => {
  const files = {};
  for (let i = 0; i < 501; i++) files[`20261031-${String(i).padStart(6, "0")}.snap`] = new Uint8Array([1]);
  const { dev, c } = mk(files);
  const list = dev.names().map((name) => ({ name, sha256: sha(files[name]) }));
  const res = await c.deleteFiles(list);
  assert.equal(res.length, 501);
  assert.ok(res.every((r) => r.ok));
  assert.deepEqual(dev.names(), []);
  const sizes = dev.requests.filter((r) => r.cmd === CMD.DELETE).map((r) => JSON.parse(dec.decode(r.payload)).files.length);
  assert.deepEqual(sizes, [500, 1]);
});

// -- clock ---------------------------------------------------------------------------------------------------

test("measureOffset symmetric delays", async () => {
  const { dev, c } = mk(undefined, { clipOffsetMs: 2500 });
  dev.timeDelays = [[10, 10], [4, 4], [0.5, 0.5], [7, 7]];
  const m = await c.measureOffset(4);
  assert.equal(m.rounds, 4);
  assert.ok(Math.abs(m.bestRttMs - 1) < 1e-6);
  assert.ok(Math.abs(m.offsetMs - 2500) < 1);
});

test("syncClock uses the smallest round trip sample", async () => {
  const { dev, c } = mk(undefined, { clipOffsetMs: -3217 });
  dev.timeDelays = [[30, 5], [2, 9], [0.5, 0.5], [12, 3], [4, 4], [6, 20], [1.5, 1.5], [8, 8]];
  dev.otherDelay = [0.5, 0.5];
  const r = await c.syncClock(8);
  assert.equal(r.rounds, 8);
  assert.ok(Math.abs(r.bestRttMs - 1) < 1e-6);
  assert.ok(Math.abs(r.offsetBeforeMs - -3217) < 1);
  assert.ok(Math.abs(dev.settimeUtcMs - dev.settimeHostMs) < 1);
  assert.equal(r.sentUtcMs, dev.settimeUtcMs);
  assert.equal(r.reply.rtc, "ok");
  assert.ok(Math.abs(dev.clipOffsetMs) <= 1);
  assert.deepEqual(dev.requests.map((x) => x.cmd), [...Array(8).fill(CMD.TIME), CMD.SETTIME]);
});

test("syncClock default is eight rounds", async () => {
  const { dev, c } = mk();
  await c.syncClock();
  assert.equal(dev.requests.filter((r) => r.cmd === CMD.TIME).length, 8);
});

// -- helpers -------------------------------------------------------------------------------------------------

test("batteryLevel thresholds", () => {
  assert.deepEqual([220, 221, 225, 226, 235, 236, null].map(batteryLevel), [1, 2, 2, 3, 3, 4, null]);
});

test("groupByDay", () => {
  const g = groupByDay([
    { name: "20261031-101500.jpg", size: 10 }, { name: "20261101-000001.jpg", size: 5 }, { name: "20261031-101500.snap", size: 1 },
  ]);
  assert.deepEqual(g.map((x) => [x.day, x.files.length, x.size]), [["20261031", 2, 11], ["20261101", 1, 5]]);
});
