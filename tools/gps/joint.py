#!/usr/bin/env python3
"""Run-level solve: one shared clock offset for all captures of a run, from the captures that solve on their own.

  python3 -I joint.py MEAS.jsonl brdcDDD0.YYn.gz --ms 1024 [--zmin 8] [--sigma 0.5] [--ref truth.txt] [--out FILE]

Pass 1: every capture with >= 5 PRNs, coarse-time fix (5 unknowns) -> capture-time error tc_k.
The run's clock offset T = median(tc_k). Pass 2: every capture (>= 4 PRNs) again, with the soft prior tc = T +- sigma.
sigma: ~0.5 s for file-name times (gpsnap start-up jitter); ~0.02 s once captures carry a gpsnap .time sidecar.
With --ref (git-ignored file of approximate reference points) distances are printed; otherwise the spread around
the median position. Results contain the user's location: keep them in git-ignored files.
"""
import sys, os, json, argparse
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import snapfix as sf  # noqa: E402


def dist(a, b):
    la = np.radians((a[0] + b[0]) / 2)
    return float(np.hypot((a[0] - b[0]) * 111132.9, (a[1] - b[1]) * 111319.5 * np.cos(la)))


def fix(meas, eph, t0, prior=None, sigma=None):
    """best of: all PRNs, and (if that fits badly and there is a spare) each PRN left out once"""
    if SEED is not None:                         # elevation mask 10 deg at the seed position
        keep = {}
        for p, m in meas.items():
            rho, clk, v, sp = sf.geom(sf.pick(eph, p, t0), SEED, t0)
            if sf.elev(SEED, sp) >= 10.0:
                keep[p] = m
        meas = keep
    v = fix1(meas, eph, t0, prior, sigma)
    need = 4 if prior is not None else 5
    if (v is None or v['rms'] > 30) and len(meas) > need:
        for p in sorted(meas):
            w = fix1({k: m for k, m in meas.items() if k != p}, eph, t0, prior, sigma)
            if w and (v is None or w['rms'] < v['rms']):
                w['dropped'] = p
                v = w
    return v


SEED = None


def fix1(meas, eph, t0, prior=None, sigma=None):
    xd = SEED if SEED is not None else sf.doppler_fix(meas, eph, t0)[0]
    best, e = sf.code_fix(meas, eph, t0, xd, prior, sigma, grid=SEED is None)
    if best is None:
        return None
    rms, x, cb, tc, r, N = best
    lat, lon, h = sf.ecef2lla(x)
    return dict(lat=float(lat), lon=float(lon), h=float(h), rms=float(rms), tc=float(tc), n=len(meas))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('meas')
    ap.add_argument('nav')
    ap.add_argument('--ms', type=int, required=True)
    ap.add_argument('--zmin', type=float, default=8.0)
    ap.add_argument('--sigma', type=float, default=0.5)
    ap.add_argument('--ref', default=None)
    ap.add_argument('--out', default=None)
    ap.add_argument('--seed-file', default=None, help='"lat lon" file (last known position) used instead of the Doppler fix')
    a = ap.parse_args()
    global SEED
    if a.seed_file:
        SEED = sf.seed_ecef(a.seed_file)

    eph = sf.read_nav(a.nav)
    caps = {}
    for l in open(a.meas):
        j = json.loads(l)
        if j['ms'] == a.ms:
            caps[j['name']] = (j['t0'], {int(k): v for k, v in j['meas'].items() if v['z'] >= a.zmin})
    ref = None
    if a.ref:
        ref = [tuple(map(float, l.split())) for l in open(a.ref) if l.strip() and not l.startswith('#')][0]

    p1 = {}
    for name, (t0, m) in sorted(caps.items()):
        if len(m) >= 5:
            p1[name] = fix(m, eph, t0)
    tcs = [v['tc'] for v in p1.values() if v and v['rms'] < 50]
    if not tcs:
        print('no capture solved on its own; no clock offset')
        return
    T = float(np.median(tcs))
    lines = ['%d ms: pass 1 solved %d of %d captures, clock offset T = %+.3f s (from %d)' %
             (a.ms, sum(1 for v in p1.values() if v), len(caps), T, len(tcs))]
    p2 = {}
    for name, (t0, m) in sorted(caps.items()):
        p2[name] = fix(m, eph, t0, T, a.sigma) if len(m) >= 4 else None
    good = [v for v in p2.values() if v and v['rms'] < 50]
    med = (np.median([v['lat'] for v in good]), np.median([v['lon'] for v in good])) if good else None
    for name in sorted(caps):
        v1, v2 = p1.get(name), p2[name]
        def fmt(v):
            if not v:
                return 'no fix'
            d = dist((v['lat'], v['lon']), ref) if ref else (dist((v['lat'], v['lon']), med) if med else -1)
            return '%4.0f m %s (rms %5.1f, h %5.0f, tc %+.2f)' % (d, 'to ref' if ref else 'to median', v['rms'], v['h'], v['tc'])
        lines.append('  %s  %d PRN | alone: %-48s | with T: %s' % (name[9:15], len(caps[name][1]), fmt(v1), fmt(v2)))
    print('\n'.join(lines))
    if a.out:
        with open(a.out, 'a') as fh:
            fh.write('\n'.join(lines) + '\n\n')


if __name__ == '__main__':
    main()
