#!/usr/bin/env python3
"""g2_geom.py -- shared geometry + telemetry-IO helpers for the cleaned-GT / MSE tools.

Pure offline analysis support (no production code). Quaternions are stored
(x, y, z, w) to match the telemetry schema. World frame is OpenXR Y-up, so world
gravity points down = (0, -1, 0).

All functions are vectorized over a leading sample axis where it is cheap to do so.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from manifest import Manifest

DOWN = np.array([0.0, -1.0, 0.0])  # world gravity direction (OpenXR Y-up)
G_MS2 = 9.81


# ---------------------------------------------------------------------------
# telemetry IO
# ---------------------------------------------------------------------------
def load_stream(d: Path, m: Manifest, name: str) -> np.ndarray:
    """np.fromfile one telemetry stream, floored to whole rows."""
    s = m.streams[name]
    p = d / s.file
    n = p.stat().st_size // s.row_size
    return np.fromfile(p, dtype=s.structured_dtype(), count=n)


# ---------------------------------------------------------------------------
# quaternion math (x, y, z, w convention)
# ---------------------------------------------------------------------------
def quat_normalize(q: np.ndarray) -> np.ndarray:
    q = np.asarray(q, dtype=float)
    n = np.linalg.norm(q, axis=-1, keepdims=True)
    n = np.where(n == 0.0, 1.0, n)
    return q / n


def quat_conj(q: np.ndarray) -> np.ndarray:
    q = np.asarray(q, dtype=float)
    out = q.copy()
    out[..., :3] *= -1.0
    return out


def quat_mul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Hamilton product a*b, both (x,y,z,w)."""
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    ax, ay, az, aw = a[..., 0], a[..., 1], a[..., 2], a[..., 3]
    bx, by, bz, bw = b[..., 0], b[..., 1], b[..., 2], b[..., 3]
    return np.stack([
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    ], axis=-1)


def quat_rotate(q: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Rotate vector(s) v by quaternion(s) q (body->world if q is R_world_body)."""
    q = np.asarray(q, dtype=float)
    v = np.asarray(v, dtype=float)
    x, y, z, w = q[..., 0], q[..., 1], q[..., 2], q[..., 3]
    vx, vy, vz = v[..., 0], v[..., 1], v[..., 2]
    tx = 2.0 * (y * vz - z * vy)
    ty = 2.0 * (z * vx - x * vz)
    tz = 2.0 * (x * vy - y * vx)
    return np.stack([
        vx + w * tx + (y * tz - z * ty),
        vy + w * ty + (z * tx - x * tz),
        vz + w * tz + (x * ty - y * tx),
    ], axis=-1)


def quat_rotate_inv(q: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Rotate v by the inverse of q (world->body if q is R_world_body)."""
    return quat_rotate(quat_conj(q), v)


def quat_geodesic_deg(q1: np.ndarray, q2: np.ndarray) -> np.ndarray:
    """Shortest geodesic angle (deg) between two unit quats (sign-insensitive)."""
    q1 = quat_normalize(q1)
    q2 = quat_normalize(q2)
    d = np.abs(np.sum(q1 * q2, axis=-1))
    d = np.clip(d, -1.0, 1.0)
    return np.degrees(2.0 * np.arccos(d))


def quat_canonical_sign(q: np.ndarray, ref: np.ndarray) -> np.ndarray:
    """Flip q's sign so it lies in the same hemisphere as ref (q and -q are equal
    rotations; SLERP/averaging need a consistent sign)."""
    q = np.asarray(q, dtype=float)
    ref = np.asarray(ref, dtype=float)
    dot = np.sum(q * ref, axis=-1, keepdims=True)
    return np.where(dot < 0.0, -q, q)


def quat_slerp(q0: np.ndarray, q1: np.ndarray, t: float) -> np.ndarray:
    """SLERP a single pair (scalar t)."""
    q0 = quat_normalize(q0)
    q1 = quat_normalize(q1)
    dot = float(np.dot(q0, q1))
    if dot < 0.0:
        q1 = -q1
        dot = -dot
    if dot > 0.9995:  # nearly parallel -> lerp
        return quat_normalize(q0 + t * (q1 - q0))
    theta0 = np.arccos(np.clip(dot, -1.0, 1.0))
    s0 = np.sin((1.0 - t) * theta0) / np.sin(theta0)
    s1 = np.sin(t * theta0) / np.sin(theta0)
    return quat_normalize(s0 * q0 + s1 * q1)


# ---------------------------------------------------------------------------
# rotation-vector (log/exp) for orientation smoothing in the tangent space
# ---------------------------------------------------------------------------
def quat_log(q: np.ndarray) -> np.ndarray:
    """Map a unit quat to its rotation vector (axis * angle), shape (...,3)."""
    q = quat_normalize(q)
    w = np.clip(q[..., 3], -1.0, 1.0)
    v = q[..., :3]
    vn = np.linalg.norm(v, axis=-1, keepdims=True)
    angle = 2.0 * np.arctan2(vn[..., 0], w)
    # axis = v / |v|; guard tiny rotations
    scale = np.where(vn[..., 0] > 1e-9, angle / np.where(vn[..., 0] == 0, 1, vn[..., 0]), 0.0)
    return v * scale[..., None]


def quat_exp(rv: np.ndarray) -> np.ndarray:
    """Map a rotation vector (axis*angle) back to a unit quat (...,4)."""
    rv = np.asarray(rv, dtype=float)
    angle = np.linalg.norm(rv, axis=-1, keepdims=True)
    half = angle * 0.5
    small = angle[..., 0] < 1e-9
    s = np.where(small[..., None], 0.5, np.sin(half) / np.where(angle == 0, 1, angle))
    xyz = rv * s
    w = np.cos(half)
    return np.concatenate([xyz, w], axis=-1)


# ---------------------------------------------------------------------------
# gravity-tilt cue
# ---------------------------------------------------------------------------
def gravity_tilt_err_deg(q_cand: np.ndarray, g_body: np.ndarray) -> np.ndarray:
    """Angle (deg) between predicted-world-gravity (q_cand rotates g_body to world)
    and the true world down. 0 == the candidate's tilt matches gravity; ~180 for a
    tilt flip. q_cand is R_world_body, g_body is the body-frame gravity direction."""
    pred = quat_rotate(q_cand, g_body)
    pred = pred / (np.linalg.norm(pred, axis=-1, keepdims=True) + 1e-12)
    cos = np.clip(np.sum(pred * DOWN, axis=-1), -1.0, 1.0)
    return np.degrees(np.arccos(cos))


# ---------------------------------------------------------------------------
# world heading (yaw about the gravity axis) -- the flip axis gravity is blind to
# ---------------------------------------------------------------------------
UP = -DOWN  # world up (OpenXR Y-up)


def world_yaw_deg(q: np.ndarray, fwd: np.ndarray = np.array([0.0, 0.0, -1.0])) -> np.ndarray:
    """Heading (deg, in [-180,180]) of a body's forward axis projected onto the
    ground plane, about the world up axis. q is R_world_body. This is the component
    a pure-yaw mirror-flip rotates by ~180 deg and that gravity-tilt cannot see."""
    f = quat_rotate(q, fwd)
    # project out the vertical (up) component, then heading in the ground plane
    return np.degrees(np.arctan2(f[..., 0], -f[..., 2]))


def yaw_diff_deg(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Signed shortest difference a-b of two headings (deg), wrapped to [-180,180]."""
    return (np.asarray(a) - np.asarray(b) + 180.0) % 360.0 - 180.0
