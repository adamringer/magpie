#!/usr/bin/env python3
"""Convert a raw YUYV or UYVY frame (from `snap`) to PNG.

    .venv/bin/python -I tools/yuv2png.py photo.yuyv [-o photo.png] [--size 640x480] [--format yuyv|uyvy]

The format defaults to the file extension; the size is inferred when the file is exactly
640x480x2 bytes. Conversion is full-range BT.601 (JFIF), which is what the OV5640 outputs by default.
"""
import argparse
import os
import sys

import numpy as np
from PIL import Image

DEFAULT_SIZE = (640, 480)


def yuv_to_rgb(data: bytes, width: int, height: int, fmt: str = "yuyv") -> np.ndarray:
    """Return an (height, width, 3) uint8 RGB array from packed 4:2:2 data."""
    if fmt not in ("yuyv", "uyvy"):
        raise ValueError(f"unknown format {fmt!r}")
    if width % 2:
        raise ValueError("width must be even")
    need = width * height * 2
    if len(data) < need:
        raise ValueError(f"need {need} bytes for {width}x{height}, got {len(data)}")
    px = np.frombuffer(data, dtype=np.uint8, count=need).reshape(height, width // 2, 4)
    if fmt == "yuyv":
        y0, u, y1, v = (px[:, :, i] for i in range(4))
    else:
        u, y0, v, y1 = (px[:, :, i] for i in range(4))
    y = np.empty((height, width), dtype=np.float32)
    y[:, 0::2] = y0
    y[:, 1::2] = y1
    uu = np.repeat(u.astype(np.float32), 2, axis=1) - 128.0
    vv = np.repeat(v.astype(np.float32), 2, axis=1) - 128.0
    r = y + 1.402 * vv
    g = y - 0.344136 * uu - 0.714136 * vv
    b = y + 1.772 * uu
    rgb = np.stack([r, g, b], axis=2)
    return np.clip(np.rint(rgb), 0, 255).astype(np.uint8)


def parse_size(s: str) -> tuple[int, int]:
    try:
        w, h = s.lower().split("x")
        return int(w), int(h)
    except ValueError:
        raise argparse.ArgumentTypeError(f"bad size {s!r}, want WxH")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("input")
    ap.add_argument("-o", "--output", help="PNG path (default: input with .png)")
    ap.add_argument("--size", type=parse_size, help="WxH (default: infer 640x480 from file size)")
    ap.add_argument("--format", choices=("yuyv", "uyvy"), help="default: from the file extension")
    args = ap.parse_args(argv)

    fmt = args.format or os.path.splitext(args.input)[1].lstrip(".").lower()
    if fmt not in ("yuyv", "uyvy"):
        ap.error("cannot tell the format from the extension; use --format")
    with open(args.input, "rb") as f:
        data = f.read()
    size = args.size
    if size is None:
        if len(data) == DEFAULT_SIZE[0] * DEFAULT_SIZE[1] * 2:
            size = DEFAULT_SIZE
        else:
            ap.error(f"file is {len(data)} bytes, not 640x480; give --size")
    out = args.output or os.path.splitext(args.input)[0] + ".png"
    Image.fromarray(yuv_to_rgb(data, *size, fmt), "RGB").save(out)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
