#!/usr/bin/env python3
"""Write KML placemarks (Google Earth, Google My Maps import, most map apps).

    python3 -I kml.py OUT.kml NAME LAT LON [DESCRIPTION] [-- NAME LAT LON [DESCRIPTION] ...]

Library use: write(path, {folder name: [(name, lat, lon, description, style), ...]}) with style one of
'ref' (yellow pushpin), 'fix' (small dot), 'median' (red pushpin). Coordinates in decimal degrees (WGS 84; NAD 83
differs by ~1-2 m in CONUS). Output files usually contain a location: keep them under analysis/ (git-ignored).
"""
import sys
from xml.sax.saxutils import escape

STYLES = {
    'ref': 'http://maps.google.com/mapfiles/kml/pushpin/ylw-pushpin.png',
    'median': 'http://maps.google.com/mapfiles/kml/pushpin/red-pushpin.png',
    'fix': 'http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png',
}


def write(path, folders, title='magpie'):
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<kml xmlns="http://www.opengis.net/kml/2.2">', '<Document>',
           '<name>%s</name>' % escape(title)]
    for k, href in STYLES.items():
        scale = 0.8 if k == 'fix' else 1.1
        out.append('<Style id="%s"><IconStyle><scale>%.1f</scale><Icon><href>%s</href></Icon></IconStyle></Style>'
                   % (k, scale, href))
    for fname, pts in folders.items():
        out.append('<Folder><name>%s</name>' % escape(fname))
        for name, lat, lon, desc, style in pts:
            out.append('<Placemark><name>%s</name><description>%s</description><styleUrl>#%s</styleUrl>'
                       '<Point><coordinates>%.9f,%.9f,0</coordinates></Point></Placemark>'
                       % (escape(name), escape(desc or ''), style, lon, lat))
        out.append('</Folder>')
    out += ['</Document>', '</kml>']
    with open(path, 'w') as fh:
        fh.write('\n'.join(out) + '\n')


def main():
    if len(sys.argv) < 5:
        sys.exit(__doc__)
    pts, cur = [], []
    for a in sys.argv[2:] + ['--']:
        if a == '--':
            if cur:
                pts.append((cur[0], float(cur[1]), float(cur[2]), cur[3] if len(cur) > 3 else '', 'ref'))
            cur = []
        else:
            cur.append(a)
    write(sys.argv[1], {'points': pts})


if __name__ == '__main__':
    main()
