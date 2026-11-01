#!/usr/bin/env python3
"""GPS acquisition on Clip .snap captures with the code drift tied to the carrier offset.

Model (F0 = 0, i.e. the LO is derived from the same TCXO that clocks the sampler and nominally lands L1 on 0 Hz):
a satellite with Doppler fd seen through a reference that is off by eps has
    carrier offset  f = s * eta * L1          (s = +-1: unknown I/Q assignment / spectral sense)
    code rate       (1023/1024) * (1 + eta)   chips per sample,   eta = fd/L1 - eps
so ONE parameter eta sets both the carrier and how fast the code phase slides through the capture
(eta * 1.024e6 samples/s). Earlier searches summed 1 ms / 10 ms blocks at a fixed code phase, which smears a
signal over |eta| * 1.024e6 * T samples (e.g. eta = 12 ppm: 6 samples in 512 ms, 50 in 4 s).

Method: 1 ms blocks (1024 samples at a nominal 1.024 MHz complex rate), FFT per block (plus a copy pre-shifted by
500 Hz, so the residual carrier error is <= 250 Hz), carrier removed by a bin roll, code correlation in the
frequency domain, the per-block code-phase slide undone by an exact fractional delay (phase ramp), then the
power summed over all blocks. Score per (format, s, PRN, eta): robust z of the best lag,
z = (peak - median) / (1.4826 * MAD) over the 1024 lags.

Formats (two survive the word-structure statistics in snap_format.md 4.2):
  A = 32-bit words byte-swapped, bits LSB first, file order ('lsb/swapped/fwd')
  B = whole file MSB first, time reversed ('msb/stored/rev')
I/Q pairing: bits 0,1 of each pair = I,Q. Swapping I and Q only mirrors the spectrum, which s covers.

usage:
  drift_search.py search FILE.snap [FILE...] [--ms N] [--eta-ppm 16] [--fmt AB] [--prn 1-32] [--out out.csv]
  drift_search.py selftest [--cn0 38] [--eta-ppm-true -7.3] [--prn 19] [--ms 1024]
Run with the system python3 (has scipy):  python3 -I drift_search.py ...
"""
import sys, os, argparse, time
import numpy as np
try:
    from scipy import fft as sfft
    FFT = lambda a: sfft.fft(a, axis=1, workers=-1)
    IFFT = lambda a: sfft.ifft(a, axis=1, workers=-1)
except ImportError:
    FFT = lambda a: np.fft.fft(a, axis=1)
    IFFT = lambda a: np.fft.ifft(a, axis=1)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gpsca  # noqa: E402  (ca(prn) -> +-1 C/A code, 1023 chips)

FS = 1.024e6          # nominal complex sample rate (page interval 15.999 ms = 16384 samples)
N = 1024              # samples per 1 ms block
L1 = 1575.42e6
KF = np.fft.fftfreq(N) * N            # signed bin index, for exact fractional delays
TIN = np.arange(N) / FS


def unpack(raw, fmt):
    if fmt == 'A':
        bits = np.unpackbits(raw.reshape(-1, 4)[:, ::-1].reshape(-1), bitorder='little')
    elif fmt == 'B':
        bits = np.unpackbits(raw, bitorder='big')[::-1]
    else:
        raise ValueError(fmt)
    t = bits.astype(np.float32) * 2 - 1
    return (t[0::2] + 1j * t[1::2]).astype(np.complex64)


_code = {}
def code_conj_fft(prn):
    if prn not in _code:
        c = gpsca.ca(prn)[np.floor(np.arange(N) * 1.023e6 / FS).astype(int) % 1023]
        _code[prn] = np.conj(np.fft.fft(c)).astype(np.complex64)
    return _code[prn]


def prepare(z, ms=None):
    nb = len(z) // N
    if ms:
        nb = min(nb, ms)
    Z = z[:nb * N].reshape(nb, N)
    Z = Z - Z.mean(axis=1, keepdims=True)
    Z = Z - Z.mean(axis=0, keepdims=True)   # remove 1 kHz-periodic interference (as coh.py 'deperiod')
    F0 = FFT(Z).astype(np.complex64)
    F1 = FFT(Z * np.exp(-2j * np.pi * 500.0 * TIN).astype(np.complex64)).astype(np.complex64)
    return F0, F1


def robust_z(S):
    med = np.median(S)
    mad = np.median(np.abs(S - med)) * 1.4826
    j = int(np.argmax(S))
    return (S[j] - med) / mad, j, S[j] / med


def search(F0, F1, prns, etas, signs):
    """yield (s, prn, eta, z, lag, peak/median) for the best lag of every (s, prn, eta)."""
    nb = F0.shape[0]
    tb = np.arange(nb) * 1e-3                              # block start times (s)
    for s in signs:
        for eta in etas:
            f = s * eta * L1
            h = int(np.floor(f / 500.0 + 0.5))              # nearest 500 Hz step
            k, half = divmod(h, 2)
            F = F1 if half else F0
            X = np.roll(F, -k, axis=1)
            delta = eta * FS * tb                           # code slide in samples at each block
            # the correlation peak of block b sits at lag -delta_b; delay it back by delta_b
            P = np.exp((-2j * np.pi / N) * np.outer(delta, KF)).astype(np.complex64)
            XP = X * P
            for prn in prns:
                R = IFFT(XP * code_conj_fft(prn)[None, :])
                S = (R.real ** 2 + R.imag ** 2).sum(axis=0)
                z, lag, pr = robust_z(S)
                yield s, prn, eta, z, lag, pr


def eta_grid(eta_ppm, ms):
    # step so the slide error over the whole span stays <= ~0.4 sample
    step = 0.4 / (FS * ms * 1e-3)
    n = int(np.ceil(eta_ppm * 1e-6 / step))
    return np.arange(-n, n + 1) * step


def parse_prns(s):
    out = []
    for part in s.split(','):
        a, _, b = part.partition('-')
        out += list(range(int(a), int(b or a) + 1))
    return out


def cmd_search(a):
    prns = parse_prns(a.prn)
    with open(a.out, 'a') as fh:
        for path in a.files:
            raw = np.fromfile(path, np.uint8)
            name = os.path.basename(path)
            for fmt in a.fmt:
                t0 = time.time()
                F0, F1 = prepare(unpack(raw, fmt), a.ms)
                ms = F0.shape[0]
                etas = eta_grid(a.eta_ppm, ms)
                best = {}
                for s, prn, eta, z, lag, pr in search(F0, F1, prns, etas, (1, -1)):
                    key = (s, prn)
                    if key not in best or z > best[key][1]:
                        best[key] = (eta, z, lag, pr)
                for (s, prn), (eta, z, lag, pr) in sorted(best.items(), key=lambda kv: -kv[1][1]):
                    fh.write('%s,%s,%d,%d,%d,%.3f,%.0f,%d,%.2f,%.4f\n' %
                             (name, fmt, ms, s, prn, eta * 1e6, s * eta * L1, lag, z, pr))
                fh.flush()
                top = sorted(best.items(), key=lambda kv: -kv[1][1])[:5]
                print('%s fmt %s %d ms, %d etas, %.0f s; top: %s' % (
                    name, fmt, ms, len(etas), time.time() - t0,
                    '  '.join('PRN%d s%+d %.1fppm z=%.1f' % (p, s, v[0] * 1e6, v[1]) for (s, p), v in top)),
                    flush=True)


def cmd_selftest(a):
    """Synthetic 1-bit I/Q at FS with one PRN at the given eta and C/N0; format A packing round trip."""
    rng = np.random.default_rng(1)
    n = a.ms * N
    eta = a.eta_ppm_true * 1e-6
    t = np.arange(n)
    chip = (t * (1023 / 1024) * (1 + eta) + 345.6) % 1023
    sig = gpsca.ca(a.prn)[chip.astype(int)] * np.exp(2j * np.pi * (eta * L1) * t / FS)
    snr = 10 ** (a.cn0 / 10) / FS                       # per complex sample, noise power 1
    x = np.sqrt(snr) * sig + (rng.standard_normal(n) + 1j * rng.standard_normal(n)) / np.sqrt(2)
    # 1-bit quantise, pack as format A (I,Q interleaved; LSB first; 32-bit words byte-swapped)
    bits = np.empty(2 * n, np.uint8)
    bits[0::2] = x.real > 0
    bits[1::2] = x.imag > 0
    raw = np.packbits(bits, bitorder='little').reshape(-1, 4)[:, ::-1].reshape(-1)
    for label, drift in (('with drift model', True), ('fixed code phase (old method)', False)):
        F0, F1 = prepare(unpack(raw, 'A'))
        etas = eta_grid(abs(a.eta_ppm_true) + 2, a.ms)
        res = []
        for s, prn, e, z, lag, pr in search(F0, F1, [a.prn], etas, (1,)):
            res.append((z, e, lag))
        if not drift:   # emulate the old search: carrier tied but no slide compensation
            res = []
            tb = np.arange(F0.shape[0]) * 1e-3
            for e in etas:
                h = int(np.floor(e * L1 / 500.0 + 0.5)); k, half = divmod(h, 2)
                X = np.roll(F1 if half else F0, -k, axis=1)
                R = IFFT(X * code_conj_fft(a.prn)[None, :])
                S = (R.real ** 2 + R.imag ** 2).sum(axis=0)
                z, lag, pr = robust_z(S); res.append((z, e, lag))
        z, e, lag = max(res)
        print('%-30s PRN %d C/N0 %.0f dB-Hz, %d ms, true eta %.2f ppm: best z %.1f at eta %.2f ppm lag %d' %
              (label, a.prn, a.cn0, a.ms, a.eta_ppm_true, z, e * 1e6, lag))
    # a wrong PRN on the same data as a noise control
    F0, F1 = prepare(unpack(raw, 'A'))
    zc = max(r[3] for r in search(F0, F1, [a.prn % 32 + 1], eta_grid(abs(a.eta_ppm_true) + 2, a.ms), (1,)))
    print('%-30s PRN %d (absent): best z %.1f' % ('control', a.prn % 32 + 1, zc))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('search')
    p.add_argument('files', nargs='+')
    p.add_argument('--ms', type=int, default=None, help='use only the first N ms')
    p.add_argument('--eta-ppm', type=float, default=16.0, help='search eta in +-this (ppm); 16 ppm = +-25 kHz')
    p.add_argument('--fmt', default='AB')
    p.add_argument('--prn', default='1-32')
    p.add_argument('--out', default='drift_search.csv')
    q = sub.add_parser('selftest')
    q.add_argument('--cn0', type=float, default=38.0)
    q.add_argument('--eta-ppm-true', type=float, default=-7.3)
    q.add_argument('--prn', type=int, default=19)
    q.add_argument('--ms', type=int, default=1024)
    a = ap.parse_args()
    (cmd_search if a.cmd == 'search' else cmd_selftest)(a)


if __name__ == '__main__':
    main()
