#!/usr/bin/env python3
"""analyze.py -- offline analysis of the G2 telemetry parquets.

Loads the per-stream parquets produced by convert.py and reports NUMBERS plus
matplotlib PNGs. Covers: IMU rate/noise/gravity sanity, per-cam frame rate +
blob yield + exposure/gain/led, pose_attempt accept/reject/recover + inliers/
blobs/reproj distributions, fusion SLAM<->IMU residuals over time, an event
timeline, and per-stream ring overflow from the manifest.

Usage:
    python3 analyze.py [TELEMETRY_DIR] [-o OUT_DIR]
TELEMETRY_DIR holds the parquets + manifest.json (default $G2_TELEMETRY or .).
OUT_DIR for PNGs defaults to TELEMETRY_DIR/analysis.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from manifest import (
    DEVICE_NAMES, EVENT_TYPES, FUSION_OUTCOME, POSE_OUTCOME, Manifest,
)

NS = 1_000_000_000


def load_parquet(path: Path) -> dict[str, np.ndarray]:
    """Load a parquet as a dict of name -> numpy array (pyarrow)."""
    import pyarrow.parquet as pq

    table = pq.read_table(path)
    return {name: table.column(name).to_numpy(zero_copy_only=False) for name in table.column_names}


def dev_label(dev: int) -> str:
    return DEVICE_NAMES.get(int(dev), f"dev{int(dev)}")


def rate_hz(t_ns: np.ndarray) -> float:
    """Robust rate from monotonic ns timestamps (median delta)."""
    if t_ns.size < 2:
        return 0.0
    dt = np.diff(np.sort(t_ns.astype(np.int64)))
    dt = dt[dt > 0]
    if dt.size == 0:
        return 0.0
    return float(NS / np.median(dt))


def fmt_row(*cols, widths) -> str:
    """Left-justified, space-separated cells; never lets a long cell collide
    with the next (always at least one space between columns)."""
    return " ".join(str(c).ljust(w) for c, w in zip(cols, widths)).rstrip()


# ----------------------------------------------------------------------------- IMU
def analyze_imu(data: dict, out: Path, summary: list):
    t = data["t_mono_ns"].astype(np.int64)
    dev = data["device_id"].astype(np.int64)
    acc = np.stack([data["ax"], data["ay"], data["az"]], axis=1).astype(np.float64)
    gyr = np.stack([data["gx"], data["gy"], data["gz"]], axis=1).astype(np.float64)

    print("\n== IMU ==")
    print(f"{'device':<8}{'rate Hz':>10}{'|g| m/s^2':>12}{'acc sd':>12}{'gyr sd':>12}")
    devices = sorted(set(dev.tolist()))
    fig, axes = plt.subplots(len(devices), 1, figsize=(10, 2.6 * len(devices)), squeeze=False)
    for k, d in enumerate(devices):
        m = dev == d
        td, ad, gd = t[m], acc[m], gyr[m]
        hz = rate_hz(td)
        # noise: per-axis stddev over a "still" window -- here whole capture is at rest;
        # use the central 50% to avoid startup/teardown transients.
        n = ad.shape[0]
        lo, hi = int(n * 0.25), int(n * 0.75) if n > 4 else n
        still_acc = ad[lo:hi]
        still_gyr = gd[lo:hi]
        acc_sd = still_acc.std(axis=0)
        gyr_sd = still_gyr.std(axis=0)
        gmag = np.linalg.norm(ad, axis=1).mean()
        print(f"{dev_label(d):<8}{hz:>10.1f}{gmag:>12.3f}"
              f"{acc_sd.mean():>12.4f}{gyr_sd.mean():>12.4f}")
        summary.append(("imu", dev_label(d), f"{hz:.0f}Hz |g|={gmag:.2f} accSD={acc_sd.mean():.3f}"))

        ts = (td - td[0]) / NS
        ax = axes[k][0]
        ax.plot(ts, ad[:, 0], lw=0.5, label="ax")
        ax.plot(ts, ad[:, 1], lw=0.5, label="ay")
        ax.plot(ts, ad[:, 2], lw=0.5, label="az")
        ax.set_title(f"IMU accel -- {dev_label(d)}  ({hz:.0f} Hz, |g|={gmag:.2f})")
        ax.set_ylabel("m/s^2")
        ax.legend(loc="upper right", fontsize=7)
    axes[-1][0].set_xlabel("t (s)")
    fig.tight_layout()
    fig.savefig(out / "imu_accel.png", dpi=110)
    plt.close(fig)

    # gravity-magnitude sanity check
    gmag_all = np.linalg.norm(acc, axis=1)
    ok = abs(gmag_all.mean() - 9.80665) < 0.5
    print(f"gravity sanity (at rest |accel| ~ 9.8): mean={gmag_all.mean():.3f}  "
          f"-> {'OK' if ok else 'OFF'}")


# --------------------------------------------------------------------------- FRAMES
def analyze_frames(data: dict, out: Path, summary: list):
    t = data["t_mono_ns"].astype(np.int64)
    cam = data["cam_id"].astype(np.int64)
    n_blobs = data["n_blobs"].astype(np.float64)
    exposure = data["exposure"].astype(np.float64)
    gain = data["gain"].astype(np.float64)
    led = data["led_intensity"].astype(np.float64)

    print("\n== FRAMES ==")
    print(f"{'cam':<6}{'rate Hz':>10}{'blobs mean':>12}{'blobs p50':>12}{'frames':>10}")
    cams = sorted(set(cam.tolist()))
    for c in cams:
        m = cam == c
        hz = rate_hz(t[m])
        bm = n_blobs[m]
        print(f"cam{c:<3}{hz:>10.1f}{bm.mean():>12.1f}{np.median(bm):>12.1f}{m.sum():>10}")
        summary.append(("frame", f"cam{c}", f"{hz:.0f}Hz blobs~{bm.mean():.1f}"))

    # blob-yield distribution (per cam)
    fig, ax = plt.subplots(figsize=(9, 4))
    for c in cams:
        ax.hist(n_blobs[cam == c], bins=range(0, int(n_blobs.max()) + 2),
                histtype="step", label=f"cam{c}")
    ax.set_title("Blob yield distribution per cam")
    ax.set_xlabel("blobs per frame"); ax.set_ylabel("frames"); ax.legend(fontsize=8)
    fig.tight_layout(); fig.savefig(out / "frame_blobs.png", dpi=110); plt.close(fig)

    # exposure / gain / led over time (cam 0 representative)
    fig, axs = plt.subplots(3, 1, figsize=(10, 6), sharex=True)
    for c in cams:
        m = cam == c
        ts = (t[m] - t.min()) / NS
        axs[0].plot(ts, exposure[m], lw=0.6, label=f"cam{c}")
        axs[1].plot(ts, gain[m], lw=0.6)
        axs[2].plot(ts, led[m], lw=0.6)
    axs[0].set_ylabel("exposure"); axs[0].legend(fontsize=7, ncol=4)
    axs[1].set_ylabel("gain"); axs[2].set_ylabel("led"); axs[2].set_xlabel("t (s)")
    axs[0].set_title("Exposure / gain / led over time")
    fig.tight_layout(); fig.savefig(out / "frame_exposure.png", dpi=110); plt.close(fig)


# --------------------------------------------------------------------- POSE_ATTEMPT
def analyze_pose(data: dict, out: Path, summary: list):
    t = data["t_mono_ns"].astype(np.int64)
    dev = data["device_id"].astype(np.int64)
    outcome = data["outcome"].astype(np.int64)
    inliers = data["inliers"].astype(np.float64)
    blobs = data["blobs_matched"].astype(np.float64)
    reproj = data["reproj_err_px"].astype(np.float64)

    print("\n== POSE_ATTEMPT ==")
    print(f"{'ctrl':<8}{'accept Hz':>11}{'accept%':>9}{'reject%':>9}{'recover%':>10}"
          f"{'inliers':>9}{'reproj p50':>12}")
    devices = sorted(set(dev.tolist()))
    fig, ax = plt.subplots(figsize=(9, 4))
    for d in devices:
        m = dev == d
        om = outcome[m]
        total = om.size
        acc = (om == 1)
        acc_hz = rate_hz(t[m][acc]) if acc.sum() > 1 else 0.0
        pa = 100 * acc.sum() / total
        pr = 100 * (om == 0).sum() / total
        pv = 100 * (om == 2).sum() / total
        inl = np.median(inliers[m][om != 0]) if (om != 0).any() else 0.0
        rp = np.median(reproj[m])
        print(f"{dev_label(d):<8}{acc_hz:>11.1f}{pa:>9.0f}{pr:>9.0f}{pv:>10.0f}"
              f"{inl:>9.0f}{rp:>12.2f}")
        summary.append(("pose", dev_label(d),
                        f"acc {acc_hz:.0f}Hz ({pa:.0f}% acc/{pr:.0f}% rej/{pv:.0f}% rec) reproj~{rp:.1f}px"))
        ax.hist(reproj[m], bins=30, histtype="step", label=f"{dev_label(d)}")
    ax.set_title("Reprojection error distribution"); ax.set_xlabel("reproj err (px)")
    ax.set_ylabel("attempts"); ax.legend(fontsize=8)
    fig.tight_layout(); fig.savefig(out / "pose_reproj.png", dpi=110); plt.close(fig)

    # inliers + blobs_matched distributions
    fig, axs = plt.subplots(1, 2, figsize=(11, 4))
    for d in devices:
        m = dev == d
        axs[0].hist(inliers[m], bins=range(0, int(inliers.max()) + 2),
                    histtype="step", label=dev_label(d))
        axs[1].hist(blobs[m], bins=range(0, int(blobs.max()) + 2),
                    histtype="step", label=dev_label(d))
    axs[0].set_title("inliers"); axs[0].set_xlabel("inliers"); axs[0].legend(fontsize=8)
    axs[1].set_title("blobs_matched"); axs[1].set_xlabel("blobs")
    fig.tight_layout(); fig.savefig(out / "pose_inliers_blobs.png", dpi=110); plt.close(fig)


# --------------------------------------------------------------------------- FUSION
def analyze_fusion(data: dict, out: Path, summary: list):
    t = data["t_mono_ns"].astype(np.int64)
    dev = data["device_id"].astype(np.int64)
    pos = data["pos_residual_m"].astype(np.float64)
    rot = data["rot_residual_deg"].astype(np.float64)

    print("\n== FUSION (SLAM<->IMU drift) ==")
    print(f"{'ctrl':<8}{'pos mean':>10}{'pos 95p':>10}{'pos max':>10}"
          f"{'rot mean':>10}{'rot 95p':>10}{'rot max':>10}")
    devices = sorted(set(dev.tolist()))
    fig, axs = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
    for d in devices:
        m = dev == d
        p, r = pos[m], rot[m]
        ts = (t[m] - t.min()) / NS
        print(f"{dev_label(d):<8}{p.mean():>10.4f}{np.percentile(p,95):>10.4f}{p.max():>10.4f}"
              f"{r.mean():>10.3f}{np.percentile(r,95):>10.3f}{r.max():>10.3f}")
        summary.append(("fusion", dev_label(d),
                        f"pos {p.mean()*1000:.1f}mm(95p {np.percentile(p,95)*1000:.1f}) "
                        f"rot {r.mean():.2f}deg(95p {np.percentile(r,95):.2f})"))
        axs[0].plot(ts, p * 1000, lw=0.7, label=dev_label(d))
        axs[1].plot(ts, r, lw=0.7, label=dev_label(d))
    axs[0].set_ylabel("pos residual (mm)"); axs[0].set_title("SLAM<->IMU residual over time")
    axs[0].legend(fontsize=8)
    axs[1].set_ylabel("rot residual (deg)"); axs[1].set_xlabel("t (s)")
    fig.tight_layout(); fig.savefig(out / "fusion_residual.png", dpi=110); plt.close(fig)


# --------------------------------------------------------------------------- EVENTS
def analyze_events(data: dict, out: Path, summary: list):
    t = data["t_mono_ns"].astype(np.int64)
    dev = data["device_id"].astype(np.int64)
    etype = data["event_type"].astype(np.int64)
    value = data["value"].astype(np.float64)

    print("\n== EVENTS ==")
    if t.size == 0:
        print("(none)")
        return
    t0 = t.min()
    # counts per (device, type)
    counts: dict[tuple[int, int], int] = {}
    for d, e in zip(dev, etype):
        counts[(int(d), int(e))] = counts.get((int(d), int(e)), 0) + 1
    print("timeline:")
    order = np.argsort(t)
    for i in order:
        name = EVENT_TYPES.get(int(etype[i]), f"type{int(etype[i])}")
        extra = f" value={value[i]:g}" if etype[i] in (3, 5) else ""
        print(f"  t+{(t[i]-t0)/NS:6.2f}s  {dev_label(dev[i]):<5} {name}{extra}")
    print("counts:")
    for (d, e), n in sorted(counts.items()):
        name = EVENT_TYPES.get(e, f"type{e}")
        print(f"  {dev_label(d):<5} {name:<22} {n}")
        summary.append(("event", dev_label(d), f"{name} x{n}"))

    # timeline scatter PNG (event type per device)
    fig, ax = plt.subplots(figsize=(10, 3.5))
    devices = sorted(set(dev.tolist()))
    for d in devices:
        m = dev == d
        ax.scatter((t[m] - t0) / NS, etype[m], label=dev_label(d), s=40)
    ax.set_yticks(sorted(EVENT_TYPES.keys()))
    ax.set_yticklabels([EVENT_TYPES[k] for k in sorted(EVENT_TYPES.keys())], fontsize=8)
    ax.set_xlabel("t (s)"); ax.set_title("Event timeline"); ax.legend(fontsize=8)
    fig.tight_layout(); fig.savefig(out / "events_timeline.png", dpi=110); plt.close(fig)


# -------------------------------------------------------------------------- OVERFLOW
def report_overflow(manifest: Manifest, summary: list):
    print("\n== OVERFLOW (from manifest) ==")
    any_of = False
    for name, s in manifest.streams.items():
        ov = s.overflow_total or 0
        flag = "  <<< OVERFLOW" if ov > 0 else ""
        if ov > 0:
            any_of = True
            summary.append(("overflow", name, f"{ov} rows dropped"))
        print(f"  {name:<14} overflow_total={ov}{flag}")
    if any_of:
        print("\n  !!! RING OVERFLOW DETECTED -- data has gaps; this must not happen in normal use.")
    else:
        print("  all streams clean (0).")
    return any_of


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Analyze G2 telemetry parquets")
    ap.add_argument("telemetry_dir", nargs="?",
                    default=os.environ.get("G2_TELEMETRY", "."))
    ap.add_argument("-o", "--out", default=None, help="PNG output dir (default: <dir>/analysis)")
    args = ap.parse_args(argv)

    tdir = Path(args.telemetry_dir)
    out = Path(args.out) if args.out else tdir / "analysis"
    out.mkdir(parents=True, exist_ok=True)

    manifest = Manifest.load(tdir)
    print(f"analyzing {tdir}  (clock={manifest.clock})")
    print(f"PNGs -> {out}")

    summary: list[tuple[str, str, str]] = []
    handlers = {
        "imu": analyze_imu, "frame": analyze_frames, "pose_attempt": analyze_pose,
        "fusion": analyze_fusion, "event": analyze_events,
    }
    for name, fn in handlers.items():
        pq_path = tdir / f"{name}.parquet"
        if not pq_path.is_file():
            print(f"\n(skip {name}: no {pq_path.name})")
            continue
        data = load_parquet(pq_path)
        if next(iter(data.values())).size == 0:
            print(f"\n(skip {name}: empty)")
            continue
        fn(data, out, summary)

    report_overflow(manifest, summary)

    # ---- concise summary table ----
    print("\n" + "=" * 64)
    print("SUMMARY")
    print("=" * 64)
    widths = (12, 8, 44)
    print(fmt_row("stream", "who", "result", widths=widths))
    print("-" * sum(widths))
    for row in summary:
        print(fmt_row(*row, widths=widths))
    print("=" * 64)
    print(f"PNGs saved in {out}:")
    for p in sorted(out.glob("*.png")):
        print(f"  {p.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
