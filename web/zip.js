// Minimal ZIP writer (store method, CRC-32). No ZIP64: keep each archive below 2 GiB.
// Output is an array of parts (Uint8Array) suitable for `new Blob(parts)`; file data is referenced, not copied.

const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();

export function crc32(data, crc = 0) {
  let c = ~crc >>> 0;
  for (let i = 0; i < data.length; i++) c = CRC_TABLE[(c ^ data[i]) & 0xff] ^ (c >>> 8);
  return ~c >>> 0;
}

function dosDateTime(date) {
  const y = Math.max(1980, date.getUTCFullYear());
  const time = (date.getUTCHours() << 11) | (date.getUTCMinutes() << 5) | (date.getUTCSeconds() >> 1);
  const day = ((y - 1980) << 9) | ((date.getUTCMonth() + 1) << 5) | date.getUTCDate();
  return { time, day };
}

const LIMIT = 0xffffffff - 1024 * 1024; // stay clear of the 32-bit fields
const enc = new TextEncoder();

export class ZipWriter {
  constructor() {
    this.parts = [];
    this.entries = [];
    this.offset = 0;
  }

  get size() {
    return this.offset;
  }

  // data: Uint8Array; date: Date (UTC fields are stored); returns the CRC-32.
  addFile(name, data, date = new Date(Date.UTC(1980, 0, 1))) {
    if (this.entries.length >= 0xfffe) throw new RangeError("too many files for a non-ZIP64 archive");
    if (this.offset + data.length + 1024 * 1024 > LIMIT) throw new RangeError("archive would exceed 2 GiB");
    const nameBytes = enc.encode(name);
    const crc = crc32(data);
    const { time, day } = dosDateTime(date);
    const h = new Uint8Array(30);
    const v = new DataView(h.buffer);
    v.setUint32(0, 0x04034b50, true);
    v.setUint16(4, 20, true); // version needed
    v.setUint16(6, 0x0800, true); // UTF-8 names
    v.setUint16(8, 0, true); // stored
    v.setUint16(10, time, true);
    v.setUint16(12, day, true);
    v.setUint32(14, crc, true);
    v.setUint32(18, data.length, true);
    v.setUint32(22, data.length, true);
    v.setUint16(26, nameBytes.length, true);
    v.setUint16(28, 0, true);
    this.entries.push({ nameBytes, crc, size: data.length, time, day, offset: this.offset });
    this.parts.push(h, nameBytes, data);
    this.offset += 30 + nameBytes.length + data.length;
    return crc;
  }

  // Returns the parts array (local entries, central directory, end record).
  finish() {
    const cdStart = this.offset;
    let cdSize = 0;
    for (const e of this.entries) {
      const c = new Uint8Array(46);
      const v = new DataView(c.buffer);
      v.setUint32(0, 0x02014b50, true);
      v.setUint16(4, 20, true); // made by
      v.setUint16(6, 20, true); // needed
      v.setUint16(8, 0x0800, true);
      v.setUint16(10, 0, true);
      v.setUint16(12, e.time, true);
      v.setUint16(14, e.day, true);
      v.setUint32(16, e.crc, true);
      v.setUint32(20, e.size, true);
      v.setUint32(24, e.size, true);
      v.setUint16(28, e.nameBytes.length, true);
      v.setUint32(42, e.offset, true);
      this.parts.push(c, e.nameBytes);
      cdSize += 46 + e.nameBytes.length;
    }
    const end = new Uint8Array(22);
    const v = new DataView(end.buffer);
    v.setUint32(0, 0x06054b50, true);
    v.setUint16(8, this.entries.length, true);
    v.setUint16(10, this.entries.length, true);
    v.setUint32(12, cdSize, true);
    v.setUint32(16, cdStart, true);
    this.parts.push(end);
    this.offset += cdSize + 22;
    return this.parts;
  }
}
