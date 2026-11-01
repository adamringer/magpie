#!/usr/bin/env python3
"""Generate web/icons/icon-192.png and icon-512.png (flat camera on a solid background; stdlib only).

    python3 tools/web/make_icons.py

Deterministic: the same output bytes every run. The artwork stays inside the central 80% so the icons also
work as maskable icons.
"""
import os
import struct
import zlib

BG = (29, 53, 87)  # #1d3557
BODY = (241, 250, 238)
LENS_RING = (168, 218, 220)
LENS = (29, 53, 87)
GLINT = (241, 250, 238)
ACCENT = (230, 57, 70)


def rrect(x, y, x0, y0, x1, y1, r):
    if x < x0 or x > x1 or y < y0 or y > y1:
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def circle(x, y, cx, cy, r):
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def color_at(x, y):
    """x, y in 0..1."""
    if circle(x, y, 0.5, 0.56, 0.085):
        return GLINT if circle(x, y, 0.47, 0.53, 0.025) else LENS
    if circle(x, y, 0.5, 0.56, 0.14):
        return LENS_RING
    if circle(x, y, 0.5, 0.56, 0.175):
        return LENS
    if circle(x, y, 0.72, 0.40, 0.025):
        return ACCENT
    if rrect(x, y, 0.36, 0.26, 0.52, 0.34, 0.02):  # viewfinder bump
        return BODY
    if rrect(x, y, 0.2, 0.32, 0.8, 0.78, 0.07):
        return BODY
    return BG


def render(size, ss=3):
    rows = []
    for py in range(size):
        row = bytearray([0])  # filter type 0
        for px in range(size):
            r = g = b = 0
            for sy in range(ss):
                for sx in range(ss):
                    c = color_at((px + (sx + 0.5) / ss) / size, (py + (sy + 0.5) / ss) / size)
                    r += c[0]
                    g += c[1]
                    b += c[2]
            n = ss * ss
            row += bytes(((r + n // 2) // n, (g + n // 2) // n, (b + n // 2) // n))
        rows.append(bytes(row))
    return b"".join(rows)


def chunk(tag, data):
    c = struct.pack(">I", len(data)) + tag + data
    return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def png(size):
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(render(size), 9)) + chunk(b"IEND", b"")


def main():
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "web", "icons")
    os.makedirs(out, exist_ok=True)
    for size in (192, 512):
        path = os.path.join(out, "icon-%d.png" % size)
        with open(path, "wb") as f:
            f.write(png(size))
        print("wrote", os.path.normpath(path))


if __name__ == "__main__":
    main()
