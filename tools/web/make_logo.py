#!/usr/bin/env python3
"""Draw the magpie logo: a magpie in side profile whose eye is a camera lens.

    python3 -I tools/web/make_logo.py

Writes (deterministic, from the same vector paths):
    docs/img/logo.svg, docs/img/logo.png      transparent background, for the README
    web/icons/logo.svg                        transparent, for the page header
    web/icons/icon-192.png, icon-512.png      on a light rounded tile, bird inside the maskable safe zone
Needs pycairo (system package python3-cairo); no other dependencies.
"""
import math
import os

import cairo

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LINE = (0.07, 0.08, 0.11)           # cartoon outline
BODY = (0.17, 0.19, 0.24)           # soft black
TAIL = (0.13, 0.50, 0.55)           # magpie's blue-green sheen, simplified
BEAK = (0.24, 0.26, 0.31)
WHITE = (0.98, 0.98, 0.96)
TILE = (0.93, 0.95, 0.97)


def outlined(cr, fill, width=12):
    """fill the current path, then stroke it with the cartoon outline"""
    cr.set_source_rgb(*fill)
    cr.fill_preserve()
    cr.set_source_rgb(*LINE)
    cr.set_line_width(width)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    cr.stroke()


def blob(cr, cx, cy, rx, ry):
    cr.save()
    cr.translate(cx, cy)
    cr.scale(rx, ry)
    cr.arc(0, 0, 1, 0, 2 * math.pi)
    cr.restore()


def bird(cr):
    """cartoon magpie on a 512 x 512 canvas, facing right: round body, big head, big lens eye"""
    # tail: short, chunky, perked up behind the body
    cr.move_to(170, 300)
    cr.curve_to(130, 270, 80, 220, 60, 180)
    cr.curve_to(52, 162, 70, 150, 86, 160)
    cr.curve_to(130, 190, 180, 230, 210, 262)
    cr.close_path()
    outlined(cr, TAIL)
    # feet
    cr.set_source_rgb(*LINE)
    cr.set_line_width(12)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    for x in (238, 290):
        cr.move_to(x, 380)
        cr.line_to(x, 420)
        cr.move_to(x - 16, 424)
        cr.line_to(x + 18, 424)
        cr.stroke()
    # body
    blob(cr, 262, 310, 118, 92)
    outlined(cr, BODY)
    # white belly
    cr.save()
    blob(cr, 262, 310, 118, 92)
    cr.clip()
    blob(cr, 300, 352, 100, 70)
    cr.set_source_rgb(*WHITE)
    cr.fill()
    cr.restore()
    blob(cr, 262, 310, 118, 92)                         # re-stroke the body outline over the belly
    cr.set_source_rgb(*LINE)
    cr.set_line_width(12)
    cr.stroke()
    # wing with a white shoulder stripe
    cr.move_to(196, 268)
    cr.curve_to(220, 236, 290, 236, 300, 282)
    cr.curve_to(290, 322, 222, 330, 196, 268)
    cr.close_path()
    outlined(cr, BODY, 10)
    cr.move_to(214, 262)
    cr.curve_to(236, 246, 272, 248, 284, 268)
    cr.curve_to(262, 258, 236, 258, 214, 262)
    cr.close_path()
    cr.set_source_rgb(*WHITE)
    cr.fill()
    # beak: short and rounded
    cr.move_to(392, 168)
    cr.curve_to(430, 176, 462, 190, 474, 204)
    cr.curve_to(462, 214, 428, 222, 392, 228)
    cr.close_path()
    outlined(cr, BEAK, 10)
    # head: big
    cr.arc(330, 186, 92, 0, 2 * math.pi)
    outlined(cr, BODY)
    cr.arc(298, 136, 26, math.radians(200), math.radians(280))   # shine on the head
    cr.set_source_rgba(1, 1, 1, 0.55)
    cr.set_line_width(9)
    cr.stroke()
    lens(cr, 346, 180, 50)


def lens(cr, x, y, r):
    """the eye: a big, simple camera lens"""
    cr.arc(x, y, r, 0, 2 * math.pi)                       # silver barrel with outline
    outlined(cr, (0.80, 0.83, 0.88), 10)
    cr.arc(x, y, r * 0.72, 0, 2 * math.pi)                # glass
    g = cairo.RadialGradient(x - r * 0.2, y - r * 0.2, r * 0.05, x, y, r * 0.72)
    g.add_color_stop_rgb(0, 0.36, 0.52, 0.95)
    g.add_color_stop_rgb(0.6, 0.20, 0.18, 0.55)
    g.add_color_stop_rgb(1, 0.08, 0.08, 0.20)
    cr.set_source(g)
    cr.fill()
    cr.arc(x, y, r * 0.30, 0, 2 * math.pi)                # pupil-like centre
    cr.set_source_rgba(0.05, 0.05, 0.12, 0.85)
    cr.fill()
    cr.arc(x - r * 0.28, y - r * 0.30, r * 0.17, 0, 2 * math.pi)   # big glint
    cr.set_source_rgb(1, 1, 1)
    cr.fill()
    cr.arc(x + r * 0.26, y + r * 0.22, r * 0.08, 0, 2 * math.pi)
    cr.set_source_rgba(1, 1, 1, 0.7)
    cr.fill()


def draw_transparent(surface, size):
    cr = cairo.Context(surface)
    cr.scale(size / 512.0, size / 512.0)
    bird(cr)
    return cr


def draw_icon(surface, size):
    cr = cairo.Context(surface)
    cr.scale(size / 512.0, size / 512.0)
    rr = 96                                               # rounded tile
    cr.new_path()
    cr.arc(rr, rr, rr, math.pi, 1.5 * math.pi)
    cr.arc(512 - rr, rr, rr, 1.5 * math.pi, 2 * math.pi)
    cr.arc(512 - rr, 512 - rr, rr, 0, 0.5 * math.pi)
    cr.arc(rr, 512 - rr, rr, 0.5 * math.pi, math.pi)
    cr.close_path()
    cr.set_source_rgb(*TILE)
    cr.fill()
    cr.translate(256, 256)                                # maskable: bird inside the central 80 %
    cr.scale(0.80, 0.80)
    cr.translate(-266, -290)
    bird(cr)


def main():
    docs = os.path.join(ROOT, "docs", "img")
    web = os.path.join(ROOT, "web", "icons")
    os.makedirs(docs, exist_ok=True)
    os.makedirs(web, exist_ok=True)
    for path in (os.path.join(docs, "logo.svg"), os.path.join(web, "logo.svg")):
        s = cairo.SVGSurface(path, 512, 512)
        s.set_document_unit(cairo.SVGUnit.PX)
        draw_transparent(s, 512)
        s.finish()
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, 256, 256)
    draw_transparent(s, 256)
    s.write_to_png(os.path.join(docs, "logo.png"))
    for size in (192, 512):
        s = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        draw_icon(s, size)
        s.write_to_png(os.path.join(web, "icon-%d.png" % size))
    print("wrote docs/img/logo.svg, docs/img/logo.png, web/icons/logo.svg, web/icons/icon-192.png, icon-512.png")


if __name__ == "__main__":
    main()
