#!/usr/bin/env python3
"""make_synthetic.py -- emit a realistic sample G2 telemetry dataset.

Produces manifest.json + one packed .bin per stream that obey the on-disk
contract in docs/TELEMETRY-SCHEMA.md, so convert.py / analyze.py can be
exercised without a Monado build. Mimics the producer: it computes packed
offsets and row_size, writes the layout-only manifest, streams rows, then
rewrites the manifest with rows_written + overflow_total.

Scenario (~10 s on CLOCK_MONOTONIC ns):
  imu          1000 Hz x 3 devices (HMD + 2 controllers), gravity at rest + noise
  frame          90 Hz x 4 cams, varying blob yield / exposure / gain / led
  pose_attempt  ~40 Hz x 2 controllers, accept/reject/recover mix, reproj error
  fusion        ~40 Hz x 2 controllers, small SLAM<->IMU residuals that drift
  event          sparse: lock_lost/acquired/recover/jump/anomaly + 1 ring_overflow
A small overflow is simulated on the imu stream (dropped rows are not written but
counted in overflow_total, and a ring_overflow event is emitted).

Usage:
    python3 make_synthetic.py [OUT_DIR] [--seconds N] [--seed N]
OUT_DIR defaults to $G2_TELEMETRY or ./synthetic.
"""
from __future__ import annotations

import argparse
import json
import os
import struct
from pathlib import Path

import numpy as np

# ---- schema: field tables (type tokens match the manifest contract) ----------
# Each list is (name, type). Offsets/row_size are derived below, like the producer.
NS = 1_000_000_000

STREAM_FIELDS: dict[str, list[tuple[str, str]]] = {
    "imu": [
        ("t_mono_ns", "u64"), ("hw_ts_ns", "u64"), ("device_id", "u8"),
        ("ax", "f32"), ("ay", "f32"), ("az", "f32"),
        ("gx", "f32"), ("gy", "f32"), ("gz", "f32"),
    ],
    "frame": [
        ("t_mono_ns", "u64"), ("hw_ts_ns", "u64"), ("cam_id", "u8"),
        ("frame_seq", "u32"), ("n_blobs", "u16"), ("exposure", "u16"),
        ("gain", "u16"), ("led_intensity", "u16"),
    ],
    "pose_attempt": [
        ("t_mono_ns", "u64"), ("hw_ts_ns", "u64"), ("device_id", "u8"),
        ("cam_id", "u8"), ("leds_visible", "u8"), ("blobs_matched", "u8"),
        ("inliers", "u8"), ("outcome", "u8"), ("reproj_err_px", "f32"),
        ("px", "f32"), ("py", "f32"), ("pz", "f32"),
        ("qx", "f32"), ("qy", "f32"), ("qz", "f32"), ("qw", "f32"),
    ],
    "fusion": [
        ("t_mono_ns", "u64"), ("device_id", "u8"), ("outcome", "u8"),
        ("pos_residual_m", "f32"), ("rot_residual_deg", "f32"),
        ("opt_px", "f32"), ("opt_py", "f32"), ("opt_pz", "f32"),
        ("opt_qx", "f32"), ("opt_qy", "f32"), ("opt_qz", "f32"), ("opt_qw", "f32"),
        ("pred_px", "f32"), ("pred_py", "f32"), ("pred_pz", "f32"),
        ("pred_qx", "f32"), ("pred_qy", "f32"), ("pred_qz", "f32"), ("pred_qw", "f32"),
    ],
    "event": [
        ("t_mono_ns", "u64"), ("device_id", "u8"), ("event_type", "u16"), ("value", "f32"),
    ],
}

# type token -> (struct char, size). Little-endian assembled per row.
TYPE_STRUCT = {
    "u8": ("B", 1), "u16": ("H", 2), "u32": ("I", 4),
    "u64": ("Q", 8), "f32": ("f", 4), "f64": ("d", 8),
}
STREAM_FILE = {k: f"{k}.bin" for k in STREAM_FIELDS}
# stream id index used by ring_overflow event value (order of declaration)
STREAM_ID = {name: i for i, name in enumerate(STREAM_FIELDS)}


def layout(fields: list[tuple[str, str]]) -> tuple[list[dict], int, str]:
    """Packed (no padding) offsets + row_size + struct format for a field list."""
    offset = 0
    out = []
    fmt = "<"
    for name, typ in fields:
        ch, sz = TYPE_STRUCT[typ]
        out.append({"name": name, "type": typ, "offset": offset})
        fmt += ch
        offset += sz
    return out, offset, fmt


def build_manifest(start_mono: int, start_real: int) -> dict:
    streams = {}
    for name, fields in STREAM_FIELDS.items():
        flds, row_size, _ = layout(fields)
        streams[name] = {
            "file": STREAM_FILE[name],
            "row_size": row_size,
            "fields": flds,
            # rows_written / overflow_total filled at shutdown
        }
    return {
        "version": 1,
        "clock": "CLOCK_MONOTONIC",
        "start": {"t_mono_ns": start_mono, "t_realtime_ns": start_real},
        "types": "little-endian; u8 u16 u32 u64 f32 f64",
        "streams": streams,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate synthetic G2 telemetry")
    ap.add_argument("out_dir", nargs="?",
                    default=os.environ.get("G2_TELEMETRY", "./synthetic"))
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--seed", type=int, default=1234)
    args = ap.parse_args(argv)

    rng = np.random.default_rng(args.seed)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    t0 = 12_345_000_000_000          # arbitrary monotonic base (ns)
    t0_real = 1_700_000_000_000_000_000
    dur_ns = int(args.seconds * NS)

    manifest = build_manifest(t0, t0_real)
    # layout-only manifest first (crash-safety: written at init)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))

    fmts = {name: layout(f)[2] for name, f in STREAM_FIELDS.items()}
    rows_written = {name: 0 for name in STREAM_FIELDS}
    overflow = {name: 0 for name in STREAM_FIELDS}
    files = {name: open(out / STREAM_FILE[name], "wb") for name in STREAM_FIELDS}

    def emit(stream: str, values: tuple):
        files[stream].write(struct.pack(fmts[stream], *values))
        rows_written[stream] += 1

    # ---- IMU: 1000 Hz x 3 devices, at-rest gravity on +Z accel + sensor noise --
    imu_hz = 1000
    n_imu = int(args.seconds * imu_hz)
    g = 9.80665
    acc_noise, gyr_noise = 0.02, 0.003     # m/s^2 , rad/s (still)
    # simulate a short overflow burst on imu: drop a window of HMD rows
    drop_lo = int(n_imu * 0.40)
    drop_hi = int(n_imu * 0.41)            # ~1% window dropped for device 0
    for i in range(n_imu):
        t = t0 + int(i * NS / imu_hz)
        for dev in (0, 1, 2):
            if dev == 0 and drop_lo <= i < drop_hi:
                overflow["imu"] += 1       # ring full -> producer drops + counts
                continue
            ax = rng.normal(0.0, acc_noise)
            ay = rng.normal(0.0, acc_noise)
            az = rng.normal(g, acc_noise)  # at rest -> gravity on Z
            gx = rng.normal(0.0, gyr_noise)
            gy = rng.normal(0.0, gyr_noise)
            gz = rng.normal(0.0, gyr_noise)
            hw = t - 50_000               # sensor stamp slightly earlier
            emit("imu", (t, hw, dev, ax, ay, az, gx, gy, gz))

    # ---- frames: 90 Hz x 4 cams ------------------------------------------------
    frame_hz = 90
    n_frame = int(args.seconds * frame_hz)
    seq = [0, 0, 0, 0]
    for i in range(n_frame):
        t = t0 + int(i * NS / frame_hz)
        for cam in range(4):
            n_blobs = int(max(0, rng.normal(14 - cam, 4)))     # cams differ a bit
            exposure = int(np.clip(rng.normal(6000, 400), 1000, 12000))
            gain = int(np.clip(rng.normal(16, 3), 0, 63))
            led = int(np.clip(rng.normal(200, 20), 0, 255))
            hw = t - 100_000
            emit("frame", (t, hw, cam, seq[cam], n_blobs, exposure, gain, led))
            seq[cam] += 1

    # ---- pose_attempt: ~40 Hz x 2 controllers ---------------------------------
    pose_hz = 40
    n_pose = int(args.seconds * pose_hz)
    for i in range(n_pose):
        t = t0 + int(i * NS / pose_hz)
        for dev in (1, 2):
            cam = int(rng.integers(0, 4))
            leds = int(np.clip(rng.normal(9, 2), 0, 20))
            blobs = int(np.clip(rng.normal(leds - 1, 2), 0, leds))
            r = rng.random()
            if r < 0.78:
                outcome, inliers, reproj = 1, max(4, blobs - int(rng.integers(0, 2))), rng.normal(0.8, 0.25)
            elif r < 0.93:
                outcome, inliers, reproj = 0, int(rng.integers(0, 4)), rng.normal(3.5, 1.0)
            else:
                outcome, inliers, reproj = 2, max(4, blobs - 2), rng.normal(1.6, 0.4)
            reproj = float(max(0.05, reproj))
            px, py, pz = rng.normal(0, 0.3, 3)
            q = rng.normal(0, 1, 4); q /= np.linalg.norm(q)
            hw = t - 100_000
            emit("pose_attempt", (t, hw, dev, cam, leds, blobs, inliers, outcome,
                                  reproj, px, py, pz, q[0], q[1], q[2], q[3]))

    # ---- fusion: ~40 Hz x 2 controllers, residuals that slowly drift ----------
    for i in range(n_pose):
        t = t0 + int(i * NS / pose_hz)
        frac = i / max(1, n_pose)
        for dev in (1, 2):
            base_pos = 0.004 + 0.010 * frac      # drift grows over time
            base_rot = 0.20 + 0.50 * frac
            r = rng.random()
            outcome = 1 if r < 0.9 else (2 if r < 0.97 else 0)  # accept/reset/reject
            pos_res = float(abs(rng.normal(base_pos, 0.002)))
            rot_res = float(abs(rng.normal(base_rot, 0.10)))
            opt = list(rng.normal(0, 0.3, 3)) + list(rng.normal(0, 1, 4))
            pred = [o + rng.normal(0, 0.01) for o in opt]
            emit("fusion", (t, dev, outcome, pos_res, rot_res, *opt, *pred))

    # ---- events: sparse timeline + the one ring_overflow ----------------------
    def ev(t, dev, etype, val):
        emit("event", (t, dev, etype, float(val)))

    ev(t0 + int(0.5 * NS), 1, 1, 0)                 # left lock_acquired
    ev(t0 + int(0.6 * NS), 2, 1, 0)                 # right lock_acquired
    ev(t0 + int(3.2 * NS), 1, 3, 4.7)               # optical_jump_rejected (px)
    ev(t0 + int(4.0 * NS), 0, 4, 1.0)               # HMD imu_anomaly
    # ring_overflow event coincides with the dropped imu window above
    overflow_t = t0 + int(NS * (drop_lo / imu_hz))
    ev(overflow_t, 0, 5, STREAM_ID["imu"])          # ring_overflow, value=stream id
    ev(t0 + int(5.5 * NS), 2, 0, 0)                 # right lock_lost
    ev(t0 + int(5.9 * NS), 2, 2, 0)                 # right recover_attempt
    ev(t0 + int(6.1 * NS), 2, 1, 0)                 # right lock_acquired

    for f in files.values():
        f.close()

    # ---- rewrite manifest with totals (shutdown) ------------------------------
    for name in STREAM_FIELDS:
        manifest["streams"][name]["rows_written"] = rows_written[name]
        manifest["streams"][name]["overflow_total"] = overflow[name]
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))

    print(f"wrote synthetic telemetry to {out}")
    for name in STREAM_FIELDS:
        print(f"  {name:<14} rows={rows_written[name]:<8} overflow={overflow[name]}")
    print(f"duration ~{args.seconds}s, seed={args.seed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
