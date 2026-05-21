# Project-VR — bringing an HP Reverb G2 back to life on Linux 🐧🥽

<p align="center">
  <img alt="Platform" src="https://img.shields.io/badge/OS-Ubuntu%2024.04%20(X11)%20%2F%2026.04%20(Wayland)-E95420?logo=ubuntu&logoColor=white">
  <img alt="GPU" src="https://img.shields.io/badge/GPU-NVIDIA%20open%20595-76B900?logo=nvidia&logoColor=white">
  <img alt="Runtime" src="https://img.shields.io/badge/runtime-Monado%20%2B%20SteamVR-1f6feb">
  <img alt="Status" src="https://img.shields.io/badge/status-working%20(personal%20project)-success">
</p>

<p align="center"><sub>🤖 The docs in this repo (including this README) are written &amp; maintained by <b>Claude</b> (Anthropic) and presented as-is.</sub></p>

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
- [What actually works today](#what-actually-works-today)
- [The setup this was built on](#the-setup-this-was-built-on)
- [What was actually wrong (and how it got fixed)](#what-was-actually-wrong-and-how-it-got-fixed)
- [Repository map](#repository-map)
- [Quick start](#quick-start)
- [Keeping it alive — the self-healing infra](#keeping-it-alive--the-self-healing-infra)
- [Honest caveats](#honest-caveats)
- [Credits & thanks](#credits--thanks)
- [License](#license)

---

## What actually works today

| Capability | Status |
|---|---|
| G2 displays its **native 4320×2160 @ 90 Hz** panel mode | ✅ |
| Full **SteamVR** (via the Monado/OpenComposite bridge) | ✅ |
| Works on **X11** (Ubuntu 24.04) — native direct-mode SteamVR | ✅ |
| Works on **Wayland** (Ubuntu 26.04 / GNOME 50) alongside the desktop | ✅ |
| Desktop stays usable during VR (no double-compositor hacks) | ✅ |
| Motion controllers as real controllers (not bare "gloves") | ✅ |
| 6DoF head + controller tracking (Basalt SLAM + a Kalman fusion pass) | ✅ *(still tuning jitter)* |
| Survives OS / kernel / driver / GNOME updates automatically | ✅ *(see `infra/`)* |

This is a **personal research project**, not a product — it's tested on my exact hardware and is
still actively evolving (tracking smoothness in particular). But the headset genuinely works, and
everything here is engineered to be clean and to generalize to other WMR/VR HMDs, not just mine.

## The setup this was built on

| | |
|---|---|
| **Headset** | HP Reverb G2 (Windows Mixed Reality, DisplayPort + USB) |
| **GPU** | NVIDIA RTX 4080 (AD103), `nvidia-driver-595-open` |
| **OS** | Ubuntu 24.04 (X11 era) → 26.04 LTS (Wayland era) |
| **Compositor** | GNOME 50 / mutter 50.1 on Wayland (and X11 before) |
| **XR stack** | Monado (Collabora) + a WMR driver fork + Basalt SLAM + OpenComposite → SteamVR |

## What was actually wrong (and how it got fixed)

The short version: **nothing about this headset is plug-and-play on Linux/NVIDIA**, and the bugs
spanned the whole stack — from the EDID parser in the GPU driver all the way up to the Wayland
compositor. Each fix is a small, self-contained, upstream-quality patch (see
[`patches/INDEX.md`](patches/INDEX.md)).

<details>
<summary><b>The display wouldn't even light up at native resolution</b> (NVIDIA kernel driver)</summary>

- **DisplayID 2.0 Type-VII parser bug** — NVIDIA's open driver silently dropped the descriptor that
  carries the G2's real 4320×2160 mode whenever the block had optional payload bytes. Walking the
  descriptors by their spec stride fixed it. (Genuinely an upstream bug — NVIDIA bug 5923212.)
- **DSC (Display Stream Compression)** — the rate-control tables didn't match the VESA DSC 1.1 spec,
  so the 90 Hz DSC handshake failed. Corrected against the spec.
- **DisplayPort link training** — the G2 trained at 2 lanes instead of HBR3×4 without a workaround.
- **Microsoft VR VSDB** — the driver only read the "this is a VR headset" flag on a newer block
  version than the G2 ships, so it never knew it was talking to an HMD.
</details>

<details>
<summary><b>SteamVR couldn't find the headset / controllers were wrong</b> (Monado)</summary>

- Flagged the native VR mode as RandR-"preferred" so SteamVR's vrcompositor actually selects it.
- Fixed the WMR 90 Hz frame interval (it reported 0 → the bridge fell back to 60 Hz judder).
- Remapped the G2 controller bindings so they show up as controllers, not pose-only gloves.
- A wait-for-the-HMD-mode step on X11, skipped on Wayland (which leases differently).
</details>

<details>
<summary><b>Wayland was a whole second adventure</b> (mutter + NVIDIA + a userspace fix)</summary>

- **gnome-shell kept crashing** when the G2 hot-plugged — a real NULL-deref race in mutter during
  monitor rebuild. Fixed with proper null-safety guards + a DRM-lease lifecycle fix (so the desktop
  doesn't wedge after you exit VR).
- **The headset would lease but not present** (`VK_ERROR_UNKNOWN`). This was the hardest bug. After
  chasing several wrong theories, the real cause turned out to be **display ISO-bandwidth
  contention**: with high-refresh desktop monitors live, the GPU couldn't also fit the G2's stream,
  so it quietly downgraded the headset's pixel format and the present failed. The fix is a small
  userspace **auto-negotiator** ([`g2-studio/core/vr_display.py`](g2-studio/core/vr_display.py))
  that briefly *asks the real driver* what fits and dips the desktop just enough on VR-enter, then
  restores it on exit. No bandwidth guesswork, no per-GPU model — it's portable.
- The kernel side also needed to expose the headset to Wayland as a leasable, non-desktop output.
</details>

A longer, narrative write-up of the whole saga — including the dead-ends I'm *not* proud of — is in
[`docs/JOURNEY.md`](docs/JOURNEY.md).

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
└── tools/          One-off diagnostic probes (vkdisplays, safe TTY VR tests)
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
- **Head/controller tracking still has some jitter** I'm working on — it's good, not yet perfect.
- Some pieces require root (kernel module install) and a reboot. There's no magic one-click yet.
- I leaned heavily on Claude for research, root-causing, and a lot of the patch authoring. I've
  tried to understand and verify everything that went in, but I'm honest about how I got here.

## Credits & thanks

Standing on the shoulders of giants:
- **[Monado](https://monado.dev/) / Collabora** — the open OpenXR runtime that makes any of this possible.
- **thaytan** and the Monado WMR contributors — the WMR driver + constellation tracking groundwork.
- **[Basalt](https://gitlab.com/VladyslavUsenko/basalt)** — visual-inertial SLAM for the inside-out tracking.
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
