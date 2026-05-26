#!/usr/bin/env python3
"""deflip.py -- flip detector + de-flipper for the G2 controller optical pose stream.

INPUT  : the recorded WORLD-frame optical poses (fusion.bin opt_*, accepted folds),
         per controller, plus the controller IMU (imu.bin) for the gravity cue.
OUTPUT : a per-controller cleaned pose sequence + a per-pose quality flag.

A "flip" is a mirror-twin mis-selection by the live front-end: two consecutive
accepted optical poses differ by a physically-impossible orientation jump (>=90 deg
in <120 ms). We CANNOT recover the alternate twin (it was never recorded), so the
de-flipper instead identifies the majority-consistent orientation TRACK and, for each
detected flip, decides which side of the jump is wrong using three cues, then CORRECTS
the wrong pose toward the trusted track (SLERP of trusted neighbours) and downgrades
its quality flag.

Cues (in priority order):
  (a) DRIFTLESS GRAVITY-TILT (the dominant one -- 92% of flips are tilt flips):
      the controller accel pins gravity in BODY frame. A pose whose predicted world
      gravity (R_world_obj . g_body) deviates from world-down by > TILT_TOL has the
      wrong tilt branch. Gated to low-acceleration samples (|a| ~ g) where specific
      force ~= gravity; motion samples are abstained (gravity unobservable).
  (b) TEMPORAL CONTINUITY: the branch consistent with the non-flipped neighbours
      (the majority side of the jump) is kept; the minority side is the flip.
  (c) HIGH-CONFIDENCE ANCHORS: poses with many matched LEDs + low reproj
      (from pose_attempt.bin, joined by hw_ts_ns) are trusted; low-confidence poses
      are corrected toward them and never used to overrule a high-confidence neighbour.

Quality flags (per cleaned pose):
  GOOD       2  high-confidence, not flipped, gravity-consistent
  OK         1  accepted, consistent, but lower confidence (few LEDs / higher reproj)
  CORRECTED -1  was a detected flip; orientation replaced by the trusted-track estimate
  UNCERTAIN -2  flip detected but cue agreement was weak/ambiguous (correction is a guess)

This module exposes deflip_device() for reuse by the smoother and the MSE evaluator.
Run standalone for a per-controller flip report.

Usage: deflip.py <telemetry_dir> [flip_deg=90] [gap_ms=120]
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from manifest import Manifest, DEVICE_NAMES
import g2_geom as G

# --- tuning (few knobs; derived where possible) ---
FLIP_DEG = 90.0          # consecutive jump that is a physically-impossible orientation change
GAP_MS = 120.0           # ... within this window (real frame-to-frame motion is << 90 deg/120ms)
TILT_TOL_DEG = 25.0      # driftless gravity-tilt veto (per 10-gravity-twin doc); a tilt flip is >= ~90
ACCEL_STATIC_TOL = 0.20  # |a|/g within +-20% => specific force ~ gravity (cue usable)
HICONF_LEDS = 6          # >= this many matched blobs + low reproj => trusted anchor
HICONF_REPROJ_PX = 1.5
JOIN_MS = 33.0           # pose_attempt<->fusion join window (one frame)

# quality flags
Q_GOOD = 2
Q_OK = 1
Q_CORRECTED = -1
Q_UNCERTAIN = -2


@dataclass
class DeflipResult:
    device_id: int
    t_ns: np.ndarray          # (N,) sorted monotonic times of accepted optical poses
    pos_raw: np.ndarray       # (N,3) raw world positions
    quat_raw: np.ndarray      # (N,4) raw world quats (x,y,z,w)
    pos: np.ndarray           # (N,3) cleaned positions
    quat: np.ndarray          # (N,4) cleaned quats
    quality: np.ndarray       # (N,) quality flag
    flip_idx: np.ndarray      # indices flagged as flips (raw)
    g_body: np.ndarray        # (N,3) body-frame gravity estimate at each pose (nan if not static)
    stats: dict = field(default_factory=dict)


def _join_confidence(t_opt, pa_dev):
    """For each optical pose time, return (blobs_matched, reproj) from the nearest
    accepted pose_attempt by hw_ts_ns within JOIN_MS, else (-1, inf)."""
    # pose_attempt rows: use hw_ts_ns (frame time) per audit T6 -- it is the cross-stream key.
    keep = np.isin(pa_dev["outcome"], (1, 2))
    pa = pa_dev[keep]
    ht = pa["hw_ts_ns"].astype(np.int64)
    order = np.argsort(ht)
    ht = ht[order]
    mb = pa["blobs_matched"][order].astype(int)
    rp = pa["reproj_err_px"][order].astype(float)
    out_mb = np.full(t_opt.shape[0], -1, dtype=int)
    out_rp = np.full(t_opt.shape[0], np.inf)
    for k, t in enumerate(t_opt):
        # fusion opt_ time is the observation (frame) time too, so match against hw_ts_ns
        j = np.searchsorted(ht, t)
        best = -1
        for cand in (j - 1, j):
            if 0 <= cand < ht.shape[0] and abs(int(ht[cand]) - int(t)) <= JOIN_MS * 1e6:
                if best < 0 or abs(int(ht[cand]) - int(t)) < abs(int(ht[best]) - int(t)):
                    best = cand
        if best >= 0:
            out_mb[k] = mb[best]
            out_rp[k] = rp[best]
    return out_mb, out_rp


def _body_gravity(t_opt, imu_dev):
    """Body-frame gravity unit vector at each optical pose time, from the nearest IMU
    sample IF that sample is near-static (|a| ~ g). Returns (N,3); rows are NaN where
    the cue is not usable (controller accelerating). The IMU body frame == optical body
    frame for the G2 controllers (verified: best-fit rotation is ~identity)."""
    t = imu_dev["t_mono_ns"].astype(np.int64)
    order = np.argsort(t)
    t = t[order]
    a = np.stack([imu_dev["ax"], imu_dev["ay"], imu_dev["az"]], axis=1)[order]
    amag = np.linalg.norm(a, axis=1)
    out = np.full((t_opt.shape[0], 3), np.nan)
    for k, tt in enumerate(t_opt):
        j = np.searchsorted(t, tt)
        j = min(max(j, 0), t.shape[0] - 1)
        # specific force ~= -gravity_body when static; gravity_body = -a_hat
        if amag[j] <= 1e-6:
            continue
        if abs(amag[j] / G.G_MS2 - 1.0) <= ACCEL_STATIC_TOL:
            out[k] = -a[j] / amag[j]
    return out


def _jump_neighbour(t_ns, quat, flip_deg, gap_ms):
    """Per-pose bool: True if the pose makes a >=flip_deg jump from EITHER temporal
    neighbour within gap_ms (used to mark gravity-blind poses that still flip)."""
    N = t_ns.shape[0]
    out = np.zeros(N, dtype=bool)
    for i in range(N):
        for nb in (i - 1, i + 1):
            if 0 <= nb < N and abs(t_ns[i] - t_ns[nb]) / 1e6 <= gap_ms \
                    and G.quat_geodesic_deg(quat[i], quat[nb]) >= flip_deg:
                out[i] = True
                break
    return out


def deflip_device(t_ns, pos, quat, g_body, conf_mb, conf_rp,
                  flip_deg=FLIP_DEG, gap_ms=GAP_MS) -> DeflipResult:
    """Core de-flip on already-extracted per-device arrays. All inputs (N,...) sorted by t_ns.

    Strategy (non-causal, full trajectory in hand):
      1. Score every pose's orientation as TRUSTABLE or SUSPECT using the driftless
         gravity-tilt cue: a static pose with tilt > TILT_TOL is on the wrong twin
         branch (real frame-to-frame tilt << TILT_TOL; a tilt flip is >= ~90 deg).
      2. Promote high-confidence (many LEDs, low reproj), gravity-OK poses to ANCHORS.
      3. For each SUSPECT pose, correct its orientation by SLERP-interpolating the
         nearest trustable neighbours on each side (temporal continuity + confidence).
         Decide confidence of the correction from cue agreement:
           - gravity says suspect AND a trustable neighbour exists  -> CORRECTED
           - no gravity available (pure-yaw / motion) but it jumps   -> UNCERTAIN
      A residual consecutive-jump check then catches any leftover (gravity-blind) flips.
    """
    N = t_ns.shape[0]
    quat = G.quat_normalize(quat.astype(float))
    pos = pos.astype(float).copy()

    hiconf = (conf_mb >= HICONF_LEDS) & (conf_rp <= HICONF_REPROJ_PX)

    # --- step 1: gravity-tilt error per pose (driftless cue) ---
    tilt = np.full(N, np.nan)
    static = ~np.isnan(g_body[:, 0])
    if static.any():
        tilt[static] = G.gravity_tilt_err_deg(quat[static], g_body[static])

    grav_ok = static & (tilt <= TILT_TOL_DEG)        # gravity says tilt is right
    grav_bad = static & (tilt > flip_deg)            # gravity says it's a tilt flip (>=90)
    grav_mild = static & (tilt > TILT_TOL_DEG) & (tilt <= flip_deg)  # ambiguous tilt

    # --- step 2: trustable set = gravity-OK poses; anchors = trustable + high-confidence ---
    trustable = grav_ok | (~static & ~_jump_neighbour(t_ns, quat, flip_deg, gap_ms))
    anchor = grav_ok & hiconf
    # if too few anchors, fall back to all trustable as anchors
    if anchor.sum() < max(3, N // 50):
        anchor = trustable.copy()

    quality = np.where(anchor, Q_GOOD, np.where(trustable, Q_OK, Q_UNCERTAIN)).astype(int)
    quat_clean = quat.copy()
    flip_marks = []

    trust_idx = np.where(trustable)[0]
    if trust_idx.shape[0] == 0:
        # no gravity reference anywhere: cannot de-flip; pass through, all UNCERTAIN
        quality[:] = Q_UNCERTAIN
        res = DeflipResult(device_id=-1, t_ns=t_ns, pos_raw=pos.copy(), quat_raw=quat.copy(),
                           pos=pos, quat=quat_clean, quality=quality,
                           flip_idx=np.array([], int), g_body=g_body)
        res.stats = dict(n=N, n_flip=0, n_corrected=0, n_uncertain=int(N),
                         n_hiconf=int(hiconf.sum()), n_static=int(static.sum()),
                         n_grav_consistent=0)
        return res

    # --- step 3: iteratively correct suspect poses toward trustable neighbours ---
    # Non-causal: each pass lets newly-corrected (now gravity-consistent) poses join the
    # trustable set, so sustained bad runs get filled from both ends inward. Converges
    # when no more poses flip into trust (capped to avoid runaway).
    corrected_mask = np.zeros(N, dtype=bool)
    for _pass in range(6):
        trust_idx = np.where(trustable)[0]
        if trust_idx.shape[0] == 0:
            break
        changed = False
        for i in range(N):
            if trustable[i]:
                continue
            left = trust_idx[trust_idx < i]
            right = trust_idx[trust_idx > i]
            lo = int(left[-1]) if left.shape[0] else -1
            hi = int(right[0]) if right.shape[0] else -1

            is_flip = grav_bad[i]
            if not is_flip:
                for nb in (lo, hi):
                    if nb >= 0 and abs(t_ns[i] - t_ns[nb]) / 1e6 <= gap_ms * 2 \
                            and G.quat_geodesic_deg(quat_clean[i], quat_clean[nb]) >= flip_deg:
                        is_flip = True
                        break
            if not is_flip:
                if grav_mild[i] and not corrected_mask[i]:
                    quality[i] = Q_OK
                continue

            # build the candidate correction (SLERP / nearest trustable)
            if lo >= 0 and hi >= 0:
                span = t_ns[hi] - t_ns[lo]
                frac = (t_ns[i] - t_ns[lo]) / span if span > 0 else 0.5
                cand = G.quat_slerp(quat_clean[lo], quat_clean[hi], float(frac))
            elif lo >= 0:
                cand = G.quat_canonical_sign(quat_clean[lo].copy(), quat[i])
            elif hi >= 0:
                cand = G.quat_canonical_sign(quat_clean[hi].copy(), quat[i])
            else:
                continue

            quat_clean[i] = cand
            corrected_mask[i] = True
            changed = True
            # gravity-confirmed wrong branch -> CORRECTED; gravity-blind jump -> UNCERTAIN
            quality[i] = Q_CORRECTED if grav_bad[i] else Q_UNCERTAIN
            # the correction is gravity-consistent by construction (neighbours are);
            # promote it to trustable so the next pass can lean on it.
            if static[i]:
                tc = G.gravity_tilt_err_deg(cand[None, :], g_body[i][None, :])[0]
                if tc <= TILT_TOL_DEG:
                    trustable[i] = True
            else:
                trustable[i] = True
        if not changed:
            break

    flip_marks = list(np.where(corrected_mask)[0])
    flip_idx = np.array(sorted(set(flip_marks)), dtype=int)

    stats = dict(
        n=N,
        n_flip=int(flip_idx.shape[0]),
        n_corrected=int((quality == Q_CORRECTED).sum()),
        n_uncertain=int((quality == Q_UNCERTAIN).sum()),
        n_hiconf=int(hiconf.sum()),
        n_static=int(static.sum()),
        n_grav_consistent=int(grav_ok.sum()),
    )
    return DeflipResult(
        device_id=-1, t_ns=t_ns, pos_raw=pos.copy(), quat_raw=quat.copy(),
        pos=pos, quat=quat_clean, quality=quality, flip_idx=flip_idx,
        g_body=g_body, stats=stats,
    )


def deflip_from_capture(telemetry_dir, dev, flip_deg=FLIP_DEG, gap_ms=GAP_MS) -> DeflipResult | None:
    """Load a capture and de-flip one controller (dev=1 left, 2 right)."""
    d = Path(telemetry_dir)
    m = Manifest.load(d)
    fu = G.load_stream(d, m, "fusion")
    pa = G.load_stream(d, m, "pose_attempt")
    imu = G.load_stream(d, m, "imu")

    acc = fu[(fu["device_id"] == dev) & (fu["outcome"] == 1)]
    if acc.shape[0] < 3:
        return None
    t = acc["t_mono_ns"].astype(np.int64)
    order = np.argsort(t)
    acc = acc[order]
    t = t[order]
    pos = np.stack([acc["opt_px"], acc["opt_py"], acc["opt_pz"]], axis=1)
    quat = np.stack([acc["opt_qx"], acc["opt_qy"], acc["opt_qz"], acc["opt_qw"]], axis=1)

    pa_dev = pa[pa["device_id"] == dev]
    conf_mb, conf_rp = _join_confidence(t, pa_dev)
    g_body = _body_gravity(t, imu[imu["device_id"] == dev])

    res = deflip_device(t, pos, quat, g_body, conf_mb, conf_rp, flip_deg, gap_ms)
    res.device_id = dev
    return res


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    d = sys.argv[1]
    flip_deg = float(sys.argv[2]) if len(sys.argv) > 2 else FLIP_DEG
    gap_ms = float(sys.argv[3]) if len(sys.argv) > 3 else GAP_MS

    for dev in (1, 2):
        res = deflip_from_capture(d, dev, flip_deg, gap_ms)
        if res is None:
            print(f"device {dev}: <3 accepted optical poses, skipped")
            continue
        s = res.stats
        # raw consecutive flip rate for context
        N = s["n"]
        raw_flips = 0
        ncons = 0
        for i in range(1, N):
            if (res.t_ns[i] - res.t_ns[i - 1]) / 1e6 <= gap_ms:
                ncons += 1
                if G.quat_geodesic_deg(res.quat_raw[i], res.quat_raw[i - 1]) >= flip_deg:
                    raw_flips += 1
        # post-deflip residual consecutive flip rate
        post_flips = 0
        npc = 0
        for i in range(1, N):
            if (res.t_ns[i] - res.t_ns[i - 1]) / 1e6 <= gap_ms:
                npc += 1
                if G.quat_geodesic_deg(res.quat[i], res.quat[i - 1]) >= flip_deg:
                    post_flips += 1
        print(f"==== device {dev} ({DEVICE_NAMES[dev]}): {N} accepted optical poses ====")
        print(f"  static (gravity-usable) poses: {s['n_static']}/{N}  "
              f"gravity-consistent: {s['n_grav_consistent']}")
        print(f"  high-confidence anchors (>= {HICONF_LEDS} LEDs, reproj <= {HICONF_REPROJ_PX}px): "
              f"{s['n_hiconf']}/{N}")
        print(f"  RAW consecutive flips (>= {flip_deg:.0f} deg, <= {gap_ms:.0f}ms): "
              f"{raw_flips}/{ncons} = {100.0*raw_flips/max(ncons,1):.2f}%")
        print(f"  flips detected & decided: {s['n_flip']}  "
              f"(corrected: {s['n_corrected']}, uncertain: {s['n_uncertain']})")
        print(f"  POST-deflip residual flips: {post_flips}/{npc} = "
              f"{100.0*post_flips/max(npc,1):.2f}%")
        deflip_rate = 100.0 * (raw_flips - post_flips) / max(raw_flips, 1)
        print(f"  de-flip success: {deflip_rate:.1f}% of raw flips removed")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
