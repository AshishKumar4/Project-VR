#!/usr/bin/env python3
"""headpose_anchor.py -- an INDEPENDENT (non-self-referential) orientation anchor for the
G2 controller optical pose stream, derived from the recorded SLAM HMD head pose.

WHY (the self-reference problem this fixes)
-------------------------------------------
The cleaned-GT de-flipper (deflip.py) resolves mirror-twin flips with two cues that both
live INSIDE the controller stream: the controller accelerometer gravity-tilt and temporal
continuity of the controller track itself. That is self-referential: a flip the de-flipper
misses pollutes BOTH the reference and the candidate, so the wrong-branch fraction is only a
lower bound, and a PURE-YAW flip (gravity-invariant) is unvalidatable by tilt alone.

The recorded `head_pose.bin` is a SLAM-derived HMD world pose, produced INDEPENDENTLY of the
controller LED stream (the SLAM tracker never sees the controller blobs). Its world frame is
gravity-aligned (verified: head IMU specific force rotated to world aligns with the vertical
axis to <8 deg median) and its YAW does NOT drift (SLAM closes the loop on the room, unlike a
free-running gyro). So it provides two anchors the in-stream cues cannot:

  * TILT  -- world-down is a real, SLAM-validated constant. A controller orientation whose
    body-gravity (from the controller accel, where static) does not rotate to world-down is on
    the wrong tilt branch. (This is the same gravity cue deflip uses, but with the world-down
    direction now independently CONFIRMED by SLAM rather than merely asserted.)
  * YAW   -- the controller's world heading measured RELATIVE TO THE HEAD heading. Because the
    head heading is non-drifting and independent of the controller, a sustained controller
    yaw-flip (which fools deflip's temporal continuity -- both sides of a long bad run agree
    with each other) stands out as a ~180 deg step against a robust head-relative yaw track.

FUSION (do not replace -- abstain on disagreement)
--------------------------------------------------
This anchor does NOT replace the controller-accel cue; it is fused with it:
  * tilt: the anchor and the accel cue share the gravity physics, so they agree by
    construction where both are available; the anchor's contribution is to extend the verdict
    to MOVING frames (world-down is constant; it needs no static window) by carrying the last
    static body-gravity forward only over short gaps.
  * yaw: a verdict the accel cue simply cannot produce.
A per-frame branch verdict is GOOD / TILT_FLIP / YAW_FLIP / ABSTAIN. ABSTAIN is emitted when
the two cues disagree, or when neither has an observable opinion (gravity unobservable AND the
head-relative yaw track is itself uncertain there) -- honest abstention, never a false call.

This module is the C1 deliverable. It exposes:
  load_head_pose(d, m)               -> HeadPose (stale warm-up rows dropped)
  headpose_anchor_device(...)        -> AnchorResult (pure arrays; unit-testable)
  anchor_from_capture(d, dev)        -> AnchorResult bound to a real capture
  deflipper_miss_rate(d, dev)        -> the de-flipper's measured miss-rate vs this anchor
Run standalone for a per-controller anchor + de-flipper trust-bound report.

Usage: headpose_anchor.py <telemetry_dir>
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from manifest import Manifest, DEVICE_NAMES
import g2_geom as G

# --- tuning (few knobs; shared with deflip where the physics is shared) ---
from deflip import (
    FLIP_DEG, GAP_MS, TILT_TOL_DEG, ACCEL_STATIC_TOL,
    _body_gravity, deflip_device, _join_confidence,
    Q_GOOD, Q_OK, Q_CORRECTED, Q_UNCERTAIN,
)

# head-relative yaw track: how far a frame may deviate from the robust local yaw track
# before it is a yaw-flip candidate. A real swing between frames is small; a yaw flip is ~180.
YAW_FLIP_TOL_DEG = 90.0       # >= this from the local head-relative track == wrong yaw branch
YAW_TRACK_WIN = 9             # samples each side for the robust (median) head-relative yaw track
YAW_TRACK_MIN_INLIERS = 5     # need this many consistent neighbours or the track is UNCERTAIN here
YAW_TRACK_INLIER_TOL = 35.0   # a neighbour within this of the running median is a track inlier
STATIC_CARRY_MS = 120.0       # carry the last static body-gravity forward at most this long

# per-frame independent branch verdict
A_GOOD = 1        # both observable cues agree the orientation branch is correct
A_TILT_FLIP = -1  # gravity says the tilt branch is wrong
A_YAW_FLIP = -2   # head-relative yaw says the heading branch is wrong (gravity-blind flip)
A_ABSTAIN = 0     # cues disagree, or neither has an observable opinion -> no independent call


@dataclass
class HeadPose:
    t_ns: np.ndarray   # (H,) frame times of the real (non warm-up) head poses
    quat: np.ndarray   # (H,4) R_world_imu (x,y,z,w), gravity-aligned SLAM world
    pos: np.ndarray    # (H,3)
    n_dropped_warmup: int = 0

    def quat_at(self, t_query: np.ndarray, max_gap_ms: float = 40.0) -> np.ndarray:
        """Nearest head quat per query time; rows NaN where no head pose is within max_gap_ms."""
        t_query = np.atleast_1d(np.asarray(t_query, dtype=np.int64))
        out = np.full((t_query.shape[0], 4), np.nan)
        j = np.searchsorted(self.t_ns, t_query)
        for k, t in enumerate(t_query):
            best = -1
            for cand in (j[k] - 1, j[k]):
                if 0 <= cand < self.t_ns.shape[0] \
                        and abs(int(self.t_ns[cand]) - int(t)) <= max_gap_ms * 1e6:
                    if best < 0 or abs(int(self.t_ns[cand]) - int(t)) < abs(int(self.t_ns[best]) - int(t)):
                        best = cand
            if best >= 0:
                out[k] = self.quat[best]
        return out


@dataclass
class AnchorResult:
    device_id: int
    t_ns: np.ndarray            # (N,) accepted optical pose times
    verdict: np.ndarray         # (N,) A_GOOD / A_TILT_FLIP / A_YAW_FLIP / A_ABSTAIN
    tilt_err: np.ndarray        # (N,) world-down tilt error (deg), NaN where gravity unobservable
    yaw_resid: np.ndarray       # (N,) deviation from head-relative yaw track (deg), NaN where track uncertain
    head_quat: np.ndarray       # (N,4) head world quat at each frame (NaN if no head pose)
    stats: dict = field(default_factory=dict)


def load_head_pose(d, m: Manifest | None = None) -> HeadPose | None:
    """Load head_pose.bin and drop any stale SLAM warm-up block (rows recorded before the
    capture proper -- detected as a large backward time gap, zero translation)."""
    d = Path(d)
    if m is None:
        m = Manifest.load(d)
    if "head_pose" not in m.streams:
        return None
    hp = G.load_stream(d, m, "head_pose")
    if hp.shape[0] < 3:
        return None
    t = hp["t_mono_ns"].astype(np.int64)
    pos = np.stack([hp["px"], hp["py"], hp["pz"]], axis=1).astype(float)
    quat = G.quat_normalize(np.stack([hp["qx"], hp["qy"], hp["qz"], hp["qw"]], axis=1))
    # the real capture is the block ending at the LAST row; a warm-up block (if any) is
    # separated by the single largest forward time gap. Keep everything after that gap.
    n_drop = 0
    if t.shape[0] > 1:
        dt = np.diff(t)
        g = int(np.argmax(dt))
        # only treat it as a warm-up split if the gap is absurdly large (> 5 s) AND the
        # pre-gap block barely moved (a parked SLAM idle), else keep all rows.
        if dt[g] > 5e9 and float(np.linalg.norm(pos[g] - pos[0])) < 0.02:
            n_drop = g + 1
            t, pos, quat = t[n_drop:], pos[n_drop:], quat[n_drop:]
    order = np.argsort(t)
    return HeadPose(t_ns=t[order], quat=quat[order], pos=pos[order], n_dropped_warmup=n_drop)


def _static_body_gravity_carry(t_ns, g_body, carry_ms=STATIC_CARRY_MS):
    """Extend the per-frame static body-gravity over short non-static gaps by carrying the most
    recent static estimate forward (and the next one backward), bounded by carry_ms. World-down
    is constant, so a body-gravity that was valid 100 ms ago is still a good tilt reference now
    if the controller has not re-oriented much -- this lets the gravity verdict reach MOVING
    frames, which the static-only accel cue abstains on."""
    N = t_ns.shape[0]
    out = g_body.copy()
    static = ~np.isnan(g_body[:, 0])
    last_i = -1
    for i in range(N):
        if static[i]:
            last_i = i
        elif last_i >= 0 and (t_ns[i] - t_ns[last_i]) / 1e6 <= carry_ms:
            out[i] = g_body[last_i]
    next_i = -1
    for i in range(N - 1, -1, -1):
        if static[i]:
            next_i = i
        elif np.isnan(out[i, 0]) and next_i >= 0 and (t_ns[next_i] - t_ns[i]) / 1e6 <= carry_ms:
            out[i] = g_body[next_i]
    return out


def _head_relative_yaw_track(rel_yaw, t_ns):
    """Robust local track of the head-relative controller yaw + per-frame residual against it.

    The head-relative yaw is not constant -- the wrist can swing while the head turns -- so a
    constant-window median would falsely flag fast (but smooth) swings. For each frame we fit a
    robust LOCAL LINEAR trend (slope + intercept in time) to the neighbouring head-relative
    yaws, predict the frame's value FROM ITS NEIGHBOURS ONLY (leave-one-out, so a flipped frame
    cannot pull its own prediction), and report the deviation. The fit is iterated once with the
    flipped minority rejected (residual > inlier tol). Frames with too few consistent neighbours
    get NaN -- the track is uncertain there and the yaw cue abstains. Returns (track, resid, inl)."""
    N = rel_yaw.shape[0]
    track = np.full(N, np.nan)
    resid = np.full(N, np.nan)
    inliers = np.zeros(N, dtype=int)
    t_s = (t_ns - t_ns[0]) / 1e9 if N else t_ns
    for i in range(N):
        lo = max(0, i - YAW_TRACK_WIN)
        hi = min(N, i + YAW_TRACK_WIN + 1)
        idx = np.array([j for j in range(lo, hi) if j != i and not np.isnan(rel_yaw[j])])
        if idx.shape[0] < YAW_TRACK_MIN_INLIERS:
            continue
        # unwrap the neighbour yaws about their median so a robust line can be fit, then fit a
        # line t->yaw and predict at t_s[i]. Reject the flipped minority and refit once.
        yv = rel_yaw[idx]
        base = yv[len(yv) // 2]
        yu = base + G.yaw_diff_deg(yv, base)            # locally unwrapped (no 360 wrap in-window)
        tt = t_s[idx]
        keep = np.ones(idx.shape[0], dtype=bool)
        pred = np.nan
        for _it in range(2):
            if int(keep.sum()) < YAW_TRACK_MIN_INLIERS:
                break
            A = np.vstack([tt[keep], np.ones(int(keep.sum()))]).T
            coef, *_ = np.linalg.lstsq(A, yu[keep], rcond=None)
            fit = coef[0] * tt + coef[1]
            r = np.abs(yu - fit)
            keep = r <= YAW_TRACK_INLIER_TOL
            pred = coef[0] * t_s[i] + coef[1]
        if int(keep.sum()) < YAW_TRACK_MIN_INLIERS or not np.isfinite(pred):
            continue
        track[i] = (pred + 180.0) % 360.0 - 180.0
        resid[i] = abs(float(G.yaw_diff_deg(rel_yaw[i], pred)))
        inliers[i] = int(keep.sum())
    return track, resid, inliers


def headpose_anchor_device(t_ns, quat_opt, g_body, head_quat,
                           tilt_tol=TILT_TOL_DEG, flip_deg=FLIP_DEG,
                           yaw_flip_tol=YAW_FLIP_TOL_DEG) -> AnchorResult:
    """Core independent anchor on already-extracted per-device arrays (all (N,...), sorted by t_ns).

    quat_opt  : (N,4) recorded controller world orientation R_world_obj (fusion.opt_*)
    g_body    : (N,3) controller body-frame gravity from the accel (NaN where not static)
    head_quat : (N,4) SLAM head world orientation R_world_imu at each frame (NaN if missing)
    """
    N = t_ns.shape[0]
    quat_opt = G.quat_normalize(quat_opt.astype(float))

    # --- TILT cue (independent: world-down is SLAM-validated, body-gravity from the accel) ---
    g_carry = _static_body_gravity_carry(t_ns, g_body)
    tilt = np.full(N, np.nan)
    havg = ~np.isnan(g_carry[:, 0])
    if havg.any():
        tilt[havg] = G.gravity_tilt_err_deg(quat_opt[havg], g_carry[havg])

    # --- YAW cue (independent: heading relative to the non-drifting head yaw) ---
    have_head = ~np.isnan(head_quat[:, 0])
    rel_yaw = np.full(N, np.nan)
    if have_head.any():
        cy = G.world_yaw_deg(quat_opt[have_head])
        hy = G.world_yaw_deg(G.quat_normalize(head_quat[have_head]))
        rel_yaw[have_head] = G.yaw_diff_deg(cy, hy)
    yaw_track, yaw_resid, yaw_inl = _head_relative_yaw_track(rel_yaw, t_ns)

    # --- per-frame branch verdict, fusing the two cues; abstain on disagreement ---
    verdict = np.full(N, A_ABSTAIN, dtype=int)
    tilt_obs = ~np.isnan(tilt)
    yaw_obs = ~np.isnan(yaw_resid)

    tilt_bad = tilt_obs & (tilt > flip_deg)              # gravity: clear tilt flip (>=90)
    tilt_ok = tilt_obs & (tilt <= tilt_tol)              # gravity: tilt is right
    tilt_mild = tilt_obs & (tilt > tilt_tol) & (tilt <= flip_deg)  # ambiguous tilt
    yaw_bad = yaw_obs & (yaw_resid >= yaw_flip_tol)      # head: clear yaw flip
    yaw_ok = yaw_obs & (yaw_resid < tilt_tol)            # head: yaw on the track

    for i in range(N):
        if tilt_bad[i] and yaw_bad[i]:
            verdict[i] = A_TILT_FLIP            # both flag it; tilt is the stronger physical cue
        elif tilt_bad[i] and not yaw_bad[i] and not yaw_ok[i]:
            verdict[i] = A_TILT_FLIP
        elif yaw_bad[i] and not tilt_bad[i] and not tilt_mild[i]:
            verdict[i] = A_YAW_FLIP            # gravity-blind flip the in-stream tilt cue misses
        elif tilt_ok[i] and (yaw_ok[i] or not yaw_obs[i]):
            verdict[i] = A_GOOD
        elif yaw_ok[i] and (tilt_ok[i] or not tilt_obs[i]):
            verdict[i] = A_GOOD
        else:
            verdict[i] = A_ABSTAIN            # cues disagree or neither is observable

    stats = dict(
        n=N,
        n_head=int(have_head.sum()),
        n_tilt_obs=int(tilt_obs.sum()),
        n_yaw_obs=int(yaw_obs.sum()),
        n_good=int((verdict == A_GOOD).sum()),
        n_tilt_flip=int((verdict == A_TILT_FLIP).sum()),
        n_yaw_flip=int((verdict == A_YAW_FLIP).sum()),
        n_abstain=int((verdict == A_ABSTAIN).sum()),
    )
    return AnchorResult(device_id=-1, t_ns=t_ns, verdict=verdict, tilt_err=tilt,
                        yaw_resid=yaw_resid, head_quat=head_quat, stats=stats)


def _extract_device(d, m, dev):
    """Accepted optical poses + accel body-gravity + per-frame head quat for one controller."""
    fu = G.load_stream(d, m, "fusion")
    imu = G.load_stream(d, m, "imu")
    acc = fu[(fu["device_id"] == dev) & (fu["outcome"] == 1)]
    if acc.shape[0] < 3:
        return None
    t = acc["t_mono_ns"].astype(np.int64)
    order = np.argsort(t)
    acc, t = acc[order], t[order]
    quat = np.stack([acc["opt_qx"], acc["opt_qy"], acc["opt_qz"], acc["opt_qw"]], axis=1)
    g_body = _body_gravity(t, imu[imu["device_id"] == dev])
    hp = load_head_pose(d, m)
    head_quat = hp.quat_at(t) if hp is not None else np.full((t.shape[0], 4), np.nan)
    return t, quat, g_body, head_quat, (hp.n_dropped_warmup if hp else 0)


def anchor_from_capture(d, dev) -> AnchorResult | None:
    d = Path(d)
    m = Manifest.load(d)
    ex = _extract_device(d, m, dev)
    if ex is None:
        return None
    t, quat, g_body, head_quat, n_drop = ex
    res = headpose_anchor_device(t, quat, g_body, head_quat)
    res.device_id = dev
    res.stats["n_dropped_warmup"] = n_drop
    return res


def score_candidate_against_anchor(d, dev, t_cand, quat_cand) -> dict | None:
    """Score an ARBITRARY candidate orientation stream (e.g. an offline_vio_replay optical CSV)
    against the independent head-pose anchor of capture `d`, controller `dev`.

    Returns the candidate's independently-judged tilt-flip and pure-YAW-flip rates: the share of
    candidate frames whose tilt is on the wrong gravity branch (>flip_deg) and whose head-relative
    heading is ~180 off its own robust track. The yaw-flip rate is the A3 headline -- a residual
    measured against a NON-DRIFTING yaw reference, not the self-referential cleaned-GT."""
    d = Path(d)
    m = Manifest.load(d)
    imu = G.load_stream(d, m, "imu")
    t_cand = np.asarray(t_cand, dtype=np.int64)
    quat_cand = G.quat_normalize(np.asarray(quat_cand, dtype=float))
    order = np.argsort(t_cand)
    t_cand, quat_cand = t_cand[order], quat_cand[order]
    fin = np.isfinite(quat_cand).all(1) & (np.linalg.norm(quat_cand, axis=1) > 0.5)
    t_cand, quat_cand = t_cand[fin], quat_cand[fin]
    if t_cand.shape[0] < 3:
        return None

    g_body = _body_gravity(t_cand, imu[imu["device_id"] == dev])
    hp = load_head_pose(d, m)
    head_quat = hp.quat_at(t_cand) if hp is not None else np.full((t_cand.shape[0], 4), np.nan)
    a = headpose_anchor_device(t_cand, quat_cand, g_body, head_quat)
    s = a.stats
    judged = s["n"] - s["n_abstain"]
    return dict(
        device_id=dev,
        n=s["n"],
        n_judged=judged,
        n_good=s["n_good"],
        n_tilt_flip=s["n_tilt_flip"],
        n_yaw_flip=s["n_yaw_flip"],
        n_abstain=s["n_abstain"],
        tilt_flip_rate_pct=100.0 * s["n_tilt_flip"] / max(judged, 1),
        yaw_flip_rate_pct=100.0 * s["n_yaw_flip"] / max(judged, 1),
        wrong_branch_rate_pct=100.0 * (s["n_tilt_flip"] + s["n_yaw_flip"]) / max(judged, 1),
    )


def deflipper_miss_rate(d, dev) -> dict | None:
    """Measure the de-flipper's miss-rate against the INDEPENDENT head-pose anchor: of the
    frames the anchor independently calls a flip (tilt or yaw), how many did the de-flipper
    leave on the wrong branch? This is the reference's published trust bound.

    A de-flipper output is 'still wrong' at frame i if, after de-flipping, the cleaned
    orientation still violates the anchor's verdict there (tilt still >flip_deg for a tilt
    call; head-relative yaw still ~180 off the track for a yaw call)."""
    d = Path(d)
    m = Manifest.load(d)
    ex = _extract_device(d, m, dev)
    if ex is None:
        return None
    t, quat, g_body, head_quat, _ = ex
    anchor = headpose_anchor_device(t, quat, g_body, head_quat)

    # run the actual de-flipper on the same frames (its own cues), get the cleaned orientation
    pa = G.load_stream(d, m, "pose_attempt")
    pos = np.zeros((t.shape[0], 3))  # position is irrelevant to the orientation miss check
    conf_mb, conf_rp = _join_confidence(t, pa[pa["device_id"] == dev])
    de = deflip_device(t, pos, quat, g_body, conf_mb, conf_rp)
    quat_clean = G.quat_normalize(de.quat)

    g_carry = _static_body_gravity_carry(t, g_body)
    have_head = ~np.isnan(head_quat[:, 0])

    def tilt_after(i):
        if np.isnan(g_carry[i, 0]):
            return np.nan
        return float(G.gravity_tilt_err_deg(quat_clean[i][None, :], g_carry[i][None, :])[0])

    # tilt misses: anchor called TILT_FLIP, cleaned tilt still > flip_deg
    tilt_calls = np.where(anchor.verdict == A_TILT_FLIP)[0]
    tilt_miss = [i for i in tilt_calls if (ta := tilt_after(i)) is not None
                 and not np.isnan(ta) and ta > FLIP_DEG]

    # yaw misses: anchor called YAW_FLIP, cleaned head-relative yaw STILL ~180 off the track.
    # rebuild the cleaned head-relative yaw track to compare against (same robust estimator).
    rel_clean = np.full(t.shape[0], np.nan)
    if have_head.any():
        cy = G.world_yaw_deg(quat_clean[have_head])
        hy = G.world_yaw_deg(G.quat_normalize(head_quat[have_head]))
        rel_clean[have_head] = G.yaw_diff_deg(cy, hy)
    _, resid_clean, _ = _head_relative_yaw_track(rel_clean, t)
    yaw_calls = np.where(anchor.verdict == A_YAW_FLIP)[0]
    yaw_miss = [i for i in yaw_calls
                if not np.isnan(resid_clean[i]) and resid_clean[i] >= YAW_FLIP_TOL_DEG]

    n_calls = int(tilt_calls.shape[0] + yaw_calls.shape[0])
    n_miss = len(tilt_miss) + len(yaw_miss)
    return dict(
        device_id=dev,
        n_anchor_tilt_calls=int(tilt_calls.shape[0]),
        n_anchor_yaw_calls=int(yaw_calls.shape[0]),
        n_anchor_calls=n_calls,
        n_tilt_miss=len(tilt_miss),
        n_yaw_miss=len(yaw_miss),
        n_miss=n_miss,
        miss_rate_pct=100.0 * n_miss / max(n_calls, 1),
        tilt_miss_rate_pct=100.0 * len(tilt_miss) / max(int(tilt_calls.shape[0]), 1),
        yaw_miss_rate_pct=100.0 * len(yaw_miss) / max(int(yaw_calls.shape[0]), 1),
        anchor_stats=anchor.stats,
    )


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    d = sys.argv[1]
    hp = load_head_pose(d)
    if hp is None:
        print(f"no usable head_pose.bin in {d} (this capture cannot supply the independent anchor)")
        return 1
    span_s = (hp.t_ns[-1] - hp.t_ns[0]) / 1e9
    print(f"head_pose: {hp.t_ns.shape[0]} world poses over {span_s:.1f}s "
          f"(dropped {hp.n_dropped_warmup} stale warm-up rows)")
    print()
    for dev in (1, 2):
        a = anchor_from_capture(d, dev)
        if a is None:
            print(f"device {dev}: <3 accepted optical poses, skipped\n")
            continue
        s = a.stats
        print(f"==== device {dev} ({DEVICE_NAMES[dev]}) independent head-pose anchor ====")
        print(f"  accepted optical poses : {s['n']}  (head pose present: {s['n_head']})")
        print(f"  tilt-observable: {s['n_tilt_obs']}  yaw-observable: {s['n_yaw_obs']}")
        print(f"  verdict: GOOD={s['n_good']}  TILT_FLIP={s['n_tilt_flip']}  "
              f"YAW_FLIP={s['n_yaw_flip']}  ABSTAIN={s['n_abstain']}")
        mr = deflipper_miss_rate(d, dev)
        print(f"  -- de-flipper trust bound (independent) --")
        print(f"  anchor flip calls: {mr['n_anchor_calls']} "
              f"(tilt {mr['n_anchor_tilt_calls']}, yaw {mr['n_anchor_yaw_calls']})")
        print(f"  de-flipper MISSED: {mr['n_miss']}  "
              f"(tilt {mr['n_tilt_miss']}, yaw {mr['n_yaw_miss']})")
        print(f"  ** de-flipper MISS-RATE vs independent anchor = {mr['miss_rate_pct']:.2f}% "
              f"(tilt {mr['tilt_miss_rate_pct']:.2f}%, yaw {mr['yaw_miss_rate_pct']:.2f}%) **")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
