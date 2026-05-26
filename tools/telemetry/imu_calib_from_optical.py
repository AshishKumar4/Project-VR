#!/usr/bin/env python3
"""imu_calib_from_optical.py -- measure residual IMU calibration errors that drive the
rotation-correlated inertial drift, using recorded controller IMU vs the optical pose as an
external truth. Decoupled from the filter (pure data); robust to optical mirror-flips.

Why: the deployed ESKF estimates accel scale + gyro BIAS (ZUPT/ZARU/ellipsoid) but NOT gyro
SCALE/misalignment. A gyro scale error s makes the integrated orientation over- or under-shoot
by (s-1) during rotation -> the gravity vector tilts -> g*sin(dtheta) leaks into horizontal
accel -> position drift that grows with how much you rotated. This tool checks whether such an
error exists, and how big, before we decide to model it.

Method (gyro): the optical relative body rotation is the TRUTH, the composed gyro rotation the
measurement. We accumulate flip-CLEANED, long (>=12deg) rotation segments — accepting a segment only
when the two rotations agree within a tolerance (this rejects ~180deg optical mirror-flips and bad
anchors) — then fit a physically-STRUCTURED M_g = R_md @ (scale*I): the misalignment rotation R_md by
Kabsch on the segment rotation-AXES (weighted by angle), and the scale by a robust median of
theta_opt/theta_gyro. The DOMINANT real-data threat is the constellation mirror-flip; structuring M_g
as rotation x scale and fitting it only on flip-cleaned long segments is robust to that, whereas an
unstructured per-short-interval 3x3 affine fit blows up under flips (its singular values leave the
plausibility band entirely), so that fit is printed only as a diagnostic and is NOT persisted. Honest
limitation: the scalar scale is NOISE-sensitive — optical orientation noise biases the angle-ratio a
few % high — so the misalignment is the trustworthy part; the scale is a bounded, guard-checked best
estimate. Validating M_g needs many clean, large rotations (low optical flip rate), not just motion.

Method (accel): at-rest samples (|gyro|<0.05 rad/s and |accel| within band) give per-axis
gravity -> |accel|/g scale and the body gravity direction spread.

The estimated intrinsics are persisted, when --write <serial> is given, to the SAME per-controller
cache the live ESKF loads (~/.config/monado/wmr/imu-cal-<serial>.txt, v2 line). The live filter applies
the gyro correction as w = M_g*(w_m - bg) and the accel correction as f = T_a*(a_m - ba) — exactly this
tool's M and T — so the two share one model (single source of truth). The cache's online-owned bias and
accel scale are preserved untouched (M is bias-independent: it is the slope, bias is the intercept).

Usage: imu_calib_from_optical.py <file.replay> [--write <serial>] [--config-dir <dir>]
"""
from __future__ import annotations

import os
import struct
import sys

import numpy as np

G = 9.80665


def fit_accel_ellipsoid(rest_accel):
    """Symmetric accel-ellipsoid correction T from at-rest samples, matching the live filter's
    fit_accel_calibration EXACTLY (single source of truth): solve symmetric A from vᵀAv=1 over the rest
    readings, then T = g·√A so |T·v| = g in every orientation. Returns (T, ok); ok mirrors the filter's
    guards (>=9 samples, directions spanning 3D, A positive-definite, T a plausibly-small correction)."""
    n = len(rest_accel)
    if n < 9:
        return np.eye(3), False
    dirs = rest_accel / np.linalg.norm(rest_accel, axis=1, keepdims=True)
    dcov = (dirs[:, :, None] * dirs[:, None, :]).mean(axis=0)
    if np.linalg.eigvalsh(dcov)[0] < 0.04:
        return np.eye(3), False  # directions ~coplanar -> unidentifiable
    s = rest_accel
    Mm = np.column_stack([s[:, 0] ** 2, s[:, 1] ** 2, s[:, 2] ** 2,
                          2 * s[:, 0] * s[:, 1], 2 * s[:, 0] * s[:, 2], 2 * s[:, 1] * s[:, 2]])
    p, *_ = np.linalg.lstsq(Mm, np.ones(n), rcond=None)
    A = np.array([[p[0], p[3], p[4]], [p[3], p[1], p[5]], [p[4], p[5], p[2]]])
    ev, V = np.linalg.eigh(A)
    if not np.all(np.isfinite(ev)) or ev[0] <= 1e-9:
        return np.eye(3), False
    T = G * (V @ np.diag(np.sqrt(ev)) @ V.T)
    evT = np.linalg.eigvalsh(T)
    if evT[0] < 0.8 or evT[-1] > 1.25:
        return np.eye(3), False  # a real correction is a small scaling; reject a wild (bad-data) fit
    return T, True


def write_cache(config_dir, serial, M_g, T_a, ta_ok):
    """Persist M_g (and T_a if it fit) into the live ESKF's v2 cache line, PRESERVING the cache's
    online-owned bias/scale (M is the bias-independent slope). Creates a fresh v2 record if none exists."""
    fn = "imu-cal-" + "".join(c if c.isalnum() or c in ".-_" else "_" for c in serial) + ".txt"
    path = os.path.join(config_dir, fn)
    bg, ba, scale, count = [0.0, 0.0, 0.0], [0.0, 0.0, 0.0], 1.0, 0
    ta = T_a if ta_ok else np.eye(3)
    if os.path.exists(path):
        tok = open(path).read().split()
        if len(tok) >= 9 and tok[0] in ("1", "2"):
            bg = [float(tok[i]) for i in (1, 2, 3)]
            ba = [float(tok[i]) for i in (4, 5, 6)]
            scale = float(tok[7]); count = int(tok[8])
            if not ta_ok and tok[0] == "2" and len(tok) >= 27:  # keep an existing T_a if we didn't re-fit
                ta = np.array([float(t) for t in tok[18:27]]).reshape(3, 3)
    vals = ["2"] + [f"{v:.9g}" for v in (*bg, *ba, scale)] + [str(count)]
    vals += [f"{v:.9g}" for v in M_g.flatten()] + [f"{v:.9g}" for v in np.asarray(ta).flatten()]
    os.makedirs(config_dir, exist_ok=True)
    with open(path, "w") as f:
        f.write(" ".join(vals) + "\n")
    print(f"# wrote intrinsics to {path}")


def load_replay(path):
    with open(path, "rb") as f:
        b = f.read()
    assert b[:4] == b"G2RP", "bad magic"
    ver, ni, npz = struct.unpack_from("<III", b, 4)
    off = 16
    imu = np.zeros((ni, 7))
    for i in range(ni):
        t = struct.unpack_from("<q", b, off)[0]
        imu[i] = (t, *struct.unpack_from("<6f", b, off + 8))
        off += 32
    pose = np.zeros((npz, 8))
    for i in range(npz):
        t = struct.unpack_from("<q", b, off)[0]
        pose[i] = (t, *struct.unpack_from("<7f", b, off + 8))
        off += 36
    return imu, pose


def quat_mul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return np.array([
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    ])


def quat_log_vec(q):
    """2*log(q) -> rotation vector (xyz), q = [x,y,z,w], assumed near-unit."""
    q = q / np.linalg.norm(q)
    if q[3] < 0:
        q = -q  # shortest arc
    v = q[:3]
    n = np.linalg.norm(v)
    if n < 1e-9:
        return 2.0 * v
    ang = 2.0 * np.arctan2(n, q[3])
    return (ang / n) * v


def quat_inv(q):
    return np.array([-q[0], -q[1], -q[2], q[3]]) / np.dot(q, q)


def quat_angle(q):
    q = q / np.linalg.norm(q)
    return np.degrees(2 * np.arctan2(np.linalg.norm(q[:3]), abs(q[3])))


def exp_quat(v):
    a = np.linalg.norm(v)
    if a < 1e-9:
        return np.array([v[0] / 2, v[1] / 2, v[2] / 2, 1.0])
    ax = v / a
    return np.array([*(np.sin(a / 2) * ax), np.cos(a / 2)])


def compose_gyro(its, gyro, t0, t1):
    """Proper quaternion composition of body-frame gyro over [t0,t1] (midpoint per step,
    interpolated endpoints). Returns the relative-rotation quaternion."""
    m = (its > t0) & (its < t1)
    ts = np.concatenate([[t0], its[m], [t1]])
    we = lambda t: np.array([np.interp(t, its, gyro[:, c]) for c in range(3)])
    w = np.vstack([we(t0), gyro[m], we(t1)])
    q = np.array([0.0, 0.0, 0.0, 1.0])
    for i in range(len(ts) - 1):
        q = quat_mul(q, exp_quat(0.5 * (w[i] + w[i + 1]) * (ts[i + 1] - ts[i])))
    return q


def gyro_intrinsic_cleaned(its, gyro, pt, pq, min_seg_deg=12.0, consist_deg=30.0):
    """The PERSISTED gyro intrinsic M_g, fit robustly from flip-CLEANED high-rotation segments. Walk
    optical poses; extend a segment until it spans >=min_seg_deg of rotation; accept it only if the
    optical relative rotation agrees with the composed-gyro rotation within consist_deg (this rejects
    ~180deg mirror-flips and bad anchors). From the clean pairs:
      - misalignment R_md (gyro->device): Kabsch on the rotation-AXES (weighted by segment angle), and
      - scale: robust median of theta_opt/theta_gyro over the segments.
    M_g = R_md @ (scale * I). This physically-STRUCTURED estimate (rotation x scale) is robust to optical
    orientation noise, whereas a per-short-interval 3x3 affine fit suffers attenuation bias (noisy
    response + residual flips bias the slope low) and can fall outside the plausibility band. Returns
    (M_g, ok, n_segments, scale, mis_deg, n_flips_skipped); ok=False (M_g=identity) when too few clean
    segments to identify the misalignment."""
    ratios = []
    ag, ao, wts = [], [], []   # gyro/optical rotation axes + weights, for misalignment (Kabsch)
    anchor = 0
    i = 1
    flips = 0
    n = len(pt)
    while i < n:
        qg = compose_gyro(its, gyro, pt[anchor] * 1e-9, pt[i] * 1e-9)
        qo = quat_mul(quat_inv(pq[anchor]), pq[i])
        thg, tho = quat_angle(qg), quat_angle(qo)
        if tho < min_seg_deg and thg < min_seg_deg:
            i += 1  # not enough rotation yet -> extend the segment
            continue
        derr = quat_angle(quat_mul(quat_inv(qg), qo))  # disagreement between the two rotations
        if derr < consist_deg and thg > 3.0:
            ratios.append(tho / thg)
            ng = qg[:3] / (np.linalg.norm(qg[:3]) + 1e-12)
            no = qo[:3] / (np.linalg.norm(qo[:3]) + 1e-12)
            ag.append(ng); ao.append(no); wts.append(thg)
            anchor = i
        else:
            flips += 1  # i disagrees grossly (mirror flip or noise) -> skip it, keep anchor
        i += 1
    if len(ag) < 8:
        return np.eye(3), False, len(ratios), float("nan"), float("nan"), flips
    ag = np.array(ag); ao = np.array(ao); w = np.array(wts)
    # Kabsch: device-axis = R_md @ gyro-axis. Solve R_md from the clean axis pairs (weighted by angle).
    H = (ao * w[:, None]).T @ ag
    U, _, Vt = np.linalg.svd(H)
    R = U @ Vt
    if np.linalg.det(R) < 0:
        R = U @ np.diag([1, 1, -1]) @ Vt
    mis = np.degrees(np.arccos(np.clip((np.trace(R) - 1) / 2, -1, 1)))
    rr = np.array(ratios)
    rr = rr[(rr > 0.5) & (rr < 1.5)]   # drop residual mirror-flip ratios before the median
    scale = float(np.median(rr)) if len(rr) else 1.0
    return R @ (scale * np.eye(3)), True, len(ratios), scale, mis, flips


def huber_fit(x, y, delta=None, iters=12):
    """Robust 1-D affine fit y ~ a*x + b via IRLS with Huber weights. Returns (a, b, w)."""
    a, b = np.polyfit(x, y, 1)
    for _ in range(iters):
        r = y - (a * x + b)
        s = 1.4826 * np.median(np.abs(r - np.median(r))) + 1e-12
        d = (delta if delta else 1.5) * s
        w = np.where(np.abs(r) <= d, 1.0, d / np.abs(r))
        W = np.sqrt(w)
        A = np.vstack([x * W, W]).T
        sol, *_ = np.linalg.lstsq(A, y * W, rcond=None)
        a, b = sol
    return a, b, w


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    write_serial = None
    config_dir = os.path.expanduser("~/.config/monado/wmr")
    rest = []
    i = 0
    while i < len(args):
        if args[i] == "--write":
            write_serial = args[i + 1]; i += 2
        elif args[i] == "--config-dir":
            config_dir = args[i + 1]; i += 2
        else:
            rest.append(args[i]); i += 1
    if not rest:
        print(__doc__)
        return 2
    replay_path = rest[0]
    imu, pose = load_replay(replay_path)
    it = imu[:, 0]
    gyro = imu[:, 4:7]
    accel = imu[:, 1:4]
    pt = pose[:, 0]
    pq = pose[:, 4:8]

    # --- GYRO scale/bias from optical relative rotation ---
    its = it * 1e-9                              # IMU sample times in seconds (sorted)
    gi = []   # gyro integral per interval (body)
    po = []   # optical relative rotation vector (body)
    dts = []
    for k in range(len(pt) - 1):
        t0, t1 = pt[k] * 1e-9, pt[k + 1] * 1e-9
        dt = t1 - t0
        if not (0.008 <= dt <= 0.060):
            continue
        # Integrate gyro over EXACTLY [t0,t1]: include interpolated endpoints so the partial
        # sub-sample intervals at both ends are not dropped (dropping the tail under-counts the
        # integral by ~half a sample period -> a spurious >1 "scale"). Trapezoid on the union.
        m = (its > t0) & (its < t1)
        ts = np.concatenate([[t0], its[m], [t1]])
        wcol = [np.interp([t0], its, gyro[:, c]).tolist() + gyro[m, c].tolist() +
                np.interp([t1], its, gyro[:, c]).tolist() for c in range(3)]
        ww = np.array(wcol).T                    # (len(ts), 3)
        if len(ts) < 2:
            continue
        gint = np.sum(0.5 * (ww[1:] + ww[:-1]) * np.diff(ts)[:, None], axis=0)
        # optical relative body rotation: q0^-1 (x) q1
        q0 = pq[k]; q1 = pq[k + 1]
        q0inv = np.array([-q0[0], -q0[1], -q0[2], q0[3]]) / np.dot(q0, q0)
        phi = quat_log_vec(quat_mul(q0inv, q1))
        gi.append(gint); po.append(phi); dts.append(dt)
    gi = np.array(gi); po = np.array(po); dts = np.array(dts)
    print(f"# {replay_path}")
    print(f"# gyro: {len(gi)} usable inter-optical intervals (dt 8-60ms)")

    # PERSISTED M_g: physically-structured (misalignment rotation x scalar scale) from flip-cleaned,
    # long high-rotation segments. This is the robust answer the cache stores and the filter applies.
    M, m_ok, nseg, scale, mis, nfl = gyro_intrinsic_cleaned(its, gyro, pt, pq)
    print(f"  M_g from {nseg} flip-cleaned segments >=12deg ({nfl} flips skipped): "
          f"{'OK' if m_ok else 'too few clean segments (kept identity)'}")
    if m_ok:
        print(f"    scale = {scale:.4f}   gyro->device misalignment = {mis:.2f} deg")
        print(f"    M_g =\n{np.array2string(M, precision=4, prefix='        ')}")

    # Diagnostic only (NOT persisted): the unstructured per-interval 3x3 affine fit phi_opt = A @ g_int +
    # d*dt. On real, optically-noisy data its slope is attenuated low (noise-in-response + residual flips)
    # so its singular values can drop out of the plausibility band — exactly why the structured M_g above
    # is what we persist. Printed to expose that gap.
    X = np.hstack([gi, dts[:, None]])           # n x 4
    Y = po                                       # n x 3
    w = np.ones(len(X))
    for _ in range(15):
        W = np.sqrt(w)[:, None]
        sol, *_ = np.linalg.lstsq(X * W, Y * W, rcond=None)  # 4x3
        res = Y - X @ sol
        rn = np.linalg.norm(res, axis=1)
        s = 1.4826 * np.median(rn) + 1e-12
        d = 1.5 * s
        w = np.where(rn <= d, 1.0, d / rn)
    A = sol[:3, :].T
    sv = np.linalg.svd(A)[1]
    inl = (w > 0.5).mean() * 100
    print(f"  [diag] unstructured 3x3 fit (inliers {inl:.0f}%): singular values = {sv.round(4)} "
          f"(attenuation-prone; not persisted)")

    # --- ACCEL at-rest scale/axis ---
    gmag = np.linalg.norm(gyro, axis=1)
    amag = np.linalg.norm(accel, axis=1)
    at_rest = (gmag < 0.05) & (np.abs(amag - G) < 1.5)
    print(f"# accel: {at_rest.sum()} at-rest samples (|gyro|<0.05, |a| within 1.5 of g)")
    T_a, ta_ok = np.eye(3), False
    if at_rest.sum() > 50:
        ar = accel[at_rest]
        print(f"  rest |accel| median={np.median(amag[at_rest]):.4f} (g={G})  -> scale={np.median(amag[at_rest])/G:.4f}")
        # per-axis: spread of body gravity direction shows if rest orientations span 3D
        ndir = ar / np.linalg.norm(ar, axis=1, keepdims=True)
        print(f"  rest gravity-dir coverage (std per body axis): "
              f"{np.std(ndir,axis=0).round(3)}  (low std = single orientation, can't see per-axis)")
        T_a, ta_ok = fit_accel_ellipsoid(ar)
        print(f"  accel ellipsoid fit: {'OK' if ta_ok else 'insufficient orientation spread (kept identity)'}")
        if ta_ok:
            print(f"    T_a =\n{np.array2string(T_a, precision=4, prefix='        ')}")

    if write_serial is not None:
        write_cache(config_dir, write_serial, M, T_a, ta_ok)
    return 0


if __name__ == "__main__":
    sys.exit(main())
