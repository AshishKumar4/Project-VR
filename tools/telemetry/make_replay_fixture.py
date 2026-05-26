#!/usr/bin/env python3
"""Extract a slice of a REAL recorded controller session from G2 telemetry into a
compact binary .replay fixture for the kalman-fusion real-data replay tests.

A fixture is the faithful fusion INPUT stream for one controller: its raw IMU
(imu.bin, device-filtered) plus the world-frame optical poses that were fed to
process_pose (fusion.bin opt_*), timestamps rebased to 0 on the shared monotonic
clock so the real optical-vs-IMU lag is preserved. Decoupled by construction:
real recorded inputs, never synthesised from the filter's own model.

Format (little-endian): magic "G2RP", u32 version=1, u32 n_imu, u32 n_pose,
then n_imu * {i64 t_ns; f32 ax,ay,az,gx,gy,gz} then n_pose * {i64 t_ns; f32
px,py,pz,qx,qy,qz,qw}. The C++ loader reads field-groups (no struct-padding
assumption). See tests/replay_fixture.hpp.
"""
import argparse
import struct
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from manifest import Manifest  # noqa: E402

MAGIC = b"G2RP"
VERSION = 1


def read_stream(telemetry_dir: str, name: str) -> np.ndarray:
    m = Manifest.load(telemetry_dir)
    s = m.streams[name]
    arr = np.fromfile(Path(telemetry_dir) / s.file, dtype=s.structured_dtype(),
                      count=s.rows_written if s.rows_written else -1)
    return arr


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("telemetry_dir", help="capture telemetry dir (has manifest.json + *.bin)")
    ap.add_argument("--device", type=int, required=True, help="device_id: 1=left, 2=right")
    ap.add_argument("--start", type=float, default=0.0, help="window start (s from first IMU sample)")
    ap.add_argument("--dur", type=float, default=20.0, help="window duration (s)")
    ap.add_argument("-o", "--out", required=True, help="output .replay path")
    a = ap.parse_args()

    imu = read_stream(a.telemetry_dir, "imu")
    fus = read_stream(a.telemetry_dir, "fusion")
    imu = imu[imu["device_id"] == a.device]
    fus = fus[fus["device_id"] == a.device]
    if len(imu) == 0:
        print(f"no IMU rows for device {a.device}", file=sys.stderr)
        return 1

    t_first = int(imu["t_mono_ns"].min())
    lo = t_first + int(a.start * 1e9)
    hi = lo + int(a.dur * 1e9)
    imu = imu[(imu["t_mono_ns"] >= lo) & (imu["t_mono_ns"] < hi)]
    fus = fus[(fus["t_mono_ns"] >= lo) & (fus["t_mono_ns"] < hi)]
    # Drop non-finite optical rows up front (the loader/filter would skip them anyway).
    if len(fus):
        fin = np.isfinite(fus["opt_px"]) & np.isfinite(fus["opt_py"]) & np.isfinite(fus["opt_pz"]) & \
              np.isfinite(fus["opt_qw"])
        fus = fus[fin]
    if len(imu) == 0:
        print("empty window", file=sys.stderr)
        return 1

    base = int(imu["t_mono_ns"].min())
    if len(fus):
        base = min(base, int(fus["t_mono_ns"].min()))

    with open(a.out, "wb") as f:
        f.write(MAGIC)
        f.write(struct.pack("<III", VERSION, len(imu), len(fus)))
        for r in imu:
            f.write(struct.pack("<qffffff", int(r["t_mono_ns"]) - base,
                                float(r["ax"]), float(r["ay"]), float(r["az"]),
                                float(r["gx"]), float(r["gy"]), float(r["gz"])))
        for r in fus:
            f.write(struct.pack("<qfffffff", int(r["t_mono_ns"]) - base,
                                float(r["opt_px"]), float(r["opt_py"]), float(r["opt_pz"]),
                                float(r["opt_qx"]), float(r["opt_qy"]), float(r["opt_qz"]),
                                float(r["opt_qw"])))
    dur = (int(imu["t_mono_ns"].max()) - base) / 1e9
    print(f"wrote {a.out}: {len(imu)} imu + {len(fus)} pose over {dur:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
