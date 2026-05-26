#!/usr/bin/env python3
"""smooth_ref.py -- batch RTS smoother that turns the de-flipped controller poses into a
continuous "cleaned ground-truth reference" trajectory (position + orientation).

Pipeline per controller:
  1. de-flip (deflip.py) -> cleaned poses + quality flags.
  2. SPEED GATE: drop poses whose implied speed from the previous kept pose exceeds a
     physical bound (MAX_SPEED + slack/gap) -- a degenerate PnP fly-away. Mirrors the
     gate already proven in tests/eskf_coast_experiment.cpp.
  3. POSITION: a constant-velocity Kalman filter + Rauch-Tung-Striebel backward smoother,
     run over the kept poses on their real timestamps. Measurement noise is scaled up for
     low-quality (CORRECTED/UNCERTAIN) poses so the smoother leans on the trusted ones.
  4. ORIENTATION: tangent-space (rotation-vector) smoothing about a per-pose reference,
     forward-backward averaged, re-exponentiated to unit quats. Also quality-weighted.

The result is a per-controller reference: t_ns, pos, quat, valid (bool), quality.
"valid" is False inside long gaps (no nearby measurement) where the reference is an
extrapolation and must not be scored against.

Reuse: build_reference(telemetry_dir, dev) returns a Reference; mse_eval.py consumes it.

Usage: smooth_ref.py <telemetry_dir> [--write]  (--write dumps reference_<dev>.npz to capture dir)
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from deflip import deflip_from_capture, Q_GOOD, Q_OK, Q_CORRECTED, Q_UNCERTAIN
import g2_geom as G
from manifest import DEVICE_NAMES

# --- physical gates / smoother knobs ---
MAX_SPEED_M_S = 4.0       # controller hand speed bound (matches eskf_coast_experiment)
SPEED_MARGIN_M = 0.30     # additive slack for the speed gate
MAX_GAP_VALID_MS = 150.0  # reference is "valid" only within this of a kept measurement
# constant-velocity process noise (accel PSD, m^2/s^3) and base measurement noise (m)
ACCEL_PSD = 8.0           # how much the velocity is allowed to wander
MEAS_STD_GOOD_M = 0.01    # high-confidence position measurement std
MEAS_STD_OK_M = 0.02
MEAS_STD_CORR_M = 0.05    # corrected/uncertain poses trusted less
# orientation smoothing window (samples each side) and per-quality weights
ORI_WIN = 5
QUAL_W = {Q_GOOD: 1.0, Q_OK: 0.6, Q_CORRECTED: 0.25, Q_UNCERTAIN: 0.1}
ORI_MEAS_STD_DEG = {Q_GOOD: 1.0, Q_OK: 2.0, Q_CORRECTED: 6.0, Q_UNCERTAIN: 15.0}


@dataclass
class Reference:
    device_id: int
    t_ns: np.ndarray     # (M,) kept + smoothed sample times
    pos: np.ndarray      # (M,3)
    quat: np.ndarray     # (M,4) x,y,z,w
    valid: np.ndarray    # (M,) bool -- safe to score against
    quality: np.ndarray  # (M,) source quality flag
    stats: dict


def _speed_gate(t_ns, pos, quat, quality):
    """Keep poses whose step speed from the last kept pose is physically plausible."""
    N = t_ns.shape[0]
    keep = np.ones(N, dtype=bool)
    last = 0
    for i in range(1, N):
        gap_s = (t_ns[i] - t_ns[last]) / 1e9
        if gap_s <= 0:
            keep[i] = False  # duplicate / backwards timestamp
            continue
        moved = float(np.linalg.norm(pos[i] - pos[last]))
        if moved > MAX_SPEED_M_S * gap_s + SPEED_MARGIN_M:
            keep[i] = False
            continue
        last = i
    return keep


def _rts_position(t_ns, pos, meas_std):
    """Constant-velocity Kalman filter + RTS smoother on irregular timestamps.
    State = [px,py,pz,vx,vy,vz]. Returns smoothed positions (M,3)."""
    M = t_ns.shape[0]
    ns = 6
    xs = np.zeros((M, ns))
    Ps = np.zeros((M, ns, ns))
    # init
    x = np.zeros(ns)
    x[:3] = pos[0]
    P = np.eye(ns)
    P[:3, :3] *= (meas_std[0] ** 2)
    P[3:, 3:] *= 1.0
    xf = np.zeros((M, ns))
    Pf = np.zeros((M, ns, ns))
    xp = np.zeros((M, ns))
    Pp = np.zeros((M, ns, ns))
    H = np.zeros((3, ns))
    H[:3, :3] = np.eye(3)
    for k in range(M):
        if k == 0:
            dt = 0.0
        else:
            dt = (t_ns[k] - t_ns[k - 1]) / 1e9
        F = np.eye(ns)
        F[0, 3] = F[1, 4] = F[2, 5] = dt
        # process noise (continuous white-accel CV model)
        q = ACCEL_PSD
        dt2, dt3 = dt * dt, dt * dt * dt
        Qb = np.array([[dt3 / 3, dt2 / 2], [dt2 / 2, dt]]) * q
        Q = np.zeros((ns, ns))
        for a in range(3):
            Q[a, a] = Qb[0, 0]
            Q[a, a + 3] = Qb[0, 1]
            Q[a + 3, a] = Qb[1, 0]
            Q[a + 3, a + 3] = Qb[1, 1]
        # predict
        x = F @ x
        P = F @ P @ F.T + Q
        xp[k] = x
        Pp[k] = P
        # update with measurement
        R = np.eye(3) * (meas_std[k] ** 2)
        S = H @ P @ H.T + R
        K = P @ H.T @ np.linalg.inv(S)
        innov = pos[k] - H @ x
        x = x + K @ innov
        P = (np.eye(ns) - K @ H) @ P
        xf[k] = x
        Pf[k] = P
    # RTS backward pass
    xs[-1] = xf[-1]
    Ps[-1] = Pf[-1]
    for k in range(M - 2, -1, -1):
        dt = (t_ns[k + 1] - t_ns[k]) / 1e9
        F = np.eye(ns)
        F[0, 3] = F[1, 4] = F[2, 5] = dt
        C = Pf[k] @ F.T @ np.linalg.inv(Pp[k + 1])
        xs[k] = xf[k] + C @ (xs[k + 1] - xp[k + 1])
        Ps[k] = Pf[k] + C @ (Ps[k + 1] - Pp[k + 1]) @ C.T
    return xs[:, :3]


def _smooth_orientation(t_ns, quat, quality):
    """Local quality-weighted tangent-space averaging of orientations.
    For each pose, average the log of nearby quats (canonicalized to its hemisphere)
    weighted by quality and a temporal Gaussian, then re-exponentiate. Forward-backward
    symmetric window -> non-causal smoothing."""
    M = quat.shape[0]
    q = G.quat_normalize(quat.astype(float))
    out = q.copy()
    # temporal scale ~ median dt
    dts = np.diff(t_ns) / 1e9
    tau = max(np.median(dts) * 2.0, 1e-3) if dts.size else 1.0
    for i in range(M):
        lo = max(0, i - ORI_WIN)
        hi = min(M, i + ORI_WIN + 1)
        ref = q[i]
        acc = np.zeros(3)
        wsum = 0.0
        for j in range(lo, hi):
            qj = G.quat_canonical_sign(q[j], ref)
            tw = np.exp(-0.5 * ((t_ns[j] - t_ns[i]) / 1e9 / tau) ** 2)
            qw = QUAL_W.get(int(quality[j]), 0.1)
            w = tw * qw
            # work in the tangent space about ref: log(ref^-1 * qj)
            dq = G.quat_mul(G.quat_conj(ref), qj)
            acc += w * G.quat_log(dq)
            wsum += w
        if wsum > 0:
            mean_rv = acc / wsum
            out[i] = G.quat_normalize(G.quat_mul(ref, G.quat_exp(mean_rv)))
    return out


def build_reference(telemetry_dir, dev) -> Reference | None:
    res = deflip_from_capture(telemetry_dir, dev)
    if res is None:
        return None
    t = res.t_ns
    pos = res.pos
    quat = res.quat
    qual = res.quality

    keep = _speed_gate(t, pos, quat, qual)
    tk, pk, qk, qlk = t[keep], pos[keep], quat[keep], qual[keep]
    M = tk.shape[0]
    if M < 3:
        return None

    meas_std = np.array([
        MEAS_STD_GOOD_M if v == Q_GOOD else
        MEAS_STD_OK_M if v == Q_OK else MEAS_STD_CORR_M
        for v in qlk
    ])
    pos_s = _rts_position(tk, pk, meas_std)
    quat_s = _smooth_orientation(tk, qk, qlk)

    # validity: a sample is valid if it has a kept measurement within MAX_GAP_VALID_MS
    # on at least one side (i.e. it is interpolation, not long extrapolation).
    valid = np.ones(M, dtype=bool)
    for i in range(M):
        prev_gap = (tk[i] - tk[i - 1]) / 1e6 if i > 0 else 0.0
        next_gap = (tk[i + 1] - tk[i]) / 1e6 if i < M - 1 else 0.0
        # endpoints valid; interior invalid only if BOTH neighbours are far
        if i == 0:
            valid[i] = next_gap <= MAX_GAP_VALID_MS
        elif i == M - 1:
            valid[i] = prev_gap <= MAX_GAP_VALID_MS
        else:
            valid[i] = (prev_gap <= MAX_GAP_VALID_MS) or (next_gap <= MAX_GAP_VALID_MS)

    stats = dict(
        n_raw=res.stats["n"],
        n_kept=int(M),
        n_speed_rejected=int(res.stats["n"] - M),
        n_valid=int(valid.sum()),
        n_good=int((qlk == Q_GOOD).sum()),
        n_corrected=int((qlk == Q_CORRECTED).sum()),
        n_uncertain=int((qlk == Q_UNCERTAIN).sum()),
        deflip=res.stats,
    )
    return Reference(device_id=dev, t_ns=tk, pos=pos_s, quat=quat_s,
                     valid=valid, quality=qlk, stats=stats)


def _plot_reference(d, dev, ref, res):
    """Save a diagnostic PNG: position track + gravity-tilt raw vs cleaned/smoothed."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    static = ~np.isnan(res.g_body[:, 0])
    traw = np.full(res.stats["n"], np.nan)
    traw[static] = G.gravity_tilt_err_deg(res.quat_raw[static], res.g_body[static])
    gset = {int(t): res.g_body[i] for i, t in enumerate(res.t_ns)}
    tref = np.full(ref.t_ns.shape[0], np.nan)
    for k, t in enumerate(ref.t_ns):
        g = gset.get(int(t))
        if g is not None and not np.isnan(g[0]):
            tref[k] = G.gravity_tilt_err_deg(ref.quat[k][None, :], g[None, :])[0]

    t0 = res.t_ns[0]
    fig, ax = plt.subplots(2, 1, figsize=(11, 6))
    ax[0].plot((res.t_ns - t0) / 1e9, res.pos_raw[:, 0], ".", ms=2, alpha=0.4, label="raw px")
    ax[0].plot((ref.t_ns - t0) / 1e9, ref.pos[:, 0], "-", lw=1, label="smoothed px")
    ax[0].plot((ref.t_ns - t0) / 1e9, ref.pos[:, 1], "-", lw=1, label="smoothed py")
    ax[0].plot((ref.t_ns - t0) / 1e9, ref.pos[:, 2], "-", lw=1, label="smoothed pz")
    ax[0].set_ylabel("position (m)"); ax[0].legend(fontsize=8, ncol=4); ax[0].set_title(
        f"device {dev} ({DEVICE_NAMES[dev]}) cleaned-GT reference")
    ax[1].plot((res.t_ns - t0) / 1e9, traw, ".", ms=2, alpha=0.4, label="raw gravity-tilt")
    ax[1].plot((ref.t_ns - t0) / 1e9, tref, "-", lw=1, label="cleaned gravity-tilt")
    ax[1].axhline(25, color="r", ls="--", lw=0.8, label="TILT_TOL")
    ax[1].set_ylabel("gravity-tilt (deg)"); ax[1].set_xlabel("t (s)")
    ax[1].legend(fontsize=8); ax[1].set_ylim(0, 185)
    fig.tight_layout()
    out = Path(d) / f"reference_{DEVICE_NAMES[dev]}.png"
    fig.savefig(out, dpi=110)
    plt.close(fig)
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    d = sys.argv[1]
    write = "--write" in sys.argv[2:]
    plot = "--plot" in sys.argv[2:]
    for dev in (1, 2):
        ref = build_reference(d, dev)
        if ref is None:
            print(f"device {dev}: insufficient poses, skipped")
            continue
        s = ref.stats
        print(f"==== device {dev} ({DEVICE_NAMES[dev]}) reference ====")
        print(f"  raw optical poses : {s['n_raw']}")
        print(f"  speed-gate rejected: {s['n_speed_rejected']}")
        print(f"  kept + smoothed   : {s['n_kept']}  (valid: {s['n_valid']})")
        print(f"  source quality    : GOOD={s['n_good']} CORRECTED={s['n_corrected']} "
              f"UNCERTAIN={s['n_uncertain']}")
        # smoothing magnitude (how far the smoother moved positions)
        if write:
            out = Path(d) / f"reference_{DEVICE_NAMES[dev]}.npz"
            np.savez(out, t_ns=ref.t_ns, pos=ref.pos, quat=ref.quat,
                     valid=ref.valid, quality=ref.quality, device_id=dev)
            print(f"  wrote {out}")
        if plot:
            from deflip import deflip_from_capture
            res = deflip_from_capture(d, dev)
            png = _plot_reference(d, dev, ref, res)
            print(f"  wrote {png}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
