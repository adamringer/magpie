#!/usr/bin/env python3
"""cep_plot.py REF OUT.png TITLE BLIND.kml AIDED.kml
Error scatter for a run (process_run.sh writes the KMLs), the real-data version of doc_figures.py fig_fixes:
blind (grey) and aided (blue) fixes from joint.py KMLs as east/north metres from REF ("lat lon"), 25/50/100 m
rings, CEP50/CEP95 of the aided set (of the blind set if there is none). Missing KMLs are skipped.
The plot shows positions only as offsets from REF, but REF itself is a location: keep REF and the output under
analysis/.
"""
import math, os, re, sys
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

reff, out, title, kb, ka = sys.argv[1:6]
lat0, lon0 = map(float, next(l for l in open(reff) if l.strip() and not l.startswith('#')).split()[:2])
C_BLUE, C_GREY, C_RED = '#2a6fdb', '#888888', '#c0392b'
R = 6378137.0


def load(kml):
    if not os.path.exists(kml):
        return np.zeros((0, 2))
    m = re.search(r'<Folder><name>fixes.*?</Folder>', open(kml).read(), re.S)
    pts = re.findall(r'<coordinates>([-0-9.]+),([-0-9.]+)', m.group(0)) if m else []
    return np.array([(math.radians(float(lo) - lon0) * R * math.cos(math.radians(lat0)),
                      math.radians(float(la) - lat0) * R) for lo, la in pts]).reshape(-1, 2)


blind, aided = load(kb), load(ka)
plt.rcParams.update({'figure.dpi': 110, 'font.size': 10, 'axes.grid': True, 'grid.alpha': 0.3})
fig, ax = plt.subplots(figsize=(6, 5.6))
allr = np.hypot(*np.vstack([blind, aided]).T) if len(blind) + len(aided) else np.array([100.0])
lim = max(120, min(np.percentile(allr, 95) * 1.15, 2000))
for name, P, col in (('blind', blind, C_GREY), ('aided', aided, C_BLUE)):
    if len(P):
        r = np.hypot(*P.T)
        ax.scatter(*P.T, s=22, color=col, alpha=0.7,
                   label='%s: %d fixes, median %.0f m, CEP95 %.0f m' % (name, len(P), np.median(r),
                                                                     np.percentile(r, 95)))
        med = np.median(P, axis=0)
        ax.plot(*med, 'x', ms=9, mew=2, color=col)
best = aided if len(aided) else blind
if len(best):
    rb = np.hypot(*best.T)
    c50, c95 = np.percentile(rb, 50), np.percentile(rb, 95)
    bc = C_BLUE if len(aided) else C_GREY
    ax.add_patch(plt.Circle((0, 0), c50, fill=False, ls='-', lw=1.5, color=bc))
    ax.add_patch(plt.Circle((0, 0), c95, fill=False, ls='--', lw=1.5, color=bc))
    ax.text(-c50 * 0.72, c50 * 0.72, 'CEP50 %.0f m' % c50, fontsize=9, color=bc, ha='right')
    ax.text(-c95 * 0.72, c95 * 0.72, 'CEP95 %.0f m' % c95, fontsize=9, color=bc, ha='right')
ax.plot(0, 0, '*', ms=14, color=C_RED, label='reference')
for rr in (25, 50, 100, 250, 500, 1000):
    if rr < lim:
        ax.add_patch(plt.Circle((0, 0), rr, fill=False, ls=':', color=C_GREY))
        ax.text(rr * 0.72, rr * 0.72, '%d m' % rr, fontsize=8, color=C_GREY)
ax.set_aspect('equal'); ax.set_xlim(-lim, lim); ax.set_ylim(-lim, lim)
ax.set_xlabel('east (m)'); ax.set_ylabel('north (m)'); ax.set_title(title)
ax.legend(fontsize=8, loc='lower left')
fig.tight_layout(); fig.savefig(out); print('wrote', out)
for name, P in (('blind', blind), ('aided', aided)):
    if len(P):
        r = np.hypot(*P.T)
        print('%s: n %d  CEP50 %.0f  CEP95 %.0f  max %.0f  median offset E %.0f N %.0f m  off-plot %d' % (
            name, len(P), np.median(r), np.percentile(r, 95), r.max(), *np.median(P, axis=0), (r > lim).sum()))
