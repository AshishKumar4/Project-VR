"""Read live system state — HMD, GPU, session (X11/Wayland), monado-service."""
import subprocess
import re
import os
import glob
from pathlib import Path

HOME = Path.home()


def is_wayland() -> bool:
    return os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" \
        or bool(os.environ.get("WAYLAND_DISPLAY"))


def run(cmd, timeout=3):
    try:
        r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                           text=True, timeout=timeout, check=False)
        return r.stdout, r.returncode
    except Exception as e:
        return f"ERR: {e}", -1


def gpu_status():
    out, _ = run(["nvidia-smi", "--query-gpu=name,driver_version,persistence_mode,"
                  "clocks.current.graphics,clocks.current.memory,"
                  "power.draw,power.limit,utilization.gpu,utilization.memory,"
                  "memory.used,memory.total,temperature.gpu",
                  "--format=csv,noheader"])
    if "ERR" in out or not out.strip():
        return {"available": False}
    parts = [p.strip() for p in out.strip().split(",")]
    if len(parts) < 12:
        return {"available": False, "raw": out}
    return {
        "available": True,
        "name": parts[0],
        "driver": parts[1],
        "persistence": parts[2] == "Enabled",
        "clock_gr_mhz": int(re.sub(r"\D", "", parts[3]) or 0),
        "clock_mem_mhz": int(re.sub(r"\D", "", parts[4]) or 0),
        "power_w": float(re.sub(r"[^\d.]", "", parts[5]) or 0),
        "power_limit_w": float(re.sub(r"[^\d.]", "", parts[6]) or 0),
        "util_gpu": int(re.sub(r"\D", "", parts[7]) or 0),
        "util_mem": int(re.sub(r"\D", "", parts[8]) or 0),
        "mem_used_mib": int(re.sub(r"\D", "", parts[9]) or 0),
        "mem_total_mib": int(re.sub(r"\D", "", parts[10]) or 0),
        "temp_c": int(re.sub(r"\D", "", parts[11]) or 0),
    }


def xrandr_state():
    out, _ = run(["xrandr", "--listactivemonitors"])
    monitors = []
    for line in out.splitlines()[1:]:
        m = re.search(r"(\S+)\s+(\d+)/(\d+)x(\d+)/(\d+)\+\d+\+\d+\s+(\S+)", line)
        if m:
            monitors.append({
                "name": m.group(1).lstrip("+*"),
                "width": int(m.group(2)),
                "height": int(m.group(4)),
                "output": m.group(6),
            })

    out, _ = run(["xrandr"])
    outputs = []
    for line in out.splitlines():
        m = re.match(r"^(\S+) (connected|disconnected)", line)
        if m:
            outputs.append({"name": m.group(1), "connected": m.group(2) == "connected"})

    # HMD non-desktop status
    hmd_nondesktop = None
    out, _ = run(["xrandr", "--prop", "--output", "DP-4"])
    if "non-desktop" in out:
        m = re.search(r"non-desktop:\s*(\d+)", out)
        if m:
            hmd_nondesktop = int(m.group(1))

    return {"monitors": monitors, "outputs": outputs, "hmd_nondesktop": hmd_nondesktop}


# --- Wayland display state ---------------------------------------------------

def _drm_read(conn, attr):
    try:
        with open(f"/sys/class/drm/{conn}/{attr}") as f:
            return f.read().strip()
    except OSError:
        return None


def drm_connectors():
    """All DRM connectors from sysfs (session-independent, reliable)."""
    out = []
    for p in sorted(glob.glob("/sys/class/drm/card*-*")):
        conn = os.path.basename(p)
        out.append({
            "name": conn,                                # e.g. card1-DP-2
            "status": _drm_read(conn, "status"),         # connected/disconnected
            "enabled": _drm_read(conn, "enabled"),       # enabled/disabled
            "connected": _drm_read(conn, "status") == "connected",
            "modes": (_drm_read(conn, "modes") or "").splitlines()[:1],  # top mode
        })
    return out


def _drm_to_mutter(name):
    """card1-HDMI-A-1 -> HDMI-1 ; card1-DP-2 -> DP-2 (mutter connector naming)."""
    base = re.sub(r"^card\d+-", "", name)
    return base.replace("HDMI-A-", "HDMI-")


def wayland_hmd_connector():
    """DRM sysfs connector (card1-DP-N) of the HMD, matched via mutter EDID; else None.

    The G2 only appears once awake/connected. Falls back to None when asleep."""
    try:
        from core import mutter
        hmd = mutter.find_hmd()
    except Exception:
        hmd = None
    if not hmd:
        return None
    target = hmd["connector"]
    for c in drm_connectors():
        if _drm_to_mutter(c["name"]) == target:
            return c["name"]
    return None


def wayland_state():
    """Wayland equivalent of xrandr_state: DRM sysfs + mutter monitor info."""
    conns = drm_connectors()
    mons, hmd, dup = [], None, None
    try:
        from core import mutter
        mons = mutter.monitors()
        hmd = mutter.find_hmd()
        d = mutter.find_duplicate_head()
        dup = d["connector"] if d else None
    except Exception as e:
        mons = [{"error": str(e)}]
    return {
        "session": "wayland",
        "drm_connectors": conns,
        "monitors": mons,                 # mutter physical monitors w/ vendor/product
        "hmd_connector": hmd["connector"] if hmd else None,
        "hmd_drm": wayland_hmd_connector(),
        "hmd_present": hmd is not None,
        "redundant_head": dup,            # connector that prepare_for_vr would free
        # non_desktop is a DRM connector property (not in sysfs); on Wayland it's
        # set by the nvidia-drm kernel patch from EDID, so reported as N/A here.
        "hmd_nondesktop": None,
    }


def display_state():
    """Session-aware display snapshot."""
    return wayland_state() if is_wayland() else {"session": "x11", **xrandr_state()}


def hmd_usb_state():
    """All 5 G2 USB devices present?"""
    out, _ = run(["lsusb"])
    expected = {
        "03f0:0580": "HMD (Quanta QHMD A85V)",
        "04b4:6504": "Cypress WMR SuperSpeed",
        "04b4:6506": "Cypress WMR Hi-Speed",
        "045e:0659": "HoloLens sensors",
        "0bda:4c15": "USB Audio",
    }
    seen = {}
    for vid_pid, name in expected.items():
        seen[vid_pid] = {"name": name, "present": vid_pid in out}
    return seen


def monado_state():
    """Is monado-service running, what's the IPC socket?"""
    out, _ = run(["pgrep", "-x", "monado-service"])
    pids = []
    for p in out.split():
        if p.isdigit():
            try:
                exe = os.readlink(f"/proc/{p}/exe")
                if exe.endswith("/monado-service"):
                    pids.append(int(p))
            except (OSError, FileNotFoundError, PermissionError):
                pass
    socket = Path("/run/user/1000/monado_comp_ipc")
    return {
        "running": len(pids) > 0,
        "pids": pids,
        "ipc_socket": socket.exists(),
    }


def kernel_module_state():
    """Is patched nvidia-modeset loaded?"""
    out, _ = run("modinfo nvidia-modeset | grep -E '^signer|^version'")
    ours = "White0racle" in out
    version = ""
    m = re.search(r"version:\s+(\S+)", out)
    if m:
        version = m.group(1)
    return {
        "loaded": "nvidia-modeset" in subprocess.getoutput("lsmod"),
        "version": version,
        "is_patched_build": ours,
    }


def xorg_conf_state():
    p = Path("/etc/X11/xorg.conf")
    if not p.exists():
        return {"present": False}
    text = ""
    try:
        text = subprocess.getoutput("sudo cat /etc/X11/xorg.conf")
    except Exception:
        text = ""
    return {
        "present": True,
        "allow_hmd": 'AllowHMD" "yes"' in text,
        "allow_vr": 'AllowVR" "yes"' in text,
        "use_display_device": 'UseDisplayDevice' in text,
        "metamodes": "MetaModes" in text,
    }


def _wayland_xrandr_compat(ds):
    """Map Wayland display_state into the legacy xrandr-shaped dict the UI reads."""
    outputs = [{"name": _drm_to_mutter(c["name"]), "connected": c["connected"]}
               for c in ds.get("drm_connectors", [])]
    return {"monitors": [], "outputs": outputs, "hmd_nondesktop": ds.get("hmd_nondesktop")}


def system_status():
    wayland = is_wayland()
    ds = display_state()
    return {
        "session": "wayland" if wayland else "x11",
        "gpu": gpu_status(),
        "display": ds,                                       # session-aware (preferred)
        "xrandr": _wayland_xrandr_compat(ds) if wayland else ds,  # legacy UI shape
        "usb": hmd_usb_state(),
        "monado": monado_state(),
        "kmod": kernel_module_state(),
        "xorg": {"present": False} if wayland else xorg_conf_state(),  # X11-only
    }
