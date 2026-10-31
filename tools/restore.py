#!/usr/bin/env python3
"""SAM-BA restore path for the Clip (docs/RECOVERY.md). The Clip must be in
SAM-BA mode (03eb:6124).

    .venv/bin/python -I -B tools/restore.py sdram      # load + run restore/sdram_init.bin
    .venv/bin/python -I -B tools/restore.py memtest    # check the 32 MB SDRAM
    .venv/bin/python -I -B tools/restore.py uboot      # SDRAM init, then run U-Boot (ums)
"""
import argparse
import glob
import os
import re
import sys

import serial

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from samba import BSC_CR, BSC_KEY, Samba, SambaError, find_port  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APPLET = os.path.join(ROOT, "restore", "sdram_init.bin")
APPLET_ADDR = 0x300000
SDRAM = 0x20000000
SDRAM_SIZE = 32 << 20
DDRSDRC_RTR = 0xFFFFE804
GPBR1 = 0xFFFFFE64
UBOOT_BIN = os.path.join(ROOT, "output-restore", "images", "u-boot.bin")
UBOOT_BUILD = os.path.join(ROOT, "output-restore", "build", "uboot-*", ".config")
UBOOT_ADDR = 0x21100000  # CONFIG_TEXT_BASE in br2-external/board/magpie/uboot/restore.fragment


def sdram_init(sb):
    with open(APPLET, "rb") as f:
        code = f.read()
    sb.write(APPLET_ADDR, code)
    if sb.read(APPLET_ADDR, len(code)) != code:
        raise SystemExit("applet readback mismatch")
    sb.go(APPLET_ADDR)
    rtr = sb.read32(DDRSDRC_RTR)
    print(f"applet ran ({len(code)} bytes), RTR = {rtr:#x}")
    if rtr != 0x176:
        raise SystemExit("RTR not set: applet didn't complete")


def uboot_link_addr():
    """UBOOT_ADDR, checked against CONFIG_TEXT_BASE of the U-Boot build if present."""
    for cfg in glob.glob(UBOOT_BUILD):
        with open(cfg) as f:
            m = re.search(r"^CONFIG_TEXT_BASE=(0x[0-9a-fA-F]+)$", f.read(), re.M)
        if m and int(m.group(1), 16) != UBOOT_ADDR:
            raise SystemExit(f"{cfg}: CONFIG_TEXT_BASE={m.group(1)} but UBOOT_ADDR={UBOOT_ADDR:#x}")
    return UBOOT_ADDR


def uboot(sb, path):
    addr = uboot_link_addr()
    with open(path, "rb") as f:
        code = f.read()
    if not code or addr + len(code) > SDRAM + SDRAM_SIZE:
        raise SystemExit(f"{path}: bad size {len(code)}")
    sdram_init(sb)
    # U-Boot ends with "ums 0 mmc 0; reset". Set normal boot (BOOT=4) now, so
    # that reset boots the eMMC instead of coming back to SAM-BA.
    sb.write32(BSC_CR, BSC_KEY | 4)
    if sb.read32(BSC_CR) & 0xFF != 4:
        raise SystemExit("could not set BSC_CR to 4")
    sb.write(addr, code)
    if sb.read(addr, len(code)) != code:
        raise SystemExit("U-Boot readback mismatch")
    # U-Boot leaves progress markers in GPBR1 (see restore.fragment); start from 0
    sb.write32(GPBR1, 0)
    print(f"U-Boot loaded ({len(code)} bytes at {addr:#x}), starting it")
    sb.go(addr)
    print("U-Boot started. In a few seconds the camera should re-enumerate as a USB "
          "drive 0525:a4a5 (the eMMC, via ums). When done, eject it on the host "
          "(e.g. `udisksctl power-off -b /dev/sdX`); U-Boot then resets and boots "
          "normally (BSC_CR is already 4).")


def memtest(sb):
    ok = True
    for addr in (SDRAM, SDRAM + 0x800000, SDRAM + 0x1000000, SDRAM + SDRAM_SIZE - 0x10000):
        data = os.urandom(0x10000)
        sb.write(addr, data)
        back = sb.read(addr, len(data))
        bad = sum(1 for a, b in zip(data, back) if a != b)
        print(f"{addr:#010x} 64 KiB: {'ok' if not bad else f'{bad} bad bytes'}")
        ok &= not bad
    # 32 MB fitted: SDRAM + 32 MB aliases SDRAM
    sb.write32(SDRAM, 0x11111111)
    sb.write32(SDRAM + SDRAM_SIZE, 0x22222222)
    alias = sb.read32(SDRAM)
    print(f"size check: word at base after writing base+32MB = {alias:#010x}",
          "(32 MB, aliases)" if alias == 0x22222222 else "(no alias at 32 MB)")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port")
    ap.add_argument("cmd", choices=["sdram", "memtest", "uboot"])
    ap.add_argument("--image", default=UBOOT_BIN,
                    help="u-boot.bin for the uboot command (default: %(default)s)")
    args = ap.parse_args()
    ser = serial.Serial(args.port or find_port(), 115200, timeout=2)
    sb = Samba(ser)
    sb.init()
    if args.cmd == "sdram":
        sdram_init(sb)
        return 0
    if args.cmd == "uboot":
        uboot(sb, args.image)
        return 0
    return memtest(sb)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (SambaError, serial.SerialException) as e:
        sys.exit(f"error: {e}")
