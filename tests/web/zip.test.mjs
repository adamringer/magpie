import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { crc32, ZipWriter } from "../../web/zip.js";

const enc = new TextEncoder();

test("crc32 known vectors", () => {
  assert.equal(crc32(new Uint8Array(0)), 0);
  assert.equal(crc32(enc.encode("123456789")), 0xcbf43926);
  assert.equal(crc32(enc.encode("a")), 0xe8b7be43);
  assert.equal(crc32(enc.encode("The quick brown fox jumps over the lazy dog")), 0x414fa339);
  // incremental
  assert.equal(crc32(enc.encode("6789"), crc32(enc.encode("12345"))), 0xcbf43926);
});

test("zip validates with python zipfile", () => {
  const big = new Uint8Array(200000).map((_, i) => (i * 13) & 0xff);
  const files = [
    ["20261031-101500.jpg", big],
    ["20261031-101500.snap.time", new Uint8Array(0)],
    ["SHA256SUMS", enc.encode("abc  20261031-101500.jpg\n")],
    ["ünï.txt", enc.encode("utf8 name")],
  ];
  const z = new ZipWriter();
  for (const [n, d] of files) z.addFile(n, d, new Date(Date.UTC(2026, 9, 31, 10, 15, 30)));
  const parts = z.finish();
  assert.ok(Array.isArray(parts));
  const blob = Buffer.concat(parts);
  assert.equal(blob.length, z.size);
  const dir = mkdtempSync(join(tmpdir(), "magpie-zip-"));
  try {
    const f = join(dir, "t.zip");
    writeFileSync(f, blob);
    const out = execFileSync("python3", ["-c", "import zipfile,sys; z=zipfile.ZipFile(sys.argv[1]); assert z.testzip() is None; print(z.namelist())", f]).toString();
    assert.match(out, /20261031-101500\.jpg/);
    assert.match(out, /SHA256SUMS/);
    const chk = execFileSync("python3", ["-c",
      "import zipfile,sys; z=zipfile.ZipFile(sys.argv[1]); " +
      "assert z.read('20261031-101500.snap.time')==b''; assert len(z.read('20261031-101500.jpg'))==200000; " +
      "assert z.read('SHA256SUMS')==b'abc  20261031-101500.jpg\\n'; assert z.read('\\u00fcn\\u00ef.txt')==b'utf8 name'; " +
      "i=z.getinfo('20261031-101500.jpg'); assert i.date_time==(2026,10,31,10,15,30), i.date_time; print('ok')", f]).toString();
    assert.match(chk, /ok/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("empty zip is valid", () => {
  const parts = new ZipWriter().finish();
  const dir = mkdtempSync(join(tmpdir(), "magpie-zip-"));
  try {
    const f = join(dir, "e.zip");
    writeFileSync(f, Buffer.concat(parts));
    execFileSync("python3", ["-c", "import zipfile,sys; assert zipfile.ZipFile(sys.argv[1]).namelist()==[]", f]);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
