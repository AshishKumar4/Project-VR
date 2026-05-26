# Project-VR — bringing an HP Reverb G2 back to life on Linux 🐧🥽

<p align="center">
  <img alt="Platform" src="https://img.shields.io/badge/OS-Ubuntu%2024.04%20(X11)%20%2F%2026.04%20(Wayland)-E95420?logo=ubuntu&logoColor=white">
  <img alt="GPU" src="https://img.shields.io/badge/GPU-NVIDIA%20open%20595-76B900?logo=nvidia&logoColor=white">
  <img alt="Runtime" src="https://img.shields.io/badge/runtime-Monado%20%2B%20SteamVR-1f6feb">
  <img alt="Status" src="https://img.shields.io/badge/status-working%20(personal%20project)-success">
</p>

> I had an old **HP Reverb G2** sitting in a drawer. Then Microsoft pulled the plug on Windows
> Mixed Reality — one Windows update and a perfectly good headset turns into e-waste. I didn't
> want to throw it out, so I set out to make it work on **Linux + NVIDIA** instead. It turned into
> a much deeper rabbit hole than I expected. This repo is my honest journey, plus every patch,
> script, and note I gathered, built, and discovered along the way to get it working *properly* —
> not a hack that limps along, but a real, durable setup.
>
> I'm not a kernel developer by trade. I got here by being stubborn, reading a *lot* of source
> code, and pairing with **Claude** as a tireless research-and-debugging partner. If you have a
> WMR headset gathering dust, maybe this saves you some of the pain. 💚

---

## Table of contents
- [What works today](#what-works-today)
- [The setup this was built on](#the-setup-this-was-built-on)
- [What was wrong, and how it got fixed](#what-was-wrong-and-how-it-got-fixed)
  - [NVIDIA open kernel modules](#nvidia-open-kernel-modules)
  - [mutter / GNOME (Wayland compositor)](#mutter--gnome-wayland-compositor)
  - [Our Monado fork](#our-monado-fork)
- [Repository map](#repository-map)
- [Quick start](#quick-start)
- [Keeping it alive — the self-healing infra](#keeping-it-alive--the-self-healing-infra)
- [Honest caveats](#honest-caveats)
- [Credits & thanks](#credits--thanks)
- [License](#license)

---

## What works today

| Capability | Status |
|---|---|
| G2 drives its native **4320×2160 @ 90 Hz** (2160×2160 per eye) panel mode | ✅ |
| Full **SteamVR** — our self-built Monado driver loads directly into `vrserver`; OpenComposite only shims OpenVR-only titles | ✅ |
| **X11** (Ubuntu 24.04) — native direct-mode SteamVR | ✅ |
| **Wayland** (Ubuntu 26.04 / GNOME 50) — the G2 runs on a DRM lease while the desktop keeps running on its own monitors, in one GNOME session (no second compositor, no session switch) | ✅ |
| **6-DoF head tracking** — Basalt visual-inertial SLAM | ✅ |
| **6-DoF controller tracking** — LED constellation + a purpose-built error-state Kalman filter fusing optical + IMU | ✅ *(optical front-end still being tuned)* |
| Controllers expose their **buttons / sticks / triggers** to SteamVR — a full input profile, not a pose-only device | ✅ |
| Survives OS / kernel / NVIDIA / GNOME updates automatically | ✅ *(see `infra/`)* |

This is a **personal research project**, not a product — it's tested on my exact hardware and is
still actively evolving (controller-tracking smoothness in particular). But the headset genuinely
works, and everything here is written to be clean and to generalize to other WMR/VR HMDs, not just
mine.

## The setup this was built on

| | |
|---|---|
| **Headset** | HP Reverb G2 (Windows Mixed Reality, DisplayPort + USB) |
| **GPU** | NVIDIA RTX 4080 (AD103), `nvidia-driver-595-open` |
| **OS** | Ubuntu 24.04 (X11 era) → 26.04 LTS (Wayland era) |
| **Compositor** | GNOME 50 / mutter 50.1 on Wayland (and X11 before) |
| **XR stack** | Monado (Collabora) + our WMR driver fork + Basalt SLAM + OpenComposite → SteamVR |

## What was wrong, and how it got fixed

Nothing about this headset is plug-and-play on Linux/NVIDIA. The bugs spanned the whole stack — from
the EDID parser in the GPU driver up to the Wayland compositor. Each fix is a small, self-contained,
upstream-quality patch (see [`patches/INDEX.md`](patches/INDEX.md)); the longer narrative, dead-ends
included, is in [`docs/JOURNEY.md`](docs/JOURNEY.md).

### NVIDIA open kernel modules

| Problem | Fix |
|---|---|
| DisplayID 2.0 **Type-VII** timing descriptor dropped — the G2's 4320×2160 mode never appeared when the block carried optional payload bytes | Walk the descriptors by their spec stride (upstream bug, NVIDIA #5923212) |
| **DSC 1.1** rate-control / PPS tables didn't match the VESA spec, so the 90 Hz compression handshake failed | Correct the rate-control + PPS tables against the spec |
| **Microsoft VR VSDB** was only parsed on a newer block version than the G2 ships → the driver never recognised it as an HMD | Parse the version the G2 actually uses, and throttle the VSDB re-read storm |
| The HMD wasn't exposed to Wayland as a **leasable, non-desktop** output | Expose `isVrHmd` as a `non_desktop=1` DRM connector property |
| The DRM-leased CRTC couldn't flip — `NVKMS_IOCTL_FLIP` returned **EPERM** after a head was freed | Bridge the DRM lease to `NVKMS_IOCTL_GRANT_PERMISSIONS` and fix the post-free permission bookkeeping |
| A spurious write-combining flush in the atomic-commit path | Gate the flush in `nv_drm` |

### mutter / GNOME (Wayland compositor)

| Problem | Fix |
|---|---|
| gnome-shell **SIGSEGV** when the G2 hot-plugged — NULL `logical_monitor` during monitor rebuild | Null-safety fix at the root cause, with a regression test added to mutter's own suite |
| NULL deref in `meta_monitor_mode_foreach_crtc` | Guard the CRTC walk |
| Desktop **input + render freeze** once SteamVR takes the DRM lease | Fix the DRM-lease lifecycle so the desktop doesn't wedge during/after VR |

### Our Monado fork

Lives at **[github.com/AshishKumar4/monado-wmr](https://github.com/AshishKumar4/monado-wmr)** (a fork of thaytan/monado).

| Area | What it adds |
|---|---|
| Bring-up | HP-Inc. device allowlist; select the G2's native mode as RandR-preferred; Wayland-aware lease wait; 90 Hz frame-interval fix; controller input bindings |
| Present | Root-caused the Wayland `VK_ERROR_UNKNOWN` to display ISO-bandwidth contention; a userspace auto-negotiator dips the desktop just enough on VR-enter and restores it on exit |
| Controller optical front-end | Soft anisotropic prior-cost mirror-flip disambiguation; joint multi-camera (non-central) PnP; pose-predicted LED label propagation; saturation-aware blob detection |
| Controller fusion | A 15-error-state ESKF — out-of-view body-lock, IMU intrinsics + cross-session calibration, out-of-sequence IMU handling, render-time acceleration de-noising, camera-gain / LED-brightness fixes |
| Validation | An offline VIO replay harness + a standing regression benchmark over captured frames |

## Repository map

```
Project-VR/
├── patches/        The actual fixes (the good stuff)
│   ├── consolidated/   clean, grouped patch series → NVIDIA, Monado, mutter
│   └── INDEX.md        authoritative index of every patch + status
├── infra/          Durable, self-healing setup — survives system updates
│   ├── g2ctl           one tool: status / verify / heal / doctor (idempotent)
│   └── g2.manifest.toml  single source of truth (repos, patches, holds)
├── scripts/        NVIDIA driver porting (fetch → rebase → build → MOK-sign → install)
├── g2-studio/      The userspace VR runtime (display auto-negotiator, Monado
│                   lifecycle, GPU/CPU perf, a small web UI)
├── docs/           The long-form journey write-up
└── tools/          Diagnostic probes (vkdisplays, safe TTY VR tests) + telemetry/replay analysis
```
> Note: the big upstream source trees (NVIDIA / Monado / mutter forks) are **not** vendored here —
> only the patches against them. `infra/` + `scripts/` fetch the right upstream and apply the patches.

## Quick start

> ⚠️ This is involved — it builds and MOK-signs kernel modules and rebuilds parts of the desktop.
> Read [`docs/JOURNEY.md`](docs/JOURNEY.md) and [`patches/INDEX.md`](patches/INDEX.md) first. It
> assumes Secure Boot MOK enrollment is already set up.

```bash
git clone git@github.com:AshishKumar4/Project-VR.git
cd Project-VR

# See what the tooling expects and what's drifted from the desired state:
infra/g2ctl status        # read-only health/drift report

# Bring the whole stack into sync (fetch upstream, apply patches, build, sign, install):
infra/install.sh          # wire up g2ctl + the auto-heal hooks
g2ctl doctor              # status → heal → verify
# reboot to load the patched NVIDIA modules, then start SteamVR via g2-studio.
```

## Keeping it alive — the self-healing infra

The thing I care about most: **updates shouldn't silently break this.** So `infra/g2ctl` is an
idempotent orchestrator driven by one manifest. After any system update an apt hook flags a drift
check; `g2ctl heal` re-applies only what drifted, rebuilds, and verifies. If re-applying a patch
ever hits a merge conflict against new upstream code, it can hand the conflict to Claude Code to
resolve on a backup branch, rebuild, and re-verify — and it never touches your live system unless
verification passes. It's lean on purpose: one manifest, one tool, one event hook, no daemons.

## Honest caveats

- It's tuned and tested on **my** hardware (RTX 4080 / G2). The patches are written to generalize to
  the WMR class, but I can't promise other headsets/GPUs work out of the box.
- **Controller tracking still has some jitter** I'm working on — it's good, not yet perfect. The
  current tracking work is validated offline against captured data; final tuning is in-headset.
- Some pieces require root (kernel module install) and a reboot. There's no one-click yet.

## Credits & thanks

Standing on the shoulders of giants:
- **[Monado](https://monado.dev/) / Collabora** — the open OpenXR runtime that makes any of this possible.
- **thaytan** and the Monado WMR contributors — the WMR driver + constellation-tracking groundwork.
- **[Basalt](https://gitlab.com/VladyslavUsenko/basalt)** — visual-inertial SLAM for the inside-out head tracking.
- **NVIDIA's open GPU kernel modules** — being open-source is the only reason these driver bugs were fixable.
- **OpenComposite** — the OpenVR→OpenXR shim that lets SteamVR titles run.
- **Claude (Anthropic)** — my debugging and research partner through the whole thing.

## License

My own code here (`g2-studio/`, `infra/`, `scripts/`, `tools/`) is offered freely in the MIT spirit —
use it, learn from it, improve it. The files under `patches/` are diffs against their respective
upstream projects and carry **those projects' licenses** (NVIDIA open-gpu-kernel-modules, Monado,
mutter). When in doubt, defer to upstream.

---

<p align="center"><i>Made because a good headset shouldn't die just because a vendor moved on.</i></p>
