// WebUSB transport for the Clip's vendor-specific (class 0xFF) interface. Browser only.
// Only that interface is claimed; the network (CDC) interfaces are never touched.

export const VENDOR_ID = 0x0525;
export const PRODUCT_ID = 0xa4a1;
export const FILTERS = [{ vendorId: VENDOR_ID, productId: PRODUCT_ID }];
const PACKET = 512;
const MAX_TRANSFER = 65536;

export function isSupported() {
  return typeof navigator !== "undefined" && !!navigator.usb;
}

export function isClip(device) {
  return device.vendorId === VENDOR_ID && device.productId === PRODUCT_ID;
}

export class WebUsbTransport {
  constructor(device, ifaceNumber, epIn, epOut) {
    this.device = device;
    this.ifaceNumber = ifaceNumber;
    this.epIn = epIn;
    this.epOut = epOut;
  }

  static async open(device) {
    await device.open();
    try {
      if (device.configuration === null) await device.selectConfiguration(1);
      let found = null;
      for (const iface of device.configuration.interfaces) {
        const alt = iface.alternates.find((a) => a.interfaceClass === 0xff);
        if (!alt) continue;
        const inEp = alt.endpoints.find((e) => e.direction === "in" && e.type === "bulk");
        const outEp = alt.endpoints.find((e) => e.direction === "out" && e.type === "bulk");
        if (inEp && outEp) {
          found = { iface, alt, inEp, outEp };
          break;
        }
      }
      if (!found) throw new Error("The Clip's data interface was not found (is the Clip firmware up to date?)");
      await device.claimInterface(found.iface.interfaceNumber);
      return new WebUsbTransport(device, found.iface.interfaceNumber, found.inEp.endpointNumber, found.outEp.endpointNumber);
    } catch (e) {
      try {
        await device.close();
      } catch (_) {}
      throw e;
    }
  }

  async write(data) {
    const r = await this.device.transferOut(this.epOut, data);
    if (r.status !== "ok" || r.bytesWritten !== data.length) throw new Error("USB write failed: " + r.status);
  }

  // The transfer length is rounded up to whole packets (a full packet must never overflow the buffer) and
  // capped at 64 KiB. It is never larger than needed for the data the caller still wants: the device sends no
  // zero-length packet, so a transfer larger than the remaining data that ends on a packet boundary would
  // never complete.
  async read(maxLen) {
    const len = Math.min(MAX_TRANSFER, Math.max(PACKET, Math.ceil(maxLen / PACKET) * PACKET));
    const r = await this.device.transferIn(this.epIn, len);
    if (r.status !== "ok") throw new Error("USB read failed: " + r.status);
    return new Uint8Array(r.data.buffer, r.data.byteOffset, r.data.byteLength);
  }

  async close() {
    try {
      await this.device.releaseInterface(this.ifaceNumber);
    } catch (_) {}
    try {
      await this.device.close();
    } catch (_) {}
  }
}
