#!/usr/bin/env python3
"""mse_eval.py -- score a candidate controller trajectory against the cleaned-GT reference.

The reference is built (deflip -> RTS-smooth) from the recorded optical poses themselves;
see smooth_ref.py and docs/audit-2026-05-24/11-mse-cleaned-gt.md for what it can and CANNOT
validate (it is SELF-REFERENTIAL -- a flip the de-flipper misses pollutes both sides).

Candidate sources:
  - offline_vio_replay CSV  (header: t_ns,opt_valid,opt_px..opt_qw,pred_px..pred_qw,pred_tracked)
        choose the column set with --col opt   (front-end optical poses, default)
                              or  --col pred  (ESKF fused prediction)
  - the capture's own fusion stream (--source fusion --col opt|pred): a self-baseline
        (opt_* = recorded optical pose, pred_* = recorded gyro prior).

Metrics, over reference-valid frames where a candidate sample lands within --match-ms:
  - position MSE (m^2) and RMSE (cm)
  - orientation MSE (deg^2) and RMS (deg)   [geodesic angle]
  - FLIP-RATE of the candidate: consecutive candidate poses jumping >= 90 deg in <= 120 ms
  - coverage: how many reference-valid frames had a matched candidate sample

Usage:
  mse_eval.py <telemetry_dir> --device 1|2 [--source csv --csv FILE | --source fusion]
              [--col opt|pred] [--match-ms 25] [--valid-only]
"""
from __future__ import annotations

import argparse
import csv as csvmod
import sys
from pathlib import Path

import numpy as np

from smooth_ref import build_reference
import g2_geom as G
from manifest import Manifest, DEVICE_NAMES

FLIP_DEG = 90.0
FLIP_GAP_MS = 120.0


def load_candidate_csv(path, col):
    """Load offline_vio_replay CSV -> (t_ns, pos, quat, valid_mask)."""
    rows = []
    with open(path, newline="") as f:
        rd = csvmod.DictReader(f)
        for r in rd:
            rows.append(r)
    if not rows:
        raise ValueError(f"empty CSV {path}")
    t = np.array([int(r["t_ns"]) for r in rows], dtype=np.int64)
    px = f"{col}_px"
    py = f"{col}_py"
    pz = f"{col}_pz"
    qx, qy, qz, qw = f"{col}_qx", f"{col}_qy", f"{col}_qz", f"{col}_qw"
    pos = np.array([[float(r[px]), float(r[py]), float(r[pz])] for r in rows])
    quat = np.array([[float(r[qx]), float(r[qy]), float(r[qz]), float(r[qw])] for r in rows])
    if col == "opt":
        valid = np.array([int(float(r.get("opt_valid", 1))) != 0 for r in rows])
    else:
        valid = np.array([int(float(r.get("pred_tracked", 1))) != 0 for r in rows])
    # drop non-finite / non-valid candidate samples
    fin = np.isfinite(pos).all(1) & np.isfinite(quat).all(1)
    keep = valid & fin
    return t[keep], pos[keep], G.quat_normalize(quat[keep])


def load_candidate_fusion(telemetry_dir, dev, col):
    """Candidate = the capture's own fusion stream (self-baseline)."""
    d = Path(telemetry_dir)
    m = Manifest.load(d)
    fu = G.load_stream(d, m, "fusion")
    fm = fu[(fu["device_id"] == dev) & (fu["outcome"] == 1)]
    t = fm["t_mono_ns"].astype(np.int64)
    order = np.argsort(t)
    fm = fm[order]
    t = t[order]
    pos = np.stack([fm[f"{col}_px"], fm[f"{col}_py"], fm[f"{col}_pz"]], axis=1)
    quat = np.stack([fm[f"{col}_qx"], fm[f"{col}_qy"], fm[f"{col}_qz"], fm[f"{col}_qw"]], axis=1)
    fin = np.isfinite(pos).all(1) & np.isfinite(quat).all(1)
    qn = np.linalg.norm(quat, axis=1)
    fin &= qn > 0.5
    return t[fin], pos[fin], G.quat_normalize(quat[fin])


# per-failure-mode thresholds (few knobs; physical, not tuned to a capture)
ARM_REACH_M = 0.9          # a hand cannot be further than ~arm's length from the body anchor
FLY_GAP_MS = 120.0         # consecutive-jump window (matches the flip window)
JITTER_WIN = 5             # +-samples for the self-smoothing baseline (matches smooth_ref ORI_WIN)


def candidate_flip_rate(t, quat):
    n = 0
    f = 0
    for i in range(1, t.shape[0]):
        if (t[i] - t[i - 1]) / 1e6 <= FLIP_GAP_MS:
            n += 1
            if G.quat_geodesic_deg(quat[i], quat[i - 1]) >= FLIP_DEG:
                f += 1
    return f, n


def fly_off_metrics(t, pos, arm_reach_m=ARM_REACH_M, gap_ms=FLY_GAP_MS):
    """Out-of-view fly-off failure mode: when optical drops out / a degenerate PnP fires, the
    pose teleports out of the play volume. Reports the max and p99 of consecutive position jumps
    (m, over frames within gap_ms -- a real hand jump is bounded), and the fraction of frames
    that land beyond arm-reach of the trajectory's robust centre (a hand cannot be there)."""
    n = t.shape[0]
    jumps = []
    for i in range(1, n):
        if (t[i] - t[i - 1]) / 1e6 <= gap_ms:
            jumps.append(float(np.linalg.norm(pos[i] - pos[i - 1])))
    jumps = np.array(jumps) if jumps else np.array([0.0])
    centre = np.median(pos, axis=0)
    dist = np.linalg.norm(pos - centre, axis=1)
    return dict(
        max_jump_m=float(jumps.max()),
        p99_jump_m=float(np.percentile(jumps, 99)),
        frac_beyond_reach=float((dist > arm_reach_m).mean()),
        n_consecutive=int(jumps.shape[0]),
    )


def jitter_metric(t, pos, win=JITTER_WIN, gap_ms=FLY_GAP_MS):
    """High-frequency jitter: real hand motion is smooth/continuous/DIFFERENTIABLE, so the right
    baseline is the LOCAL LINEAR (constant-velocity) fit through a pose's neighbours -- a uniform
    motion matches it exactly (zero jitter), only true high-freq wobble deviates. Reports the RMS
    and p95 of the per-frame residual against that fit (cm). Computed only inside continuous runs
    (consecutive samples within gap_ms) so a legitimate dropout gap is not counted as jitter."""
    n = t.shape[0]
    t_s = (t - t[0]) / 1e9 if n else t
    resid = []
    for i in range(n):
        # the contiguous run of samples around i (no jumping a dropout gap)
        a = i
        while a > max(0, i - win) and (t[a] - t[a - 1]) / 1e6 <= gap_ms:
            a -= 1
        b = i
        while b + 1 < min(n, i + win + 1) and (t[b + 1] - t[b]) / 1e6 <= gap_ms:
            b += 1
        if b - a < 2:
            continue
        tt = t_s[a:b + 1]
        A = np.vstack([tt, np.ones(tt.shape[0])]).T
        d = 0.0
        for ax in range(3):
            coef, *_ = np.linalg.lstsq(A, pos[a:b + 1, ax], rcond=None)
            fit_i = coef[0] * t_s[i] + coef[1]
            d += (pos[i, ax] - fit_i) ** 2
        resid.append(float(np.sqrt(d)))
    if not resid:
        return dict(rms_cm=0.0, p95_cm=0.0, n=0)
    r = np.array(resid)
    return dict(rms_cm=float(np.sqrt(np.mean(r * r)) * 100.0),
                p95_cm=float(np.percentile(r, 95) * 100.0), n=int(r.shape[0]))


def yield_metric(t_cand_all, n_cand_valid, ref):
    """Yield + latency line. yield = fraction of candidate rows that carried a usable (valid,
    finite) pose; the rest are frames the tracker produced no pose for (a dropout cost). The
    'latency' proxy is the median inter-sample interval of valid candidate poses (ms) -- a sparse
    valid stream means the tracker is effectively lower-rate / laggier from the user's view."""
    n_all = int(t_cand_all.shape[0]) if t_cand_all is not None else n_cand_valid
    dt = np.diff(np.sort(t_cand_all)) / 1e6 if (t_cand_all is not None and n_all > 1) else np.array([0.0])
    return dict(
        yield_pct=100.0 * n_cand_valid / max(n_all, 1),
        n_valid=int(n_cand_valid),
        n_total=n_all,
        median_interval_ms=float(np.median(dt)) if dt.size else 0.0,
    )


def csv_row_count(path):
    """Total data rows in an offline_vio_replay CSV (for the yield denominator)."""
    with open(path, newline="") as f:
        return sum(1 for _ in csvmod.DictReader(f))


REENTRY_GAP_MS = 100.0  # a coast longer than this makes the re-acquire a user-visible "snap"
# A re-entry event is INFORMATIVE (the controller actually moved out of view) only when ground truth at
# re-entry is this far from the freeze anchor. Below that, BF and freeze are trivially indistinguishable
# and including those events would wash out the metric. Anthropometric: a small noise threshold above
# steady-hand tremor (~cm) but well below typical hand reach motion.
INFORMATIVE_DISPLACEMENT_M = 0.15
# Minimum dropout span (frames opt_valid=0 between the last valid and the re-entry) for an event to count
# -- skip single-frame blips where freeze and BF are both ~perfect and the metric is noise.
REENTRY_MIN_GAP_FRAMES = 2


def reentry_snap_m_from_csv(path):
    """Re-entry snap of the FUSED stream: when the controller comes back into view after a coast, the
    reported (pred) position jumps from where it was held/frozen to the freshly re-acquired pose. We
    take the max single-frame jump in pred position across a pred_tracked 0->1 transition that follows a
    coast gap > REENTRY_GAP_MS. This is the user-visible 'snap back' the gate must not let regress.

    Only meaningful for the pred column (opt has no tracked/coast notion). Returns 0.0 if the CSV lacks
    pred_tracked or has no such transition."""
    with open(path, newline="") as f:
        rd = csvmod.DictReader(f)
        if "pred_tracked" not in (rd.fieldnames or []):
            return 0.0
        rows = list(rd)
    if len(rows) < 2:
        return 0.0
    t = np.array([int(r["t_ns"]) for r in rows], dtype=np.int64)
    pos = np.array([[float(r["pred_px"]), float(r["pred_py"]), float(r["pred_pz"])] for r in rows])
    tracked = np.array([int(float(r.get("pred_tracked", 0))) != 0 for r in rows])
    fin = np.isfinite(pos).all(1)
    snap = 0.0
    for i in range(1, len(rows)):
        reacquired = tracked[i] and not tracked[i - 1] and fin[i] and fin[i - 1]
        if reacquired and (t[i] - t[i - 1]) / 1e6 > REENTRY_GAP_MS:
            snap = max(snap, float(np.linalg.norm(pos[i] - pos[i - 1])))
    return snap


def reentry_accuracy_from_csv(path):
    """Re-entry GT-accuracy floor: at each optical re-entry (opt_valid 0->1 after a >=2-frame dropout)
    the first in-view opt pose is GROUND TRUTH for where the controller actually was. Compare the
    fusion's body-follow prediction (pred[i-1], the last out-of-view report) against a synthetic FREEZE
    counterfactual (last_opt_before_dropout). Restricted to INFORMATIVE events (GT displacement from
    the freeze anchor > INFORMATIVE_DISPLACEMENT_M) so a near-stationary controller doesn't wash the
    medians. Returns BF and freeze err medians/means (cm) and event count -- used as the out-of-view
    accuracy gate (replaces fly_frac_beyond_reach, which the GT test proved is a freeze-biased
    distance-from-median proxy and does not track real prediction accuracy)."""
    with open(path, newline="") as f:
        rd = csvmod.DictReader(f)
        fn = rd.fieldnames or []
        if "opt_valid" not in fn or "pred_px" not in fn:
            return dict(bf_err_cm_median=0.0, freeze_err_cm_median=0.0,
                        bf_err_cm_mean=0.0, freeze_err_cm_mean=0.0, n_events=0)
        rows = list(rd)
    if len(rows) < 3:
        return dict(bf_err_cm_median=0.0, freeze_err_cm_median=0.0,
                    bf_err_cm_mean=0.0, freeze_err_cm_mean=0.0, n_events=0)
    opt_valid = np.array([int(float(r.get("opt_valid", 0))) != 0 for r in rows])
    opt_pos = np.array([[float(r["opt_px"]), float(r["opt_py"]), float(r["opt_pz"])] for r in rows])
    pred_pos = np.array([[float(r["pred_px"]), float(r["pred_py"]), float(r["pred_pz"])] for r in rows])
    opt_fin = np.isfinite(opt_pos).all(1)
    pred_fin = np.isfinite(pred_pos).all(1)
    # opt_pos[i-1] is NaN during a dropout (opt_valid=0 emits NaN); only require finiteness for the
    # values we actually use: opt at re-entry i (GT), pred at i-1 (BF pred), opt at last_valid_i (freeze).
    bf_errs, fz_errs = [], []
    last_valid_i = -1
    for i in range(1, len(rows)):
        if opt_valid[i] and not opt_valid[i - 1] and opt_fin[i] and pred_fin[i - 1]:
            if (last_valid_i >= 0 and opt_fin[last_valid_i] and
                    (i - 1 - last_valid_i) >= REENTRY_MIN_GAP_FRAMES):
                gt = opt_pos[i]
                fz_pred = opt_pos[last_valid_i]
                disp = float(np.linalg.norm(gt - fz_pred))
                if disp > INFORMATIVE_DISPLACEMENT_M:
                    bf_errs.append(float(np.linalg.norm(pred_pos[i - 1] - gt)))
                    fz_errs.append(disp)  # ||fz_pred - gt|| identically equals disp
        if opt_valid[i] and opt_fin[i]:
            last_valid_i = i
    if not bf_errs:
        return dict(bf_err_cm_median=0.0, freeze_err_cm_median=0.0,
                    bf_err_cm_mean=0.0, freeze_err_cm_mean=0.0, n_events=0)
    bf = np.array(bf_errs); fz = np.array(fz_errs)
    return dict(
        bf_err_cm_median=float(np.median(bf)) * 100.0,
        freeze_err_cm_median=float(np.median(fz)) * 100.0,
        bf_err_cm_mean=float(np.mean(bf)) * 100.0,
        freeze_err_cm_mean=float(np.mean(fz)) * 100.0,
        n_events=int(bf.shape[0]),
    )


def compute_metrics(ref, t_c, pos_c, quat_c, n_total, match_ms, valid_only) -> dict | None:
    """All candidate-vs-reference + per-failure-mode metrics in one dict (decoupled from I/O so
    the CLI and run_ab share one implementation). Returns None if nothing matched the reference."""
    ci, ri = match_to_reference(ref, t_c, match_ms, valid_only)
    if ci.shape[0] == 0:
        return None
    dp = pos_c[ci] - ref.pos[ri]
    pos_sq = np.sum(dp * dp, axis=1)
    ang = G.quat_geodesic_deg(quat_c[ci], ref.quat[ri])
    f, nf = candidate_flip_rate(t_c, quat_c)
    fly = fly_off_metrics(t_c, pos_c)
    jit = jitter_metric(t_c, pos_c)
    yld = yield_metric(t_c, t_c.shape[0], ref)  # n_valid from the loaded (valid-only) stream
    yld_total = max(int(n_total), int(t_c.shape[0]))
    n_valid_ref = int(ref.valid.sum())
    return dict(
        n_matched=int(ci.shape[0]),
        n_valid_ref=n_valid_ref,
        coverage_pct=100.0 * int(np.unique(ri).shape[0]) / max(n_valid_ref, 1),
        pos_mse=float(np.mean(pos_sq)),
        pos_rmse_cm=float(np.sqrt(np.mean(pos_sq)) * 100.0),
        pos_med_cm=float(np.median(np.sqrt(pos_sq)) * 100.0),
        pos_p90_cm=float(np.sqrt(np.percentile(pos_sq, 90)) * 100.0),
        ori_mse=float(np.mean(ang * ang)),
        ori_rms=float(np.sqrt(np.mean(ang * ang))),
        ori_med=float(np.median(ang)),
        ori_p90=float(np.percentile(ang, 90)),
        wrong_branch_pct=100.0 * float((ang > 90.0).mean()),
        flip_rate_pct=100.0 * f / max(nf, 1),
        n_flip=int(f),
        n_flip_pairs=int(nf),
        fly_max_jump_m=fly["max_jump_m"],
        fly_p99_jump_m=fly["p99_jump_m"],
        fly_frac_beyond_reach=fly["frac_beyond_reach"],
        jitter_rms_cm=jit["rms_cm"],
        jitter_p95_cm=jit["p95_cm"],
        yield_pct=100.0 * int(t_c.shape[0]) / max(yld_total, 1),
        n_valid=int(t_c.shape[0]),
        n_total=yld_total,
        median_interval_ms=yld["median_interval_ms"],
    )


def match_to_reference(ref, t_cand, match_ms, valid_only):
    """For each candidate sample, find the nearest reference sample within match_ms.
    Returns index arrays (cand_idx, ref_idx) of matched pairs."""
    tr = ref.t_ns
    ci, ri = [], []
    for k, t in enumerate(t_cand):
        j = np.searchsorted(tr, t)
        best = -1
        for cand in (j - 1, j):
            if 0 <= cand < tr.shape[0] and abs(int(tr[cand]) - int(t)) <= match_ms * 1e6:
                if best < 0 or abs(int(tr[cand]) - int(t)) < abs(int(tr[best]) - int(t)):
                    best = cand
        if best < 0:
            continue
        if valid_only and not ref.valid[best]:
            continue
        ci.append(k)
        ri.append(best)
    return np.array(ci, int), np.array(ri, int)


def main() -> int:
    ap = argparse.ArgumentParser(description="MSE of a candidate trajectory vs cleaned-GT reference")
    ap.add_argument("telemetry_dir")
    ap.add_argument("--device", type=int, choices=(1, 2), required=True)
    ap.add_argument("--source", choices=("csv", "fusion"), default="fusion")
    ap.add_argument("--csv", help="offline_vio_replay CSV (for --source csv)")
    ap.add_argument("--col", choices=("opt", "pred"), default="opt")
    ap.add_argument("--match-ms", type=float, default=25.0)
    ap.add_argument("--valid-only", action="store_true", default=True,
                    help="score only against reference-valid frames (default on)")
    ap.add_argument("--all-frames", dest="valid_only", action="store_false",
                    help="score against all reference frames (incl. extrapolated)")
    args = ap.parse_args()

    dev = args.device
    ref = build_reference(args.telemetry_dir, dev)
    if ref is None:
        print(f"device {dev}: could not build reference (insufficient poses)")
        return 1

    if args.source == "csv":
        if not args.csv:
            print("--source csv requires --csv FILE")
            return 2
        t_c, pos_c, quat_c = load_candidate_csv(args.csv, args.col)
        src = f"csv:{Path(args.csv).name}:{args.col}"
    else:
        t_c, pos_c, quat_c = load_candidate_fusion(args.telemetry_dir, dev, args.col)
        src = f"fusion:{args.col}"

    if t_c.shape[0] == 0:
        print("candidate has no usable samples")
        return 1

    n_total = csv_row_count(args.csv) if args.source == "csv" else t_c.shape[0]
    mt = compute_metrics(ref, t_c, pos_c, quat_c, n_total, args.match_ms, args.valid_only)
    if mt is None:
        print("no candidate sample matched a reference frame within --match-ms")
        return 1

    print(f"=== MSE eval: device {dev} ({DEVICE_NAMES[dev]})  candidate={src} ===")
    print(f"  reference: {ref.stats['n_kept']} smoothed poses ({mt['n_valid_ref']} valid), "
          f"deflip success embedded")
    print(f"  matched frames: {mt['n_matched']}  (coverage of valid ref = {mt['coverage_pct']:.1f}%, "
          f"match window {args.match_ms:.0f}ms)")
    print(f"  POSITION  MSE = {mt['pos_mse']:.6f} m^2   RMSE = {mt['pos_rmse_cm']:.2f} cm   "
          f"(median = {mt['pos_med_cm']:.2f} cm, p90 = {mt['pos_p90_cm']:.2f} cm)")
    print(f"  ORIENT    MSE = {mt['ori_mse']:.3f} deg^2  RMS  = {mt['ori_rms']:.2f} deg   "
          f"(median = {mt['ori_med']:.2f} deg, p90 = {mt['ori_p90']:.2f} deg)")
    print(f"  ORIENT    wrong-branch frames (>90deg vs ref) = {mt['wrong_branch_pct']:.2f}%  "
          f"(RMS is dominated by this tail)")
    print(f"  CANDIDATE flip-rate = {mt['flip_rate_pct']:.2f}%  "
          f"({mt['n_flip']}/{mt['n_flip_pairs']} consecutive >= 90deg <= 120ms)")
    print(f"  FLY-OFF   max-jump = {mt['fly_max_jump_m']*100:.1f} cm  p99-jump = "
          f"{mt['fly_p99_jump_m']*100:.1f} cm  beyond-arm-reach = {mt['fly_frac_beyond_reach']*100:.2f}%")
    print(f"  JITTER    RMS = {mt['jitter_rms_cm']:.3f} cm  p95 = {mt['jitter_p95_cm']:.3f} cm  "
          f"(pred vs its own smoothed self)")
    print(f"  YIELD     {mt['yield_pct']:.1f}%  ({mt['n_valid']}/{mt['n_total']} rows carried a pose)  "
          f"median-interval = {mt['median_interval_ms']:.1f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
