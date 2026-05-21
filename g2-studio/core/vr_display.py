"""G2 VR display auto-negotiation (Wayland).

The G2's 4320x2160@90 32bpp scanout competes with desktop monitors for the
display engine's ISO bandwidth. Whether a given multi-display config leaves the
G2 enough is decided by NVKMS's IMP model (per-head limits, 2Head1OR, mclk
floors) — NOT a simple bandwidth sum (proven: a 10.6 GB/s config works while a
9.1 GB/s one fails). So we never model bandwidth; we ASK the driver via a probe
and negotiate the least-disruptive config that works, then cache it.

Policy (current): keep all monitors on; dip refresh, secondaries before the
primary, to the highest rate that still lets the G2 present. Hardware-agnostic —
works on any GPU/monitor mix, which matters for open-source distribution.
"""
import hashlib
import json
import os
import subprocess
import time

from . import mutter

CACHE = os.path.expanduser("~/.config/g2-studio/vr-display-cache.json")
SAVED = "/tmp/g2_vr_saved_layout.json"
MONADO = os.path.expanduser("~/.local/bin/monado-service")
_RUNTIME_SOCK = "/run/user/%d/monado_comp_ipc" % os.getuid()


def _signature():
    """Stable key for the current DESKTOP monitor set (the HMD is leased and
    comes/goes, so exclude it). Re-negotiates only when a desktop display is
    plugged/unplugged."""
    hmd = mutter.find_hmd()
    hmd_key = (hmd["vendor"], hmd["product"], hmd["serial"]) if hmd else None
    mons = sorted((m["vendor"], m["product"], m["serial"])
                  for m in mutter.monitors()
                  if (m["vendor"], m["product"], m["serial"]) != hmd_key)
    return hashlib.sha1(repr(mons).encode()).hexdigest()[:16]


def _load_cache():
    try:
        return json.load(open(CACHE))
    except (OSError, ValueError):
        return {}


def _save_cache(c):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    json.dump(c, open(CACHE, "w"), indent=2)


def probe(timeout=16):
    """Ground-truth oracle: does the G2 present at full 32bpp in the CURRENT
    display config? Briefly runs monado-service (which leases the G2, modesets,
    and presents) and watches for success vs the VK_ERROR_UNKNOWN that the
    bandwidth downgrade produces. Returns True/False."""
    log, fifo = "/tmp/g2_probe.log", "/tmp/g2_probe_fifo"
    for p in (log, fifo, _RUNTIME_SOCK):
        try:
            os.remove(p)
        except OSError:
            pass
    os.mkfifo(fifo)
    # Detached, with a held-open FIFO on stdin (monado's epoll mainloop needs it).
    subprocess.Popen(["setsid", "bash", "-c",
                      "exec 3<>%s; %s <&3 > %s 2>&1" % (fifo, MONADO, log)])
    # The bandwidth downgrade makes the present fail with VK_ERROR_UNKNOWN, which
    # appears within ~1-2s of compositor init. So: fail on VK_ERROR; otherwise,
    # once the compositor is up (frame_period) and stays ~3s with no error, pass.
    result, frame_at = False, None
    for i in range(timeout):
        time.sleep(1)
        try:
            txt = open(log).read()
        except OSError:
            txt = ""
        if "VK_ERROR_UNKNOWN" in txt or "failed to start" in txt:
            result = False
            break
        if "estimated_frame_period" in txt and frame_at is None:
            frame_at = i
        if frame_at is not None and (i - frame_at) >= 3:
            result = True
            break
    subprocess.run(["pkill", "-9", "-x", "monado-service"],
                   stderr=subprocess.DEVNULL)
    try:
        os.remove(fifo)
    except OSError:
        pass
    time.sleep(1)
    return bool(result)


def _desktop_connectors():
    """Active desktop connectors (excludes the leased HMD), and the primary."""
    hmd = mutter.find_hmd()
    hmd_conn = hmd["connector"] if hmd else None
    _, lg = mutter.canonical()
    desktops = [c for l in lg for c, _ in l[5] if c != hmd_conn]
    return desktops, mutter.primary_connector()


def _refresh(mode_id):
    """Rounded refresh (Hz) parsed from a mode id like '2560x1440@359.999'."""
    try:
        return round(float(mode_id.split("@")[1].split("+")[0]))
    except (IndexError, ValueError):
        return 0


def _mode_at_or_below(ladder, cap):
    """Highest-refresh mode id in `ladder` (desc) with refresh <= cap; lowest if
    none qualify."""
    for mid in ladder:
        if _refresh(mid) <= cap:
            return mid
    return ladder[-1]


def negotiate(force=False):
    """Find the least-disruptive desktop config that lets the G2 present at full
    32bpp, asking the driver (probe) at each step. Strategy, best->most-reduced:
      1. everything on at max refresh;
      2. drop redundant DUPLICATE heads (same panel reached twice, e.g. the eARC
         HDMI link mirroring a DP monitor) -- free: no screen lost, audio is in
         the headset during VR;
      3. BALANCED dip: keep the primary as high as possible; dip ALL secondaries
         together under a uniform refresh cap (so they degrade evenly, not one
         sacrificed first), lowering the cap until the G2 fits; only dip the
         primary if even the lowest secondary cap won't fit.
    The first config that probes OK is, by construction, the least-disruptive one
    (primary maximized, then highest uniform secondary refresh). Returns
    {connector: mode_id} (omitted connectors = disabled); cached per desktop set."""
    sig = _signature()
    cache = _load_cache()
    if not force and sig in cache:
        return cache[sig]

    desktops, primary = _desktop_connectors()
    dup = mutter.find_duplicate_head()
    dup_conn = dup["connector"] if dup else None
    ladders = {c: mutter.refresh_ladder(c) for c in desktops if mutter.refresh_ladder(c)}

    def try_cfg(config):
        mutter.apply_layout(config)
        time.sleep(2)
        return probe()

    def done(config):
        cache[sig] = config
        _save_cache(cache)
        return config

    base = [c for c in desktops if c != dup_conn and c in ladders]
    full = {c: ladders[c][0] for c in desktops if c in ladders}

    # 1. If a redundant duplicate head is present, first try keeping EVERYTHING
    #    (including it) at max. Without a duplicate this is identical to the
    #    sweep's first step below, so we skip it to avoid a wasted probe.
    if dup_conn and try_cfg(full):
        return done(full)
    secondaries = [c for c in base if c != primary]
    prim_ladder = ladders.get(primary, [None])
    sec_caps = sorted({_refresh(m) for s in secondaries for m in ladders[s]},
                      reverse=True) or [0]

    # 2/3. primary high->low (outer); for each, dip all secondaries together by a
    # uniform refresh cap high->low (inner). First OK wins = balanced + prioritized.
    last = full
    for p_id in prim_ladder:
        for cap in sec_caps:
            config = {}
            if p_id is not None and primary in base:
                config[primary] = p_id
            for s in secondaries:
                config[s] = _mode_at_or_below(ladders[s], cap)
            last = config
            if try_cfg(config):
                return done(config)

    return done(last)  # best effort (no config fit; shouldn't happen normally)


def _hmd_awake():
    """True if the HMD's DP link is up. The leased HMD isn't in mutter's monitor
    list, so detect it at the DRM level: a connected connector that is NOT one of
    mutter's active desktop monitors (i.e. the leasable/non-desktop output)."""
    import glob
    desktop = {c for _l in mutter.canonical()[1] for c, _ in _l[5]}
    for p in glob.glob("/sys/class/drm/card*-*/status"):
        conn = os.path.basename(os.path.dirname(p)).split("-", 1)[1]  # card1-DP-2 -> DP-2
        try:
            connected = open(p).read().strip() == "connected"
        except OSError:
            continue
        if connected and conn not in desktop:
            return True
    return False


def _ensure_hmd_awake(timeout=24):
    """Wake the G2's panel if asleep. The HMD's DP link drops to standby when
    idle; monado-service's WMR driver powers it back on over USB. We must wake it
    BEFORE dipping the desktop, because the disconnect->connect hotplug makes
    mutter re-apply the full layout (undoing the dip). Returns True if awake."""
    if _hmd_awake():
        return True
    log, fifo = "/tmp/g2_wake.log", "/tmp/g2_wake_fifo"
    for p in (fifo, _RUNTIME_SOCK):
        try:
            os.remove(p)
        except OSError:
            pass
    os.mkfifo(fifo)
    subprocess.Popen(["setsid", "bash", "-c",
                      "exec 3<>%s; %s <&3 > %s 2>&1" % (fifo, MONADO, log)])
    awake = False
    for _ in range(timeout):
        time.sleep(1)
        if mutter.find_hmd() is not None:
            awake = True
            break
    subprocess.run(["pkill", "-9", "-x", "monado-service"], stderr=subprocess.DEVNULL)
    try:
        os.remove(fifo)
    except OSError:
        pass
    time.sleep(1)
    return awake


def enter_vr():
    """Save the full layout, wake the G2, negotiate (or use cache), apply the VR
    display config. Returns the applied {connector: mode_id} (or None)."""
    _, lg = mutter.canonical()
    json.dump(lg, open(SAVED, "w"))
    _ensure_hmd_awake()
    config = negotiate()
    mutter.apply_layout(config)
    time.sleep(1)
    return config


def exit_vr():
    """Restore the full desktop layout saved by enter_vr()."""
    if not os.path.exists(SAVED):
        return False
    lg = [tuple(l[:5]) + ([tuple(m) for m in l[5]],)
          for l in json.load(open(SAVED))]
    mutter.apply(lg, method=1)
    return True
