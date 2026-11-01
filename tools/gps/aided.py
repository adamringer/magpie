#!/usr/bin/env python3
"""Aided acquisition: find weak satellites by predicting where they must be, then searching only there.

  python3 -I aided.py MEAS.jsonl NAV --dir CAPTURE_DIR --seed-file SEED --ms N [--tc T] [--meas-out OUT.jsonl]
                      [--zstrong 12] [--zdet Z] [--elmin 5] [--loo] [--null]

Inputs: the blind measurements of snapfix.py --lengths (MEAS.jsonl, one line per capture and length), the captures
themselves, broadcast ephemeris, and a "lat lon" seed (last known position; within a few hundred metres).

1. Calibration from the strong satellites (blind z >= --zstrong, at least 2 that agree): predicted Doppler is
   -range_rate/lambda and predicted code phase is (range - c*sat_clock + troposphere + ionosphere)/c mod 1 ms, both
   at the seed position and capture time t0 + T. The receiver's frequency offset fc (Doppler residual, Hz) and code
   bias b (code-phase residual mod 1 ms) are common to all satellites: their medians over the strong ones.
2. Search for every other satellite above --elmin: Doppler = prediction + fc +- 25 Hz in 5 Hz steps, code phase =
   prediction + b +- 4 samples. Per-block correlations (drift_search 1 ms blocks, code slide undone) are derotated
   at the trial frequency and summed COHERENTLY over 20 ms (one navigation data bit; the 20 bit-edge offsets are
   tried), then the bit powers are summed. Detection statistic: robust z of the best cell in the window against
   the median/MAD of all 1024 lags. The window has ~2000 cells instead of ~90 000 for a blind search, so a much
   lower threshold keeps the same false-alarm rate.
3. --null runs the identical search on satellites below -5 deg elevation (no signal possible): the largest z seen
   there sets the threshold. --loo predicts each strong satellite from the others (code-phase error in samples,
   Doppler error in Hz, and whether the aided search finds it).
4. --meas-out: per capture the strong measurements plus aided detections (z >= --zdet), with 'src': 'aided' and
   'zb' (blind z) / 'za' (aided z). Run joint.py on it with --zmin 0 (this file is already thresholded).
Results contain the user's location only through the solver outputs; keep everything under analysis/.
"""
import argparse, json, os, sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import drift_search as ds  # noqa: E402
import snapfix as sf  # noqa: E402

N, FS, L1 = ds.N, ds.FS, ds.L1
P_CODE = 1e-3                     # code period (s) = N samples
BIT = 20                          # 1 ms blocks per navigation data bit


def predict(eph, x, t, prns):
    """{prn: (elevation deg, Doppler Hz without receiver offset, code delay s without bias)}"""
    out = {}
    for p in prns:
        e = sf.pick(eph, p, t)
        if e is None:
            continue
        rho, clk, v, sp = sf.geom(e, x, t)
        el, _ = sf.azel(x, sp)
        u = (sp - x) / rho
        dopp = -(v @ u) / sf.LAM
        code = (rho - sf.C * clk + sf.atmo(eph, x, sp, t)) / sf.C
        out[p] = (float(el), float(dopp), float(code))
    return out


FC_AMB = 500.0                    # ambiguity of the blind Doppler (Hz)


def wrap(d, period):
    return (d + period / 2) % period - period / 2


def cmean(v, period):
    """circular mean of values modulo period, in (-period/2, period/2]"""
    return float(np.angle(np.mean(np.exp(2j * np.pi * np.asarray(v) / period))) / (2 * np.pi) * period)


def calibrate(strong, pred):
    """common receiver frequency offset fc (Hz) and code bias b (s) from measured strong satellites, with outliers
    (|Doppler residual| > 30 Hz or |code residual| > 3 samples from the median) dropped. Returns fc, b, kept."""
    keep = [p for p in strong if p in pred]
    for _ in range(3):
        if not keep:
            return None, None, []
        # the blind carrier-refined Doppler is ambiguous by 500 Hz (the data bits are squared away, and the
        # code-slide estimate that should pick the alias can be off by > 250 Hz on short captures), so fc is a
        # circular mean modulo 500 Hz here; main() resolves the 500 Hz multiple with a coherent search
        df = {p: strong[p]['dopp'] - pred[p][1] for p in keep}
        fc = cmean(list(df.values()), FC_AMB)
        b = cmean([strong[p]['tau'] - pred[p][2] for p in keep], P_CODE) % P_CODE
        new = [p for p in keep if abs(wrap(df[p] - fc, FC_AMB)) <= 30.0 and
               abs(wrap(strong[p]['tau'] - pred[p][2] - b, P_CODE)) * FS <= 3.0]
        if new == keep:
            break
        keep = new
    return fc, b, keep


def search(F0, F1, nb, prn, dopp, tau, fwin=25.0, fstep=5.0, lagwin=4):
    """aided search around (dopp Hz, tau s). Returns dict(z, dopp, tau, df, off, lag_err) for the best cell."""
    eta = dopp / L1
    Y = sf.power_at(F0, F1, prn, eta, nb)                 # carrier (to 500 Hz) and code slide removed
    R = ds.IFFT(Y)                                        # per-block complex correlation, integer lags
    tb = np.arange(nb) * 1e-3
    lag0 = tau * FS
    win = (int(round(lag0)) + np.arange(-lagwin, lagwin + 1)) % N
    best = None
    dfs = np.arange(-fwin, fwin + fstep / 2, fstep)
    grid = np.zeros((len(dfs), BIT))
    for i, df in enumerate(dfs):
        D = R * np.exp(-2j * np.pi * (dopp + df) * tb).astype(np.complex64)[:, None]
        cs = np.vstack([np.zeros((1, N), np.complex64), np.cumsum(D, axis=0)])
        for o in range(BIT):
            st = np.arange(o, nb - BIT + 1, BIT)
            if len(st) == 0:
                continue
            G = cs[st + BIT] - cs[st]
            P = (G.real ** 2 + G.imag ** 2).sum(axis=0)
            med = np.median(P)
            mad = np.median(np.abs(P - med)) * 1.4826
            j = win[int(np.argmax(P[win]))]
            z = float((P[j] - med) / mad)
            grid[i, o] = P[j]
            if best is None or z > best['z']:
                best = dict(z=z, i=i, df=float(df), off=o, lag=int(j))
    # fractional code phase: coherent bit sums on a 1/16-sample grid around the best integer lag
    taus = best['lag'] + np.arange(-24, 25) / 16.0
    E = (np.exp(2j * np.pi * np.outer(ds.KF, taus) / N) / N).astype(np.complex64)
    Cf = (Y @ E) * np.exp(-2j * np.pi * (dopp + best['df']) * tb).astype(np.complex64)[:, None]
    st = np.arange(best['off'], nb - BIT + 1, BIT)
    cs = np.vstack([np.zeros((1, len(taus)), np.complex64), np.cumsum(Cf, axis=0)])
    G = cs[st + BIT] - cs[st]
    S = (G.real ** 2 + G.imag ** 2).sum(axis=0)
    m = int(np.argmax(S))
    frac = 0.0
    if 0 < m < len(S) - 1:
        a, b, c = S[m - 1], S[m], S[m + 1]
        den = a - 2 * b + c
        frac = 0.5 * (a - c) / den if den else 0.0
    lag = (taus[m] + frac / 16.0) % N
    # Doppler: parabola over the frequency grid at the best bit offset
    col = grid[:, best['off']]
    i = best['i']
    dfr = best['df']
    if 0 < i < len(col) - 1:
        a, b, c = col[i - 1], col[i], col[i + 1]
        den = a - 2 * b + c
        dfr += (0.5 * (a - c) / den if den else 0.0) * fstep
    return dict(z=best['z'], dopp=float(dopp + dfr), tau=float(lag / FS), df=float(dfr), off=best['off'],
                lag_err=float(wrap(lag - lag0, N)))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('meas')
    ap.add_argument('nav')
    ap.add_argument('--dir', required=True, help='directory holding the .snap captures named in MEAS')
    ap.add_argument('--seed-file', required=True)
    ap.add_argument('--ms', type=int, required=True)
    ap.add_argument('--tc', type=float, default=0.0, help='capture-time correction T (s), e.g. joint.py clock offset')
    ap.add_argument('--zstrong', type=float, default=12.0)
    ap.add_argument('--zdet', type=float, default=None, help='aided detection threshold (default: from --null, '
                    'else 8)')
    ap.add_argument('--elmin', type=float, default=5.0)
    ap.add_argument('--loo', action='store_true')
    ap.add_argument('--null', action='store_true')
    ap.add_argument('--meas-out', default=None)
    a = ap.parse_args()

    eph = sf.read_nav(a.nav)
    x = sf.seed_ecef(a.seed_file)
    caps = []
    for l in open(a.meas):
        j = json.loads(l)
        if j['ms'] == a.ms:
            caps.append(j)
    caps.sort(key=lambda j: j['name'])
    results, nullz = [], []
    for j in caps:
        t = j['t0'] + a.tc
        blind = {int(p): m for p, m in j['meas'].items()}
        strong = {p: m for p, m in blind.items() if m['z'] >= a.zstrong}
        pred = predict(eph, x, t, range(1, 33))
        fc, b, kept = calibrate(strong, pred)
        name = j['name']
        if len(kept) < 2:
            print('%s: only %d consistent strong satellites, skipped' % (name, len(kept)), flush=True)
            continue
        raw = np.fromfile(os.path.join(a.dir, name), np.uint8)[:a.ms * N // 4]
        F0, F1 = ds.prepare(ds.unpack(raw, 'A'))
        nb = F0.shape[0]
        # resolve the 500 Hz ambiguity of fc: coherent search on the strongest satellite at each candidate
        ps = max(kept, key=lambda q: strong[q]['z'])
        el, dp, cd = pred[ps]
        fc = max((fc + k * FC_AMB for k in (-2, -1, 0, 1, 2)),
                 key=lambda f: search(F0, F1, nb, ps, dp + f, (cd + b) % P_CODE, lagwin=1)['z'])
        line = ['%s: fc %+.1f Hz, strong %s' % (name, fc, ','.join(map(str, sorted(kept))))]
        res = dict(name=name, kept=kept, aided={}, loo={}, null={})
        for p, (el, dp, cd) in sorted(pred.items()):
            if p in kept or el < a.elmin:
                continue
            r = search(F0, F1, nb, p, dp + fc, (cd + b) % P_CODE)
            res['aided'][p] = dict(r, el=el, zb=blind.get(p, {}).get('z'))
        if a.loo and len(kept) >= 3:
            for p in kept:
                others = {q: strong[q] for q in kept if q != p}
                fc2, b2, k2 = calibrate(others, pred)
                fc2 = fc + wrap(fc2 - fc, FC_AMB)       # same 500 Hz multiple as the capture's resolved fc
                el, dp, cd = pred[p]
                lag_pred = ((cd + b2) % P_CODE) * FS
                r = search(F0, F1, nb, p, dp + fc2, (cd + b2) % P_CODE)
                res['loo'][p] = dict(z=r['z'], zb=strong[p]['z'],
                                     code_err=float(wrap(strong[p]['tau'] * FS - lag_pred, N)),
                                     dopp_err=float(wrap(strong[p]['dopp'] - (dp + fc2), FC_AMB)),
                                     found=abs(wrap(r['tau'] * FS - strong[p]['tau'] * FS, N)) < 1.0)
        if a.null:
            for p, (el, dp, cd) in sorted(pred.items()):
                if el < -5.0:
                    r = search(F0, F1, nb, p, dp + fc, (cd + b) % P_CODE)
                    res['null'][p] = r['z']
                    nullz.append(r['z'])
        results.append(res)
        line.append('  aided: ' + '  '.join('PRN%d el%.0f z%.1f%s' % (p, r['el'], r['z'], '' if r['zb'] is None else
                                                                        '(b%.0f)' % r['zb'])
                                           for p, r in sorted(res['aided'].items(), key=lambda kv: -kv[1]['z'])))
        if res['loo']:
            line.append('  loo:   ' + '  '.join('PRN%d z%.0f(b%.0f) code%+.2f smp dopp%+.1f Hz%s' % (
                p, r['z'], r['zb'], r['code_err'], r['dopp_err'], '' if r['found'] else ' MISSED')
                for p, r in sorted(res['loo'].items())))
        if res['null']:
            line.append('  null max z %.1f over %d below-horizon PRNs' % (max(res['null'].values()), len(res['null'])))
        print('\n'.join(line), flush=True)

    if nullz:
        nz = np.array(nullz)
        print('\nnull: %d searches, z median %.1f, 99%% %.1f, max %.1f' % (len(nz), np.median(nz),
              np.percentile(nz, 99), nz.max()))
    zdet = a.zdet if a.zdet is not None else (float(max(nullz)) + 1.0 if nullz else 8.0)
    det = [(r['name'], p, v) for r in results for p, v in r['aided'].items() if v['z'] >= zdet]
    print('aided detections at z >= %.1f: %d (%d new, not in the blind z>=8 set)' % (
        zdet, len(det), sum(1 for _, _, v in det if v['zb'] is None or v['zb'] < 8)))
    if a.meas_out:
        with open(a.meas_out, 'a') as fh:
            for j, r in zip([c for c in caps if c['name'] in {x['name'] for x in results}], results):
                m = {str(p): dict(j['meas'][str(p)], zb=j['meas'][str(p)]['z']) for p in r['kept']}
                for p, v in r['aided'].items():
                    if v['z'] >= zdet:
                        m[str(p)] = dict(z=v['z'], za=v['z'], zb=v['zb'], eta=v['dopp'] / L1, dopp=v['dopp'],
                                         tau=v['tau'], src='aided')
                fh.write(json.dumps(dict(name=j['name'], ms=a.ms, t0=j['t0'], meas=m)) + '\n')


if __name__ == '__main__':
    main()
