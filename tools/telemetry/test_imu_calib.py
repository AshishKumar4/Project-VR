#!/usr/bin/env python3
"""test_imu_calib.py -- decoupled + adversarial tests for imu_calib_from_optical.py, the offline
IMU-intrinsics tool that fits the gyro M_g (scale + gyro->device misalignment) and the accel
ellipsoid T_a from a recorded controller session and persists them to the live ESKF's per-controller
v2 cache.

Tests assert BEHAVIOUR / CONTRACTS, not internals: a KNOWN synthetic mis-scaled/misaligned IMU is
recovered within tolerance; the fits survive mirror-flip contamination; the recovered matrices pass
the SAME plausibility band the C filter enforces ([0.8,1.25] singular values); and the cache
round-trips through the EXACT v2 text layout the C driver (wmr_controller_base.c imu_cal_parse) reads,
preserving the online-owned bias/scale. No production binary, no telemetry files required.

    ~/miniconda3/envs/g2vr/bin/python test_imu_calib.py
Exits non-zero on any failure.
"""
from __future__ import annotations

import importlib.util
import os
import struct
import sys
import tempfile
from pathlib import Path

import numpy as np

_spec = importlib.util.spec_from_file_location("ic", str(Path(__file__).resolve().parent / "imu_calib_from_optical.py"))
ic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ic)

G = 9.80665
SV_MIN, SV_MAX = 0.8, 1.25  # mirrors the C filter's INTRINSICS_SV_MIN/MAX plausibility band

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


# ---- small quaternion + rotation helpers (independent of the tool's internals) ----
def expq(v):
    a = np.linalg.norm(v)
    if a < 1e-12:
        return np.array([v[0] / 2, v[1] / 2, v[2] / 2, 1.0])
    u = v / a
    return np.array([*(np.sin(a / 2) * u), np.cos(a / 2)])


def qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return np.array([aw * bx + ax * bw + ay * bz - az * by,
                     aw * by - ax * bz + ay * bw + az * bx,
                     aw * bz + ax * by - ay * bx + az * bw,
                     aw * bw - ax * bx - ay * by - az * bz])


def quat_to_R(q):
    x, y, z, w = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def rot_axis_angle(axis, ang):
    ax = np.asarray(axis, float)
    ax /= np.linalg.norm(ax)
    K = np.array([[0, -ax[2], ax[1]], [ax[2], 0, -ax[0]], [-ax[1], ax[0], 0]])
    return np.eye(3) + np.sin(ang) * K + (1 - np.cos(ang)) * K @ K


def mis_deg(R):
    return np.degrees(np.arccos(np.clip((np.trace(R) - 1) / 2, -1, 1)))


def write_replay(path, imu_rows, pose_rows):
    """imu_rows: (t_ns, ax,ay,az,gx,gy,gz); pose_rows: (t_ns, px,py,pz, qx,qy,qz,qw). G2RP v1."""
    with open(path, "wb") as f:
        f.write(b"G2RP")
        f.write(struct.pack("<III", 1, len(imu_rows), len(pose_rows)))
        for r in imu_rows:
            f.write(struct.pack("<qffffff", int(r[0]), *[float(v) for v in r[1:]]))
        for r in pose_rows:
            f.write(struct.pack("<qfffffff", int(r[0]), *[float(v) for v in r[1:]]))


def make_gyro_session(M_g_true, *, flips=0.0, optical_noise_deg=0.0, seed=0, secs=40.0):
    """Synthesise a moving session whose gyro MEASURES w_meas = inv(M_g_true) @ w_true (so the tool's
    correction M_g_true @ w_meas == w_true). `optical_noise_deg` adds small-angle optical noise.
    `flips` injects mirror-flipped optical poses in STICKY RUNS (a persistent flip state that toggles
    occasionally) — how real constellation mirror-flips actually behave (contiguous, not i.i.d.) —
    targeting a ~`flips` duty cycle."""
    rng = np.random.default_rng(seed)
    Minv = np.linalg.inv(M_g_true)
    dt = 0.005
    n = int(secs / dt)
    t = np.arange(n) * dt
    q = np.array([0, 0, 0, 1.0])
    qs = [q.copy()]
    wlog = []
    for i in range(n):
        w = np.array([0.9 * np.sin(0.7 * t[i]), 0.8 * np.cos(0.5 * t[i] + 1), 0.7 * np.sin(0.9 * t[i] + 2)])
        wlog.append(w)
        q = qmul(q, expq(w * dt))
        q /= np.linalg.norm(q)
        qs.append(q.copy())
    qs = np.array(qs[:-1])
    wlog = np.array(wlog)
    gyro = (Minv @ wlog.T).T + rng.normal(0, 5e-4, (n, 3))
    imu_rows = []
    pose_rows = []
    ts = 0
    flip_q = np.array([1.0, 0, 0, 0])  # 180deg about x = a tilt mirror-flip
    flip_state = False
    for i in range(n):
        imu_rows.append((ts, 0.0, 0.0, G, *gyro[i]))
        if i % 3 == 0:
            if flips > 0 and rng.random() < 0.04:  # ~25-pose mean run length
                flip_state = (not flip_state) if rng.random() < flips * 2 else flip_state
            qo = qs[i].copy()
            if optical_noise_deg > 0:
                qo = qmul(qo, expq(np.deg2rad(optical_noise_deg) * rng.standard_normal(3)))
                qo /= np.linalg.norm(qo)
            if flip_state:
                qo = qmul(qo, flip_q)
            pose_rows.append((ts, 0.0, 0.0, 0.0, *qo))
        ts += int(dt * 1e9)
    return imu_rows, pose_rows


def fit_gyro(path):
    imu, pose = ic.load_replay(path)
    its = imu[:, 0] * 1e-9
    gyro = imu[:, 4:7]
    return ic.gyro_intrinsic_cleaned(its, gyro, pose[:, 0], pose[:, 4:8])


def make_rest_session(T_a_true, n_orient, seed=0):
    """At-rest accel session in n_orient distinct orientations whose accel MEASURES
    a_meas = inv(T_a_true) @ g_body (so T_a_true @ a_meas == g_body, |.|=g)."""
    rng = np.random.default_rng(seed)
    Tinv = np.linalg.inv(T_a_true)
    rest = []
    for _ in range(n_orient):
        axis = rng.standard_normal(3)
        R = rot_axis_angle(axis, rng.uniform(0.3, 2.9))
        g_body = R.T @ np.array([0, 0, G])
        for _ in range(60):
            rest.append(Tinv @ g_body + rng.normal(0, 0.01, 3))
    return np.array(rest)


def main():
    print("[1] gyro: structured M_g recovers a KNOWN scale+misalignment on clean optical truth")
    Rmis = rot_axis_angle([0.3, 0.6, 0.74], np.deg2rad(6.0))
    M_g_true = Rmis * 1.03  # uniform scale x rotation (the structured model the tool fits)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "clean.replay")
        write_replay(p, *make_gyro_session(M_g_true, seed=1, secs=40.0))
        Mg, ok, nseg, scale, mis, nfl = fit_gyro(p)
        check("clean fit converges", ok and nseg > 50, f"ok={ok} nseg={nseg}")
        check("scale recovered within 1%", abs(scale - 1.03) < 0.01, f"scale={scale:.4f}")
        check("misalignment recovered within 1 deg", abs(mis - 6.0) < 1.0, f"mis={mis:.2f}")
        check("full M_g matrix recovered (max abs err < 0.01)",
              np.abs(Mg - M_g_true).max() < 0.01, f"err={np.abs(Mg - M_g_true).max():.4f}")
        sv = np.linalg.svd(Mg)[1]
        check("recovered M_g passes the [0.8,1.25] plausibility band",
              sv.min() >= SV_MIN and sv.max() <= SV_MAX, f"sv={sv.round(4)}")

    print("[2] gyro: M_g stays robust under heavy STICKY mirror-flips + optical noise")
    # Contract under contamination: the misalignment (Kabsch on flip-cleaned axes) stays accurate, the
    # matrix always stays inside the plausibility band (never the catastrophic blow-up an unstructured
    # 3x3 fit suffers), and the scale stays bounded. The scale is honestly NOISE-sensitive (optical
    # orientation noise biases the angle-ratio a few % high) -- a documented limitation, guard-bounded.
    with tempfile.TemporaryDirectory() as d:
        for seed in (2, 12, 22):
            p = os.path.join(d, f"dirty{seed}.replay")
            write_replay(p, *make_gyro_session(M_g_true, flips=0.25, optical_noise_deg=1.5, seed=seed, secs=40.0))
            Mg, ok, nseg, scale, mis, nfl = fit_gyro(p)
            check(f"[seed {seed}] fit converges and skips flips", ok and nfl > 0, f"ok={ok} nfl={nfl}")
            check(f"[seed {seed}] misalignment within 1.5 deg despite 25% flips", abs(mis - 6.0) < 1.5,
                  f"mis={mis:.2f}")
            check(f"[seed {seed}] scale bounded (within 8%)", abs(scale - 1.03) < 0.08, f"scale={scale:.4f}")
            check(f"[seed {seed}] M_g always passes the plausibility band",
                  ic_sv_ok(Mg), f"sv={np.linalg.svd(Mg)[1].round(4)}")

    print("[3] gyro: an UNCALIBRATED IMU (identity truth) returns ~identity (no spurious correction)")
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "id.replay")
        write_replay(p, *make_gyro_session(np.eye(3), seed=3, secs=40.0))
        Mg, ok, nseg, scale, mis, nfl = fit_gyro(p)
        check("identity-truth scale ~1.0", abs(scale - 1.0) < 0.01, f"scale={scale:.4f}")
        check("identity-truth misalignment ~0 deg", mis < 1.0, f"mis={mis:.2f}")
        check("identity-truth M_g close to I", np.abs(Mg - np.eye(3)).max() < 0.012,
              f"err={np.abs(Mg - np.eye(3)).max():.4f}")

    print("[4] gyro: too few clean segments -> ok=False, identity (no untrustworthy persist)")
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "short.replay")
        write_replay(p, *make_gyro_session(M_g_true, seed=4, secs=2.0))  # ~too short to span 8 clean segs
        Mg, ok, nseg, scale, mis, nfl = fit_gyro(p)
        check("short session reports not-ok", not ok, f"ok={ok} nseg={nseg}")
        check("short session keeps identity M_g", np.allclose(Mg, np.eye(3)), str(Mg))

    print("[5] accel: ellipsoid T_a recovers a KNOWN per-axis-scale+cross-axis on multi-orientation rest")
    T_a_true = np.array([[1.03, 0.01, 0.0], [0.01, 0.98, 0.005], [0.0, 0.005, 1.015]])
    rest = make_rest_session(T_a_true, n_orient=14, seed=5)
    Ta, ta_ok = ic.fit_accel_ellipsoid(rest)
    check("ellipsoid fit converges with spread", ta_ok, f"ta_ok={ta_ok}")
    check("T_a recovered (max abs err < 0.01)", np.abs(Ta - T_a_true).max() < 0.01,
          f"err={np.abs(Ta - T_a_true).max():.4f}")
    check("recovered T_a passes the plausibility band", ic_sv_ok(Ta),
          f"sv={np.linalg.svd(Ta)[1].round(4)}")

    print("[6] accel: a SINGLE orientation -> not identifiable -> identity (no spurious ellipsoid)")
    rest1 = make_rest_session(T_a_true, n_orient=1, seed=6)
    Ta, ta_ok = ic.fit_accel_ellipsoid(rest1)
    check("single-orientation rest is rejected", not ta_ok, f"ta_ok={ta_ok}")
    check("single-orientation keeps identity T_a", np.allclose(Ta, np.eye(3)), str(Ta))

    print("[7] cache round-trip: write_cache emits a v2 line the C driver parser reads back exactly")
    with tempfile.TemporaryDirectory() as cfg:
        serial = "TESTSERIAL0001L"
        ic.write_cache(cfg, serial, M_g_true, T_a_true, True)
        bg, ba, scale, count, mg, ta, ver = parse_v2_like_driver(cfg, serial)
        check("written record is version 2", ver == 2, f"ver={ver}")
        check("round-trip M_g is bit-stable to 1e-6", np.abs(mg - M_g_true).max() < 1e-6,
              f"err={np.abs(mg - M_g_true).max():.2e}")
        check("round-trip T_a is bit-stable to 1e-6", np.abs(ta - T_a_true).max() < 1e-6,
              f"err={np.abs(ta - T_a_true).max():.2e}")
        check("a fresh record defaults bias/scale to the cache's neutral (0,0,0 / 1.0)",
              np.allclose(bg, 0) and np.allclose(ba, 0) and abs(scale - 1.0) < 1e-9,
              f"bg={bg} ba={ba} scale={scale}")

    print("[8] cache merge: write_cache PRESERVES the online-owned bias/scale of an existing v1 record")
    with tempfile.TemporaryDirectory() as cfg:
        serial = "TESTSERIAL0002R"
        # Pre-seed a v1 record (bias+scale only, no intrinsics) as the live driver would have written.
        bg0 = [0.001, -0.002, 0.003]
        ba0 = [0.01, -0.02, 0.03]
        scale0 = 1.021
        fn = "imu-cal-" + serial + ".txt"
        with open(os.path.join(cfg, fn), "w") as f:
            f.write("1 %.9g %.9g %.9g %.9g %.9g %.9g %.9g 4\n" % (*bg0, *ba0, scale0))
        ic.write_cache(cfg, serial, M_g_true, T_a_true, True)
        bg, ba, scale, count, mg, ta, ver = parse_v2_like_driver(cfg, serial)
        check("bias preserved across the intrinsics write", np.allclose(bg, bg0) and np.allclose(ba, ba0),
              f"bg={bg} ba={ba}")
        check("accel scale preserved across the intrinsics write", abs(scale - scale0) < 1e-9,
              f"scale={scale}")
        check("session count preserved", count == 4, f"count={count}")
        check("intrinsics now populated (upgraded v1 -> v2)", np.abs(mg - M_g_true).max() < 1e-6, "")

    print("[9] cache merge: NOT re-fitting T_a keeps the cached T_a (carry-forward), not identity")
    with tempfile.TemporaryDirectory() as cfg:
        serial = "TESTSERIAL0003L"
        ic.write_cache(cfg, serial, np.eye(3), T_a_true, True)        # first: T_a fitted
        ic.write_cache(cfg, serial, M_g_true, np.eye(3), False)       # later: only M_g, ta_ok=False
        bg, ba, scale, count, mg, ta, ver = parse_v2_like_driver(cfg, serial)
        check("M_g updated on the second pass", np.abs(mg - M_g_true).max() < 1e-6, "")
        check("T_a carried forward (not clobbered to identity)", np.abs(ta - T_a_true).max() < 1e-6,
              f"ta=\n{ta}")

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


def ic_sv_ok(M):
    sv = np.linalg.svd(M)[1]
    return sv.min() >= SV_MIN and sv.max() <= SV_MAX


def parse_v2_like_driver(cfg, serial):
    """Parse the cache file with the SAME field layout/order as the C driver's imu_cal_parse
    (version  bg[3] ba[3] scale count  M_g[9]  T_a[9]). Decoupled check of the on-disk contract."""
    fn = "imu-cal-" + "".join(c if c.isalnum() or c in ".-_" else "_" for c in serial) + ".txt"
    tok = open(os.path.join(cfg, fn)).read().split()
    ver = int(tok[0])
    bg = np.array([float(tok[i]) for i in (1, 2, 3)])
    ba = np.array([float(tok[i]) for i in (4, 5, 6)])
    scale = float(tok[7])
    count = int(tok[8])
    mg = np.array([float(t) for t in tok[9:18]]).reshape(3, 3)
    ta = np.array([float(t) for t in tok[18:27]]).reshape(3, 3)
    return bg, ba, scale, count, mg, ta, ver


if __name__ == "__main__":
    sys.exit(main())
