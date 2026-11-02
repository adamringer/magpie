#!/usr/bin/env python3
"""Position-aided collective detection (no strong satellites needed).

  python3 -I posaided.py NAV SEED CAP.snap [CAP.snap ...] [--ms 512] [--fc -115] [--fwin 50] [--tcs -2:2:0.25]

For weak signals (indoors, behind glass, under cover) where no satellite is strong enough for snapfix.py or
aided.py (which needs >= 2 strong satellites per capture to calibrate the receiver clock). Tells whether any
signal is present; it does not compute a position. Output: one line per capture, collective peak vs the null
peaks (a detection needs best_z clearly above null_max), the best capture-time correction tc (s) and code bias b
(samples), and each visible PRN's own z at that alignment. Uses the system python3 (numpy, scipy). Results
reveal nothing about the location by themselves, but keep them under analysis/ with the seed.

Known: position (SEED), receiver frequency offset fc (stable across runs), capture time from the .time sidecar
(error up to ~2 s, searched over --tcs). Unknown: code bias b (receiver clock mod 1 ms).
Per satellite above --elmin: Doppler window prediction + fc +- fwin, 20 ms coherent bit sums (4 bit offsets),
robust z profile over all 1024 lags (max over the Doppler/offset grid). Collective: shift each profile by its
predicted code delay and sum over satellites -> peak over b (and tc). Null: the same with satellites below
-5 deg (same count), giving the noise distribution of the collective peak. Then for the best (tc, b) each
satellite's own z at its predicted lag +- 2 samples.
"""
import argparse, datetime, os, sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import drift_search as ds  # noqa: E402
import snapfix as sf  # noqa: E402
from aided import predict  # noqa: E402

N, FS, L1, BIT = ds.N, ds.FS, ds.L1, 20
GPS_EPOCH = datetime.datetime(1980, 1, 6)


def t0_of(snap):
    for l in open(snap + '.time'):
        k, _, v = l.strip().partition('=')
        if k == 'first_page_utc':
            utc = datetime.datetime.strptime(v.rstrip('Z'), '%Y-%m-%dT%H:%M:%S.%f') - datetime.timedelta(milliseconds=16)
            return (utc - GPS_EPOCH).total_seconds() + sf.GPS_UTC
    raise SystemExit('no first_page_utc in %s.time' % snap)


def zprofile(F0, F1, nb, prn, dopp, fwin, fstep=5.0):
    """max over Doppler grid and bit offsets of the robust z of the 20 ms-coherent power, per lag (N,)"""
    Y = sf.power_at(F0, F1, prn, dopp / L1, nb)
    R = ds.IFFT(Y).astype(np.complex64)
    tb = np.arange(nb) * 1e-3
    best = np.full(N, -np.inf, np.float32)
    for df in np.arange(-fwin, fwin + fstep / 2, fstep):
        D = R * np.exp(-2j * np.pi * (dopp + df) * tb).astype(np.complex64)[:, None]
        cs = np.vstack([np.zeros((1, N), np.complex64), np.cumsum(D, axis=0)])
        for o in (0, 5, 10, 15):
            st = np.arange(o, nb - BIT + 1, BIT)
            G = cs[st + BIT] - cs[st]
            P = (G.real ** 2 + G.imag ** 2).sum(axis=0)
            med = np.median(P)
            z = (P - med) / (np.median(np.abs(P - med)) * 1.4826)
            np.maximum(best, z, out=best)
    return best


def collective(prof, codes):
    """sum over satellites of prof[p] read at (b + code_p) for every integer b; returns (N,)"""
    tot = np.zeros(N)
    for p, c in codes.items():
        tot += np.roll(prof[p], -int(round(c * FS)))
    return tot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('nav'); ap.add_argument('seed'); ap.add_argument('snaps', nargs='+')
    ap.add_argument('--ms', type=int, default=512)
    ap.add_argument('--fc', type=float, default=-115.0)
    ap.add_argument('--fwin', type=float, default=50.0)
    ap.add_argument('--elmin', type=float, default=10.0)
    ap.add_argument('--tcs', default='-2:2:0.25')
    a = ap.parse_args()
    lo, hi, step = map(float, a.tcs.split(':'))
    tcs = np.arange(lo, hi + step / 2, step)
    eph = sf.read_nav(a.nav)
    x = sf.seed_ecef(a.seed)
    print('name nvis best_z null_max null_med tc b_samples | per-PRN z at best')
    for snap in a.snaps:
        t0 = t0_of(snap)
        raw = np.fromfile(snap, np.uint8)[:a.ms * N // 4]
        F0, F1 = ds.prepare(ds.unpack(raw, 'A'))
        nb = F0.shape[0]
        pred0 = predict(eph, x, t0, range(1, 33))
        vis = [p for p, v in pred0.items() if v[0] >= a.elmin]
        nul = sorted((p for p, v in pred0.items() if v[0] < -5.0), key=lambda p: pred0[p][0])[:len(vis)]
        # Doppler changes < 1 Hz per second: profiles once at t0; only the relative code delays depend on tc
        prof = {p: zprofile(F0, F1, nb, p, pred0[p][1] + a.fc, a.fwin) for p in vis + nul}
        best = None
        nullpk = []
        for tc in tcs:
            pr = predict(eph, x, t0 + tc, vis + nul)
            cv = collective(prof, {p: pr[p][2] for p in vis})
            cn = collective(prof, {p: pr[p][2] for p in nul})
            nullpk.append(cn.max())
            j = int(np.argmax(cv))
            if best is None or cv[j] > best[0]:
                best = (cv[j], tc, j, pr)
        # significance: collective peak vs the null collective peaks over the same tc grid
        sc, tc, b, pr = best
        per = []
        for p in sorted(vis, key=lambda p: -pred0[p][0]):
            lag = (b + int(round(pr[p][2] * FS))) % N
            w = (lag + np.arange(-2, 3)) % N
            per.append('%d:%.1f' % (p, prof[p][w].max()))
        print('%s %d %.1f %.1f %.1f %+.2f %d | %s' % (os.path.basename(snap)[9:15], len(vis), sc, max(nullpk),
              np.median(nullpk), tc, b, ' '.join(per)), flush=True)


if __name__ == '__main__':
    main()
