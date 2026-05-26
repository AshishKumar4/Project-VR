#!/usr/bin/env python3
"""blobviz.py -- overlay detected blobs AND predicted controller-LED positions
on recorded G2 camera frames, so detection-vs-model mismatch is directly visible.

For one frame (or a batch) it draws two layers on the grey camera image:

  (a) DETECTED BLOBS  -- a threshold + connected-components blob finder that
      mirrors Monado's blobwatch (internal/blobwatch.c): per-row run-length
      extents over a pixel threshold, merged across rows, kept only if they
      contain a pixel above a higher "required" threshold and are not larger
      than a max width/height. Each blob is circled (cyan) with area/peak.

  (b) PREDICTED LEDs  -- if a controller pose (P_cam_obj) was logged for a
      timestamp near this frame, the controller's 3D LED model is transformed
      by that pose into camera space and projected through the per-camera
      pinhole-radtan8 intrinsics (mirrors t_camera_models.h rt8_project /
      pose_metrics.c project_led_points). Predicted LED screen positions are
      drawn (orange) so they can be compared against the detected blobs.

Coordinates / conventions (verified against Monado source -- see README):
  * The telemetry pose_attempt row stores P_cam_obj (camera<-object), exactly
    the pose pose_metrics.c feeds to math_pose_transform_point before
    t_camera_models_project. So we apply it directly; no camera extrinsics are
    needed for the per-camera projection (intrinsics suffice).
  * LED model + intrinsics come from a Reverb-G2 calibration (see README for
    provenance and the placeholder note on the controller LED ring).

Usage:
    python3 blobviz.py auto   [-n 6] [-o OUTDIR]      # pick N tracked frames
    python3 blobviz.py one  --cam 0 --ts 9743116542036 [-o OUTDIR]
    python3 blobviz.py one  --cam 0 --idx 1400        [-o OUTDIR]

Defaults assume the 20260522-134113 capture layout; override with --capture.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ----------------------------------------------------------------------------- paths
DEFAULT_CAPTURE = Path("/home/mrwhite0racle/g2-linux-research/captures/20260522-134113")
DEFAULT_CALIB = Path("/home/mrwhite0racle/.local/share/basalt/reverbg2v2_calib.json")

# ----------------------------------------------------------------------------- blob detector params
# Mirrors Monado WMR: wmr_hmd.c BLOB_PIXEL_THRESHOLD_WMR=0x08, BLOB_THRESHOLD_MIN_WMR=0x18,
# blobwatch.c blob_max_wh=35, and the "drop 1x1 blobs" rule.
PIXEL_THRESHOLD = 0x08       # a pixel is "lit" if value > this
REQUIRED_THRESHOLD = 0x18    # a blob must contain at least one pixel > this
BLOB_MAX_WH = 35             # reject blobs wider/taller than this (too big to be an LED)

DEVICE_NAMES = {0: "HMD", 1: "left-controller", 2: "right-controller"}
POSE_OUTCOME = {0: "rejected", 1: "accepted", 2: "recovered"}


# ============================================================================= blob finder
@dataclass
class Blob:
    x: float       # weighted (greysum) centroid
    y: float
    left: int
    top: int
    width: int
    height: int
    area: int      # count of lit pixels
    peak: int      # max pixel value in blob


def find_blobs(img: np.ndarray) -> list[Blob]:
    """Threshold + connected-components blob finder mirroring blobwatch's idea.

    blobwatch builds per-row run-length "extents" of pixels > pixel_threshold,
    unions extents that overlap in x across adjacent rows, then keeps a blob
    only if (a) it contains a pixel > required_threshold, (b) it is not a 1x1
    speck, and (c) it is not bigger than blob_max_wh in either dimension.
    Centroid is the greysum-weighted mean of the lit pixels (blobwatch weights
    by pixel value above the threshold)."""
    h, w = img.shape
    lit = img > PIXEL_THRESHOLD
    # Union-Find over lit pixels via per-row run merging.
    label = np.zeros((h, w), dtype=np.int32)
    parent: list[int] = [0]  # parent[0] unused

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    prev_runs: list[tuple[int, int, int]] = []  # (xstart, xend_inclusive, label)
    for y in range(h):
        row = lit[y]
        runs: list[tuple[int, int, int]] = []
        x = 0
        while x < w:
            if not row[x]:
                x += 1
                continue
            xs = x
            while x < w and row[x]:
                x += 1
            xe = x - 1
            # new provisional label
            parent.append(len(parent))
            lbl = len(parent) - 1
            # merge with any overlapping run on the previous row (8-connectivity in x)
            for (ps, pe, pl) in prev_runs:
                if ps - 1 <= xe and pe + 1 >= xs:  # x-overlap incl diagonal touch
                    union(lbl, pl)
            label[y, xs:xe + 1] = lbl
            runs.append((xs, xe, lbl))
        prev_runs = runs

    # Resolve labels and accumulate per-component stats over lit pixels.
    ys, xs = np.nonzero(lit)
    if ys.size == 0:
        return []
    roots = np.array([find(int(label[yy, xx])) for yy, xx in zip(ys, xs)])
    vals = img[ys, xs].astype(np.float64)

    blobs: list[Blob] = []
    for r in np.unique(roots):
        m = roots == r
        cx = xs[m]
        cy = ys[m]
        cv = vals[m]
        peak = int(cv.max())
        if peak <= REQUIRED_THRESHOLD:
            continue
        left, right = int(cx.min()), int(cx.max())
        top, bot = int(cy.min()), int(cy.max())
        bw = right - left + 1
        bh = bot - top + 1
        if bw == 1 and bh == 1:
            continue
        if bw > BLOB_MAX_WH or bh > BLOB_MAX_WH:
            continue
        # greysum-weighted centroid (weight = pixel value above threshold, as blobwatch does)
        wsum = (cv - PIXEL_THRESHOLD)
        wtot = wsum.sum()
        if wtot <= 0:
            gx, gy = float(cx.mean()), float(cy.mean())
        else:
            gx = float((cx * wsum).sum() / wtot)
            gy = float((cy * wsum).sum() / wtot)
        blobs.append(Blob(gx, gy, left, top, bw, bh, int(m.sum()), peak))
    return blobs


# ============================================================================= radtan8 projection
@dataclass
class CamCalib:
    fx: float
    fy: float
    cx: float
    cy: float
    k1: float
    k2: float
    p1: float
    p2: float
    k3: float
    k4: float
    k5: float
    k6: float
    rpmax: float
    width: int
    height: int


def rt8_project(c: CamCalib, x: float, y: float, z: float):
    """pinhole-radtan8 forward projection. Direct port of rt8_project()
    from monado src/xrt/auxiliary/tracking/t_camera_models.h. Returns
    (u, v, valid). valid is False for points behind the camera or beyond rpmax."""
    if z <= 0:
        return None, None, False
    xp = x / z
    yp = y / z
    rp2 = xp * xp + yp * yp
    cdist = ((1.0 + rp2 * (c.k1 + rp2 * (c.k2 + rp2 * c.k3))) /
             (1.0 + rp2 * (c.k4 + rp2 * (c.k5 + rp2 * c.k6))))
    dX = 2.0 * c.p1 * xp * yp + c.p2 * (rp2 + 2.0 * xp * xp)
    dY = 2.0 * c.p2 * xp * yp + c.p1 * (rp2 + 2.0 * yp * yp)
    xpp = xp * cdist + dX
    ypp = yp * cdist + dY
    u = c.fx * xpp + c.cx
    v = c.fy * ypp + c.cy
    in_inj = True if c.rpmax == 0.0 else rp2 <= c.rpmax * c.rpmax
    return u, v, (z > 0 and in_inj)


def load_calib(path: Path) -> list[CamCalib]:
    """Load the Reverb-G2 basalt calib (pinhole-radtan8) -> list of CamCalib."""
    j = json.loads(Path(path).read_text())["value0"]
    res = j["resolution"]
    out = []
    for i, intr in enumerate(j["intrinsics"]):
        p = intr["intrinsics"]
        out.append(CamCalib(
            fx=p["fx"], fy=p["fy"], cx=p["cx"], cy=p["cy"],
            k1=p["k1"], k2=p["k2"], p1=p["p1"], p2=p["p2"],
            k3=p["k3"], k4=p["k4"], k5=p["k5"], k6=p["k6"],
            rpmax=p.get("rpmax", 0.0),
            width=int(res[i][0]), height=int(res[i][1]),
        ))
    return out


# ============================================================================= pose / LED model
def quat_rotate(q, v):
    """Rotate vec3 v by quaternion q=(x,y,z,w)."""
    qx, qy, qz, qw = q
    vx, vy, vz = v
    # t = 2 * cross(q.xyz, v)
    tx = 2.0 * (qy * vz - qz * vy)
    ty = 2.0 * (qz * vx - qx * vz)
    tz = 2.0 * (qx * vy - qy * vx)
    # v + qw*t + cross(q.xyz, t)
    rx = vx + qw * tx + (qy * tz - qz * ty)
    ry = vy + qw * ty + (qz * tx - qx * tz)
    rz = vz + qw * tz + (qx * ty - qy * tx)
    return (rx, ry, rz)


def pose_transform_point(pos, quat, p):
    """math_pose_transform_point: out = rot(quat, p) + pos."""
    r = quat_rotate(quat, p)
    return (r[0] + pos[0], r[1] + pos[1], r[2] + pos[2])


# WMR controller ring geometry (wmr_controller_base.c). Used to build a
# PLACEHOLDER LED model because the per-device LED JSON was NOT captured in
# this recording. The real model is read off the controller at runtime; this
# ring is geometrically representative (same radius/height the tracker assumes)
# but the exact per-LED layout will differ. See README.
WMR_RING_HEIGHT = 0.02194146618190565
WMR_RING_TOP_RADIUS = 0.11277887330599087 / 2.0
WMR_RING_BOTTOM_RADIUS = 0.09375531956362483 / 2.0


def placeholder_led_model(n_leds: int = 16):
    """Ring of LEDs in OpenCV/WMR object coords (X=right, Y=down, Z=forward),
    centered on the controller ring, normals pointing radially outward.
    Returns list of (pos(x,y,z), dir(x,y,z))."""
    leds = []
    for i in range(n_leds):
        a = 2.0 * math.pi * i / n_leds
        x = WMR_RING_TOP_RADIUS * math.cos(a)
        y = WMR_RING_TOP_RADIUS * math.sin(a)
        z = 0.0
        nx, ny, nz = math.cos(a), math.sin(a), 0.2
        nl = math.sqrt(nx * nx + ny * ny + nz * nz)
        leds.append(((x, y, z), (nx / nl, ny / nl, nz / nl)))
    return leds


def project_leds(calib: CamCalib, pos, quat, leds):
    """Project LED model into the image. Returns list of dicts with screen
    pos, depth z, in-bounds flag, and front-facing flag (mirrors the
    pose_metrics.c visibility test: z>0, in-frame, normal facing camera)."""
    out = []
    for (lp, ld) in leds:
        cam_p = pose_transform_point(pos, quat, lp)
        u, v, valid = rt8_project(calib, *cam_p)
        z = cam_p[2]
        # facing test: rotate LED normal by pose, dot with view vector (cam->led)
        n = quat_rotate(quat, ld)
        vv = cam_p
        vl = math.sqrt(vv[0] ** 2 + vv[1] ** 2 + vv[2] ** 2) or 1.0
        facing = (n[0] * vv[0] + n[1] * vv[1] + n[2] * vv[2]) / vl
        in_bounds = (u is not None and v is not None and 0 <= u < calib.width and 0 <= v < calib.height)
        out.append(dict(u=u, v=v, z=z, valid=valid, in_bounds=in_bounds, facing=facing))
    return out


# ============================================================================= telemetry
def load_parquet(path: Path) -> dict[str, np.ndarray]:
    import pyarrow.parquet as pq
    t = pq.read_table(path)
    return {n: t.column(n).to_numpy(zero_copy_only=False) for n in t.column_names}


def frame_files(capture: Path) -> dict[int, np.ndarray]:
    """Return {cam_id: sorted int64 array of frame timestamps (=filenames ns)}."""
    base = next(capture.glob("euroc_*/mav0"))
    out = {}
    for cam in range(4):
        fs = glob.glob(str(base / f"cam{cam}" / "data" / "*.png"))
        out[cam] = np.array(sorted(int(os.path.basename(f)[:-4]) for f in fs), dtype=np.int64)
    return out, base


def frame_path(base: Path, cam: int, ts: int) -> Path:
    return base / f"cam{cam}" / "data" / f"{ts}.png"


def nearest_frame(frame_ts: np.ndarray, t: int):
    """Nearest frame timestamp to t, with the absolute gap in ns."""
    i = int(np.searchsorted(frame_ts, t))
    cands = []
    if i < len(frame_ts):
        cands.append(frame_ts[i])
    if i > 0:
        cands.append(frame_ts[i - 1])
    best = min(cands, key=lambda c: abs(int(c) - t))
    return int(best), abs(int(best) - t)


# ============================================================================= drawing
def to_rgb(gray: np.ndarray) -> Image.Image:
    return Image.fromarray(gray, mode="L").convert("RGB")


def get_font(size=12):
    for p in ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/TTF/DejaVuSans.ttf"]:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


CYAN = (0, 220, 255)
ORANGE = (255, 150, 0)
RED = (255, 60, 60)
GREEN = (60, 255, 90)
WHITE = (255, 255, 255)


def draw_overlay(gray, blobs, led_pts, meta, out_path: Path):
    img = to_rgb(gray)
    d = ImageDraw.Draw(img)
    font = get_font(11)
    fbig = get_font(13)

    # --- detected blobs (cyan circles) ---
    for b in blobs:
        r = max(4.0, 0.5 * max(b.width, b.height))
        d.ellipse([b.x - r, b.y - r, b.x + r, b.y + r], outline=CYAN, width=1)
        d.text((b.x + r + 1, b.y - 6), f"a{b.area} p{b.peak}", fill=CYAN, font=font)

    # --- predicted LEDs (orange = visible/front-facing, dim red = projected but back-facing/oob) ---
    n_drawn = 0
    for k, lp in enumerate(led_pts):
        if lp["u"] is None:
            continue
        u, v = lp["u"], lp["v"]
        visible = lp["valid"] and lp["in_bounds"] and lp["facing"] > 0
        col = ORANGE if visible else RED
        # cross marker so it's distinct from blob circles
        s = 5
        d.line([u - s, v, u + s, v], fill=col, width=2)
        d.line([u, v - s, u, v + s], fill=col, width=2)
        if visible:
            d.text((u + 4, v + 2), f"L{k}", fill=col, font=font)
            n_drawn += 1

    # --- header / legend ---
    lines = [
        f"cam{meta['cam']}  frame_ts={meta['frame_ts']}",
        f"blobs detected: {len(blobs)}",
    ]
    if meta.get("pose"):
        p = meta["pose"]
        lines += [
            f"pose: {DEVICE_NAMES.get(p['device'], p['device'])} "
            f"{POSE_OUTCOME.get(p['outcome'], p['outcome'])}",
            f"  inliers={p['inliers']} leds_vis={p['leds_visible']} "
            f"matched={p['blobs_matched']} reproj={p['reproj']:.2f}px",
            f"  pose|frame dt={meta['pose_dt_ms']:.1f}ms  pred LEDs in-frame={n_drawn}",
            f"  pos(m)=({p['px']:.3f},{p['py']:.3f},{p['pz']:.3f})",
        ]
    else:
        lines.append("pose: (none near this frame)")

    # legend strip
    y = 4
    d.rectangle([2, 2, 360, 2 + 14 * len(lines) + 30], fill=(0, 0, 0))
    for ln in lines:
        d.text((6, y), ln, fill=WHITE, font=fbig)
        y += 14
    # color key
    d.text((6, y + 2), "o detected blob", fill=CYAN, font=font)
    d.text((130, y + 2), "+ predicted LED (vis)", fill=ORANGE, font=font)
    d.text((6, y + 16), "+ pred LED back/oob", fill=RED, font=font)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return n_drawn


# ============================================================================= driver
def render_one(capture, base, frame_ts_map, calibs, pose, cam, ts, outdir, tag=""):
    fp = frame_path(base, cam, ts)
    if not fp.exists():
        # snap to nearest available frame
        ts2, _ = nearest_frame(frame_ts_map[cam], ts)
        fp = frame_path(base, cam, ts2)
        ts = ts2
    gray = np.asarray(Image.open(fp).convert("L"))
    blobs = find_blobs(gray)

    meta = {"cam": cam, "frame_ts": ts}
    led_pts = []
    if pose is not None:
        meta["pose"] = pose
        meta["pose_dt_ms"] = pose["dt_ns"] / 1e6
        leds = placeholder_led_model()
        led_pts = project_leds(calibs[cam], (pose["px"], pose["py"], pose["pz"]),
                               (pose["qx"], pose["qy"], pose["qz"], pose["qw"]), leds)

    name = tag or f"cam{cam}_{ts}"
    out_path = Path(outdir) / f"{name}.png"
    n_drawn = draw_overlay(gray, blobs, led_pts, meta, out_path)
    return out_path, len(blobs), n_drawn, meta


def pose_row_to_dict(pa, idx, dt_ns):
    return dict(
        device=int(pa["device_id"][idx]), cam=int(pa["cam_id"][idx]),
        outcome=int(pa["outcome"][idx]), inliers=int(pa["inliers"][idx]),
        leds_visible=int(pa["leds_visible"][idx]), blobs_matched=int(pa["blobs_matched"][idx]),
        reproj=float(pa["reproj_err_px"][idx]),
        px=float(pa["px"][idx]), py=float(pa["py"][idx]), pz=float(pa["pz"][idx]),
        qx=float(pa["qx"][idx]), qy=float(pa["qy"][idx]), qz=float(pa["qz"][idx]),
        qw=float(pa["qw"][idx]), dt_ns=int(dt_ns),
    )


def pick_auto(capture, base, frame_ts_map, n):
    """Pick N representative tracked frames for the detected-vs-predicted view.

    From accepted poses snapped to a real saved PNG, we shortlist by Monado's
    own quality (inliers, reproj) and then re-rank by how many blobs OUR
    detector actually finds on the saved frame -- so the chosen overlays are
    the ones where a detected-vs-predicted comparison is meaningful (the
    overexposed frames where the detector finds nothing are demoted). We still
    spread the picks across cameras."""
    pa = load_parquet(capture / "telemetry" / "pose_attempt.parquet")
    acc = pa["outcome"] == 1
    idxs = np.nonzero(acc)[0]
    cand = []
    for i in idxs:
        cam = int(pa["cam_id"][i])
        t = int(pa["hw_ts_ns"][i])
        ts, gap = nearest_frame(frame_ts_map[cam], t)
        if gap > 17_000_000:  # > ~half a 30Hz frame interval; skip poor matches
            continue
        cand.append([i, cam, ts, gap, int(pa["inliers"][i]), float(pa["reproj_err_px"][i]), -1])
    if not cand:
        return []
    # shortlist on Monado quality, then measure detector yield on that shortlist
    cand.sort(key=lambda c: (-c[4], c[5], c[3]))
    shortlist = cand[:200]
    seen_frame: dict[tuple, int] = {}
    for c in shortlist:
        key = (c[1], c[2])
        if key in seen_frame:
            c[6] = seen_frame[key]
            continue
        g = np.asarray(Image.open(frame_path(base, c[1], c[2]).as_posix()).convert("L"))
        nb = len(find_blobs(g))
        c[6] = nb
        seen_frame[key] = nb
    # re-rank: prefer frames where the detector finds blobs, then Monado quality
    shortlist.sort(key=lambda c: (-(c[6] > 0), -c[6], -c[4], c[5]))
    # spread across cameras: best per camera first, then fill
    chosen, seen_cams, used_frames = [], set(), set()
    for c in shortlist:
        key = (c[1], c[2])
        if c[1] not in seen_cams and key not in used_frames:
            chosen.append(c); seen_cams.add(c[1]); used_frames.add(key)
        if len(chosen) >= n:
            break
    for c in shortlist:
        if len(chosen) >= n:
            break
        key = (c[1], c[2])
        if key not in used_frames:
            chosen.append(c); used_frames.add(key)
    chosen = chosen[:n]
    return [(c[1], c[2], pose_row_to_dict(pa, c[0], c[3])) for c in chosen]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("auto", help="pick N tracked frames and render overlays")
    a.add_argument("-n", type=int, default=6)

    o = sub.add_parser("one", help="render a single frame")
    o.add_argument("--cam", type=int, required=True)
    g = o.add_mutually_exclusive_group(required=True)
    g.add_argument("--ts", type=int)
    g.add_argument("--idx", type=int)

    for p in (a, o):
        p.add_argument("--capture", type=Path, default=DEFAULT_CAPTURE)
        p.add_argument("--calib", type=Path, default=DEFAULT_CALIB)
        p.add_argument("-o", "--outdir", type=Path, default=None)

    args = ap.parse_args()
    capture = args.capture
    outdir = args.outdir or (capture / "overlays")
    calibs = load_calib(args.calib)
    frame_ts_map, base = frame_files(capture)

    if args.cmd == "auto":
        picks = pick_auto(capture, base, frame_ts_map, args.n)
        if not picks:
            print("No suitable tracked frames found.")
            return
        print(f"Rendering {len(picks)} overlays -> {outdir}\n")
        for k, (cam, ts, pose) in enumerate(picks):
            tag = f"{k:02d}_cam{cam}_{DEVICE_NAMES.get(pose['device'],'?').split('-')[0]}_{ts}"
            path, nb, nl, meta = render_one(capture, base, frame_ts_map, calibs, pose, cam, ts, outdir, tag)
            print(f"[{k}] {path.name}")
            print(f"    cam{cam} {DEVICE_NAMES.get(pose['device'])} {POSE_OUTCOME[pose['outcome']]}: "
                  f"{nb} blobs detected, {nl} predicted LEDs in-frame, "
                  f"inliers={pose['inliers']} reproj={pose['reproj']:.2f}px dt={pose['dt_ns']/1e6:.1f}ms")
    else:
        cam = args.cam
        if args.idx is not None:
            ts = int(frame_ts_map[cam][args.idx])
        else:
            ts = args.ts
        # find a pose near this frame (any device/this cam)
        pa = load_parquet(capture / "telemetry" / "pose_attempt.parquet")
        m = pa["cam_id"] == cam
        pose = None
        if m.any():
            cand_idx = np.nonzero(m)[0]
            ph = pa["hw_ts_ns"][cand_idx].astype(np.int64)
            j = int(np.argmin(np.abs(ph - ts)))
            gap = int(abs(ph[j] - ts))
            if gap <= 20_000_000:
                pose = pose_row_to_dict(pa, int(cand_idx[j]), gap)
        path, nb, nl, meta = render_one(capture, base, frame_ts_map, calibs, pose, cam, ts, outdir)
        print(f"{path}")
        print(f"  {nb} blobs detected, {nl} predicted LEDs in-frame"
              + (f", pose dt={pose['dt_ns']/1e6:.1f}ms" if pose else ", no pose"))


if __name__ == "__main__":
    main()
