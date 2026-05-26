<!-- Edited & maintained by Claude; presented as-is. -->

# Monado consolidated patches (HP Reverb G2 / WMR on Linux)

> 🤖 AI-authored & maintained, presented as-is.

Our changes on top of **thaytan's Monado `dev-constellation`** branch (forked at
`d77f2f157`, which already carries thaytan's WMR driver + flexkalman constellation
work through `32602c1ca`). Apply on top with `git am 00*.patch`.

| # | Change |
|---|---|
| 0001 | WMR HMD 90 Hz nominal frame interval (was 0 → 60 Hz judder) |
| 0002 | HP Reverb G2 controller input bindings (real controllers, not pose-only gloves) |
| 0003 | SteamVR driver: wait for the HMD native mode in X RandR before Init() (X11) |
| 0004 | Finish the controller Kalman fusion |
| 0005 | Rigorous Kalman controller-fusion test suite |
| 0006 | SteamVR driver: skip the RandR wait on Wayland (leases via wp_drm_lease) |
| 0007 | Kalman fusion made thread-safe via a seqlock snapshot |
| 0008 | Wayland direct-mode: modeset the leased CRTC (native mode + routed CRTC) |
| 0009 | Robustify Kalman fusion vs IMU/optical outliers (the drift/jitter fix) |
| 0010 | Drop a debug printf + actually reset on a non-finite optical correction |

The **standalone OpenXR/xrgears** present path uses the Wayland direct-lease
modeset (0008, in Monado's `comp_main`). Under **SteamVR**, this Monado loads
in-process as the device/tracking driver only (`IVRDisplayComponent`, no
compositor) and SteamVR's own `vrcompositor` does the present — so 0008 is not in
the SteamVR path. `g2-studio/core/steamvr.py` frees the G2 lease + dips the
desktop so `vrcompositor` can acquire and present to it.
