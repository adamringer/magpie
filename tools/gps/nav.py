#!/usr/bin/env python3
"""GPS broadcast ephemeris: NOAA NGS CORS RINEX 2 is the native format; other formats are converted into it.

    python3 -I nav.py fetch YYYY DDD [OUTDIR]   -> OUTDIR/brdcDDD0.YYn.gz (default OUTDIR: .)
    python3 -I nav.py convert IN.rnx[.gz] OUT.YYn[.gz]   RINEX 3 (GPS records) -> RINEX 2

fetch tries, in order:
  1. NOAA NGS CORS (US government, public, no login): https://geodesy.noaa.gov/corsdata/rinex/YYYY/DDD/brdcDDD0.YYn.gz
     (merged daily GPS file, updated through the day: a same-day file covers up to its generation time + ~2 h)
  2. BKG IGS mirror, RINEX 3 BRDC00WRD_S_YYYYDDD0000_01D_MN.rnx.gz, converted to RINEX 2 under the same name as 1.
Either way the scripts see one file name and one format.

read() parses RINEX 2 GPS nav files; given a RINEX 3 file it converts it first (rnx3_to_rnx2), so old files still work.
"""
import datetime
import math
import gzip
import os
import sys
import urllib.request

GPS_EPOCH = datetime.datetime(1980, 1, 6)
NOAA_URL = 'https://geodesy.noaa.gov/corsdata/rinex/{y}/{d:03d}/brdc{d:03d}0.{yy:02d}n.gz'
BKG_URL = 'https://igs.bkg.bund.de/root_ftp/IGS/BRDC/{y}/{d:03d}/BRDC00WRD_S_{y}{d:03d}0000_01D_MN.rnx.gz'


def _lines(path):
    op = gzip.open if path.endswith('.gz') else open
    with op(path, 'rt') as fh:
        return fh.read().splitlines()


def _version(lines):
    return float(lines[0][:9])


def rnx3_to_rnx2(lines):
    """RINEX 3 nav (mixed or GPS-only) -> RINEX 2.11 GPS nav lines. Non-GPS records are dropped."""
    out = ['     2.11           N: GPS NAV DATA                         RINEX VERSION / TYPE',
           'converted from RINEX %-39sCOMMENT' % lines[0][:9].strip()]
    i = next(k for k, l in enumerate(lines) if 'END OF HEADER' in l) + 1
    for l in lines[:i]:                  # Klobuchar coefficients, if the RINEX 3 header has them
        if 'IONOSPHERIC CORR' in l and l[:4] in ('GPSA', 'GPSB'):
            out.append('  ' + l[5:53].rstrip().ljust(58) + ('ION ALPHA' if l[:4] == 'GPSA' else 'ION BETA'))
    out.append('                                                            END OF HEADER')
    while i < len(lines):
        l = lines[i]
        if not l.startswith('G'):        # other systems, and continuation lines of their records
            i += 1
            continue
        l = l.ljust(80)
        prn, y, mo, d, h, mi, s = (int(l[1:3]), int(l[4:8]), int(l[9:11]), int(l[12:14]), int(l[15:17]),
                                   int(l[18:20]), int(l[21:23]))
        out.append('%2d %02d %2d %2d %2d %2d%5.1f' % (prn, y % 100, mo, d, h, mi, s) + l[23:80].rstrip())
        out += [lines[i + k][1:].rstrip() for k in range(1, 8)]   # RINEX 3 indents 4 columns, RINEX 2 indents 3
        i += 8
    return out


class Eph(dict):
    """{prn: [ephemeris dict, ...]}, plus .iono = (alpha[4], beta[4]) Klobuchar coefficients or None."""
    iono = None


def read(path):
    """Eph from a RINEX 2 GPS nav file (a RINEX 3 file is converted first)."""
    lines = _lines(path)
    if _version(lines) >= 3:
        lines = rnx3_to_rnx2(lines)
    i = next(k for k, l in enumerate(lines) if 'END OF HEADER' in l) + 1
    f = lambda t: float(t.replace('D', 'E').replace('d', 'e')) if t.strip() else 0.0
    eph = Eph()
    hdr = {l[60:].strip(): [f(l[2:14]), f(l[14:26]), f(l[26:38]), f(l[38:50])] for l in lines[:i]
           if l[60:].strip() in ('ION ALPHA', 'ION BETA')}
    if len(hdr) == 2:
        eph.iono = (hdr['ION ALPHA'], hdr['ION BETA'])
    else:
        print('nav: no Klobuchar coefficients in %s, no ionosphere correction' % path, file=sys.stderr)
    while i + 7 < len(lines):
        l = lines[i].ljust(79)
        prn = int(l[0:2])
        yy, mo, d, h, mi = int(l[3:5]), int(l[6:8]), int(l[9:11]), int(l[12:14]), int(l[15:17])
        s = float(l[17:22])
        y = 2000 + yy if yy < 80 else 1900 + yy
        v = [f(l[22:41]), f(l[41:60]), f(l[60:79])]
        for k in range(1, 8):
            ll = lines[i + k].ljust(79)
            v += [f(ll[3:22]), f(ll[22:41]), f(ll[41:60]), f(ll[60:79])]
        toc = (datetime.datetime(y, mo, d, h, mi) - GPS_EPOCH).total_seconds() + s
        e = dict(toc=toc, af0=v[0], af1=v[1], af2=v[2], crs=v[4], dn=v[5], m0=v[6], cuc=v[7], ecc=v[8],
                 cus=v[9], sqa=v[10], toe=v[11], cic=v[12], om0=v[13], cis=v[14], i0=v[15], crc=v[16],
                 w=v[17], omd=v[18], idot=v[19], week=v[21], health=v[24], tgd=v[25])
        e['toe_abs'] = e['week'] * 604800.0 + e['toe']
        eph.setdefault(prn, []).append(e)
        i += 8
    return eph


def klobuchar(iono, lat, lon, el, az, tow):
    """L1 ionospheric delay (s), IS-GPS-200 20.3.3.5.2.5. lat, lon, el, az in degrees, tow = GPS time of week (s)."""
    alpha, beta = iono
    E = el / 180.0                                        # semicircles
    psi = 0.0137 / (E + 0.11) - 0.022                     # Earth-centred angle
    phi = lat / 180.0 + psi * math.cos(math.radians(az))  # ionospheric pierce point
    phi = max(-0.416, min(0.416, phi))
    lam = lon / 180.0 + psi * math.sin(math.radians(az)) / math.cos(phi * math.pi)
    phim = phi + 0.064 * math.cos((lam - 1.617) * math.pi)   # geomagnetic latitude
    t = (4.32e4 * lam + tow) % 86400.0                    # local time
    F = 1.0 + 16.0 * (0.53 - E) ** 3                      # slant factor
    amp = max(0.0, sum(a * phim ** n for n, a in enumerate(alpha)))
    per = max(72000.0, sum(b * phim ** n for n, b in enumerate(beta)))
    x = 2 * math.pi * (t - 50400.0) / per
    if abs(x) < 1.57:
        return F * (5e-9 + amp * (1 - x * x / 2 + x ** 4 / 24))
    return F * 5e-9


def convert(src, dst):
    lines = _lines(src)
    if _version(lines) >= 3:
        lines = rnx3_to_rnx2(lines)
    wo = gzip.open if dst.endswith('.gz') else open
    with wo(dst, 'wt') as fh:
        fh.write('\n'.join(lines) + '\n')


def _get(url, dst, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as r, open(dst, 'wb') as fh:
        fh.write(r.read())


def fetch(year, doy, outdir='.'):
    """Download day DOY's GPS broadcast ephemeris as OUTDIR/brdcDDD0.YYn.gz (NOAA first, BKG converted second)."""
    kw = dict(y=year, d=doy, yy=year % 100)
    dst = os.path.join(outdir, 'brdc{d:03d}0.{yy:02d}n.gz'.format(**kw))
    try:
        _get(NOAA_URL.format(**kw), dst)
        return dst, 'NOAA'
    except Exception as ex:
        print('NOAA failed (%s), trying BKG' % ex, file=sys.stderr)
    tmp = dst + '.rnx3.tmp.gz'
    try:
        _get(BKG_URL.format(**kw), tmp)
        convert(tmp, dst)
        return dst, 'BKG (converted from RINEX 3)'
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == 'fetch':
        path, src = fetch(int(sys.argv[2]), int(sys.argv[3]), sys.argv[4] if len(sys.argv) > 4 else '.')
        e = read(path)
        last = max(x['toc'] for v in e.values() for x in v)
        print('%s from %s: %d PRNs, latest toc %s (GPS time)' % (path, src, len(e),
              (GPS_EPOCH + datetime.timedelta(seconds=last)).strftime('%Y-%m-%d %H:%M')))
    elif len(sys.argv) == 4 and sys.argv[1] == 'convert':
        convert(sys.argv[2], sys.argv[3])
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main()
