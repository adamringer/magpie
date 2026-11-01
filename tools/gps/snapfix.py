#!/usr/bin/env python3
"""Snapshot GPS position fix from one Clip .snap capture (coarse-time navigation).

  python3 -I snapfix.py CAPTURE.snap NAV.rnx[.gz] [--prn 26,9,3,16,27,31] [--utc 'YYYY-MM-DD HH:MM:SS'] [--out FILE]

Steps
 1. Measurements (drift_search.py model: format A, s = +1): per PRN a coarse eta search on the first 1024 ms, a
    fine eta search on the whole capture, Doppler = eta * L1 (parabolic peak), code phase at sample 0 from an
    8x upsampled, drift-compensated non-coherent correlation (parabolic peak). PRNs below --zmin are dropped.
 2. Doppler fix: position + receiver frequency offset from the Dopplers (start: centroid of the sub-satellite
    directions on the Earth's surface, soft constraint |x| ~ Earth radius). Good to some tens of km, which is
    enough to fix the whole milliseconds of the pseudoranges (ambiguity 300 km).
 3. Code-phase fix with coarse time: unknowns x, y, z, common bias, capture-time error. The whole milliseconds
    come from step 2 and are then refined by a small +-1 search. GPS broadcast ephemeris (RINEX 3), satellite
    clock, Earth rotation; no ionosphere/troposphere (tens of metres).
Capture time: from the file name (UTC, the Clip's clock, written just before gpsnap starts), GPS = UTC + 18 s.
Output: geodetic position, residuals, elevations, time error. Results contain the user's location: keep them in
git-ignored files only.
"""
import sys, os, gzip, argparse, itertools, datetime, json
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import drift_search as ds  # noqa: E402

C = 299792458.0
OMEGA_E = 7.2921151467e-5
MU = 3.986005e14
F_REL = -4.442807633e-10
L1 = ds.L1
LAM = C / L1
MS = C * 1e-3
GPS_UTC = 18.0
GPS_EPOCH = datetime.datetime(1980, 1, 6)


# ---------------------------------------------------------------- ephemeris
def read_nav(path):
    op = gzip.open if path.endswith('.gz') else open
    with op(path, 'rt') as fh:
        lines = fh.read().splitlines()
    i = next(k for k, l in enumerate(lines) if 'END OF HEADER' in l) + 1
    eph = {}
    while i < len(lines):
        l = lines[i]
        if not l.startswith('G'):
            i += 1
            continue
        prn = int(l[1:3])
        y, mo, d, h, mi, s = (int(l[4:8]), int(l[9:11]), int(l[12:14]), int(l[15:17]), int(l[18:20]), int(l[21:23]))
        f = lambda t: float(t.replace('D', 'E')) if t.strip() else 0.0
        v = [f(l[23:42]), f(l[42:61]), f(l[61:80])]
        for k in range(1, 8):
            ll = lines[i + k].ljust(80)
            v += [f(ll[4:23]), f(ll[23:42]), f(ll[42:61]), f(ll[61:80])]
        toc = (datetime.datetime(y, mo, d, h, mi, s) - GPS_EPOCH).total_seconds()
        e = dict(toc=toc, af0=v[0], af1=v[1], af2=v[2], crs=v[4], dn=v[5], m0=v[6], cuc=v[7], ecc=v[8],
                 cus=v[9], sqa=v[10], toe=v[11], cic=v[12], om0=v[13], cis=v[14], i0=v[15], crc=v[16],
                 w=v[17], omd=v[18], idot=v[19], week=v[21], health=v[24], tgd=v[25])
        e['toe_abs'] = e['week'] * 604800.0 + e['toe']
        eph.setdefault(prn, []).append(e)
        i += 8
    return eph


def pick(eph, prn, t):
    cands = [e for e in eph.get(prn, []) if e['health'] == 0]
    return min(cands, key=lambda e: abs(t - e['toe_abs'])) if cands else None


def sat_pos(e, t):
    """ECEF position (m) and clock offset (s) at GPS time t (absolute seconds since the GPS epoch)."""
    A = e['sqa'] ** 2
    tk = t - e['toe_abs']
    n = np.sqrt(MU / A ** 3) + e['dn']
    M = e['m0'] + n * tk
    E = M
    for _ in range(10):
        E = M + e['ecc'] * np.sin(E)
    nu = np.arctan2(np.sqrt(1 - e['ecc'] ** 2) * np.sin(E), np.cos(E) - e['ecc'])
    phi = nu + e['w']
    du = e['cus'] * np.sin(2 * phi) + e['cuc'] * np.cos(2 * phi)
    dr = e['crs'] * np.sin(2 * phi) + e['crc'] * np.cos(2 * phi)
    di = e['cis'] * np.sin(2 * phi) + e['cic'] * np.cos(2 * phi)
    u = phi + du
    r = A * (1 - e['ecc'] * np.cos(E)) + dr
    inc = e['i0'] + di + e['idot'] * tk
    x, y = r * np.cos(u), r * np.sin(u)
    om = e['om0'] + (e['omd'] - OMEGA_E) * tk - OMEGA_E * e['toe']
    pos = np.array([x * np.cos(om) - y * np.cos(inc) * np.sin(om), x * np.sin(om) + y * np.cos(inc) * np.cos(om),
                    y * np.sin(inc)])
    dtc = t - e['toc']
    clk = e['af0'] + e['af1'] * dtc + e['af2'] * dtc ** 2 + F_REL * e['ecc'] * e['sqa'] * np.sin(E) - e['tgd']
    return pos, clk


def geom(e, rx, t_rx):
    """range (m), sat clock (s), ECEF sat velocity (m/s) for a receiver at rx receiving at t_rx."""
    tau = 0.075
    for _ in range(3):
        p, clk = sat_pos(e, t_rx - tau)
        th = OMEGA_E * tau
        p = np.array([p[0] * np.cos(th) + p[1] * np.sin(th), -p[0] * np.sin(th) + p[1] * np.cos(th), p[2]])
        rho = np.linalg.norm(p - rx)
        tau = rho / C
    p1, _ = sat_pos(e, t_rx - tau + 0.5)
    p0, _ = sat_pos(e, t_rx - tau - 0.5)
    v = p1 - p0          # sat_pos is already Earth-fixed, so this is the ECEF velocity
    return rho, clk, v, p


def ecef2lla(x):
    a, f = 6378137.0, 1 / 298.257223563
    e2 = f * (2 - f)
    lon = np.arctan2(x[1], x[0])
    p = np.hypot(x[0], x[1])
    lat = np.arctan2(x[2], p * (1 - e2))
    for _ in range(6):
        N = a / np.sqrt(1 - e2 * np.sin(lat) ** 2)
        h = p / np.cos(lat) - N
        lat = np.arctan2(x[2], p * (1 - e2 * N / (N + h)))
    return np.degrees(lat), np.degrees(lon), h


def elev(rx, sp):
    lat, lon, _ = ecef2lla(rx)
    la, lo = np.radians(lat), np.radians(lon)
    up = np.array([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)])
    d = (sp - rx) / np.linalg.norm(sp - rx)
    return np.degrees(np.arcsin(d @ up))


# ---------------------------------------------------------------- measurements
def wiped(F0, F1, eta, nb):
    """block spectra with the carrier removed and the code slide undone (PRN-independent)"""
    tb = np.arange(nb) * 1e-3
    f = eta * L1
    h = int(np.floor(f / 500.0 + 0.5))
    k, half = divmod(h, 2)
    X = np.roll(F1[:nb] if half else F0[:nb], -k, axis=1)
    P = np.exp((-2j * np.pi / ds.N) * np.outer(eta * ds.FS * tb, ds.KF)).astype(np.complex64)
    return X * P


def power_at(F0, F1, prn, eta, nb):
    return wiped(F0, F1, eta, nb) * ds.code_conj_fft(prn)[None, :]


def peak_stats(Y):
    R = ds.IFFT(Y)
    S = (R.real ** 2 + R.imag ** 2).sum(axis=0)
    z, lag, _ = ds.robust_z(S)
    return S, z, lag


def measure(raw, prns, zmin, coarse=None):
    z = ds.unpack(raw, 'A')
    F0, F1 = ds.prepare(z)
    nb = F0.shape[0]
    out = {}
    nc = min(1024, nb)
    # coarse: first <= 1024 ms over +-16 ppm, all PRNs per eta (the carrier wipe is shared)
    have = coarse is not None
    coarse = coarse if have else {p: (-1.0, 0.0) for p in prns}
    for eta in ([] if have else ds.eta_grid(16, nc)):
        XP = wiped(F0, F1, eta, nc)
        for p in prns:
            _, zz, _ = peak_stats(XP * ds.code_conj_fft(p)[None, :])
            if zz > coarse[p][0]:
                coarse[p] = (float(zz), eta)
    cand = [p for p in prns if coarse[p][0] >= 6.0]
    print('coarse (%d ms): %s' % (nc, '  '.join('PRN%d z%.1f' % (p, coarse[p][0]) for p in
                                               sorted(prns, key=lambda q: -coarse[q][0])[:10])), flush=True)
    for prn in cand:
        best = coarse[prn]
        # fine: whole capture, +-0.4 ppm in 0.02 ppm steps
        etas = best[1] + np.arange(-20, 21) * 0.02e-6
        pk = []
        for eta in etas:
            S, zz, lag = peak_stats(power_at(F0, F1, prn, eta, nb))
            pk.append((S.max(), zz, lag))
        j = int(np.argmax([p[0] for p in pk]))
        zz = pk[j][1]
        if zz < min(zmin, 6.0):
            print('PRN %2d: z %.1f, not detected' % (prn, zz), flush=True)
            continue
        de = 0.0
        if 0 < j < len(etas) - 1:
            a, b, c = pk[j - 1][0], pk[j][0], pk[j + 1][0]
            den = a - 2 * b + c
            de = 0.5 * (a - c) / den if den else 0.0
        eta = etas[j] + de * 0.02e-6
        # code phase: 8x upsampled correlation, summed in chunks
        U = 8
        S8 = np.zeros(ds.N * U)
        for b0 in range(0, nb, 512):
            Y = power_at(F0[b0:b0 + 512], F1[b0:b0 + 512], prn, eta, min(512, nb - b0))
            # this chunk starts at b0 ms: its code slide is eta*FS*(b0 ms) more; undo it too
            Y = Y * np.exp((-2j * np.pi / ds.N) * ds.KF * eta * ds.FS * b0 * 1e-3).astype(np.complex64)[None, :]
            Z = np.zeros((Y.shape[0], ds.N * U), np.complex64)
            Z[:, :ds.N // 2] = Y[:, :ds.N // 2]
            Z[:, -ds.N // 2:] = Y[:, ds.N // 2:]
            R = ds.IFFT(Z)
            S8 += (R.real ** 2 + R.imag ** 2).sum(axis=0)
        m = int(np.argmax(S8))
        a, b, c = S8[(m - 1) % len(S8)], S8[m], S8[(m + 1) % len(S8)]
        den = a - 2 * b + c
        frac = 0.5 * (a - c) / den if den else 0.0
        lag = (m + frac) / U                     # samples, code epoch start after sample 0
        # fine Doppler from the carrier phase: the complex correlation at the peak, block by block, rotates at
        # the true carrier (mod 1 kHz); remove eta*L1, square away the 50 bit/s data, FFT over the blocks
        f0 = eta * L1
        v = np.empty(nb, np.complex64)
        li = int(round(lag)) % ds.N
        for b0 in range(0, nb, 512):
            Y = power_at(F0[b0:b0 + 512], F1[b0:b0 + 512], prn, eta, min(512, nb - b0))
            Y = Y * np.exp((-2j * np.pi / ds.N) * ds.KF * eta * ds.FS * b0 * 1e-3).astype(np.complex64)[None, :]
            v[b0:b0 + Y.shape[0]] = ds.IFFT(Y)[:, li]
        tb = np.arange(nb) * 1e-3
        w = (v * np.exp(-2j * np.pi * f0 * tb)) ** 2
        nfft = 1 << 18
        W = np.abs(np.fft.fft(w, nfft))
        fr = np.fft.fftfreq(nfft, 1e-3)
        k = int(np.argmax(W))
        a, b, c = W[k - 1], W[k], W[(k + 1) % nfft]
        den = a - 2 * b + c
        kk = k + (0.5 * (a - c) / den if den else 0.0)
        f2 = np.interp(kk, np.arange(nfft), np.fft.fftshift(fr)) if False else kk * (1000.0 / nfft)
        if f2 > 500:
            f2 -= 1000.0
        snr2 = b / np.median(W)
        dopp = f0 + f2 / 2.0
        out[prn] = dict(z=float(zz), eta=float(eta), dopp=float(dopp), tau=float(lag / ds.FS))
        print('PRN %2d: z %6.1f  Doppler %+9.2f Hz (code-slide estimate %+8.1f, carrier peak/median %.0f)  '
              'code phase %8.3f samples (%.6f ms)' % (prn, zz, dopp, f0, snr2, lag, lag / ds.FS * 1e3), flush=True)
    return out, coarse


# ---------------------------------------------------------------- solvers
def seed_ecef(path):
    lat, lon = [tuple(map(float, l.split())) for l in open(path) if l.strip() and not l.startswith('#')][0]
    la, lo = np.radians(lat), np.radians(lon)
    a, e2 = 6378137.0, 6.69437999014e-3
    n = a / np.sqrt(1 - e2 * np.sin(la) ** 2)
    return np.array([n * np.cos(la) * np.cos(lo), n * np.cos(la) * np.sin(lo), n * (1 - e2) * np.sin(la)])


def doppler_fix(meas, eph, t):
    prns = sorted(meas)
    e = {p: pick(eph, p, t) for p in prns}
    # start: Earth-surface point under the mean satellite direction
    dirs = [sat_pos(e[p], t)[0] for p in prns]
    x = np.mean([d / np.linalg.norm(d) for d in dirs], axis=0)
    x = 6371e3 * x / np.linalg.norm(x)
    fc = 0.0

    def resid(x, fc):
        r = []
        for p in prns:
            rho, clk, v, sp = geom(e[p], x, t)
            u = (sp - x) / rho
            rr = v @ u
            r.append(meas[p]['dopp'] - (-rr / LAM + fc))
        r.append((np.linalg.norm(x) - 6371e3) / 2000.0)    # soft: near the surface (+-2 km ~ 1 Hz)
        return np.array(r)

    for _ in range(30):
        r0 = resid(x, fc)
        J = np.zeros((len(r0), 4))
        for k in range(3):
            dx = np.zeros(3); dx[k] = 100.0
            J[:, k] = (resid(x + dx, fc) - r0) / 100.0
        J[:, 3] = (resid(x, fc + 1.0) - r0) / 1.0
        step = np.linalg.lstsq(J, -r0, rcond=None)[0]
        x = x + step[:3]
        fc = fc + step[3]
        if np.linalg.norm(step[:3]) < 1.0:
            break
    return x, fc, resid(x, fc)[:-1]


def code_fix(meas, eph, t0, x0, tc_prior=None, tc_sigma=None, grid=True):
    """tc_prior/tc_sigma (s): soft prior on the capture-time error, weighted against a 10 m pseudorange
    error; with a tight prior four satellites are enough."""
    prns = sorted(meas)
    e = {p: pick(eph, p, t0) for p in prns}

    def model(x, cb, tc, N):
        r = []
        for p in prns:
            t = t0 + tc + meas[p]['tau']
            rho, clk, v, sp = geom(e[p], x, t)
            pr_meas = C * meas[p]['tau'] + N[p] * MS
            el = np.radians(max(elev(x, sp), 3.0)) if np.linalg.norm(x) > 6.0e6 else np.pi / 2
            tropo = 2.4 / np.sin(el)                 # simple zenith delay / sin(elevation)
            r.append(pr_meas - (rho + tropo + cb - C * clk))
        # soft constraint: near the ground (a 100 m height error weighs like a 10 m pseudorange error);
        # five unknowns from six satellites otherwise allow false fits deep underground with a large time error
        r.append(ecef2lla(x)[2] / 10.0)
        if tc_prior is not None:
            r.append((tc - tc_prior) / tc_sigma * 10.0)
        return np.array(r)

    def solve(N, x, cb, tc):
        for _ in range(20):
            r0 = model(x, cb, tc, N)
            J = np.zeros((len(r0), 5))
            for k in range(3):
                dx = np.zeros(3); dx[k] = 10.0
                J[:, k] = (model(x + dx, cb, tc, N) - r0) / 10.0
            J[:, 3] = -1.0
            J[len(prns):, 3] = 0.0               # constraint rows do not depend on the bias
            J[:, 4] = (model(x, cb, tc + 0.01, N) - r0) / 0.01
            step = np.linalg.lstsq(J, -r0, rcond=None)[0]
            x, cb, tc = x + step[:3], cb + step[3], tc + step[4]
            if np.linalg.norm(step[:3]) < 0.1 and abs(step[4]) < 1e-4:
                break
        return x, cb, tc, model(x, cb, tc, N)

    ref = prns[0]
    others = [p for p in prns if p != ref]

    def ms_from(xs):
        # whole milliseconds predicted from a position guess, relative to a reference PRN
        pred = {}
        for p in prns:
            rho, clk, _, _ = geom(e[p], xs, t0)
            pred[p] = rho - C * clk - C * meas[p]['tau']
        return {p: int(round((pred[p] - pred[ref]) / MS)) + 70 for p in prns}, pred

    # the Doppler fix can be off by a few hundred km: try seeds on a +-300 km grid around it
    lat0, lon0 = np.radians(ecef2lla(x0)[:2])
    east = np.array([-np.sin(lon0), np.cos(lon0), 0.0])
    north = np.array([-np.sin(lat0) * np.cos(lon0), -np.sin(lat0) * np.sin(lon0), np.cos(lat0)])
    seeds = []
    for de, dn in (itertools.product(range(-300, 301, 100), repeat=2) if grid else [(0, 0)]):
        xs = x0 + 1e3 * (de * east + dn * north)
        seeds.append(6371e3 * xs / np.linalg.norm(xs))
    first = None
    for xs in seeds:
        N, pred = ms_from(xs)
        cb0 = np.mean([N[p] * MS - pred[p] for p in prns])
        try:
            x, cb, tc, r = solve(N, xs.copy(), cb0, tc_prior or 0.0)
        except np.linalg.LinAlgError:
            continue
        cost = np.sqrt(np.mean(r ** 2))
        ok = abs(tc) <= 30 and (tc_prior is None or abs(tc - tc_prior) <= 3 * tc_sigma)
        if ok and np.isfinite(cost) and (first is None or cost < first[0]):
            first = (cost, x)
    x0 = first[1] if first else x0
    N0, pred = ms_from(x0)
    cb0 = np.mean([N0[p] * MS - pred[p] for p in prns])
    try:
        x, cb, tc, r = solve(N0, x0.copy(), cb0, tc_prior or 0.0)
        rms = np.sqrt(np.mean(r[:len(prns)] ** 2))
        # a wrong whole millisecond shows as ~300 km; with a reasonable seed anything under a few km is the right set
        if rms < 5000 and abs(tc) <= 30 and (tc_prior is None or abs(tc - tc_prior) <= 3 * tc_sigma):
            return (rms, x, cb, tc, r[:len(prns)], N0), e          # good without the +-1 search
    except np.linalg.LinAlgError:
        pass
    if not grid:
        return None, e      # seeded (last known position): no +-1 ms search; a failure here means bad data
    best = None
    for d in itertools.product((-1, 0, 1), repeat=len(others)):
        N = dict(N0)
        for p, k in zip(others, d):
            N[p] += k
        cb0 = np.mean([C * meas[p]['tau'] + N[p] * MS - pred[p] - C * meas[p]['tau'] for p in prns])
        try:
            x, cb, tc, r = solve(N, x0.copy(), cb0, tc_prior or 0.0)
        except np.linalg.LinAlgError:
            continue
        cost = np.sqrt(np.mean(r ** 2))              # incl. the height / time-prior rows
        r = r[:len(prns)]                            # pseudorange residuals only
        rms = np.sqrt(np.mean(r ** 2))
        if abs(tc) > 30 or not np.isfinite(rms):     # the Clip's clock is good to seconds
            continue
        if tc_prior is not None and abs(tc - tc_prior) > 3 * tc_sigma:
            continue
        if best is None or cost < best[6]:
            best = (rms, x, cb, tc, r, N, cost)
    return (best[:6] if best else None), e


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('snap')
    ap.add_argument('nav')
    ap.add_argument('--prn', default='all', help="'all' (coarse search 1-32 first) or a list like 26,9,3")
    ap.add_argument('--zmin', type=float, default=15.0)
    ap.add_argument('--utc', default=None, help='capture start UTC (default: from the file name)')
    ap.add_argument('--out', default=None)
    ap.add_argument('--ms', type=int, default=None, help='use only the first N ms of the capture')
    ap.add_argument('--meas-out', default=None, help='append the measurements (JSON line) to this file')
    ap.add_argument('--meas-in', default=None, help='reuse measurements from this JSON-lines file')
    ap.add_argument('--tc-prior', type=float, default=None, help='prior capture-time error (s)')
    ap.add_argument('--tc-sigma', type=float, default=0.5)
    ap.add_argument('--lengths', default=None, help='measure only, for these lengths (ms, comma list), one coarse search')
    ap.add_argument('--seed-file', default=None, help='file with "lat lon" (last known position) to seed the fix')
    a = ap.parse_args()

    name = os.path.basename(a.snap)
    utc = datetime.datetime.strptime(a.utc, '%Y-%m-%d %H:%M:%S') if a.utc else \
        datetime.datetime.strptime(name[:15], '%Y%m%d-%H%M%S')
    tfile = a.snap + '.time'          # gpsnap sidecar: UTC of the first DATA READY edge (page 0 complete)
    if not a.utc and os.path.exists(tfile):
        for l in open(tfile):
            if l.startswith('first_page_utc='):
                v = l.split('=', 1)[1].strip().rstrip('Z')
                utc = datetime.datetime.strptime(v, '%Y-%m-%dT%H:%M:%S.%f') - datetime.timedelta(milliseconds=16)
    t0 = (utc - GPS_EPOCH).total_seconds() + GPS_UTC
    eph = read_nav(a.nav)
    raw = np.fromfile(a.snap, np.uint8)
    if a.ms:
        raw = raw[:a.ms * ds.N // 4]
    print('%s: %d ms, capture start %s UTC (GPS %.3f)' % (name, len(raw) * 4 // ds.N, utc, t0), flush=True)
    if a.lengths:
        prns = list(range(1, 33)) if a.prn == 'all' else [int(p) for p in a.prn.split(',')]
        full = np.fromfile(a.snap, np.uint8)
        coarse = None
        for ms in [int(v) for v in a.lengths.split(',')]:
            print('--- %d ms' % ms, flush=True)
            m, coarse = measure(full[:ms * ds.N // 4], prns, a.zmin, coarse)
            with open(a.meas_out, 'a') as fh:
                fh.write(json.dumps(dict(name=name, ms=ms, t0=t0, meas=m)) + '\n')
        return
    meas = None
    if a.meas_in and os.path.exists(a.meas_in):
        for l in open(a.meas_in):
            j = json.loads(l)
            if j['name'] == name and j['ms'] == len(raw) * 4 // ds.N:
                meas = {int(k): v for k, v in j['meas'].items()}
    if meas is None:
        prns = list(range(1, 33)) if a.prn == 'all' else [int(p) for p in a.prn.split(',')]
        meas, _ = measure(raw, prns, a.zmin)
        if a.meas_out:
            with open(a.meas_out, 'a') as fh:
                fh.write(json.dumps(dict(name=name, ms=len(raw) * 4 // ds.N, t0=t0, meas=meas)) + '\n')
    meas = {p: m for p, m in meas.items() if m['z'] >= a.zmin}
    need = 4 if a.tc_prior is not None else 5
    if len(meas) < need:
        print('only %d PRNs above z %.0f: need %d' % (len(meas), a.zmin, need))
        return
    xd, fc, rd = doppler_fix(meas, eph, t0)
    if a.seed_file:
        xd = seed_ecef(a.seed_file)
    lat, lon, h = ecef2lla(xd)
    print('Doppler fix: %.4f, %.4f, h %.0f m; receiver offset %+.1f Hz (%.2f ppm); residuals (Hz) %s' %
          (lat, lon, h, fc, -fc / L1 * 1e6, np.round(rd, 1)))
    best, e = code_fix(meas, eph, t0, xd, a.tc_prior, a.tc_sigma)
    if best is None:
        print('no code-phase solution with |time error| <= 30 s')
        return
    rms, x, cb, tc, r, N = best
    lat, lon, h = ecef2lla(x)
    lines = ['Code-phase fix: %.6f, %.6f, h %.0f m' % (lat, lon, h),
             '  rms residual %.1f m, capture-time error %+.3f s, %.1f km from the Doppler fix' %
             (rms, tc, np.linalg.norm(x - xd) / 1e3)]
    for p, rr in zip(sorted(meas), r):
        _, _, _, sp = geom(e[p], x, t0 + tc)
        lines.append('  PRN %2d: residual %+8.1f m, elevation %5.1f deg, z %.0f' % (p, rr, elev(x, sp), meas[p]['z']))
    print('\n'.join(lines))
    if a.out:
        with open(a.out, 'a') as fh:
            fh.write('%s\n%s\n\n' % (name, '\n'.join(lines)))


if __name__ == '__main__':
    main()
