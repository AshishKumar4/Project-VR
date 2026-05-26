#!/usr/bin/env python3
"""inertial_drift.py -- characterize the controller's INERTIAL dead-reckon drift vs the
optical truth, from a real capture's fusion telemetry (no rerun).

Each fusion row carries the pre-fold prediction (pred_*, the inertial dead-reckon from the
last fold) and the optical pose being folded (opt_*), plus pos_residual_m / rot_residual_deg
= the dead-reckon error accumulated over the gap since the previous fold. Binning that error
by the gap duration gives the inertial drift curve directly on real sessions; the pred-vs-opt
trajectory extent catches fly-aways.

Usage: inertial_drift.py <telemetry_dir> [flip_deg=45]
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

from manifest import Manifest, DEVICE_NAMES, EVENT_TYPES


def L(d: Path, m: Manifest, name: str) -> np.ndarray:
    s = m.streams[name]
    p = d / s.file
    return np.fromfile(p, dtype=s.structured_dtype(), count=p.stat().st_size // s.row_size)


def quat_ang(q1, q2):
    dot = np.abs(np.sum(q1 * q2, axis=1))
    return np.degrees(2 * np.arccos(np.clip(dot, 0, 1)))


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    d = Path(sys.argv[1])
    flip_deg = float(sys.argv[2]) if len(sys.argv) > 2 else 45.0
    m = Manifest.load(d)
    fu = L(d, m, "fusion")
    try:
        ev = L(d, m, "event")
    except Exception:
        ev = None

    edges = [20, 50, 100, 200, 350, 500, 1000, 2000, 1e12]
    labels = ["0-20ms", "20-50", "50-100", "100-200", "200-350", "350-500", "0.5-1s", "1-2s", ">2s"]

    print(f"# {d}")
    if ev is not None and len(ev):
        et = ev["event_type"]
        hist = {EVENT_TYPES.get(int(k), int(k)): int((et == k).sum()) for k in np.unique(et)}
        print(f"# events: {hist}")
    for dev in (1, 2):
        f = fu[fu["device_id"] == dev]
        # accepted/reset folds carry a meaningful pre-fold residual
        f = f[np.isin(f["outcome"], (1, 2))]
        if len(f) < 5:
            continue
        f = f[np.argsort(f["t_mono_ns"])]
        t = f["t_mono_ns"].astype(np.int64)
        gap_ms = np.diff(t) * 1e-9 * 1e3
        gap_ms = np.concatenate([[np.nan], gap_ms])  # first row has no preceding gap

        pos = f["pos_residual_m"].astype(float)
        rot = f["rot_residual_deg"].astype(float)
        optp = np.stack([f["opt_px"], f["opt_py"], f["opt_pz"]], axis=1).astype(float)
        predp = np.stack([f["pred_px"], f["pred_py"], f["pred_pz"]], axis=1).astype(float)
        derr = np.linalg.norm(predp - optp, axis=1)  # full pred-vs-opt position error

        dur_s = (t[-1] - t[0]) * 1e-9
        print(f"\n==== dev {dev} ({DEVICE_NAMES[dev]}): {len(f)} folds over {dur_s:.0f}s, "
              f"{len(f)/max(dur_s,1):.0f} folds/s ====")
        print(f"  pred-vs-opt pos err: median={np.median(derr)*100:.1f}cm  p95={np.percentile(derr,95)*100:.1f}cm  "
              f"max={derr.max()*100:.0f}cm   ( >30cm: {(derr>0.30).sum()}  >1m: {(derr>1.0).sum()} )")
        print(f"  inertial dead-reckon error binned by gap since last fold:")
        print(f"    {'gap':>9} {'n':>5}   pos: med/p95/max (cm)        rot: med/p95 (deg)")
        lo = 0.0
        for e, lab in zip(edges, labels):
            mk = (gap_ms > lo) & (gap_ms <= e)
            n = int(np.nansum(mk))
            lo = e
            if n == 0:
                continue
            p = pos[mk]; r = rot[mk]
            print(f"    {lab:>9} {n:>5}   {np.median(p)*100:6.1f} {np.percentile(p,95)*100:6.1f} "
                  f"{p.max()*100:6.0f}        {np.median(r):6.1f} {np.percentile(r,95):6.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
