#!/usr/bin/env python3
"""Client for the Atmel SAM-BA boot-ROM monitor over USB CDC-ACM (03eb:6124).

    .venv/bin/python -I tools/samba.py version
    .venv/bin/python -I tools/samba.py read32 0xFFFFFE54
    .venv/bin/python -I tools/samba.py set-bootmode 4
"""
import argparse
import sys

import serial
import serial.tools.list_ports

VID = 0x03EB
PID = 0x6124
BSC_CR = 0xFFFFFE54
BSC_KEY = 0x66830000
RSTC_CR = 0xFFFFFE00
RSTC_RESET = 0xA5000005
CHUNK = 4096


class SambaError(Exception):
    pass


class Samba:
    def __init__(self, ser):
        self.ser = ser

    def _cmd(self, text):
        self.ser.write(text.encode("ascii"))
        self.ser.flush()

    def _read_exact(self, n):
        data = self.ser.read(n)
        if len(data) != n:
            raise SambaError(f"short read: wanted {n} bytes, got {len(data)}")
        return data

    def init(self):
        old = self.ser.timeout
        self.ser.timeout = 0.1
        try:
            while self.ser.read(4096):
                pass
            self._cmd("N#")
            self.ser.read(2)
        finally:
            self.ser.timeout = old

    def version(self):
        old = self.ser.timeout
        self.ser.timeout = 0.1
        try:
            self._cmd("V#")
            data = self.ser.read(256)
        finally:
            self.ser.timeout = old
        out = bytearray()
        for b in data:
            if not 0x20 <= b < 0x7F:
                break
            out.append(b)
        return out.decode("ascii")

    def read32(self, addr):
        self._cmd(f"w{addr:08X},4#")
        return int.from_bytes(self._read_exact(4), "little")

    def write32(self, addr, val):
        self._cmd(f"W{addr:08X},{val:08X}#")

    def read8(self, addr):
        self._cmd(f"o{addr:08X},4#")
        return self._read_exact(1)[0]

    def write8(self, addr, val):
        self._cmd(f"O{addr:08X},{val:02X}#")

    def read(self, addr, length):
        out = bytearray()
        while len(out) < length:
            n = min(CHUNK, length - len(out))
            # The SAM9G25 ROM (v1.4) sends R data in 2048-byte pieces and loses a
            # final full 512-byte packet: lengths of 512 mod 2048 come back
            # short. Read 256 less now; the rest follows as a 256-byte read.
            if n % 2048 == 512:
                n -= 256
            self._cmd(f"R{addr + len(out):08X},{n:08X}#")
            out += self._read_exact(n)
        return bytes(out)

    def write(self, addr, data):
        for off in range(0, len(data), CHUNK):
            chunk = data[off:off + CHUNK]
            self._cmd(f"S{addr + off:08X},{len(chunk):08X}#")
            self.ser.write(bytes(chunk))
            self.ser.flush()

    def go(self, addr):
        self._cmd(f"G{addr:08X}#")


def find_port():
    for p in serial.tools.list_ports.comports():
        if p.vid == VID and p.pid == PID:
            return p.device
    raise SambaError(f"no SAM-BA device {VID:04x}:{PID:04x} found")


def num(s):
    return int(s, 0)


def bootmode_text(boot):
    if boot == 4:
        return "normal boot (SPI flash first, then SAM-BA)"
    if 5 <= boot <= 7:
        return "SAM-BA monitor only"
    if boot == 0:
        return "factory full boot sequence"
    return "see datasheet table 10-2"


def build_parser():
    ap = argparse.ArgumentParser(description="SAM-BA monitor client (USB only)")
    ap.add_argument("--port", help="serial device (default: auto-detect 03eb:6124)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("version")
    p = sub.add_parser("read32")
    p.add_argument("addr", type=num)
    p = sub.add_parser("write32")
    p.add_argument("addr", type=num)
    p.add_argument("value", type=num)
    sub.add_parser("bootmode", help="read BSC_CR and show the boot mode")
    p = sub.add_parser("set-bootmode", help="write BSC_CR (4 normal, 5 SAM-BA only)")
    p.add_argument("mode", type=num, choices=range(8))
    sub.add_parser("reset", help="write RSTC_CR to reset the chip")
    p = sub.add_parser("dump")
    p.add_argument("addr", type=num)
    p.add_argument("length", type=num)
    p.add_argument("outfile")
    p = sub.add_parser("load")
    p.add_argument("addr", type=num)
    p.add_argument("infile")
    p = sub.add_parser("go")
    p.add_argument("addr", type=num)
    return ap


def run(args, sb):
    if args.cmd == "version":
        print(sb.version())
    elif args.cmd == "read32":
        print(f"0x{args.addr:08X}: 0x{sb.read32(args.addr):08X}")
    elif args.cmd == "write32":
        sb.write32(args.addr, args.value)
    elif args.cmd == "bootmode":
        boot = sb.read32(BSC_CR) & 0xFF
        print(f"BOOT={boot}: {bootmode_text(boot)}")
    elif args.cmd == "set-bootmode":
        sb.write32(BSC_CR, BSC_KEY | args.mode)
        boot = sb.read32(BSC_CR) & 0xFF
        if boot != args.mode:
            print(f"verify failed: wrote BOOT={args.mode}, read back {boot}")
            return 1
        print(f"BOOT={boot}: {bootmode_text(boot)}")
    elif args.cmd == "reset":
        sb.write32(RSTC_CR, RSTC_RESET)
        print("reset sent; the device will re-enumerate")
    elif args.cmd == "dump":
        data = sb.read(args.addr, args.length)
        with open(args.outfile, "wb") as f:
            f.write(data)
        print(f"{len(data)} bytes -> {args.outfile}")
    elif args.cmd == "load":
        with open(args.infile, "rb") as f:
            data = f.read()
        sb.write(args.addr, data)
        print(f"{len(data)} bytes -> 0x{args.addr:08X}")
    elif args.cmd == "go":
        sb.go(args.addr)
    return 0


def main(argv=None, ser=None):
    args = build_parser().parse_args(argv)
    if ser is None:
        port = args.port or find_port()
        ser = serial.Serial(port, 115200, timeout=1)
    sb = Samba(ser)
    sb.init()
    return run(args, sb)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (SambaError, serial.SerialException) as e:
        sys.exit(f"error: {e}")
