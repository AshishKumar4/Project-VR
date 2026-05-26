#!/usr/bin/env python3
"""imu_spectral.py — Welch PSD + spectrogram of all historical G2 IMU telemetry.

Answers: is the controller/HMD IMU noise WHITE (handled by the KF noise model) or COLORED
(resonant peaks -> a notch/LPF would help, Betaflight-style)? Quantifies noise floor vs the ESKF's
assumed SIGMA_A/SIGMA_G, sample-rate jitter, and any aliasing. Read-only.

Usage: python3 imu_spectral.py            # all captures/*/telemetry/imu.parquet
Outputs PNGs + a printed summary into captures/_spectral/.
"""
import os, glob, numpy as np, pyarrow.parquet as pq
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy import signal

ROOT = "/home/mrwhite0racle/g2-linux-research/captures"
OUT = f"{ROOT}/_spectral"; os.makedirs(OUT, exist_ok=True)
DEVN = {0: "HMD", 1: "LEFT", 2: "RIGHT"}
AX = ["ax", "ay", "az", "gx", "gy", "gz"]
# ESKF assumed continuous noise densities (t_tracker_kalman_fusion.cpp)
SIGMA_A, SIGMA_G = 0.02, 2.0e-3  # (m/s^2)/sqrt(Hz), (rad/s)/sqrt(Hz)

def uniform(t_ns, x):
    """Resample x(t) onto a uniform grid at the median sample period; returns (fs, xu)."""
    t = (t_ns - t_ns[0]) / 1e9
    dt = np.median(np.diff(t))
    if dt <= 0:
        return None, None
    grid = np.arange(t[0], t[-1], dt)
    return 1.0 / dt, np.interp(grid, t, x)

def load(f):
    t = pq.read_table(f); return {c: t.column(c).to_numpy() for c in t.column_names}

def main():
    files = sorted(glob.glob(f"{ROOT}/*/telemetry/imu.parquet"))
    summary = []
    # PSD overlays: one figure per device, 6 axes, all captures overlaid
    for dev in (1, 2, 0):
        fig, ax = plt.subplots(2, 3, figsize=(18, 9))
        any_data = False
        for f in files:
            cap = f.split("captures/")[1].split("/")[0]
            d = load(f); m = d["device_id"] == dev
            if m.sum() < 2000:
                continue
            any_data = True
            tns = d["t_mono_ns"][m]; tns = np.sort(tns)
            dts = np.diff((tns - tns[0]) / 1e9)
            fs = 1.0 / np.median(dts)
            jit = np.std(dts) / np.median(dts) * 100  # % sample-period jitter
            for ai, name in enumerate(AX):
                fsu, xu = uniform(d["t_mono_ns"][m], d[name][m])
                if fsu is None:
                    continue
                nper = min(4096, len(xu) // 4)
                fr, pxx = signal.welch(xu, fs=fsu, nperseg=nper, detrend="constant")
                r, c = divmod(ai, 3)
                ax[r, c].loglog(fr[1:], np.sqrt(pxx[1:]), lw=0.8, label=f"{cap[:13]}")
                # peak find (exclude < 1 Hz)
                band = fr > 1.0
                if band.any():
                    pk = fr[band][np.argmax(pxx[band])]
                    pkv = np.sqrt(pxx[band].max())
                    floor = np.sqrt(np.median(pxx[fr > fs * 0.3]))  # high-band noise floor (ASD)
                    summary.append((DEVN[dev], cap[:13], name, fs, jit, pk, pkv, floor))
        if not any_data:
            plt.close(fig); continue
        for ai, name in enumerate(AX):
            r, c = divmod(ai, 3)
            a = ax[r, c]
            ref = SIGMA_A if name.startswith("a") else SIGMA_G
            a.axhline(ref, color="k", ls="--", lw=1, label=f"ESKF SIGMA={ref:g}")
            a.set_title(f"{DEVN[dev]} {name}  ASD"); a.set_xlabel("Hz")
            a.set_ylabel("(m/s²)/√Hz" if name[0] == "a" else "(rad/s)/√Hz")
            a.grid(True, which="both", alpha=.3)
        ax[0, 0].legend(fontsize=6, loc="lower left")
        fig.suptitle(f"{DEVN[dev]} IMU amplitude spectral density — all captures (flat=white noise; peaks=resonance)")
        fig.tight_layout(); fig.savefig(f"{OUT}/psd_{DEVN[dev].lower()}.png", dpi=110); plt.close(fig)

    # spectrogram of one representative controller capture (stationarity / motion-dependence)
    rep = f"{ROOT}/20260523-174457-imu-only/telemetry/imu.parquet"
    if os.path.exists(rep):
        d = load(rep); m = d["device_id"] == 2
        fig, ax = plt.subplots(2, 1, figsize=(16, 8), sharex=True)
        for i, name in enumerate(["az", "gx"]):
            fsu, xu = uniform(d["t_mono_ns"][m], d[name][m])
            fr, tt, Sxx = signal.spectrogram(xu, fs=fsu, nperseg=256)
            ax[i].pcolormesh(tt, fr, 10 * np.log10(Sxx + 1e-12), shading="gouraud")
            ax[i].set_ylabel(f"{name} Hz"); ax[i].set_title(f"RIGHT {name} spectrogram (dB)")
        ax[1].set_xlabel("time [s]")
        fig.tight_layout(); fig.savefig(f"{OUT}/spectrogram_right.png", dpi=110); plt.close(fig)

    # printed summary
    print(f"{'dev':6s}{'capture':15s}{'axis':5s}{'fs':>7s}{'jit%':>6s}{'peakHz':>8s}{'peakASD':>10s}{'floorASD':>10s}{'  vs SIGMA'}")
    for dev, cap, name, fs, jit, pk, pkv, floor in summary:
        ref = SIGMA_A if name[0] == "a" else SIGMA_G
        flag = "  << " + ("HIGH" if floor > 2 * ref else "ok") if floor == floor else ""
        print(f"{dev:6s}{cap:15s}{name:5s}{fs:7.0f}{jit:6.1f}{pk:8.1f}{pkv:10.4f}{floor:10.4f}  {floor/ref:5.1f}x SIGMA")
    print(f"\nwrote PSD + spectrogram PNGs to {OUT}/")

if __name__ == "__main__":
    main()
