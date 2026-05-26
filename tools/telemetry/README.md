<!-- Edited & maintained by Claude; presented as-is. -->

# G2 telemetry offline tooling

> AI-authored. Pure-Python consumer for the G2 tracking telemetry pipeline.
> No Monado build required.

Reads the self-describing on-disk dump emitted by Monado's `u_g2_telemetry.c`
(`manifest.json` + one packed `*.bin` per stream), converts it to Parquet, and
analyzes it. **Everything is driven by `manifest.json`** — the consumer builds a
numpy structured dtype per stream from each field's `name`/`type`/`offset` and
the stream's `row_size`, so there are **no hardcoded byte offsets**. The contract
is `docs/TELEMETRY-SCHEMA.md`.

## Install deps

None of these are installed by default. Install with:

```bash
pip install --user numpy pyarrow polars matplotlib
```

- **numpy** — structured-dtype parsing of the packed binaries.
- **pyarrow** — Parquet read/write.
- **matplotlib** — the analysis PNGs.
- **polars** — listed per the deliverable; the tools work on pyarrow alone, polars
  is an optional convenience for ad-hoc Parquet exploration.

## Usage

```bash
export G2_TELEMETRY=/path/to/dump          # dir with manifest.json + *.bin

# 1. (optional) make a synthetic dataset to exercise the pipeline
python3 make_synthetic.py "$G2_TELEMETRY"

# 2. convert .bin -> .parquet (one parquet per stream)
python3 convert.py "$G2_TELEMETRY"

# 3. analyze: prints numbers + a summary table, writes PNGs to <dir>/analysis
python3 analyze.py "$G2_TELEMETRY"
```

All three accept the telemetry dir as the first arg (default `$G2_TELEMETRY` or
`.`). `convert.py`/`analyze.py` take `-o OUT_DIR`; `make_synthetic.py` takes
`--seconds` and `--seed`.

## Files

| file | role |
|---|---|
| `manifest.py` | shared manifest parser + enum tables; builds the numpy dtype per stream |
| `convert.py` | `*.bin` → `*.parquet`, generic from manifest, crash-safe (floors a truncated final row) |
| `analyze.py` | loads parquets, prints numbers + summary, saves PNGs |
| `make_synthetic.py` | generates a realistic manifest + bins (incl. a simulated overflow) |
| `g2_geom.py` | shared quaternion / rotation-vector / gravity-tilt math + stream IO for the cleaned-GT tools |
| `deflip.py` | flip detector + de-flipper (gravity-tilt + temporal + confidence cues) → cleaned poses + quality flags |
| `smooth_ref.py` | speed-gate + RTS position smoother + tangent-space orientation smoother → the cleaned-GT reference |
| `mse_eval.py` | position/orientation MSE + flip-rate of a candidate (replay CSV or fusion) vs the cleaned-GT reference |
| `test_cleaned_gt.py` | decoupled+adversarial tests for the cleaned-GT / MSE tooling (no telemetry needed) |

> Cleaned-GT / MSE workflow: see `docs/audit-2026-05-24/11-mse-cleaned-gt.md`. The reference
> is **self-referential** (built from the optical poses it scores) — read that doc for what it
> can and cannot validate before trusting the numbers.

## Streams

- **imu** — per-device (HMD + 2 controllers) accelerometer + gyro at sensor rate
  (~1 kHz). Each row: emit time, sensor `hw_ts_ns`, `device_id`, accel `ax/ay/az`,
  gyro `gx/gy/gz`. Used for rate, per-axis noise (still-window stddev), and a
  gravity-magnitude sanity check (|accel| ≈ 9.8 at rest).
- **frame** — per-camera (4 cams) tracking-frame metadata at ~90 Hz: `hw_ts_ns`,
  `cam_id`, `frame_seq`, blob count `n_blobs`, and `exposure`/`gain`/`led_intensity`.
  Drives per-cam frame rate, blob-yield distribution, and exposure/gain/led trends.
- **pose_attempt** — one row per optical pose solve attempt per controller (~40 Hz):
  LEDs visible, blobs matched, inliers, `outcome` (rejected/accepted/recovered),
  reprojection error, and the candidate pose. Drives accepted-pose rate, accept/
  reject/recover ratios, and the inlier/blob/reproj distributions.
- **fusion** — per-controller SLAM↔IMU fusion result (~40 Hz): `outcome`, the
  position/rotation residuals (`pos_residual_m`, `rot_residual_deg` — the drift
  between optical and predicted state), plus the optical and predicted poses as
  scalar fields (`opt_px..opt_qw`, `pred_px..pred_qw`). Drives the residual time
  series (mean/95p/max).
- **event** — sparse discrete events per device: `lock_lost`, `lock_acquired`,
  `recover_attempt`, `optical_jump_rejected`, `imu_anomaly`, `ring_overflow`.
  Drives the event timeline and counts.

## Overflow

The producer never blocks: if a per-stream ring is full it drops the row and
increments `overflow_total` (recorded per stream in `manifest.json`, also surfaced
as a `ring_overflow` event). `convert.py` and `analyze.py` both flag any nonzero
overflow loudly — it must not happen in normal use.
