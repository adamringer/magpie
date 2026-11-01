#!/usr/bin/env python3
"""Convert a RINEX 2 GPS navigation file (e.g. NOAA NGS CORS brdcDDD0.YYn.gz) into the RINEX 3 record layout
that snapfix.read_nav parses.

    python3 -I rnx2nav3.py brdc3050.26n.gz OUT.rnx.gz

Source (US government, public, no login): https://geodesy.noaa.gov/corsdata/rinex/YYYY/DDD/brdcDDD0.YYn.gz
Only the record layout changes: "PP YY MM DD HH MM SS.S" + 3 values becomes "Gnn YYYY MM DD HH MM SS" + 3 values,
and the 7 continuation lines get one more leading space (RINEX 2 indents 3 columns, RINEX 3 indents 4).
"""
import gzip
import sys


def convert(lines):
    out = []
    i = next(k for k, l in enumerate(lines) if 'END OF HEADER' in l) + 1
    out += ['     3.04           N: GNSS NAV DATA    G: GPS              RINEX VERSION / TYPE',
            'converted from RINEX 2 by rnx2nav3.py                       COMMENT',
            '                                                            END OF HEADER']
    while i + 7 < len(lines):
        l = lines[i].ljust(79)
        prn, yy, mo, d, h, mi, s = l[0:2], int(l[3:5]), int(l[6:8]), int(l[9:11]), int(l[12:14]), int(l[15:17]), float(l[17:22])
        year = 2000 + yy if yy < 80 else 1900 + yy
        out.append('G%02d %04d %02d %02d %02d %02d %02d' % (int(prn), year, mo, d, h, mi, int(round(s))) + l[22:79])
        out += [' ' + lines[i + k].rstrip('\n') for k in range(1, 8)]
        i += 8
    return out


def main():
    src, dst = sys.argv[1], sys.argv[2]
    op = gzip.open if src.endswith('.gz') else open
    with op(src, 'rt') as fh:
        lines = fh.read().splitlines()
    out = convert(lines)
    wo = gzip.open if dst.endswith('.gz') else open
    with wo(dst, 'wt') as fh:
        fh.write('\n'.join(out) + '\n')
    print('%d records' % ((len(out) - 3) // 8))


if __name__ == '__main__':
    main()
