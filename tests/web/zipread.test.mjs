import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { ZipWriter } from "../../web/zip.js";
import { readZip } from "../../web/zipread.js";

const enc = new TextEncoder();
const sha = (d) => createHash("sha256").update(d).digest("hex");

class CountingBlob {
  constructor(buf) { this.buf = buf; this.size = buf.length; this.slices = []; }
  slice(a, b) {
    this.slices.push([a, b]);
    const part = this.buf.slice(a, b);
    return { arrayBuffer: async () => part.buffer.slice(part.byteOffset, part.byteOffset + part.byteLength) };
  }
}

function files() {
  const a = new Uint8Array(300000).map((_, i) => (i * 7) & 0xff);
  const b = new Uint8Array(0);
  const c = enc.encode("hello snap");
  return [["20261031-101500.jpg", a], ["20261031-101500.snap.time", b], ["20261031-101501.snap", c]];
}

function build({ sums, data = files(), mutate } = {}) {
  const z = new ZipWriter();
  for (const [n, d] of data) z.addFile(n, d);
  const text = sums ?? data.map(([n, d]) => `${sha(d)}  ${n}\n`).join("");
  z.addFile("SHA256SUMS", enc.encode(text));
  const buf = Buffer.concat(z.finish());
  const out = new Uint8Array(buf);
  if (mutate) mutate(out);
  return out;
}

test("round trip: all entries ok", async () => {
  const r = await readZip(new CountingBlob(build()));
  assert.equal(r.fatal, null);
  assert.deepEqual(r.problems, []);
  assert.ok(r.ok);
  assert.equal(r.entries.length, 4);
  assert.ok(r.entries.every((e) => e.ok && e.crcOk && e.sumsOk));
  assert.equal(r.entries[0].sha256, sha(files()[0][1]));
  assert.equal(r.entries[1].size, 0);
});

test("corrupted data byte: CRC and sha mismatch reported", async () => {
  const buf = build({ mutate: (b) => { b[30 + 19 + 1000] ^= 0xff; } });
  const r = await readZip(new CountingBlob(buf));
  assert.ok(!r.ok);
  const e = r.entries[0];
  assert.equal(e.crcOk, false);
  assert.equal(e.sumsOk, false);
  assert.match(r.problems.join("\n"), /CRC-32 mismatch/);
  assert.match(r.problems.join("\n"), /does not match SHA256SUMS/);
  assert.ok(r.entries[2].ok);
});

test("entry missing from SHA256SUMS", async () => {
  const d = files();
  const sums = d.slice(0, 2).map(([n, x]) => `${sha(x)}  ${n}\n`).join("");
  const r = await readZip(new CountingBlob(build({ sums })));
  assert.ok(!r.ok);
  assert.match(r.problems.join("\n"), /20261031-101501\.snap: missing from SHA256SUMS/);
  assert.equal(r.entries[2].ok, false);
  assert.ok(r.entries[0].ok);
});

test("SHA256SUMS lists a missing file", async () => {
  const d = files();
  const sums = d.map(([n, x]) => `${sha(x)}  ${n}\n`).join("") + `${"a".repeat(64)}  20261031-101502.jpg\n`;
  const r = await readZip(new CountingBlob(build({ sums })));
  assert.ok(!r.ok);
  assert.match(r.problems.join("\n"), /20261031-101502\.jpg: listed in SHA256SUMS but missing/);
  assert.ok(r.entries.every((e) => e.ok));
});

test("truncated zip gives a clear fatal error without throwing", async () => {
  const buf = build();
  for (const cut of [buf.length - 10, buf.length - 200, 100, 10, 0]) {
    const r = await readZip(new CountingBlob(buf.slice(0, cut)));
    assert.ok(r.fatal, `cut ${cut}`);
    assert.ok(!r.ok);
    assert.match(r.fatal, /truncated|too small|not a zip|outside/);
  }
});

test("not a zip at all", async () => {
  const r = await readZip(new CountingBlob(enc.encode("x".repeat(5000))));
  assert.ok(r.fatal);
});

test("compressed entries are rejected clearly", async () => {
  const buf = build();
  // flip the method field of the first central directory entry
  const dv = new DataView(buf.buffer);
  let i = buf.length - 22;
  const cd = dv.getUint32(i + 16, true);
  dv.setUint16(cd + 10, 8, true);
  const r = await readZip(new CountingBlob(buf));
  assert.ok(!r.ok);
  assert.match(r.problems.join("\n"), /unsupported compression method 8/);
});

test("entries are read piecewise", async () => {
  const buf = build();
  const blob = new CountingBlob(buf);
  const seen = [];
  const r = await readZip(blob, { onProgress: (d, t) => seen.push([d, t]) });
  assert.ok(r.ok);
  const biggest = Math.max(...blob.slices.map(([a, b]) => b - a));
  assert.ok(biggest < buf.length);
  assert.ok(biggest <= 300000 + 100);
  // end record + central directory + (local header + data) per entry (4 entries incl. SHA256SUMS)
  assert.equal(blob.slices.length, 2 + 2 * 4);
  assert.ok(seen.length > 0 && seen.at(-1)[0] === seen.at(-1)[1]);
});
