# G2 Linux Research — project instructions

Project-specific standing constraints for getting my HP Reverb G2 fully working on Linux/NVIDIA. This
complements the global `~/.claude/CLAUDE.md` (general working style) — read both. The global rules
(the right and proper way, no gates, prove-don't-guess/$1000-bet, SOTA, autonomy, 4→2→1 audits,
commit-all-at-once-after-audit, etc.) apply here too; this file holds only the project/machine facts.

## The mission
- HP Reverb G2 fully working on Linux/NVIDIA Wayland + SteamVR — *"the level of performance and quality that
  I got out of my g2 on windows, infact even better."* Head AND controller tracking, SOTA, no hacks.
- Single compositor, direct + proper SteamVR via OUR latest self-built Monado driver/service —
  **no fallback to OpenComposite.** Patch source so it's upstreamable; must generalize to a fresh Ubuntu
  install and other WMR/Monado headsets, not just my rig.
- Display/present is **solved**. The live front is **controller tracking quality** — the optical front-end
  mirror-flip. The backend ESKF is solid.

## Where things live (re-ground from here)
- Research tree: `~/g2-linux-research/` — **put all working files here, not in `~` / home root** ("Keep that
  clean and tidy"). Telemetry/analysis tools: `~/g2-linux-research/tools/telemetry/`.
- Monado fork (the upstreamable repo, its own git): `~/g2-linux-research/src/monado-thaytan`, branch
  `g2-linux-integration`, build dir `build-cmake` (ninja). Don't drop a CLAUDE.md or AI-slop into this repo.
- Docs/audits: `~/g2-linux-research/docs/`, `docs/audit-2026-05-24/` (live tracker `08-progress.md`).
- Auto-memory: `~/.claude/projects/-home-mrwhite0racle/memory/` (`MEMORY.md` index) — read first; it has the
  G2 architecture, ESKF verdict, controller-tracking bottleneck, IMU/hardware findings, security posture.
- Canonical patch/docs: cross-reference actual applied patches + transcripts; indexes may be stale.
- VR repo remote: `git@github.com:AshishKumar4/Project-VR.git`.

## Build & test (verify, don't assume)
TWO build dirs under `src/monado-thaytan/` — don't confuse them (this has bitten me):
- **`build-cmake/` (cmake)** — builds **`driver_monado.so`** (the SteamVR tracking plugin = ALL the
  controller/ESKF/constellation/telemetry/head_pose code) AND every ctest/offline harness. This is THE
  build for tracking work. `ninja -C build-cmake driver_monado.so`.
- **`build/` (meson, prefix `~/.local`)** — builds standalone **`monado-service`** for the OpenXR path
  (xrgears/hello_xr smoke tests). `ninja -C build install` → `~/.local/bin` + `sudo setcap cap_sys_nice+ep
  ~/.local/bin/monado-service`. NOT used by the SteamVR/production/capture path.
- Tests (Catch2 via ctest, in build-cmake): `tests_kalman_fusion`, `tests_constellation_pnp`,
  `tests_g2_telemetry`. `ctest -R … --output-on-failure`.
- Offline VIO harness: `tests/offline_vio_replay.cpp` (build-cmake target `offline_vio_replay`) — drives
  the real tracker+ESKF on dumped PGM frames; deterministic via a per-frame completion barrier (not a
  sleep). Use it + the MSE/flip-rate loss (`tools/telemetry/{deflip,smooth_ref,mse_eval,g2_geom}.py`) to
  validate on real captures.

## Run & capture — the PRODUCTION path is SteamVR, NOT standalone monado-service
SteamVR's `vrserver` loads our **`driver_monado.so`** in-process as the tracking/device driver;
`vrcompositor` presents to the G2 (DRM-lease). `monado-service` is *killed* to free the lease — it is NOT
in this path. SteamVR finds the driver via `~/.config/openvr/openvrpaths.vrpath` → `external_drivers:
[~/.local/share/steamvr-monado]`. Launcher = the `~/g2-studio` repo (`python3 -m core.steamvr {start|stop}`;
wakes the G2, dips the desktop for ISO bandwidth, pins GPU/CPU). Login-time tracking env lives in
`~/.config/environment.d/g2-vr.conf` (`WMR_SLAM`, `SLAM_SUBMIT_FROM_START`, `VIT_SYSTEM_LIBRARY_PATH`,
`WMR_AUTOEXPOSURE`) — `SLAM_SUBMIT_FROM_START=true` is what makes the SLAM head pose run (else head dead-reckons).

**Controller transport + factory-calib fetch (verified 2026-05-25 by a fresh device read).** G2 controllers
connect via the **HMD tunnel** (HoloLens-Sensors USB), NOT host BlueZ — Monado adds them in
`hololens_ensure_controller` when the headset reports them *online* (`wmr_hmd_controller.c`); they only need
to be **awake** (no PC Bluetooth pairing), so `wmr_estimate_system`'s `left/right: None` (the BlueZ path) is
expected/irrelevant. The deobfuscated factory calib is cached as `~/.config/monado/wmr/controller_<serial>.json`;
`read_calibration_cache` **skips the block-0x02 device read on a cache hit**, so to re-fetch fresh from the
device **delete that cache file first** (`WMR_LOG=debug` shows the chunked `0x02` read). A fresh read is
**byte-identical** to the cache → calib is authentic; both controllers = dual-IMU **ICM-20602 (2×Gyro+2×Accel),
ZERO magnetometer** — yaw stays an optical/inertial problem, no mag fusion. DRIFT: deployed
`~/.local/bin/monado-service` is stale — caches with an underscore (`controller_%s.json`) while current source
uses a hyphen (`controller-%s.json`); rebuild+redeploy (`ninja -C build install`) to sync.

**Build + deploy the driver** (deploy = a `cp`, there is no `ninja install` for it):
```
cd ~/g2-linux-research/src/monado-thaytan
ninja -C build-cmake driver_monado.so
cp build-cmake/steamvr-monado/bin/linux64/driver_monado.so ~/.local/share/steamvr-monado/bin/linux64/
md5sum ~/.local/share/steamvr-monado/bin/linux64/driver_monado.so   # confirm vs the freshly-built one
```
**Capture (in-headset).** GOTCHA: a warm Steam reuses its OLD env, so `G2_*` flags never reach `vrserver`
— **cold-kill Steam first**, then pass the flags INLINE on the launch (vrserver inherits them):
```
cd ~/g2-studio && python3 -m core.steamvr stop
pkill -9 -x steam; pkill -9 -x steamwebhelper; sleep 3
CAP=~/g2-linux-research/captures/$(date +%Y%m%d-%H%M%S)-<tag>
mkdir -p "$CAP/telemetry" "$CAP/frames" "$CAP/euroc"; echo "$CAP" > /tmp/g2_cap_path
G2_TELEMETRY="$CAP/telemetry" G2_DUMP_FRAMES="$CAP/frames" G2_RECORD="$CAP/euroc" [G2_NO_FLIP_VETO=1] \
  setsid -f python3 -m core.steamvr start >/tmp/g2-vr-launch.log 2>&1
```
Flags: `G2_TELEMETRY`=imu(dev0 HMD/1 L/2 R)+frame+pose_attempt+fusion+event+**head_pose** .bin streams;
`G2_DUMP_FRAMES`=controller short-exp LED PGMs (offline-replay input); `G2_RECORD`=SLAM long-exp frames as
EuRoC. **Verify** vrserver actually got them: `tr '\0' '\n' </proc/$(pgrep -x vrserver)/environ | grep ^G2_`,
and the `.bin`/`.pgm`/`.png` files are growing. **Stop** (graceful flush): `python3 -m core.steamvr stop`,
then check `telemetry/manifest.json` rows_written/overflow. The user does the physical choreography
(dropouts/tilts/yaws/edge-FOV, both controllers, ~1–2 min); I do build/deploy/launch/verify/stop.

## Environment / hardware (hard constraints)
- **GNOME only** — *"I dont want KDE plasma."* **Wayland**, set default automatically (no manual steps).
- **NVIDIA's open drivers, not nouveau.** Build/install NVIDIA-open (not DKMS) + MOK-sign for Secure Boot.
- *"I want all mitigations off for maximum performance."*
- **Don't break the desktop or my Dell/Samsung displays** while bringing up the G2 (the Wayland DRM-lease
  path exists for coexistence). No TTY-only or session-switching workflows.
- **Never hardcode** connector names like `DP-2` ("Those change often") — auto-detect; survive updates.
- G2 panel often needs a **software wake** after reboot (run `monado-service` to wake DP-2 over USB; don't
  force DRM detect, don't ask me).

## Security / machine
- MOK private key `/var/lib/shim-signed/mok/MOK.priv` stays private.
- Keep the scoped `/etc/sudoers.d/g2ctl` (broad root removed; scoped perf+install kept).
- Auto-heal stays **disarmed while I'm AFK**; re-arm only when asked.

## Data safety (named, irreplaceable — this machine)
- Never blanket-delete; *"If anything important is missing, tell me BEFORE the wipe step."*
- Protected: `backup-latest.zip` (Minecraft server backup), Ollama models, games, conda envs. Preserve
  `Users` folders as-is (minus caches). Steam library is redownloadable.
- *"no dedups, only clean the 100% noises"*; prefer de-index/ignore over delete; verify backups completed.
- Damaged drives: P1 (nvme, 86k+ media errors) = scratch/regenerable only. Photo-recovery USB `/dev/sdb`
  is **read-only — stop all writes** until recovery is done.

## Git / branch / upstream (project)
- Active branch `g2-linux-integration`. **Don't touch my active branch/worktree** for exploratory work —
  use a separate worktree+branch (e.g. for the upstream rebase). I rewrite/reorganize history out-of-band;
  re-familiarize before continuing.
- Commit everything **at once, after a proper audit** — not small batches. Clean, squashed, logical history;
  no backups/diagnostics/benches in the repo; never `--no-verify`.
- Goal: **upstream everything.** Consolidate duplicates with upstream and keep the better code (adopt
  upstream's tracker), **losing none of our/thaytan's G2 work**; stay able to fetch upstream + be in sync.
- AI-maintained docs in the repo carry a visible "edited & maintained by Claude, presented as-is" disclaimer.
  README in my humble, honest, first-person voice.

## Tracking / VIO convictions (the engineering bar)
- **Complementary fusion:** *"Optical + IMU/accel… should help solve each other's weeknesses and compliment
  each other."* Pick the right source of truth per situation.
- **Uncertainty-aware, no false positives:** model uncertainty growing with fewer LEDs; resolve ambiguity
  within the envelope; use noisy data instead of bailing. No gates/flags — one unified associator.
- **Bias calibration:** full gyro+accel bias + accel ellipsoid; use factory calibration; add mag if possible;
  persist/bootstrap biases across sessions (cache/EMA).
- **Drift:** *"figure the drift out and cancel it"* (e.g. constant gyro-bias yaw drift; counter via
  optical↔inertial consistency + head/SLAM captures). Controllers must not go off-track when off-camera.
- Diagnostics: test pure-IMU-only to gauge inertial quality. Betaflight is my reference for IMU
  filtering/FFT noise analysis. Real human motion is smooth, continuous, differentiable — non-smooth output
  is a bug.

## Current target & state
- Build the **unified covariance-driven SOTA associator** (NOT incremental): the ESKF's anisotropic prior
  (tight tilt / loose yaw) folds partial frames + auto-rejects flips — **replacing** the legacy fast-path +
  ab-initio cascade entirely. No legacy paths, no gates.
- Validate against the MSE/flip-rate loss on real offline data + a cleaned-GT foundation first.
  **Validate offline before asking me for a new in-headset capture.** Don't make numbers look good by
  degrading the harness.
- Diagnosis on record: ~92% of flips are gravity-resolvable TILT flips (single-cam, drift-free, cold-start),
  ~8% pure-yaw; flips originate in ab-initio. See `docs/audit-2026-05-24/08-progress.md`.
