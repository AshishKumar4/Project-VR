"""Launch SteamVR with the G2 on Wayland — the proper #77 fix.

SteamVR loads our Monado (ovrd_driver) in-process inside vrserver as the
DEVICE/TRACKING driver only: it creates the device system but passes NULL for the
compositor and exposes the HMD via IVRDisplayComponent (NOT DirectMode). So
SteamVR's own vrcompositor does the rendering and PRESENTS to the G2 — it acquires
the display itself (wp_drm_lease_device_v1 on Wayland). There is NO standalone
monado-service in this path, and Monado's own compositor is not used.

So vrcompositor must be able to lease the G2: it needs the lease FREE (no
standalone monado-service holding it) AND the desktop dipped for display
ISO-bandwidth (nothing in this path runs our negotiator), otherwise vrcompositor's
"WaitForPresent" watchdog times out and aborts. (The G2 present is carried by the
NVIDIA kernel patches: non_desktop advertise + native-mode-preferred + flip-bridge.)

So SteamVR mode is: free the G2, dip + hold the desktop (re-applying the dip once
the lease lands, since the G2 hotplug makes mutter reconfigure and revert it), pin
GPU/CPU perf, launch SteamVR; restore the desktop + clocks on exit. Mirrors
monado.start()/stop() for the OpenXR path.
"""
import json
import os
import subprocess
import time

from . import gpu, mutter, vr_display

APPID = "250820"  # SteamVR
SVR_PROCS = ("vrserver", "vrcompositor", "vrmonitor", "vrdashboard",
             "vrwebhelper", "steamtours")


def _session_env():
    """Graphical-session env so Steam/SteamVR land on the user's display; pulled
    from gnome-shell if this process didn't inherit it."""
    env = os.environ.copy()
    if env.get("WAYLAND_DISPLAY") and env.get("DISPLAY"):
        return env
    try:
        pid = subprocess.check_output(
            ["pgrep", "-u", str(os.getuid()), "-x", "gnome-shell"]).split()[0].decode()
        for kv in open(f"/proc/{pid}/environ").read().split("\0"):
            k = kv.split("=", 1)[0]
            if k in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR",
                     "DBUS_SESSION_BUS_ADDRESS", "XAUTHORITY"):
                env[k] = kv.split("=", 1)[1]
    except Exception:
        pass
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return env


def _kill(procs):
    for p in procs:
        subprocess.run(["pkill", "-x", p], stderr=subprocess.DEVNULL)


def is_running():
    return subprocess.run(["pgrep", "-x", "vrserver"],
                          stdout=subprocess.DEVNULL).returncode == 0


def start():
    """Launch SteamVR with the G2 free, desktop dipped, and perf pinned."""
    # 1. Free the G2 lease so SteamVR's vrcompositor can acquire it (no standalone service).
    subprocess.run(["pkill", "-x", "monado-service"], stderr=subprocess.DEVNULL)
    time.sleep(2)

    # 2. Save the desktop layout, then dip it to the negotiated (cached) config so
    #    vrcompositor has the ISO-bandwidth it needs to present to the G2.
    _, lg = mutter.canonical()
    json.dump(lg, open(vr_display.SAVED, "w"))
    config = vr_display.negotiate()        # cached per desktop set (probes only if unknown)
    mutter.apply_layout(config)
    time.sleep(1)

    # 3. Pin GPU P0 clocks + CPU performance governor.
    try:
        gpu.apply_vr_optimizations()
    except Exception as e:
        print(f"[steamvr] gpu.apply skipped: {e}")

    # 4. Launch SteamVR; vrcompositor wakes + leases the G2 and presents (our Monado drives tracking).
    subprocess.Popen(["setsid", "-f", "steam", "-applaunch", APPID],
                     env=_session_env(),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 5. Wait for the lease, then re-apply the dip: the G2 connect hotplug makes
    #    mutter reconfigure and revert the dip; one re-dip after the lease holds it.
    for _ in range(60):
        if vr_display._hmd_awake():
            break
        time.sleep(1)
    time.sleep(1)
    mutter.apply_layout(config)
    return {"ok": True, "display": config, "running": is_running()}


def stop():
    """Stop SteamVR, restore the desktop layout + clocks."""
    _kill(SVR_PROCS)
    time.sleep(2)
    vr_display.exit_vr()
    try:
        gpu.revert_optimizations()
    except Exception as e:
        print(f"[steamvr] gpu.revert skipped: {e}")
    return {"ok": True}


if __name__ == "__main__":  # python3 -m core.steamvr [start|stop|status]
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"
    fn = {"start": start, "stop": stop,
          "status": lambda: {"running": is_running()}}.get(cmd)
    print(json.dumps(fn() if fn else {"ok": False, "msg": "usage: start|stop|status"},
                     default=str))
