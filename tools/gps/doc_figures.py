#!/usr/bin/env python3
"""Figures for docs/GPS_PROCESSING.md.

    python3 -I tools/gps/doc_figures.py CAPTURE.snap OUTDIR

Uses the system python3 (numpy, scipy, matplotlib). Signal figures come from one real 512 ms capture (they show
only correlation power against code phase and frequency: nothing about where it was taken). Geometry figures are
schematic and the fix-scatter figure is synthetic (illustrative numbers around a made-up origin), so no real
position ever appears in the output.
"""
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import drift_search as ds  # noqa: E402

plt.rcParams.update({"figure.dpi": 110, "font.size": 10, "axes.grid": True, "grid.alpha": 0.3})
C_BLUE, C_ORANGE, C_GREEN, C_GREY, C_RED = "#2a6fdb", "#e8833a", "#2e9d5b", "#888888", "#c0392b"


def save(fig, out, name):
    fig.tight_layout()
    fig.savefig(os.path.join(out, name))
    plt.close(fig)
    print("wrote", name)


# --------------------------------------------------------------------------------------------- pipeline
def fig_pipeline(out):
    steps = [
        ("Clip", "512 ms, 1-bit I/Q\n+ .time stamp"),
        ("Unpack", "32-bit words to\ncomplex samples"),
        ("Blind search", "all 32 PRNs x Doppler\n1 ms blocks, FFT"),
        ("Measure", "fine Doppler + code\nphase per satellite"),
        ("Aided search", "predict weak sats,\n20 ms coherent sums"),
        ("Solve", "coarse-time navigation\nx, y, z, clock, time"),
        ("Fix", "lat / lon\n+ residuals"),
    ]
    fig, ax = plt.subplots(figsize=(12, 2.3))
    ax.axis("off")
    w, gap = 1.45, 0.25
    for i, (t, d) in enumerate(steps):
        x = i * (w + gap)
        ax.add_patch(plt.Rectangle((x, 0), w, 1, fc="#eef3fc", ec=C_BLUE, lw=1.5))
        ax.text(x + w / 2, 0.7, t, ha="center", va="center", weight="bold")
        ax.text(x + w / 2, 0.33, d, ha="center", va="center", fontsize=8)
        if i:
            ax.annotate("", xy=(x, 0.5), xytext=(x - gap, 0.5), arrowprops=dict(arrowstyle="->", lw=1.4))
    ax.text(3 * (w + gap) + w / 2, -0.25, "inputs: broadcast ephemeris (NOAA), Klobuchar ionosphere, last known position, "
            "capture time", ha="center", fontsize=8, color=C_GREY)
    ax.set_xlim(-0.1, len(steps) * (w + gap))
    ax.set_ylim(-0.4, 1.1)
    save(fig, out, "pipeline.png")


# --------------------------------------------------------------------------------------------- samples
def fig_samples(z, out):
    fig, axs = plt.subplots(1, 2, figsize=(11, 3.3))
    n = 48
    t = np.arange(n) / ds.FS * 1e6
    axs[0].step(t, z.real[:n] + 1.2, where="post", color=C_BLUE, label="I")
    axs[0].step(t, z.imag[:n] - 1.2, where="post", color=C_ORANGE, label="Q")
    axs[0].set_yticks([-2.2, -0.2, 0.2, 2.2], ["-1", "+1", "-1", "+1"])
    axs[0].set_xlabel("time (µs)")
    axs[0].set_title("Raw samples: 1 bit each for I and Q")
    axs[0].legend(loc="upper right", fontsize=8)
    Z = z[: 512 * ds.N].reshape(512, ds.N)
    p = (np.abs(np.fft.fft(Z, axis=1)) ** 2).mean(0)
    f = np.fft.fftshift(np.fft.fftfreq(ds.N, 1 / ds.FS)) / 1e3
    axs[1].plot(f, 10 * np.log10(np.fft.fftshift(p) / np.median(p)), color=C_GREY, lw=0.8)
    axs[1].set_xlabel("frequency offset from L1 (kHz)")
    axs[1].set_ylabel("power (dB, median = 0)")
    axs[1].set_title("Spectrum: noise only, the satellites are ~20 dB below it")
    save(fig, out, "samples.png")


# --------------------------------------------------------------------------------------------- acquisition
def best_prn(F0, F1, nb):
    """strongest PRN over a coarse eta grid (the figures need one clear example)"""
    best = (0, None, None)
    for eta in ds.eta_grid(8, nb):
        for s, prn, e, zz, lag, _ in ds.search(F0, F1, range(1, 33), [eta], [1]):
            if zz > best[0]:
                best = (zz, prn, e)
    return best


def corr_power(F0, F1, prn, eta, nb, slide=True):
    tb = np.arange(nb) * 1e-3
    f = eta * ds.L1
    h = int(np.floor(f / 500.0 + 0.5))
    k, half = divmod(h, 2)
    X = np.roll(F1 if half else F0, -k, axis=1)[:nb]
    if slide:
        P = np.exp((-2j * np.pi / ds.N) * np.outer(eta * ds.FS * tb, ds.KF)).astype(np.complex64)
        X = X * P
    R = ds.IFFT(X * ds.code_conj_fft(prn)[None, :])
    return R


def fig_acquisition(F0, F1, nb, prn, eta, out):
    R = corr_power(F0, F1, prn, eta, nb)
    S = (np.abs(R) ** 2).sum(0)
    med, mad = np.median(S), np.median(np.abs(S - np.median(S))) * 1.4826
    zc = (S - med) / mad
    etas = eta + np.linspace(-3e-6, 3e-6, 121)
    zs = []
    for e in etas:
        S2 = (np.abs(corr_power(F0, F1, prn, e, nb)) ** 2).sum(0)
        m2, d2 = np.median(S2), np.median(np.abs(S2 - np.median(S2))) * 1.4826
        zs.append((S2.max() - m2) / d2)
    fig, axs = plt.subplots(1, 2, figsize=(11, 3.4))
    axs[0].plot(np.arange(ds.N), zc, color=C_BLUE, lw=0.8)
    j = int(np.argmax(zc))
    axs[0].annotate("satellite: code phase %d samples" % j, xy=(j, zc[j]), xytext=(j + 150 if j < 700 else j - 600, zc[j] * 0.8),
                    arrowprops=dict(arrowstyle="->"), fontsize=9)
    axs[0].set_xlabel("code phase (samples, 1024 = 1 ms)")
    axs[0].set_ylabel("z (robust SNR)")
    axs[0].set_title("Correlation with one satellite's C/A code (512 ms)")
    axs[1].plot((etas - eta) * ds.L1, zs, color=C_ORANGE)
    axs[1].set_xlabel("frequency trial minus best (Hz)")
    axs[1].set_ylabel("best z")
    axs[1].set_title("Doppler search: the peak needs the right frequency")
    save(fig, out, "acquisition.png")


def fig_codeslide(F0, F1, nb, prn, eta, out):
    """where the correlation peak sits in each 128 ms group of blocks (4 s capture), with and without slide removal"""
    fig, axs = plt.subplots(1, 2, figsize=(11, 3.4), sharey=True)
    for ax, slide, title in ((axs[0], False, "fixed code phase: the peak drifts"),
                             (axs[1], True, "slide removed: the peak stays put")):
        R = corr_power(F0, F1, prn, eta, nb, slide=slide)
        G = (np.abs(R) ** 2)[: nb // 128 * 128].reshape(-1, 128, ds.N).sum(1)  # 128 ms groups
        lags = G.argmax(1)
        ref = np.median(lags)
        ax.imshow(G[:, int(ref) - 12:int(ref) + 13].T, aspect="auto", origin="lower", cmap="viridis",
                  extent=[0, G.shape[0] * 128, -12.5, 12.5])
        ax.set_title(title)
        ax.set_xlabel("time in capture (ms)")
        ax.grid(False)
    axs[0].set_ylabel("code phase around the peak (samples)")
    save(fig, out, "codeslide.png")


# --------------------------------------------------------------------------------------------- geometry
def fig_geometry(out):
    fig, axs = plt.subplots(1, 2, figsize=(11, 4.2))
    ax = axs[0]
    ax.set_aspect("equal")
    ax.axis("off")
    earth = plt.Circle((0, -6.4), 6.4, fc="#dfe9d8", ec=C_GREEN)
    ax.add_patch(earth)
    rx = np.array([0.0, 0.0])
    ax.plot(*rx, "o", color=C_RED)
    ax.text(0.15, -0.6, "receiver (x, y, z)", color=C_RED, fontsize=9)
    sats = [(-7, 9), (-1.5, 11), (5, 10), (9, 6.5)]
    for i, (sx, sy) in enumerate(sats):
        ax.plot(sx, sy, "s", color=C_BLUE)
        ax.plot([sx, 0], [sy, 0], color=C_BLUE, lw=0.8, ls="--")
        ax.text(sx, sy + 0.5, "SV%d" % (i + 1), ha="center", fontsize=9)
    ax.text(-1.2, 4.8, r"$\rho_i = \|\mathbf{s}_i(t-\tau_i)-\mathbf{x}\|$", fontsize=11)
    ax.set_xlim(-10, 11)
    ax.set_ylim(-3, 12.5)
    ax.set_title("Pseudoranges from several satellites")
    ax = axs[1]
    t = np.linspace(0, 3.2, 400)
    ax.plot(t, np.mod(t, 1.0), color=C_ORANGE)
    for k in range(4):
        ax.axvline(k, color=C_GREY, lw=0.6, ls=":")
    ax.set_xlabel("signal travel time (ms) ≈ 67-86 ms in reality")
    ax.set_ylabel("measured code phase (ms)")
    ax.set_title("The code repeats every 1 ms = 300 km:\nonly the fraction is measured")
    ax.text(1.5, 0.85, "whole ms N unknown\n(N x 299.79 km)", ha="center", fontsize=9)
    save(fig, out, "geometry.png")


def fig_coarse_time(out):
    """why the capture time matters: range rate x time error"""
    fig, ax = plt.subplots(figsize=(6.5, 3.4))
    dt = np.linspace(0, 2, 100)
    for v, lab in ((800, "fast satellite (800 m/s)"), (300, "300 m/s"), (50, "near zenith (50 m/s)")):
        ax.plot(dt, v * dt, label=lab)
    ax.set_xlabel("capture time error (s)")
    ax.set_ylabel("range error (m)")
    ax.set_title("A wrong capture time moves every pseudorange differently")
    ax.legend(fontsize=8)
    save(fig, out, "coarse_time.png")


# --------------------------------------------------------------------------------------------- aided
def fig_aided(F0, F1, nb, prn, eta, out):
    """same satellite, simulated weak: blind non-coherent vs aided 20 ms coherent statistic"""
    R = corr_power(F0, F1, prn, eta, nb)
    rng = np.random.default_rng(1)
    # weaken the satellite by adding noise at the correlation level (keeps the real code phase/Doppler)
    scale = np.std(R) * 2.2
    Rw = R + scale * (rng.standard_normal(R.shape) + 1j * rng.standard_normal(R.shape)).astype(np.complex64)
    S = (np.abs(Rw) ** 2).sum(0)
    tb = np.arange(nb) * 1e-3
    j0 = int(np.argmax((np.abs(R) ** 2).sum(0)))
    # the aided search knows the frequency to a few Hz (prediction); here: find it on the clean signal at the peak
    v = R[:, j0]
    fr = eta * ds.L1 + np.arange(-600, 600, 2.0)
    fbest = max(fr, key=lambda f: max(np.abs((v * np.exp(-2j * np.pi * f * tb))[o:o + (nb - o) // 20 * 20]
                                             .reshape(-1, 20).sum(1)).__pow__(2).sum() for o in range(20)))
    D = Rw * np.exp(-2j * np.pi * fbest * tb)[:, None]
    best = None
    for o in range(20):
        st = np.arange(o, nb - 19, 20)
        G = np.array([D[s:s + 20].sum(0) for s in st])
        P = (np.abs(G) ** 2).sum(0)
        if best is None or P.max() > best.max():
            best = P

    def z(P):
        m = np.median(P)
        return (P - m) / (np.median(np.abs(P - m)) * 1.4826)
    zb, za = z(S), z(best)
    j = int(np.argmax(np.abs(R).sum(0)))
    fig, axs = plt.subplots(1, 2, figsize=(11, 3.4), sharey=True)
    axs[0].plot(zb, color=C_GREY, lw=0.7)
    axs[0].axvspan(j - 4, j + 4, color=C_GREEN, alpha=0.3)
    axs[0].set_title("Blind: 1 ms power sums, search all 1024 phases")
    axs[1].plot(za, color=C_BLUE, lw=0.7)
    axs[1].axvspan(j - 4, j + 4, color=C_GREEN, alpha=0.3, label="predicted window (±4)")
    axs[1].set_title("Aided: 20 ms coherent sums, look only in the window")
    axs[1].legend(fontsize=8, loc="upper right")
    for ax in axs:
        ax.set_xlabel("code phase (samples)")
    axs[0].set_ylabel("z")
    save(fig, out, "aided.png")


# --------------------------------------------------------------------------------------------- results (synthetic)
def fig_fixes(out):
    rng = np.random.default_rng(7)
    blind = rng.standard_normal((20, 2)) * np.array([26, 22]) + np.array([6, -4])
    blind[[3, 11]] *= 2.6
    aided = rng.standard_normal((20, 2)) * np.array([20, 17]) + np.array([4, -2])
    fig, ax = plt.subplots(figsize=(5.4, 5))
    ax.scatter(*blind.T, s=22, color=C_GREY, label="blind (16 of 20 solved)")
    ax.scatter(*aided.T, s=22, color=C_BLUE, label="aided (20 of 20 solved)")
    ax.plot(0, 0, "*", ms=14, color=C_RED, label="reference")
    for r in (25, 50, 100):
        ax.add_patch(plt.Circle((0, 0), r, fill=False, ls=":", color=C_GREY))
        ax.text(r * 0.72, r * 0.72, "%d m" % r, fontsize=8, color=C_GREY)
    ax.set_aspect("equal")
    ax.set_xlim(-120, 120)
    ax.set_ylim(-120, 120)
    ax.set_xlabel("east (m)")
    ax.set_ylabel("north (m)")
    ax.set_title("512 ms fixes (illustrative, synthetic)")
    ax.legend(fontsize=8, loc="lower left")
    save(fig, out, "fixes.png")


def fig_klobuchar(out):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import nav
    iono = ([1.6764e-08, -7.4506e-09, -1.1921e-07, 1.1921e-07], [118780.0, -65536.0, -327680.0, 327680.0])
    fig, axs = plt.subplots(1, 2, figsize=(11, 3.4))
    lt = np.linspace(0, 24, 200)
    for el in (15, 45, 90):
        d = [nav.klobuchar(iono, 30.0, 0.0, el, 180.0, (h * 3600.0) % 86400) * 299792458 for h in lt]
        axs[0].plot(lt, d, label="elevation %d°" % el)
    axs[0].set_xlabel("local time at the pierce point (h)")
    axs[0].set_ylabel("L1 delay (m)")
    axs[0].set_title("Klobuchar model: day bump, night floor")
    axs[0].legend(fontsize=8)
    el = np.linspace(5, 90, 100)
    axs[1].plot(el, [2.4 / np.sin(np.radians(e)) for e in el], label="troposphere 2.4 m / sin(el)", color=C_GREEN)
    axs[1].plot(el, [nav.klobuchar(iono, 30.0, 0.0, e, 180.0, 50400) * 299792458 for e in el],
                label="ionosphere at 14:00 local", color=C_ORANGE)
    axs[1].set_xlabel("elevation (°)")
    axs[1].set_ylabel("delay (m)")
    axs[1].set_title("Both grow toward the horizon")
    axs[1].legend(fontsize=8)
    save(fig, out, "atmosphere.png")


def main():
    cap, out = sys.argv[1], sys.argv[2]
    os.makedirs(out, exist_ok=True)
    raw = np.fromfile(cap, np.uint8)[: 512 * ds.N // 4]
    z = ds.unpack(raw, "A")
    F0, F1 = ds.prepare(z)
    nb = F0.shape[0]
    zz, prn, eta = best_prn(F0, F1, nb)
    print("example satellite: z %.0f" % zz)
    fig_pipeline(out)
    fig_samples(z, out)
    fig_acquisition(F0, F1, nb, prn, eta, out)
    full = ds.unpack(np.fromfile(cap, np.uint8), "A")
    G0, G1 = ds.prepare(full)
    fig_codeslide(G0, G1, G0.shape[0], prn, eta, out)
    del G0, G1
    fig_geometry(out)
    fig_coarse_time(out)
    fig_aided(F0, F1, nb, prn, eta, out)
    fig_fixes(out)
    fig_klobuchar(out)


if __name__ == "__main__":
    main()
