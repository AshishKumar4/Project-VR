> 🤖 **AI-authored & maintained — presented as-is.** This document is written and maintained by
> Claude (Anthropic) as part of this project. When in doubt, trust the code.

# G2-on-Linux/NVIDIA — Authoritative Patch Index

The single source of truth for every source-level change that gets the **HP
Reverb G2** running on Linux/NVIDIA. Reconciled against live git on
**2026-05-21**, after consolidation. Patch numbers are **not unique** across
projects — always qualify by project.

**Two eras:** the G2 first ran end-to-end on **X11** (Ubuntu 24.04, NVIDIA
595.58.03) via `vkAcquireXlibDisplayEXT`; it now runs on **Wayland** (Ubuntu
26.04, NVIDIA 595.71.05-open, GNOME 50 / mutter 50.1, RTX 4080 AD103) via
`wp_drm_lease_device_v1`. The Wayland present "failure" was **display ISO-bandwidth
contention** — fixed in userspace by the `g2-studio` negotiator, not by a kernel
hack. See `docs/JOURNEY.md` for the full narrative.

**Consolidation (2026-05-21):** the per-project histories were squashed into a few
clean, well-described commits. Diagnostics (G2DBG/G2VR), dead approaches, and the
two superseded cleanups (old P23 diagnostic, P24 WC-flush) were dropped. The
pre-consolidation history (a5df8568, numbered P20–P24, etc.) survives only on the
backup branch + tag below. Each project is exported under `patches/consolidated/`.

---

## NVIDIA — `open-gpu-kernel-modules`

- Path: `src/open-gpu-kernel-modules/`
- Branch: **`g2-patches-on-595.71.05`**, base tag `595.71.05`.
- **3 consolidated commits.** Zero diagnostics remain (`git grep G2DBG`/`g2vr` =
  none). Exported: `patches/consolidated/nvidia/0001–0003`.

| Commit | What + why |
|---|---|
| **27bbc4cd** | VESA spec-correctness for VR HMDs: DisplayID 2.0 Type-VII descriptor stride (exposes the G2's 4320×2160), DSC PPS RC tables + `flatnessDetThresh` from `bits_per_component` (90 Hz DSC handshake), MSFT VSDB `primaryUseCase` for VSDB ver ≥ 1 (identify the WMR HMD). |
| **0e0f98c1** | DRM-lease VR enablement: flag the native (largest-area) mode RandR-preferred + `isVrHmd`→DRM `non_desktop=1` (mutter advertises the G2 over wp_drm_lease) + the **P22 lessee flip-permission bridge** (per-apiHead `lesseeFlipPermissive` at CREATE/REVOKE_LEASE — necessary, though the present fix was the ISO negotiator). |
| **10c39dde** | Per-device DP WAR keyed on EDID ManufID `0x220E` → `forceMaxLinkConfig` (HBR3 ×4; the G2 otherwise trains at 2 lanes). |

---

## Monado — `monado-thaytan`

- Path: `src/monado-thaytan/`
- Branch: **`g2-linux-integration`**, HEAD **`4139d4960`** (`v21.0.0-6090`).
- Our commits sit on **thaytan's `dev-constellation` base** (WMR driver +
  flexkalman constellation through `32602c1ca`). Exported:
  `patches/consolidated/monado/0001–0010` (+ its `README.md`).

| Commit | What + why |
|---|---|
| 90 Hz / bindings / RandR-wait | WMR 90 Hz nominal interval; G2 controller input bindings; wait for the native mode in X RandR before `Init()` (X11). |
| Kalman fusion | Finish the controller Kalman fusion + rigorous test suite; seqlock-snapshot thread-safety. |
| `9854e33a8` skip RandR-wait on Wayland | The X11 wait would hang forever on Wayland (leases via wp_drm_lease). |
| **`f6655e450`** Wayland direct-lease modeset | NVIDIA's WSI doesn't implicitly modeset the leased CRTC; Monado drives a spec-correct atomic modeset (native mode + routed CRTC) after `vkAcquireDrmDisplayEXT`. Committed clean, generalized — no more uncommitted working-tree change. |
| **`680b0056b`** tracking robustness | Drift/jitter fix: sanity-gate IMU samples + reject (don't reset) divergent optical poses; gentler reset only on a sustained run. +3 tests (suite = 23 cases / 609 assertions, all pass). |
| **`4139d4960`** polish | Drop a bring-up debug printf; actually `reset_filter()` on a non-finite optical correction. |

---

## mutter 50.1

- Path: `src/mutter-patch/mutter-50.1/` (Ubuntu 26.04 source package).
- **4 quilt patches** (the last 4 `ubuntu/` entries in `debian/patches/series`):
  logical-monitor NULL-safety (keeps gnome-shell alive through G2 hotplug) +
  DRM-lease REVOKE→ENOENT bookkeeping (clears `is_leased` so the lease
  re-advertises after VR exit). Exported: `patches/consolidated/mutter/01–04`.

---

## g2-studio userspace (the production VR enablement)

Not source patches; lives in `~/g2-studio/`, but **required** end-to-end on
Wayland.

| File | What + why |
|---|---|
| `core/vr_display.py` | The Wayland present fix — the **bandwidth auto-negotiator**. Probes the real driver, then a least-disruptive ladder (drop redundant duplicate/eARC head → balanced refresh dip) fits the display ISO budget; caches per desktop-set. Frees the redundant head + wakes the HMD on VR-enter, restores on exit. |
| `core/steamvr.py` (NEW) | SteamVR-on-Wayland launcher — the **#77 fix**. SteamVR loads our Monado in-process as the **device/tracking driver only** (`IVRDisplayComponent`, compositor arg `NULL` — *not* DirectMode); **`vrcompositor` does the present and leases the G2 itself**. So it needs the G2 lease **free** (no standalone monado-service) **and** the desktop **dipped** for ISO bandwidth (nothing in this path runs the negotiator). Frees the G2, dips/holds the desktop, pins perf, launches, restores. |
| `core/gpu.py` | P0 GPU clocks (RTX 4080 floor 2820 / ceiling 3105 MHz) + CPU `performance` governor. |
| `core/monado.py` | Start/stop monado-service (renice -10), wire `vr_display.enter_vr()`/`exit_vr()`. |

---

## The load-bearing few (do NOT lose)

1. `~/g2-studio/core/vr_display.py` (+ `core/steamvr.py`, `core/monado.py`
   wiring) — the *actual* present fix. **Not in `patches/`.**
2. NVIDIA `0e0f98c1` — `non_desktop=1` (mutter advertises the G2) + the lessee
   flip-permission bridge.
3. NVIDIA `27bbc4cd` + `10c39dde` — expose/train the 4320×2160@90 mode at all.
4. Monado `f6655e450` — the leased CRTC stays dark without the lessee modeset
   (**standalone OpenXR/xrgears path only**; under SteamVR, `vrcompositor`
   presents on its own and this patch is not in the path).
5. mutter's 4 quilt patches — gnome-shell survives G2 hotplug; lease
   re-advertisable after VR exit.
6. Free the redundant HDMI/eARC head on VR-enter — 4-head AD103 exhaustion
   precondition; done by `vr_display.py`.

---

## History & status

- **History** (pre-consolidation): branch `backup/pre-consolidation-2026-05-21`
  + tag `g2-backup-pre-consolidation-2026-05-21` (NVIDIA), with the original
  numbered patches and dropped diagnostics. The X11-era docs (`STATUS.md`,
  `BUGS.md`, `INVENTORY.md`) are history only — they predate Wayland and reach
  conclusions (anti-Wayland, "Bug 7 exclusivity") that newer truth overturned.
- **Status:** full VR confirmed post-reboot **2026-05-21** — **xrgears and
  SteamVR** present at native **4320×2160@90 on Wayland**, no errors. Perf opts
  live (P0 clocks, performance governor, RT scheduling via `CAP_SYS_NICE`);
  tracking-drift fix live (subjective in-headset feel pending the user's
  confirmation).
