<!-- Edited & maintained by Claude; presented as-is. -->

# blobviz — detected-vs-predicted LED overlay for G2 camera frames

Overlays two layers on the recorded HP Reverb G2 constellation-camera frames so
detection-vs-model mismatch is directly visible:

- **Detected blobs** (cyan circles): a threshold + connected-components blob
  finder that mirrors Monado's `blobwatch` (`src/xrt/tracking/constellation/
  internal/blobwatch.c`). Each blob is annotated with `a<area> p<peak>`.
- **Predicted LEDs** (orange `+` when visible / front-facing, red `+` when
  projected but back-facing or out of frame): the controller LED 3D model
  transformed by the logged controller pose into the camera and projected
  through the per-camera intrinsics — mirroring `pose_metrics.c`
  `project_led_points()` → `t_camera_models_project()`.

If the two layers agree, predicted LEDs sit on real bright spots. Where they
diverge you can see false blobs (background), missed LEDs, or pose error.

## Usage

```bash
# Pick ~6 representative tracked frames and render overlays into <capture>/overlays
python3 blobviz.py auto -n 6

# One specific frame by timestamp (filename ns) or by index, on a chosen cam
python3 blobviz.py one --cam 0 --ts 9743116542036
python3 blobviz.py one --cam 1 --idx 1400 -o /tmp/out
```

Defaults target the `20260522-134113` capture; override with `--capture`,
`--calib`, `-o/--outdir`.

Deps: `numpy`, `Pillow`, `pyarrow` (all already present). `scipy` is optional —
only used by ad-hoc analysis, not by the tool. No OpenCV needed.

## Inputs and what each was used for

- **Frames**: `<capture>/euroc_<...>/mav0/cam{0..3}/data/*.png`, 640×480 grey,
  filename = monotonic-hardware-clock ns. 2809 frames/cam, all four cams share
  identical timestamps (synchronized) at ~30.2 Hz.
- **Telemetry**: `<capture>/telemetry/pose_attempt.parquet` supplies the pose to
  project, the camera it was attempted on, the device (1=left, 2=right
  controller), and quality fields (`inliers`, `blobs_matched`, `leds_visible`,
  `reproj_err_px`, `outcome`). `frame.parquet` carries per-cam blob counts.

### Camera intrinsics + extrinsics (documented)

The EuRoC dataset has **no** `sensor.yaml`, and the recorder did **not** dump
the per-device WMR calibration JSON (it is read off the headset at runtime and
was not captured). The WMR config code
(`src/xrt/drivers/wmr/wmr_config.c`) only *parses* that runtime JSON; there are
no hardcoded intrinsics there.

So intrinsics come from the matching **Reverb G2 v2 basalt calibration**:

    /home/mrwhite0racle/.local/share/basalt/reverbg2v2_calib.json

This is a 4-camera, 640×480, `pinhole-radtan8` calibration that exactly matches
this dataset's geometry. We use its per-camera `fx, fy, cx, cy, k1..k6, p1, p2,
rpmax`. The `radtan8` forward projection in `blobviz.py:rt8_project()` is a
direct port of `rt8_project()` in
`src/xrt/auxiliary/tracking/t_camera_models.h` (param order
`k1,k2,p1,p2,k3,k4,k5,k6`, `rpmax` = `metric_radius`).

**Extrinsics are not needed for the per-camera projection.** The telemetry
`pose_attempt` row stores `P_cam_obj` (camera←object) — verified at
`src/xrt/tracking/constellation/t_constellation_tracking.c:397`
(`telem_pack_pose(P_cam_obj, ...)`). That is exactly the pose
`pose_metrics.c` feeds to `math_pose_transform_point` before projecting, so we
apply it directly: `cam_pt = rot(q)·led_pos + p`, then `rt8_project`. The
`T_imu_cam` extrinsics in the calib JSON are therefore informational here.

### Controller LED 3D model (PLACEHOLDER — read this)

The real per-LED positions live in the controller's runtime JSON
(`wmr_config.c:wmr_controller_led_config_parse`, populated into
`wcb->config.leds[]` and copied into the constellation model at
`wmr_controller_base.c:1117`). **That JSON was not captured**, so the exact
model is unavailable for this recording.

`blobviz.py` therefore builds a **geometrically representative placeholder**:
a 16-LED ring using the real WMR ring constants from
`wmr_controller_base.c` (`WMR_RING_HEIGHT`, `WMR_RING_TOP_RADIUS`,
`WMR_RING_BOTTOM_RADIUS`), in OpenCV/WMR object coords (X=right, Y=down,
Z=forward), normals pointing radially outward. This places the predicted ring
at the correct scale and on the controller, but the **per-LED layout will not
match the device exactly**. To get a 1:1 model, capture the controller config
JSON during recording (dump `wcb->config.leds[]`) and load it here.

## Coordinate / timing notes

- Pose↔frame matching: `pose_attempt.hw_ts_ns` shares the camera hardware clock
  with the PNG filenames. The tracker runs on a higher-rate stream than the
  saved 30 Hz PNGs, so a pose lands between saved frames; we snap to the nearest
  PNG and report the gap (`dt`), accepting matches within ~half a frame (17 ms).
- `auto` shortlists accepted poses by Monado's own quality (inliers, reproj)
  then re-ranks by how many blobs *our* detector finds on the saved PNG, so the
  chosen overlays are the ones where a comparison is actually meaningful.

## Blob detector parameters (from Monado WMR)

- pixel threshold `0x08`, required-pixel threshold `0x18`
  (`wmr_hmd.c` `BLOB_PIXEL_THRESHOLD_WMR` / `BLOB_THRESHOLD_MIN_WMR`)
- max blob width/height `35` px, drop 1×1 specks (`blobwatch.c`)
- greysum-weighted centroid, peak = max pixel in the blob.

## What the overlays show (read)

The overlays make one thing obvious: **the saved PNG frames are badly
overexposed for constellation tracking, and they are not the same frames the
in-Monado tracker actually locked onto.** Properly exposed constellation frames
should be near-black with a few small bright LED dots; instead the saved frames
have a mean grey of ~40–105 (one early frame is correctly dark at ~10) with
large saturated regions from room/window light. Because `blobwatch` rejects any
"blob" wider/taller than 35 px, those big saturated areas are discarded, so on
~63% of accepted-pose frames our faithful detector finds **0** blobs and on most
of the rest only 1–3 — even though Monado reported a clean lock (median
reproj ≈ 2.9 px, here computed on the frames it really processed). Where the
detector *does* find blobs, many are background reflections (e.g. the bright
specks along the bottom of the cam1 overlay), i.e. false blobs the matcher must
reject. The predicted LED ring lands on the actual controller in the well-timed
frames (cam1 `01_…`, dt 6.5 ms — the orange ring sits on the hand-held
controller), confirming the pose/intrinsics/projection path is correct; the
ring's internal layout differs from the dots because of the placeholder LED
model (see above). Net: pose projection is sound, but **exposure is clearly too
high in the saved capture** — the saved PNG stream is room-lit rather than the
LED-flash exposure the tracker uses, which is why offline blob detection on
these PNGs disagrees with the live tracker.
