#!/usr/bin/env python3
"""flip_isolation.py -- isolate the optical front-end orientation MIRROR-FLIP mechanism
from a real capture, using only recorded telemetry (no production code, no filter rerun).

The offline ESKF study (tests/eskf_coast_experiment) and the raw consecutive-pose check
established that ~7-19% of optical poses disagree with the gyro-tracked prior by >45deg
(clustered near 180deg = mirror flips). This tool answers the NEXT question needed to fix
it the right way: WHERE in the visibility space do flips happen?

  - If flips concentrate at LOW matched-blob counts -> an OBSERVABILITY problem (the
    constellation is too under-determined to disambiguate the mirror; the fix must add
    information / refuse to accept under-determined poses).
  - If flips also occur at HIGH matched-blob counts with good reprojection -> a SELECTION
    problem (both twins are available + reproject well, and the scorer picks the wrong one
    because prior-orientation agreement is only a last-resort tiebreak in
    pose_metrics_score_is_better_pose). The fix is a prior-aware MAP selection.

Signal: the fusion stream's `rot_residual_deg` is the per-fold optical-vs-prior orientation
residual (pred = the pre-fold gyro-propagated prior). flip := rot_residual_deg > FLIP_DEG.
Context: join each accepted fold to the nearest accepted pose_attempt (same device) for its
blobs_matched / inliers / reproj_err_px / cam_id.

Usage: flip_isolation.py <telemetry_dir> [flip_deg=45]
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

from manifest import Manifest, DEVICE_NAMES


def load_stream(d: Path, m: Manifest, name: str) -> np.ndarray:
    s = m.streams[name]
    p = d / s.file
    nbytes = p.stat().st_size
    count = nbytes // s.row_size
    return np.fromfile(p, dtype=s.structured_dtype(), count=count)


def nearest_prev(times: np.ndarray, t: int) -> int:
    """Index into sorted `times` of the last entry <= t, or -1."""
    i = np.searchsorted(times, t, side="right") - 1
    return int(i)


def bin_report(label: str, keys, flips, bins) -> None:
    """Print flip-rate per bin of an integer/contiguous key."""
    print(f"  by {label}:")
    keys = np.asarray(keys)
    flips = np.asarray(flips)
    for lo, hi, name in bins:
        m = (keys >= lo) & (keys <= hi)
        n = int(m.sum())
        if n == 0:
            continue
        fr = 100.0 * flips[m].sum() / n
        print(f"    {name:>10}: n={n:5d}  flip={fr:5.1f}%")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    d = Path(sys.argv[1])
    flip_deg = float(sys.argv[2]) if len(sys.argv) > 2 else 45.0
    m = Manifest.load(d)
    fusion = load_stream(d, m, "fusion")
    pa = load_stream(d, m, "pose_attempt")

    # fusion has no own timestamp field named t_mono_ns? it does (every row begins with it).
    for dev in (1, 2):
        fm = fusion[(fusion["device_id"] == dev) & (fusion["outcome"] == 1)]
        if fm.shape[0] == 0:
            continue
        # accepted pose_attempts for this device, sorted by time (context source)
        pad = pa[(pa["device_id"] == dev) & np.isin(pa["outcome"], (1, 2))]
        pad = pad[np.argsort(pad["t_mono_ns"])]
        pat = pad["t_mono_ns"].astype(np.int64)

        rot = np.asarray(fm["rot_residual_deg"], dtype=float)
        flips = rot > flip_deg
        n = rot.shape[0]
        print(f"==== device {dev} ({DEVICE_NAMES[dev]}): {n} accepted folds ====")
        print(f"  rot residual vs prior: median={np.median(rot):.1f}deg  "
              f"p90={np.percentile(rot,90):.1f}  p99={np.percentile(rot,99):.1f}")
        print(f"  overall flip(>{flip_deg:.0f}deg)={100.0*flips.sum()/n:.1f}%  "
              f"near-180(>135deg)={100.0*(rot>135).sum()/n:.1f}%")

        # Join each fold to nearest-prev accepted pose_attempt (<= fold time, within 33ms).
        mb, inl, rep, cam, oc, joined = [], [], [], [], [], []
        for k in range(n):
            t = int(fm["t_mono_ns"][k])
            i = nearest_prev(pat, t)
            if i < 0 or (t - int(pat[i])) > 33_000_000:
                joined.append(False)
                continue
            joined.append(True)
            mb.append(int(pad["blobs_matched"][i]))
            inl.append(int(pad["inliers"][i]))
            rep.append(float(pad["reproj_err_px"][i]))
            cam.append(int(pad["cam_id"][i]))
            oc.append(int(pad["outcome"][i]))
        joined = np.asarray(joined)
        fj = flips[joined]
        nj = int(joined.sum())
        print(f"  joined to pose_attempt context: {nj}/{n}")
        if nj == 0:
            print()
            continue
        bin_report("blobs_matched", mb, fj,
                   [(0, 3, "<=3"), (4, 4, "4"), (5, 5, "5"), (6, 6, "6"),
                    (7, 8, "7-8"), (9, 99, ">=9")])
        bin_report("inliers", inl, fj,
                   [(0, 3, "<=3"), (4, 5, "4-5"), (6, 7, "6-7"), (8, 99, ">=8")])
        rep = np.asarray(rep)
        bin_report("reproj_err_px", (rep * 10).astype(int), fj,
                   [(0, 9, "<1.0px"), (10, 14, "1.0-1.5"), (15, 20, "1.5-2.0"),
                    (21, 999, ">2.0px")])
        bin_report("cam_id", cam, fj, [(c, c, f"cam{c}") for c in range(4)])
        bin_report("outcome(1=accept,2=recover)", oc, fj, [(1, 1, "accepted"), (2, 2, "recovered")])
        # blobs_matched distribution among accepted (how often few-blob regime)
        mb = np.asarray(mb)
        print("  accepted blobs_matched distribution:",
              {int(v): int((mb == v).sum()) for v in np.unique(mb)})
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
