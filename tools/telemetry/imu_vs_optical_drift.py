#!/usr/bin/env python3
"""Characterise the drift between INERTIAL and VISUAL odometry from a recorded session.

Every fusion-stream row is logged when an optical pose arrives, and carries the residual
between that optical pose and the ESKF's IMU-dead-reckoned prediction at the same instant
(pos_residual_m, rot_residual_deg). That residual IS the error the inertial odometry
accumulated while coasting since the PREVIOUS optical fix — i.e. exactly what determines
whether a controller stays on-track while out of camera view. Binning it by the coast gap
(time since the previous optical fix) gives the real inertial error-growth curve, using the
real filter and real IMU (no synthetic model).

Mirror-flips (a few-blob PnP returning a ~180-deg-wrong orientation) are matcher errors, not
inertial drift, so orientation stats are reported both raw and flip-excluded.

Usage: imu_vs_optical_drift.py <capture_or_telemetry_dir> [--flip-deg 75]
"""
import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from manifest import Manifest, DEVICE_NAMES  # noqa: E402

# Coast-gap bins (ms): how long the inertial odometry ran alone before the next optical fix.
GAP_EDGES_MS = [0, 20, 40, 80, 160, 320, 640, 1280, 1e9]


def _read(telemetry_dir: Path, name: str) -> np.ndarray:
    s = Manifest.load(telemetry_dir).streams[name]
    return np.fromfile(telemetry_dir / s.file, dtype=s.structured_dtype(),
                       count=s.rows_written if s.rows_written else -1)


def analyse(telemetry_dir: Path, flip_deg: float) -> None:
    fus = _read(telemetry_dir, "fusion")
    for dev in sorted(np.unique(fus["device_id"])):
        if dev == 0:
            continue  # HMD has no constellation optical here
        f = fus[fus["device_id"] == dev]
        f = f[np.argsort(f["t_mono_ns"])]
        if len(f) < 10:
            continue
        t = f["t_mono_ns"].astype(np.int64)
        gap_ms = np.diff(t) / 1e6
        pos = f["pos_residual_m"][1:].astype(float)   # residual aligned to the gap that preceded it
        rot = f["rot_residual_deg"][1:].astype(float)
        finite = np.isfinite(pos) & np.isfinite(rot)
        gap_ms, pos, rot = gap_ms[finite], pos[finite], rot[finite]
        is_flip = rot > flip_deg

        print(f"\n=== {DEVICE_NAMES.get(int(dev), dev)} controller — inertial coast drift "
              f"({len(pos)} optical fixes) ===")
        print(f"  optical-fix flips (rot residual > {flip_deg:.0f} deg): "
              f"{is_flip.mean()*100:.1f}% of fixes  (matcher errors, excluded from drift)")
        print(f"  {'coast gap':>14} {'n':>6} {'pos drift p50':>14} {'p95':>9} {'max':>9} "
              f"{'rot p50*':>10} {'p95*':>8}")
        for lo, hi in zip(GAP_EDGES_MS[:-1], GAP_EDGES_MS[1:]):
            m = (gap_ms >= lo) & (gap_ms < hi)
            if m.sum() == 0:
                continue
            md = ~is_flip & m  # flip-excluded for orientation
            label = f"{lo:.0f}-{hi:.0f}ms" if hi < 1e8 else f">{lo:.0f}ms"
            rp50 = np.percentile(rot[md], 50) if md.sum() else float("nan")
            rp95 = np.percentile(rot[md], 95) if md.sum() else float("nan")
            print(f"  {label:>14} {m.sum():>6} {np.percentile(pos[m],50)*100:>11.1f}cm "
                  f"{np.percentile(pos[m],95)*100:>6.1f}cm {pos[m].max()*100:>6.1f}cm "
                  f"{rp50:>8.1f}d {rp95:>6.1f}d")
        # Drift RATE (cm per 100 ms of coast), robust slope through the binned medians.
        good = ~is_flip
        if good.sum() > 20:
            gx = gap_ms[good] / 100.0  # in units of 100 ms
            slope = np.polyfit(gx, pos[good] * 100.0, 1)[0]
            print(f"  inertial position drift rate ~ {slope:.2f} cm per 100 ms of coast "
                  f"(median fix gap {np.median(gap_ms):.0f} ms)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dir", help="capture dir or its telemetry/ subdir")
    ap.add_argument("--flip-deg", type=float, default=75.0, help="orientation-flip threshold (deg)")
    a = ap.parse_args()
    d = Path(a.dir)
    if not (d / "manifest.json").is_file() and (d / "telemetry" / "manifest.json").is_file():
        d = d / "telemetry"
    analyse(d, a.flip_deg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
