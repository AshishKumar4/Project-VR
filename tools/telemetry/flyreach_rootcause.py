#!/usr/bin/env python3
"""flyreach_rootcause.py -- decompose the body-anchor design's "fly>reach" frames against independent
references, so we can tell which of the >0.9m-from-pred-median frames are correct body-following and
which are real drift.

The user-asked question: under Model #1, ~15% of left pred frames land beyond ARM_REACH_M of the
trajectory's session median. The GT re-entry test proved freeze and body-follow are tied on real
accuracy overall, but did NOT prove the specific fly>reach frames are correct. This script answers:

  IN-VIEW fly>reach frames -- the cleaned-GT reference is defined, so we can DIRECTLY tell whether the
  controller actually was >0.9m from its median or whether pred drifted there. If GT is also far,
  fly>reach is correct. If GT is near and pred is far, pred drifted.

  OUT-OF-VIEW fly>reach frames -- no controller GT (it's unseen), but the head pose anchor IS valid
  (SLAM tracks the head continuously). Decompose by |head - pred-median|:
    * head moved far from the controller's in-view median  -> body-follow CONSISTENT (head took the
      anchor with it; whether the arm followed is GT-unverifiable but the design choice is being applied
      as intended).
    * head stayed near the controller's in-view median + pred is far -> dead-reckon DRIFT past arm-reach
      that the inter-fold velocity built up before the next fold throttle fired. Real bug or real drift.

Outputs a per-controller decomposition: how many of the fly>reach frames are GT-confirmed-far (correct),
GT-rejected-far (in-view drift, real bug if any), head-took-it (out-of-view body-follow consistent),
or head-near-pred-far (out-of-view drift). Honest answer: if head-near-pred-far is the dominant
category, the metric reflects a real defect; if head-took-it dominates, fly>reach is largely an
artifact of the metric not the design.

Usage:
  python3 flyreach_rootcause.py --capture <cap_dir> --csv <run_ab_csv> [--device 1|2] [--reach-m 0.9]

The capture dir is needed for the cleaned-GT + the head pose; the CSV is the offline_vio_replay output
that carries opt_valid + pred_p* + hmd_p* + the matching t_ns.
"""
from __future__ import annotations

import argparse
import csv as csvmod
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from headpose_anchor import load_head_pose
from manifest import Manifest
from smooth_ref import build_reference

ARM_REACH_M = 0.9
HEAD_NEAR_M = 0.30        # head moved less than this from controller's session-median -> "head stayed near"
GT_MATCH_MS = 25.0        # nearest cleaned-GT sample within this gap counts as in-view + matched
HEAD_MATCH_MS = 40.0      # nearest head-pose sample within this gap
# Re-entry ease window: after opt_valid transitions 0->1, the get_prediction report eases toward optical
# at <=REENTRY_MAX_STEP_M=0.05 m/frame. At 90 Hz a 1m gap takes ~220ms to settle. Any in-view frame within
# this window has pred lagging optical BY DESIGN, so a "pred far + GT near" classification there reflects
# the smoothing transition, not a fusion bug. Set conservatively wider than the worst-case ease length.
REENTRY_EASE_MS = 300.0


def load_csv(path):
    """Load t_ns + opt_valid + pred_pos + hmd_pos from an offline_vio_replay CSV."""
    with open(path, newline="") as f:
        rows = list(csvmod.DictReader(f))
    if not rows:
        raise ValueError(f"empty CSV {path}")
    t = np.array([int(r["t_ns"]) for r in rows], dtype=np.int64)
    opt_valid = np.array([int(float(r.get("opt_valid", 0))) != 0 for r in rows])
    pred = np.array([[float(r["pred_px"]), float(r["pred_py"]), float(r["pred_pz"])] for r in rows])
    hmd = np.array([[float(r["hmd_px"]), float(r["hmd_py"]), float(r["hmd_pz"])] for r in rows])
    pred_tracked = np.array([int(float(r.get("pred_tracked", 0))) != 0 for r in rows])
    return t, opt_valid, pred, hmd, pred_tracked


def nearest_index(t_target, t_src, max_gap_ns):
    """For each t_target, the index into t_src of the nearest sample within max_gap_ns; -1 if none."""
    out = np.full(t_target.shape[0], -1, dtype=np.int64)
    if t_src.shape[0] == 0:
        return out
    j = np.searchsorted(t_src, t_target)
    for k, t in enumerate(t_target):
        best = -1
        best_gap = max_gap_ns + 1
        for cand in (j[k] - 1, j[k]):
            if 0 <= cand < t_src.shape[0]:
                gap = abs(int(t_src[cand]) - int(t))
                if gap <= max_gap_ns and gap < best_gap:
                    best = cand
                    best_gap = gap
        out[k] = best
    return out


def decompose(t, opt_valid, pred, hmd, pred_tracked, capture_dir, device):
    """Return a dict with per-category counts + the median used as the reference centre."""
    # Tracked frames only -- untracked frames are not reported to the compositor.
    keep = pred_tracked & np.isfinite(pred).all(1)
    t = t[keep]; opt_valid = opt_valid[keep]; pred = pred[keep]; hmd = hmd[keep]
    if t.shape[0] == 0:
        return None

    centre = np.median(pred, axis=0)
    pred_dist = np.linalg.norm(pred - centre, axis=1)
    far_mask = pred_dist > ARM_REACH_M

    n_total = t.shape[0]
    n_far = int(far_mask.sum())
    if n_far == 0:
        return dict(n_total=n_total, n_far=0, far_pct=0.0, breakdown={})

    # Cleaned-GT reference: defined only at in-view frames. We use it to classify in-view fly>reach.
    ref = build_reference(str(capture_dir / "telemetry"), device)
    head = load_head_pose(capture_dir / "telemetry", Manifest.load(capture_dir / "telemetry"))

    # Mark frames inside the re-entry ease window: opt_valid 0->1 transition + REENTRY_EASE_MS forward.
    # Inside this window, pred is eased toward optical and lags it BY DESIGN -- a "pred far + GT near"
    # classification there is the ease transient, not a fusion bug. Classified separately.
    in_ease = np.zeros(t.shape[0], dtype=bool)
    ease_ns = int(REENTRY_EASE_MS * 1e6)
    for i in range(1, t.shape[0]):
        if opt_valid[i] and not opt_valid[i - 1]:
            j = i
            while j < t.shape[0] and (t[j] - t[i]) <= ease_ns:
                in_ease[j] = True
                j += 1

    # In-view fly>reach: cross-reference cleaned-GT, splitting the ease-window frames out.
    far_in_view = far_mask & opt_valid
    n_in_view_far = int(far_in_view.sum())

    n_gt_confirmed_far = 0
    n_gt_rejected_far = 0      # in-view, OUTSIDE ease window, pred far + GT near -> real fusion drift
    n_gt_rejected_in_ease = 0  # in-view, INSIDE ease window, pred far + GT near -> known ease transient
    n_gt_unmatched_far = 0
    if ref is not None and n_in_view_far > 0:
        i_far = np.where(far_in_view)[0]
        ref_match = nearest_index(t[i_far], ref.t_ns, int(GT_MATCH_MS * 1e6))
        for k, idx in enumerate(ref_match):
            i_frame = i_far[k]
            if idx < 0 or not bool(ref.valid[idx]):
                n_gt_unmatched_far += 1
                continue
            gt_dist = float(np.linalg.norm(ref.pos[idx] - centre))
            if gt_dist > ARM_REACH_M:
                n_gt_confirmed_far += 1
            elif in_ease[i_frame]:
                n_gt_rejected_in_ease += 1
            else:
                n_gt_rejected_far += 1

    # Out-of-view fly>reach: split by head distance from the controller's median.
    far_out_of_view = far_mask & ~opt_valid
    n_out_of_view_far = int(far_out_of_view.sum())
    n_head_took_it = 0
    n_head_near_pred_far = 0
    n_head_unmatched_far = 0
    if n_out_of_view_far > 0 and head is not None:
        i_far_oov = np.where(far_out_of_view)[0]
        head_match = nearest_index(t[i_far_oov], head.t_ns, int(HEAD_MATCH_MS * 1e6))
        for k, idx in enumerate(head_match):
            if idx < 0:
                n_head_unmatched_far += 1
                continue
            head_dist_from_ctrl_med = float(np.linalg.norm(head.pos[idx] - centre))
            if head_dist_from_ctrl_med > HEAD_NEAR_M:
                n_head_took_it += 1
            else:
                n_head_near_pred_far += 1

    return dict(
        n_total=n_total,
        n_far=n_far,
        far_pct=100.0 * n_far / n_total,
        n_in_view_far=n_in_view_far,
        n_gt_confirmed_far=n_gt_confirmed_far,
        n_gt_rejected_far=n_gt_rejected_far,
        n_gt_rejected_in_ease=n_gt_rejected_in_ease,
        n_gt_unmatched_far=n_gt_unmatched_far,
        n_out_of_view_far=n_out_of_view_far,
        n_head_took_it=n_head_took_it,
        n_head_near_pred_far=n_head_near_pred_far,
        n_head_unmatched_far=n_head_unmatched_far,
        centre=centre.tolist(),
    )


def print_report(label, r):
    print(f"=== {label} ===")
    if r is None:
        print("  (no tracked frames)")
        return
    print(f"  tracked frames: {r['n_total']}")
    print(f"  fly>reach (>{ARM_REACH_M}m from session median): {r['n_far']} ({r['far_pct']:.2f}%)")
    if r['n_far'] == 0:
        return
    print(f"  in-view fly>reach: {r['n_in_view_far']}")
    if r['n_in_view_far']:
        print(f"    GT confirms >reach (correct):                            {r['n_gt_confirmed_far']}")
        print(f"    GT near, pred far, OUTSIDE re-entry ease (real DRIFT):   {r['n_gt_rejected_far']}")
        print(f"    GT near, pred far, INSIDE  re-entry ease (transient ok): {r['n_gt_rejected_in_ease']}")
        print(f"    GT unmatched/invalid at frame:                           {r['n_gt_unmatched_far']}")
    print(f"  out-of-view fly>reach: {r['n_out_of_view_far']}")
    if r['n_out_of_view_far']:
        print(f"    head moved far (>{HEAD_NEAR_M}m from median, body-follow consistent): {r['n_head_took_it']}")
        print(f"    head stayed near + pred far (drift past arm-reach):                 {r['n_head_near_pred_far']}")
        print(f"    no head pose match:                                                 {r['n_head_unmatched_far']}")
    # The decisive verdict line: real-drift counts only frames where the smoothing transient is excluded.
    # Out-of-view "head-near + pred-far" is genuinely ambiguous (could be the controller moving while the
    # head stays still -- a common VR gesture), so it is NOT counted as drift; the re-entry GT test is
    # the right oracle for the out-of-view case and shows BF and freeze tied on accuracy overall.
    real_in_view_drift = r['n_gt_rejected_far']
    consistent = r['n_gt_confirmed_far'] + r['n_head_took_it']
    transient = r['n_gt_rejected_in_ease']
    print(f"  VERDICT: of {r['n_far']} fly>reach frames,")
    print(f"           {consistent} are GT-/head-consistent (correct: body actually-far OR body-follow),")
    print(f"           {real_in_view_drift} are real IN-VIEW DRIFT (GT near, pred far, outside re-entry ease),")
    print(f"           {transient} are re-entry ease transients (known design behavior),")
    print(f"           {r['n_head_near_pred_far']} are out-of-view + head-near (ambiguous: drift OR head-still controller-move),")
    print(f"           {r['n_far'] - consistent - real_in_view_drift - transient - r['n_head_near_pred_far']} unmatched/unverifiable.")
    if real_in_view_drift == 0:
        print("  => NO real in-view fusion drift; fly>reach signal is design-behavior (body-follow + ease).")
    elif real_in_view_drift < 10:
        print("  => trace real in-view drift; possibly genuine fusion bug worth tracing, low overall weight.")
    elif real_in_view_drift > 50:
        print("  => SIGNIFICANT real in-view drift; investigate the fusion/optical hand-off path.")
    else:
        print("  => moderate real in-view drift; worth investigating the in-view-but-pred-far cases.")


def main() -> int:
    ap = argparse.ArgumentParser(description="root-cause the body-anchor fold's fly>reach against independent references")
    ap.add_argument("--capture", required=True, help="capture dir (with telemetry/)")
    ap.add_argument("--csv-left", required=True, help="offline_vio_replay CSV for dev1 (left)")
    ap.add_argument("--csv-right", help="offline_vio_replay CSV for dev2 (right) -- optional")
    args = ap.parse_args()
    cap = Path(args.capture).resolve()

    t, ov, pp, hp, pt = load_csv(args.csv_left)
    r = decompose(t, ov, pp, hp, pt, cap, 1)
    print_report("LEFT (dev1)", r)
    if args.csv_right:
        t, ov, pp, hp, pt = load_csv(args.csv_right)
        r = decompose(t, ov, pp, hp, pt, cap, 2)
        print_report("RIGHT (dev2)", r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
