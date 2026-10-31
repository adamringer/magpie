import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import yuv2png  # noqa: E402

# (R, G, B) -> full-range BT.601 (Y, U, V), computed independently of the converter.
COLOURS = {
    "red": ((255, 0, 0), (76, 85, 255)),
    "green": ((0, 255, 0), (150, 44, 21)),
    "blue": ((0, 0, 255), (29, 255, 107)),
    "white": ((255, 255, 255), (255, 128, 128)),
    "black": ((0, 0, 0), (0, 128, 128)),
}


def make_frame(fmt, w=8, h=len(COLOURS)):
    buf = bytearray()
    for _, (_, (y, u, v)) in COLOURS.items():
        for _ in range(w // 2):
            buf += bytes((y, u, y, v) if fmt == "yuyv" else (u, y, v, y))
    return bytes(buf), w, h


@pytest.mark.parametrize("fmt", ["yuyv", "uyvy"])
def test_known_colours(fmt):
    data, w, h = make_frame(fmt)
    rgb = yuv2png.yuv_to_rgb(data, w, h, fmt)
    assert rgb.shape == (h, w, 3)
    for row, (_, (expected, _)) in enumerate(COLOURS.items()):
        assert np.abs(rgb[row].astype(int) - expected).max() <= 3


def test_cli_infers_640x480(tmp_path):
    from PIL import Image
    src = tmp_path / "a.yuyv"
    src.write_bytes(bytes((128, 128, 128, 128)) * (640 * 480 // 2))
    out = tmp_path / "a.png"
    assert yuv2png.main([str(src), "-o", str(out)]) == 0
    img = Image.open(out)
    assert img.size == (640, 480)
    assert img.getpixel((10, 10)) == (128, 128, 128)


def test_short_data_rejected():
    with pytest.raises(ValueError):
        yuv2png.yuv_to_rgb(b"\0" * 10, 8, 8)
