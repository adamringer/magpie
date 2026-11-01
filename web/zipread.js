// Reads back a ZIP written by zip.js (store method only) to prove it is intact. No browser-only APIs.
// `blob` needs `size` and `slice(start, end).arrayBuffer()` (Blob / File, or a test double). The archive is read
// piecewise: the end record, the central directory, then one local header and one entry's data at a time.
import { crc32 } from "./zip.js";
import { sha256Hex } from "./proto.js";

export class ZipReadError extends Error {}

const dec = new TextDecoder();
const TAIL = 22 + 65535;

async function bytes(blob, start, end) {
  if (start < 0 || end > blob.size || start > end) throw new ZipReadError(`read outside the file (${start}-${end} of ${blob.size})`);
  return new Uint8Array(await blob.slice(start, end).arrayBuffer());
}

export function parseSums(text) {
  const map = new Map();
  for (const line of text.split("\n")) {
    const m = /^([0-9a-f]{64}) [ *](.+)$/.exec(line.replace(/\r$/, ""));
    if (m) map.set(m[2], m[1]);
  }
  return map;
}

// Returns { fatal: string|null, entries: [{name, size, crcOk, sha256, sumsOk, ok, problems}], problems: [string] }.
// Structural damage (truncated file, no end record, ...) sets `fatal` instead of throwing.
export async function readZip(blob, { digest = sha256Hex, onProgress } = {}) {
  const out = { fatal: null, entries: [], problems: [] };
  try {
    await readInto(blob, digest, onProgress, out);
  } catch (e) {
    if (!(e instanceof ZipReadError)) throw e;
    out.fatal = e.message;
    out.problems.push(e.message);
  }
  out.ok = !out.fatal && out.problems.length === 0 && out.entries.length > 0 && out.entries.every((x) => x.ok);
  return out;
}

async function readInto(blob, digest, onProgress, out) {
  const size = blob.size;
  if (size < 22) throw new ZipReadError("not a zip file (too small, or the download was cut short)");
  const tailStart = Math.max(0, size - TAIL);
  const tail = await bytes(blob, tailStart, size);
  let e = -1;
  for (let i = tail.length - 22; i >= 0; i--) {
    if (tail[i] === 0x50 && tail[i + 1] === 0x4b && tail[i + 2] === 5 && tail[i + 3] === 6) {
      e = i;
      break;
    }
  }
  if (e < 0) throw new ZipReadError("end of zip not found: the file is truncated or not a zip");
  const ev = new DataView(tail.buffer, tail.byteOffset + e, 22);
  const count = ev.getUint16(10, true);
  const cdSize = ev.getUint32(12, true);
  const cdOff = ev.getUint32(16, true);
  if (count === 0xffff || cdSize === 0xffffffff || cdOff === 0xffffffff) throw new ZipReadError("ZIP64 archives are not supported");
  if (cdOff + cdSize > size) throw new ZipReadError("central directory lies outside the file: truncated zip");
  const cd = await bytes(blob, cdOff, cdOff + cdSize);
  const cv = new DataView(cd.buffer, cd.byteOffset, cd.length);

  const items = [];
  let p = 0;
  for (let i = 0; i < count; i++) {
    if (p + 46 > cd.length || cv.getUint32(p, true) !== 0x02014b50) throw new ZipReadError(`bad central directory entry ${i + 1}`);
    const nameLen = cv.getUint16(p + 28, true);
    const extraLen = cv.getUint16(p + 30, true);
    const commentLen = cv.getUint16(p + 32, true);
    if (p + 46 + nameLen > cd.length) throw new ZipReadError(`bad central directory entry ${i + 1}`);
    items.push({
      method: cv.getUint16(p + 10, true),
      crc: cv.getUint32(p + 16, true),
      csize: cv.getUint32(p + 20, true),
      usize: cv.getUint32(p + 24, true),
      offset: cv.getUint32(p + 42, true),
      name: dec.decode(cd.subarray(p + 46, p + 46 + nameLen)),
    });
    p += 46 + nameLen + extraLen + commentLen;
  }

  let total = 0;
  for (const it of items) total += it.csize;
  let done = 0;
  let sums = null;
  for (const it of items) {
    const ent = { name: it.name, size: it.usize, crcOk: false, sha256: null, sumsOk: false, ok: false, problems: [] };
    out.entries.push(ent);
    if (onProgress) onProgress(done, total, it.name);
    try {
      if (it.method !== 0) throw new ZipReadError(`${it.name}: unsupported compression method ${it.method} (only stored files are supported)`);
      if (it.csize !== it.usize) throw new ZipReadError(`${it.name}: stored size ${it.csize} differs from size ${it.usize}`);
      const lh = await bytes(blob, it.offset, it.offset + 30);
      const lv = new DataView(lh.buffer, lh.byteOffset, 30);
      if (lv.getUint32(0, true) !== 0x04034b50) throw new ZipReadError(`${it.name}: bad local header`);
      const start = it.offset + 30 + lv.getUint16(26, true) + lv.getUint16(28, true);
      if (start + it.csize > cdOff) throw new ZipReadError(`${it.name}: data runs past the central directory: truncated or damaged`);
      const data = await bytes(blob, start, start + it.csize);
      ent.crcOk = crc32(data) === it.crc;
      if (!ent.crcOk) ent.problems.push(`${it.name}: CRC-32 mismatch (data is damaged)`);
      ent.sha256 = await digest(data);
      if (it.name === "SHA256SUMS") sums = parseSums(dec.decode(data));
    } catch (err) {
      if (!(err instanceof ZipReadError)) throw err;
      ent.problems.push(err.message);
    }
    done += it.csize;
    if (onProgress) onProgress(done, total, it.name);
  }

  const names = new Set(items.map((i) => i.name));
  if (!sums) out.problems.push("SHA256SUMS is missing from the zip");
  for (const ent of out.entries) {
    if (ent.name === "SHA256SUMS") {
      ent.sumsOk = true;
    } else if (sums) {
      if (!sums.has(ent.name)) ent.problems.push(`${ent.name}: missing from SHA256SUMS`);
      else if (ent.sha256 && sums.get(ent.name) !== ent.sha256) ent.problems.push(`${ent.name}: sha256 does not match SHA256SUMS`);
      else ent.sumsOk = ent.sha256 !== null;
    }
    ent.ok = ent.crcOk && ent.sumsOk && ent.problems.length === 0;
    out.problems.push(...ent.problems);
  }
  if (sums) for (const n of sums.keys()) if (!names.has(n)) out.problems.push(`${n}: listed in SHA256SUMS but missing from the zip`);
}
