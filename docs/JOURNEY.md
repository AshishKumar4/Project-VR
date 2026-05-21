> 🤖 **AI-authored & maintained — presented as-is.** This document is written and maintained by
> Claude (Anthropic) as part of this project. It's a narrative captured with AI assistance, not a
> hand-audited specification — when in doubt, trust the code.

# The G2-on-Linux/NVIDIA Journey

*One canonical narrative of how the HP Reverb G2 came to run full SteamVR on a
Linux/NVIDIA Wayland desktop — what we built, what we abandoned, the wrong turns,
and the root causes we finally nailed.*

**Synthesized 2026-05-21** from the patch index (`patches/INDEX.md`), the three
gatherer reports (`patches/_index_sources/`), the seven agent-memory files, and
the X11-era docs (`STATUS.md`, `BUGS.md`, `INVENTORY.md`, `diagnostics/`). Where
older docs reach conclusions that newer truth has overturned, the correction is
called out inline. Git ground-truth and agent memory win over older docs.

---

## 1. Mission

Run the **HP Reverb G2** as a first-class VR headset on a Linux/NVIDIA box that
*also* drives a true mixed-refresh, high-Hz (360 Hz) multi-monitor **Wayland**
desktop — and engineer it *properly*: real root-cause fixes, no hacks that wedge
the machine, durable across reboots, and generalizable to other HMDs/GPUs rather
than hardcoded to one cable on one card. The G2 must present at native
**4320×2160 @ 90 Hz** through a single compositor (no double-compositor latency
tax), coexisting with the live desktop, with head and controller tracking good
enough to actually play in.

---

## 2. Hardware / software context — the two eras

| | **Act I — X11 era** | **Acts III–V — Wayland era (current)** |
|---|---|---|
| OS | Ubuntu 24.04 LTS | Ubuntu 26.04 LTS "Resolute Raccoon" |
| Kernel | 6.17 (6.17.0-23/29) | 7.0.0-15-generic |
| NVIDIA | 595.58.03 | 595.71.05-**open** |
| Desktop | X11 + RandR | GNOME 50 / mutter 50.1, **Wayland-native** |
| VR present path | `vkAcquireXlibDisplayEXT` (Xlib direct) | `vkAcquireDrmDisplayEXT` (`wp_drm_lease_device_v1`) |

Constant across both: **RTX 4080 (AD103, 4 hardware display heads)**, i9-12900K.
Desktop monitors: a Samsung G60SD (360 Hz, on DP) and a Dell (on DP), plus the
Samsung's audio-only **eARC link surfacing as a redundant HDMI head** — a detail
that becomes load-bearing in Act V.

**The era boundary: 2026-05-19.** A `do-release-upgrade -d` took the machine
24.04→26.04 (nvidia 595.58.03→595.71.05, mutter 46→50.1, kernel 6.17→7.0.0-15).
This was a deliberate pivot, *not* a routine update — it was done **specifically
to get `wp_drm_lease_device_v1`** so the G2 could be acquired over Wayland
instead of requiring an X11 session.

Three persistent constraints shaped everything:
- **Secure Boot is on** — every patched NVIDIA `.ko` must be MOK-signed
  (`/var/lib/shim-signed/mok/MOK.{priv,der}`) or it won't load.
- **~22–31 apt-mark holds** pin the kernel, the whole `nvidia-595-open` stack,
  and our locally-rebuilt mutter so an auto-update can't silently overwrite the
  build.
- **Install drift is real**: `/usr/bin` precedes `~/.local/bin` on PATH, so a
  naked `monado-service` runs the *Debian* build, not ours. Always launch by
  explicit path or via G2 Studio. (See §9.)

---

## 3. Act I — X11 era: making the G2 display at all

On Windows the G2 just works. On Linux/NVIDIA in early 2026, the panel wouldn't
even light at native resolution. Getting there required finding and fixing a
chain of **genuinely novel driver bugs** — none previously reported in this form.
All of these were developed against 595.58.03 on 24.04/X11 and later rebased
clean onto the 595.71.05 tag (branch `g2-patches-on-595.71.05`, bundled into
commit `a5df8568`).

### 3.1 The display-enablement bug chain (NVIDIA kernel modules)

1. **DisplayID 2.0 Type-7 payload guard** (`nvt_displayid20.c`, `nvkms-evo3/4.c`).
   NVIDIA's Type-7 timing parser gated *all* descriptor parsing behind
   `if (payload_bytes_len == 0)` — so when the G2's EDID carried non-zero payload
   bytes (which VR HMDs use to encode native modes), the entire block was
   silently dropped and **4320×2160 never enumerated**; only a 2880×1440
   fallback survived. Fix: always walk descriptors at `sizeof(DESC) +
   payload_bytes_len` stride. This is the root cause of NVIDIA bug 5923212 ("G2
   stuck at 2880×1440 on Linux"). It is *Bug 1* in `BUGS.md`, the most
   upstream-worthy discovery.

2. **DP wardatabase HBR3 entry** (`dp_wardatabase.cpp`). Without an entry for HP's
   ManufID, NVIDIA trains the G2 at 2 DP lanes, not 4 — insufficient bandwidth
   for the high modes. Added a WAR-DB case → `forceMaxLinkConfig` (HBR3 ×4),
   modeled on the prior-art Bigscreen Beyond fix. *Note:* the ManufID `0x220E`
   ("HPN") was first **guessed** from PNP-code analysis and flagged as a possible
   no-op if the real EDID differed; the `-final.diff` is the verified entry. The
   `DP-WAR` dmesg line was never actually observed firing — an open question left
   from that era.

3. **DSC PPS spec compliance** (`nvt_dsc_pps.c` + `nvkms-evo3/4.c`). The DSC RC
   parameter tables (rows 9–14) didn't match the VESA DSC 1.1 spec, and
   `flatnessDetThresh` used a bogus `bitsPerPixelX16` shift instead of
   `bits_per_component` (PPS byte 3). Both produced a broken 90 Hz DSC handshake
   / "rainbow static". Ported from the `nvidia-bsb-dsc-fix` reference repo
   (Bigscreen Beyond "triple-groove" patches) — device-agnostic spec fixes.
   *These have no standalone numbered `.diff`; they live only inside `a5df8568`.*

4. **MSFT VSDB use-case for version ≥ 1** (`nvt_edidext_861.c`). NVIDIA only read
   the Microsoft VSDB "primary use case = VR headset" byte for VSDB **version ≥
   3**, but the G2 — and every Windows Mixed Reality headset — ships **version
   1**. The field is at a fixed offset in all versions (`edid-decode` reads it
   unconditionally). Reading it for version ≥ 1 is what finally lets the driver
   *know it's a VR HMD* — the prerequisite for everything VR-specific downstream.

5. **Flag the native mode RandR-preferred** (`nvkms-modepool.c`). SteamVR's
   `CHmdWindowSDL::FindDirectDisplayX` scans only an output's *preferred* RandR
   modes for a width×height match (confirmed by disassembly). The G2's preferred
   DTD is the low-res 2880×1440 compatibility mode, so SteamVR couldn't find
   4320×2160 *at all*. Fix: for displays identified as VR HMDs (via #4), flag the
   largest mode preferred. This took **three iterations** worth noting as a
   design lesson (§10): the first attempt flagged the timing in the DisplayID-2.0
   info array, but `nvkms-modepool.c` iterates the *EDID*-info array — wrong
   array, reverted. The second used a per-call timing flag that proved
   unreliable — reverted. The final form pre-scans for the highest-area Type-7
   index, then matches on that index. *Lesson: instrument which data structure
   the consumer actually reads before patching the producer.*

### 3.2 Monado / SteamVR integration (X11)

- **WMR 90 Hz frame interval** (`wmr_hmd.c`, commit `49268bc25`). The SteamVR
  bridge defaulted to a 60 Hz frame interval against the 90 Hz panel → judder.
  Set 90 Hz nominal.
- **G2 controller input bindings** (`ovrd_driver.cpp`, `fd7d8ffc8`). The G2 case
  created input components at paths absent from the generated profile, so
  controllers showed as bare poses ("gloves"). Corrected the binding paths.
- **Wait for native mode in RandR before `Init()`** (`ovrd_driver.cpp`,
  `3caa3a6c9`, +`XRT_HAVE_XCB`). Block SteamVR's preferred-mode scan until the
  native mode actually enumerates.

### 3.3 What worked end-to-end on X11 — and the runtime hack that made it go

By **2026-05-15 evening**, the G2 ran **native SteamVR on X11**: single
compositor, direct mode, 4320×2160 @ 90 Hz, image visible in the headset, head
6DOF via Basalt SLAM, controllers tracked. SteamVR's own `vrcompositor`
RandR-leased the HMD directly — coexisting with the desktop, no Path B, no
embedded compositor.

The last missing piece was **non-desktop**: NVIDIA's stack has *zero* EDID-driven
non-desktop logic (confirmed by source audit), so the HMD output reported
`non-desktop: 0`, X scanned it out as a desktop monitor, and it couldn't be
leased. The X11 fix was a per-session runtime hack:
`xrandr --output <HMD> --set non-desktop 1`, then `--off`. With that, SteamVR's
compositor acquired it first try (`Acquired xlib display!` → direct mode at
native res). This runtime hack is exactly what later became the kernel-driven
Patch 20 on Wayland (§5.3).

> **Correction (STATUS.md/BUGS.md said X; truth is Y):** `STATUS.md` and
> `BUGS.md` (both 2026-05-15) recommend *against* Wayland — "X11 + RandR is our
> proven path … `wp_drm_lease_device_v1` is broken on NVIDIA … high risk, no
> upside." That conclusion is **reversed**: the project moved to Wayland and
> confirmed full VR there on 2026-05-21. Those docs are X11-era history. They are
> *correct* about the individual X11 bug fixes, which all carried forward.

---

## 4. Act II — abandoned approaches (and why we won't repeat them)

Before the clean RandR-lease path on X11 worked, several substantial efforts were
built, sometimes ran end-to-end, and were then **deliberately dropped**. They
survive only as archive `.diff`s. Documenting them is the point of this section —
so future-us doesn't rebuild a dead end.

> **Correction:** `INVENTORY.md` lists patches **03/07/08 as "applied + on
> branch."** Git ground-truth says otherwise — they are **absent from the tree**
> (no `comp_window_direct_randr` change, **0** IVRVirtualDisplay references,
> OpenVR still **1.16.8**). They are ARCHIVE-ONLY / abandoned.

### 4.1 Path B — embedded compositor + DirectMode bypass (Bug 12 saga)

The deepest dead end. SteamVR's Linux `vrcompositor` requires the driver to
provide *either* a real DRM-leased display (`CHmdWindowSDL`, which kept failing
on NVIDIA) *or* `IVRDriverDirectModeComponent_007` (driver allocates eye textures
and accepts SubmitLayer/Present). The upstream Monado SteamVR shim only
implemented `IVRDisplayComponent`, so SteamVR fell back to the broken
`CHmdWindowSDL` path.

"Path B" was the attempt to provide DirectMode:
- **Patch 07** — `ovrd_driver.cpp` +384 LOC: `IVRVirtualDisplay` /
  `IVRDriverDirectModeComponent` stubs, then a full embedded Monado instance
  *with compositor* inside `driver_monado.so`.
- **Patch 08** — bumped the OpenVR SDK 1.16.8 → 2.15.6 (~3000-line header diff),
  needed only to compile the newer DirectMode component versions and to use
  `IVRIPCResourceManagerClient::NewSharedVulkanImage` (Bug 15).
- **`PATHB-FALLBACK-full-embedded-compositor.diff`** (174 KB) — the full superset.

It *worked*, remarkably: by 2026-05-15 ~17:20 the embedded compositor created a
`VkDisplaySurfaceKHR` on the HMD at native res, imported SteamVR's eye textures,
ran a continuous frame loop (`DM Present #1 → #1801`), and head tracking
responded. But it had two fatal problems: **black panels** (GPU-sync /
layout-transition issue) and **~10 fps** (`Present()` called `wait_frame`
*synchronously after* render, serializing the pipeline). More fundamentally it
**stacked two compositors** (a latency tax), and providing `IVRVirtualDisplay`
alone did *not* stop `CHmdWindowSDL` from trying its broken acquire anyway.

**Why dropped:** the user explicitly rejected the double-compositor —
*"I want the single compositor, direct and proper method only, with SteamVR."*
The clean RandR-lease path (Act I) made Path B unnecessary, and the Wayland
DRM-lease path (Act III+) is its proper successor. Current `ovrd_driver.cpp` has
**zero** IVRVirtualDisplay/DirectMode references; OpenVR is back at 1.16.8.

### 4.2 The HPN → "HP Inc." allowlist edit

Monado's NVIDIA allowlist had `"HPN"`, which didn't prefix-match NVIDIA's
display name `"HP Inc. (DP-N)"`. The May-15 STATUS claimed this was changed to
`"HP Inc."`.

> **Correction (STATUS.md said X; truth is Y):** the change was **reverted /
> never persisted**. `comp_settings.h:37` still reads `"HPN"`. The branch instead
> carries upstream-style allowlist commits ("remove HP desktop monitor from NV
> whitelist", "add bigscreen beyond to NV whitelist"). The runtime G2 allowlist
> entry today is `"HPN"`.

### 4.3 RandR native-resolution mode pick (Patch 03)

`comp_window_direct_randr.c` was patched to pick the highest-pixel-count mode
instead of `output_modes[0]` (compositor extents went 2880×1440 → 4320×2160). It
worked on X11. It is **superseded**: the current branch's file is unchanged, and
equivalent refresh-rate-aware `best_pixels` logic now lives upstream in
`comp_window_direct.c`. Archive-only.

### 4.4 The IPC-over-/tmp sandbox workaround (Bug 14, Plan C.1)

`driver_monado.so` as an IPC client connected to host `monado-service` succeeded
at `connect()` but the handshake **hung** — `/run/user/1000` isn't bind-mounted
into SteamVR's pressure-vessel sandbox. Running monado-service with
`XDG_RUNTIME_DIR=/tmp` fixed socket reachability but the post-connect
`SCM_RIGHTS`/credential exchange still blocked across the PID-namespace boundary.
Abandoned; pivoted to embedded (Path B) and then to native direct-mode.

### 4.5 X11 runtime non-desktop hack + xorg.conf MetaMode rewrites (Act I, §3.3)

The `xrandr --set non-desktop 1` hack and the `AllowHMD/AllowVR/NoVirtualSizeCheck`
xorg.conf surgery were genuinely load-bearing on X11, but are X11-only. On
Wayland they are **superseded** by the kernel-driven non_desktop Patch 20.

---

## 5. Act III — the Wayland port

The user wanted 360 Hz mixed-refresh multi-monitor on the desktop, which needs
Wayland. So on 2026-05-19 the box went to 26.04 for `wp_drm_lease_device_v1`,
and the X11 wins had to be re-earned on a compositor that crashes differently and
leases differently.

### 5.1 The mutter SIGSEGV saga (g2~1 → g2~6)

The G2's flaky DSC handshake makes `nvidia-modeset` **re-read the EDID on every
DP link-train cycle — 5+ times/second** (visible as `G2VR: msftVsdb` dmesg spam).
Each re-read fires a udev hotplug → `meta_monitor_manager_rebuild`. During that
rebuild, each monitor's `logical_monitor` pointer is transiently cleared to NULL
and re-assigned; the "monitors-changed" signal fires *inside* that window. The
Wayland output-event senders dereferenced the NULL → **gnome-shell SIGSEGV**, and
the whole session reset to the lock screen.

This was diagnosed precisely (crash at `meta_logical_monitor_get_layout` ←
`send_output_events` ← `meta_monitor_manager_rebuild` ← udev hotplug, frames
resolved via `addr2line` against the dbgsym package) and fixed across six mutter
iterations, **with two reverts** that are themselves the lesson:

- **g2~1 (Patch 14, SUPERSEDED):** a single NULL-guard on
  `meta_logical_monitor_get_layout`. Insufficient — gnome-shell crashed *again*,
  because the same caller also calls `_get_transform`/`_get_scale`/`_get_number`.
  Guarding one accessor masked only the deepest crash.
- **g2~2 (Patch 15-mutter, ACTIVE):** NULL-check at the *callsite* in
  `meta-wayland-outputs.c` — skip the update when `logical_monitor` is NULL (it
  fires again with a valid value once rebuild completes). The *right* fix is at
  the caller.
- **g2~3 (Patch 16-mutter, ACTIVE):** NULL-checks in `meta-monitor-manager.c` —
  a second crash site (`update_current_monitor_mode_scale`,
  `handle_orientation_change`, `get_monitor_for_connector`).
- **g2~4 (Patch 17, REVERTED — broke gdm init):** tried to enforce a global
  invariant ("current_mode≠NULL ⇒ logical_monitor≠NULL"). But mutter's *init*
  path legitimately has `logical_monitor=NULL` during `in_init=TRUE`; the
  invariant crashed the **gdm** gnome-shell at launch. The user's response set
  the project's whole tone: *"I DON'T CARE ABOUT EMERGENCY ROLLBACKS! We
  INVESTIGATE, Research, and FIX things the RIGHT and PROPER way."* Reverted.
- **g2~5 (Patch 18, ACTIVE):** DRM-lease ENOENT bookkeeping (see §5.2).
- **g2~6 (Patch 19, ACTIVE):** `g_return_*_if_fail` guards on **all 6**
  `MetaLogicalMonitor` accessors — defense-in-depth at the API boundary.

Shipped mutter set = **15b + 16b + 18 + 19**, built as `50.1-0ubuntu2+g2~6` and
held. (14 and 17 are superseded; 14 is a stray file still in `debian/patches/`
root but not in `series`.)

### 5.2 The DRM-lease desktop-wedge + ENOENT bookkeeping fix (Patch 18)

After a VR session exited, the desktop would wedge with
`drmModeAtomicCommit: Invalid argument`. Cause: when `vrcompositor` exits, the
*kernel* auto-revokes the lease, so mutter's later `REVOKE_LEASE` returns
**ENOENT**; mutter treated that as a failure and never ran `mark_revoked()`, so
the CRTC stayed flagged `is_leased` and mutter stopped re-advertising the
connector. Fix: on `REVOKE_LEASE → ENOENT`, still `mark_revoked()`.

### 5.3 Kernel-driven non_desktop=1 for HMDs (Patch 20)

The Wayland successor to the X11 `xrandr --set non-desktop 1` hack. Plumb a new
NVKMS `isVrHmd` bit (keyed on the EDID MSFT-VR VSDB + the RM
`IS_DIRECTMODE_DISPLAY` query — a *device* property, never a port) up through
`nvkms-kapi` to the DRM connector's `display_info.non_desktop = 1`. This makes
mutter advertise the G2 over `wp_drm_lease_device_v1` and stop including its CRTC
in desktop atomic batches. *Build gotcha:* both copies of `nvkms-kapi.h` had to
be edited. *Discipline:* the commit message was scrubbed of a machine-specific
"DP-2" mention — **no DP-N hardcoding** (the mission's generalizability rule).
Verified working: connector 136 (DP-2) reported `non-desktop=1`.

### 5.4 Monado: skip the RandR wait on Wayland (Patch 15-monado)

The X11 RandR-mode wait (Patch 10) would hang forever on Wayland (no X RandR).
Detect a Wayland session and skip it (commit `9854e33a8`).

> **Note on number collision:** "Patch 15" and "Patch 16" each name *two*
> different patches — a Monado one (`9854e33a8` / `5baabfea1`, exported as
> `15-monado-…`/`16-monado-…`) and a mutter one (g2~2 / g2~3). Always qualify by
> project. The `16-mutter` archive `.diff` is also a STALE-SNAPSHOT — it's
> missing the `get_monitor_for_connector` hunk that's in the built version
> (built = 3 hunks/97 lines, archive = 2 hunks/72 lines).

---

## 6. Act IV — the present-failure root-cause hunt (the hard part)

Five Wayland components verified working *independently*: patched nvidia-drm
loaded with `non_desktop=1`; mutter g2~6 installed; Monado v21-6087 acquires the
lease (connector 136 DP-2 granted); Monado creates the `VkSurface` + swapchain
(4320×2160 A2B10G10R10 FIFO) + vblank thread. And yet **the very first
`vkQueuePresentKHR` returned `VK_ERROR_UNKNOWN`** and nothing rendered.

Crucially, the *identical* Monado render logic works end-to-end on X11 via
`vkAcquireXlibDisplayEXT`. So the bug had to be in NVIDIA's
`VK_EXT_acquire_drm_display` (DRM-lease) path specifically — confirmed by the
public NVIDIA forum thread 341244 (Index / BSB / G2 all fail to DRM-lease on
555–580+, NVIDIA's own 2023 statement that "DRM display leasing does not
currently work" for VR, never lifted).

This act is a chain of **wrong hypotheses, each falsified by instrumentation**.
Recording the falsifications is the most valuable thing in this document — they
are the traps.

### Hypothesis 1 — "the lessee never modesets" (true, but not the whole story)

Mesa/RADV's WSI does an implicit atomic modeset on first present
(`CRTC.MODE_ID` + `CRTC.ACTIVE=1` + `CONNECTOR.CRTC_ID`,
`ALLOW_MODESET`). NVIDIA's WSI does **not**. mutter (like wlroots/KWin) hands the
CRTC over raw with `ACTIVE=0` and no `MODE_ID` — by Keith Packard's design, the
**lessee** must modeset. So **Patch 21** (`comp_window_direct_wayland.c`) makes
Monado itself drive the atomic modeset on the leased CRTC after
`vkAcquireDrmDisplayEXT`. This was *necessary* and correct — and it succeeded
(`Lessee modeset OK: CRTC 63 active @4320×2160@90`) — but `vkQueuePresentKHR`
**still** returned `VK_ERROR_UNKNOWN`. The modeset wasn't the wall.

### Hypothesis 2 — "flip permission denied (EPERM)" (the red herring, Patch 22)

`strace` caught the real failing syscall: `NVKMS_IOCTL_FLIP` →
`ioctl(<\/dev\/nvidia-modeset>, …) = -1 EPERM`. The reading at the time:
libnvidia-vulkan.so opens its *own* `/dev/nvidia-modeset` fd, registers surfaces
fine, then `FLIP` is rejected because that popen has no flip permission on the
leased head — on X11, Xorg calls `GRANT_PERMISSIONS`; on the DRM-lease path,
nothing does. So **Patch 22** built a DRM-lease → NVKMS bridge:
per-apiHead `lesseeFlipPermissive` set at `CREATE_LEASE`, cleared at
`REVOKE_LEASE`. `diagnostics/wayland-drm-lease-nvidia-vk-present.md` documents
this as *the* fix.

> **This is where the discipline mattered.** Instrumentation
> (G2DBG/G2DBG2/G2DBG3/G2DBG4 printks through the NVKMS flip path) revealed two
> things that demolished the EPERM-as-permission story:
> 1. `nvkms_ioctl_common` ends with `return ret ? 0 : -EPERM` — **every** NVKMS
>    failure is mapped to `-EPERM`. "EPERM = permission denied" was an *unverified
>    assumption*; EPERM is just NVKMS's catch-all.
> 2. Patch 22 *did* clear the permission gate (`setLesseeFlipPermissive=1`
>    confirmed) — and the flip **still failed**, deeper, in `ValidateUsageBounds`.
>
> **Net:** Patch 22 is real, correct, and necessary (a lessee genuinely needs
> flip permission) and stays in the build — but it was **not the present fix**.

### Hypotheses 3–5 — inert/disproven experiments (all reverted)

- **2Head1OR via `isVrHmd`.** The G2 @4320×2160@90 has a 910 MHz pixel clock,
  above the single-head max, so the reasoning went: force it across two hardware
  heads to halve per-head ISO bandwidth. Made `nvEvoUse2Heads1OR()` return TRUE
  for `isVrHmd`. **Proven INERT:** G2DBG showed `mergeSec=0` — the G2 was using a
  *single* head, and reducing desktop bandwidth gave that single head full 32 bpp
  with zero VK errors. 2Head1OR splits the raster but doesn't reduce *total* ISO,
  so it couldn't help. Reverted. (Earlier reasoning had flip-flopped repeatedly
  on whether 2Head1OR was needed; the proven answer is **no**.)
  > **Correction:** memory file `g2-head-exhaustion-rootcause` states the G2
  > "needs 2Head1OR." The *final* truth (`g2-patch22-flip-eperm`) is that for the
  > **present/flip** the G2 used a single head (`mergeSec=0`). 2Head1OR is a real
  > head-budget concern only for the *modeset* under head exhaustion (§7.2), not
  > the bandwidth ceiling.
- **fd-fix:** pass the leased *master* fd (not the read-only advertise fd) to
  `vkGetDrmDisplayEXT`. **Falsified** — same `VK_ERROR_UNKNOWN`. Reverted.
- **dumb-flip probe:** issue a raw dumb-buffer atomic flip on the leased CRTC to
  see if nv_drm would accept *any* G2 flip. It failed the same way → **proved the
  failure is not WSI-specific** (and disproved O-1, the "bypass the WSI"
  optimization, as a present fix). Reverted.
- **surfaceful modeset:** test whether Patch 21's surfaceless modeset failed to
  establish usage bounds. **Disproven.** Reverted.

### The real root cause — total display ISO-bandwidth contention

With **Dell@120 + Samsung@120 + G2 all live** on the 4-head AD103, NVKMS's IMP
(`DownGradeMetaModeUsageBounds`, nvkms-evo.c) **downgraded the leased head's
`possibleUsage` to ≤16 bpp** (`possFmt=0xf`) to fit the GPU's *total display ISO
bandwidth budget* — memory clock was already maxed (11201 MHz), no headroom. But
the G2 flip declares 32 bpp formats (`flipFmt=0x3ff`), so `ValidateUsageBounds`
rejected it → NVKMS catch-all `-EPERM` → WSI surfaces it as `VK_ERROR_UNKNOWN`.

The falsification that clinched it (the **contention test**, the "$1000 bet"):
- **Samsung-only (Dell disabled) + G2** → `possFmt` full 32 bpp, **0 VK errors,
  full VR renders.**
- **Dell@120 + Samsung@120 + G2** → downgrade → fail.

So it's a **total** ISO budget, not per-head, not permission, not WSI, not a
fundamental single-head limit.

> **Correction (BUGS.md Bug 7 said X; truth is Y):** `BUGS.md` Bug 7 framed the
> symptom as "VK_KHR_display exclusive direct mode breaks other displays — reboot
> required," an exclusivity state-machine bug. **Wrong.** The real ceiling is
> total display ISO bandwidth on the 4-head AD103. X11 worked *alongside* the
> desktop because the 24.04 desktop simply used less display bandwidth — not
> because of exclusivity. (The exclusivity hazard *is* real, but only for the old
> `VK_KHR_display`/TTY direct path, not the mutter-mediated wp_drm_lease path —
> see §7.4.)

---

## 7. Act V — the production fix

The root cause is a *policy* problem (how much desktop display bandwidth to spend
during VR), not a missing kernel mechanism. So the fix lives in **userspace**,
where it can probe the real driver and stay generalizable — not in a kernel hack.

### 7.1 The auto-negotiator (`~/g2-studio/core/vr_display.py`)

This is **the change that made full VR render** (xrgears reached
`XR_SESSION_STATE_FOCUSED`, panels lit, 90 Hz, 0 VK errors, **2026-05-21**). It
is *not* a patch and *not* under `g2-linux-research/` — it's in `~/g2-studio/`,
wired into `core/monado.py`'s start/stop.

- **`probe()` — ask the real driver, don't model bandwidth.** Briefly runs
  monado-service and watches whether the present hits `VK_ERROR_UNKNOWN`
  (bandwidth downgrade) or reaches the frame loop clean. The driver is the
  oracle, so this is portable to *any* GPU/monitor mix — essential for
  open-source rather than a fragile per-card bandwidth formula.
- **`negotiate()` — least-disruptive ladder, best→worst, first pass wins:**
  1. **Keep everything** (only probed if a duplicate head exists).
  2. **Drop redundant *duplicate* heads** — the eARC HDMI mirroring a DP monitor
     (free: audio is in the G2's headphones during VR).
  3. **Balanced refresh dip** — keep the primary as high as possible, dip *all*
     secondaries together under a uniform refresh cap (high→low), only touching
     the primary if the lowest secondary cap still fails.
- **Caching:** the winning config is cached keyed by the desktop monitor set
  (`~/.config/g2-studio/vr-display-cache.json`), so it probes once per physical
  setup, then is ~instant. On this machine it auto-discovered Samsung@360
  (primary, untouched) + Dell dipped.
- **`enter_vr()` / `exit_vr()`:** save the full layout, wake the HMD, apply the
  negotiated config; restore on exit.

### 7.2 The precondition — free the redundant eARC head (head exhaustion)

Before the bandwidth ceiling even comes into play, the *modeset* itself fails with
EINVAL when the 4 hardware heads are oversubscribed. Proven via bpftrace on the
NVKMS reply struct: `disp0.status = 6 = FAILED_TO_ASSIGN_HARDWARE_HEADS`. Head
math under the worst case: Dell(1) + Samsung video(1) + Samsung **eARC
audio**(1) + G2(2 if 2Head1OR) = 5 > 4. The redundant **HDMI-A-1 head** is just
Samsung's audio-only eARC link (video is on DP); freeing it on VR-enter clears
the exhaustion → `disp0.status=0`, modeset OK. The user accepts dropping room
audio during VR (the G2 has built-in headphones). Done by `vr_display.py` /
`scripts/vr-display-heads.py` via mutter's `ApplyMonitorsConfig`.

### 7.3 GPU clock pinning (O-5)

`~/g2-studio/scripts/vr-gpu-pin.sh` pins the RTX 4080 (SM 2820–3105 MHz, mem
11201 MHz, 320 W) + watchdog. A script, not a source patch.

### 7.4 Waking a sleeping G2 in software

If DP-2 shows `disconnected` after a reboot/idle but USB is enumerated, the panel
is just in standby. Run `~/.local/bin/monado-service` (~20 s) — its WMR driver
powers the panel on over USB and brings the DP link up. Do **not** force a DRM
detect/modeset to wake it: the "Samsung hangs on G2 wake" risk belongs to the old
`VK_KHR_display`/TTY exclusive path, *not* the mutter-mediated wp_drm_lease path.

### Why userspace policy beats a kernel hack here

A kernel patch can't know the user's preference (drop audio? dip Dell? keep 360 Hz
on the Samsung?), can't be probed against the real driver without rebooting, and
risks hardcoding one machine's monitor layout. Userspace policy that *asks the
driver* and *caches the answer* is durable, generalizable, and reversible — which
is exactly the mission. The kernel patches (20–24) provide the *mechanism*; the
negotiator provides the *policy*.

### 7.5 Two committed kernel cleanups that did land (O-3, O-7)

- **Patch 23 (`8e735f6d`, O-3):** gate the `G2VR: msftVsdb` diagnostic to once
  per EDID change (the spam — likely emitted by our own Patch 11 path — correlated
  with DP-link-flap storms).
- **Patch 24 (`c2c87dfd`, O-7):** gate the write-combining `sfence` flush to
  dumb-buffer commits only (VR present uses imported dmabufs → skips it).
  Demoted from "perf optimization" to a cleanliness patch — explicitly **no perf
  claim** (it's a single sfence).

---

## 8. Current state (2026-05-21)

**Works today:** full VR confirmed rendering in the G2 on Wayland — xrgears
FOCUSED, panels lit, 90 Hz, 0 VK errors. The end-to-end stack:

```
Game (OpenXR / OpenVR) → [OpenComposite if OpenVR] → SteamVR → driver_monado.so
  → monado-service (our build, v21-6087-g5baabfea1; WMR driver + Kalman seqlock + basalt SLAM)
  → comp_window_direct_wayland → VkDisplaySurface → vkQueuePresentKHR
  → patched nvidia-drm (isVrHmd → non_desktop=1)
  → mutter g2~6 advertises G2 via wp_drm_lease_device_v1, leases DP-2
  → HP Reverb G2 @ 4320×2160 @ 90 Hz, A2B10G10R10, DSC
```

**The load-bearing few (do not lose):**
1. `~/g2-studio/core/vr_display.py` (+ `core/monado.py` wiring) — *the actual
   present fix*. Not in `patches/`.
2. Monado `comp_window_direct_wayland.c` lessee modeset (**Patch 21,
   UNCOMMITTED** — the only working-tree change in monado; the leased CRTC stays
   dark without it).
3. NVIDIA Patch 20 (non_desktop=1), Patch 22 (lesseeFlipPermissive — necessary
   though not the fix), and the `a5df8568` foundations (DP HBR3 / DisplayID Type-7
   / DSC PPS / MSFT VSDB / preferred-mode).
4. mutter g2~6 set (the 4 `series` patches).
5. Free the redundant eARC head on VR-enter (head-exhaustion precondition).

**Pending / open:**
- **Reboot to verify the clean `.ko`.** The committed NVIDIA tree (`c2c87dfd`) is
  verified clean of G2DBG/2Head1OR diagnostics, but the modules *running at
  session end were still the diagnostic build*. After reboot, confirm the loaded
  `nvidia-modeset.ko`/`nvidia-drm.ko` match the clean signed build (compare
  `srcversion`; expected nvidia-drm `ED1AA907B910DA118328E13`).
- **Commit Patch 21** after stripping its remaining verbose diagnostic logging
  (`lessee modeset: connector_id=… count_modes=…`, `leased fd has %d CRTCs`,
  per-CRTC dump, atomic-commit errno). The dumb-flip/surfaceful test code is
  already gone; this logging is not.
- **Head/controller tracking polish.** Controller jitter was largely solved by
  the Kalman seqlock (a render-thread torn read of multi-word filter state caused
  11 m / 171 m·s⁻¹ spikes; fixed with a single-writer seqlock publishing a POD
  `FilterSnapshot`, reader wait-free; 20-case/584-assertion test suite green).
  Residual in-VR jitter/drift is SLAM/tracking tuning, tracked separately.
- **Install drift** (PATH ordering, basalt SONAME mismatch on 26.04, basalt
  source ambiguity) — verify the right binaries are actually loaded before any
  "VR is broken" claim (§9).

---

## 8b. Act VI — SteamVR parity on Wayland + tracking robustness (2026-05-21)

The capstone: consolidate the messy histories into a clean build, verify it
end-to-end after a reboot, then close the last two gaps — SteamVR (not just
OpenXR) on Wayland, and visible tracking jumps.

**Clean build, verified post-reboot.** The per-project histories were squashed
into a few well-described commits (NVIDIA → 3, Monado → 10, mutter → 4),
diagnostics and dead approaches dropped, and exported under
`patches/consolidated/`. Rebuilt and reloaded, the stack still works:
**xrgears reached `FOCUSED` at native 4320×2160 @ 90 Hz on Wayland with zero VK
errors.** No regression from the cleanup.

**Performance opts live.** P0 GPU clocks (RTX 4080 floor 2820 / ceiling 3105 MHz,
power cap), the CPU `performance` governor, and Monado's RT thread scheduling
(SCHED_FIFO via `CAP_SYS_NICE`) are all in the path during VR — wired through
`g2-studio/core/gpu.py` and the launchers.

**SteamVR on Wayland solved (#77).** OpenXR worked, but SteamVR didn't.
*Root cause:* SteamVR's Monado driver (`ovrd_driver`) loads **our** Monado
in-process inside `vrserver` as the **device/tracking driver only** — it creates
the device system but passes `NULL` for the compositor
(`xrt_instance_create_system(..., NULL)`, line 1718) and exposes the HMD via
`IVRDisplayComponent`, *not* `IVRDriverDirectModeComponent` (the abandoned Path B,
§4.1). So **SteamVR's own `vrcompositor` does the present** and acquires the G2
directly (RandR on X11, `wp_drm_lease_device_v1` on Wayland) — Monado's compositor
(`comp_main`, and thus the `f6655e450` lessee-modeset) is *not* in this path; that
patch serves the standalone OpenXR/xrgears path instead. There is no standalone
`monado-service` here, so the OpenXR-mode plumbing didn't apply: `vrcompositor`
needs the **G2 lease free** (so it, not a standalone service, can acquire it)
*and* the **desktop dipped for ISO-bandwidth** (nothing in the SteamVR path runs
our negotiator, so without the dip `vrcompositor`'s WaitForPresent watchdog times
out and aborts). The G2 present itself is carried by the **NVIDIA kernel patches**
(`non_desktop` advertise + native-mode-preferred + the flip-permission bridge),
not Monado. *Fix:* a clean, dedicated launcher, `g2-studio/core/steamvr.py` — free
the G2, dip and *hold* the desktop (re-applying the dip after the G2 hotplug makes
mutter reconfigure and revert it), pin GPU/CPU perf, launch SteamVR, restore on
exit. Mirrors `monado.start()/stop()` for the OpenXR path.

**Tracking drift/jitter fix.** Two root causes of visible jumps, both addressed
in `t_tracker_kalman_fusion.cpp`: (1) a single corrupt IMU sample (MEMS glitch /
dropped USB packet) was integrated raw and then tripped a check that wiped the
*entire* filter — now sanity-gate the raw gyro/accel, skip a lone outlier
(filter preserved), reset only on a sustained run; (2) a divergent
constellation/LED pose that passed its own reprojection score jerked tracking —
now reject (not reset) any optical pose that jumps farther than a controller
physically could, leaving the filter + anchor intact. +3 tests; the full suite
(23 cases / 609 assertions) passes.

**Honest status:** the clean build, perf opts, and both fixes are live and
test-green, but the **subjective in-headset feel** — tracking quality and SteamVR
gameplay — still awaits the user's confirmation.

---

## 9. Lessons learned (the transferable engineering)

1. **Falsify before you fix.** Every wrong hypothesis in Act IV (modeset, EPERM
   permission, 2Head1OR, fd-fix) *looked* right and some even produced a working
   patch. Each was killed by instrumentation, not argument — strace for the exact
   ioctl, printks for the exact rejecting gate, the contention test
   (Samsung-only works / Dell+Samsung fails) for the actual variable. The
   "$1000 bet" framing forced a *decisive* experiment instead of more theorizing.

2. **EPERM was an NVKMS catch-all, not a permission verdict.**
   `return ret ? 0 : -EPERM` maps *every* failure to EPERM. An errno is a hint,
   not a diagnosis — confirm what actually failed before naming it.

3. **Leased outputs aren't in mutter's monitor list.** non_desktop connectors are
   filtered out before they reach `MetaOutput`, so they never enter the desktop
   modeset pipeline — which is *why* the lessee must modeset, and why HMD-awake
   has to be detected via DRM sysfs (a connected connector not in mutter's set),
   not via mutter's monitor list.

4. **Probe the real driver instead of modeling bandwidth.** A per-card ISO
   formula would be fragile and unportable. Asking the driver "does the present
   succeed?" and caching the answer is robust across any GPU/monitor mix — the
   right shape for open-source.

5. **Keep policy in userspace; keep mechanism in the kernel.** The kernel patches
   give the *ability* to lease, flip, and mark non-desktop; the negotiator
   decides *how much desktop to sacrifice*. Policy that depends on user
   preference and live hardware belongs where it can be probed and reversed
   without a reboot.

6. **"A patch that works" ≠ "the patch that's the fix."** Patch 22 works, clears
   a real gate, and is necessary — but it never fixed the present failure.
   Distinguish patches that are *load-bearing for the actual problem* from
   patches that are *correct but incidental*, or you'll attribute the win to the
   wrong change (as the diagnostics doc initially did).

7. **Patch the producer where the consumer actually reads** (the preferred-mode
   three-iteration saga): the fix went into the wrong array twice before someone
   checked which structure `nvkms-modepool.c` iterates.

8. **Investigate, don't paper over.** The reverted g2~4 invariant and the
   superseded g2~1 single-accessor guard both *masked* symptoms; the durable fixes
   (callsite skip + all-accessor guards + correct init-path handling) came from
   understanding mutter's rebuild lifecycle, per the user's standing directive.

9. **Generalize, don't hardcode.** No "DP-2" in commit messages or code;
   `isVrHmd` keyed on a device property (MSFT VSDB + IS_DIRECTMODE_DISPLAY), not a
   port; the negotiator portable to any monitor set. The mission was a *durable,
   generalizable* setup, not a one-machine hack.

10. **Verify what's actually loaded.** Three monado binaries coexist on disk;
    PATH order silently runs the wrong one. basalt links to libs absent on 26.04.
    Before debugging upstream symptoms, run `which monado-service` and
    `ldd $(which basalt)` — fix install drift first.

---

## 10. Document trust order (when sources disagree)

Freshest → stalest, so future readers know which to believe:

1. **Agent memory** (`g2-patch22-flip-eperm`, `g2-vr-display-autoneg`,
   `g2-head-exhaustion-rootcause`, `g2-steamvr-working`, `g2-wake-via-monado`,
   `g2-architecture-end-to-end`, `g2-x11-worked-wayland-bug`) — 2026-05-20/21.
2. **`patches/INDEX.md`** — git-reconciled authoritative patch state.
3. **`diagnostics/*.md`** — current on mechanics; the
   `wayland-drm-lease-…` doc's *permission-bridge framing* is superseded by the
   ISO-bandwidth root cause (Patch 22 still works/needed).
4. **`INVENTORY.md`** — best top-level snapshot, but catalog stops at 16 and
   wrongly lists 03/07/08 as applied.
5. **`STATUS.md` / `BUGS.md` / `ROLLBACK.md` / `POST-REBOOT-CHECKLIST.md` /
   `RESEARCH.md`** — 2026-05-15, X11/24.04 era. History only; several active
   contradictions (anti-Wayland recommendation; Bug 7 exclusivity claim;
   "HP Inc." edit; wrong kernel/driver versions).
