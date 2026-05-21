"""Display operations for HMD setup — X11 (xrandr) and Wayland (mutter/DRM).

X11: xrandr toggles the HMD output and its non-desktop prop directly.
Wayland: there is no xrandr. The HMD is leased by Monado via wp_drm_lease and is
never enabled as a desktop monitor, and non_desktop=1 is set by our nvidia-drm
kernel patch from the EDID — so g2-studio does NOT enable/disable the HMD or set
non_desktop here. "Prepare for VR" on Wayland instead frees a hardware head: the
4-head RTX 4080 can't drive 3 desktop monitors + the G2's 2Head1OR 4320x2160@90
(2 heads) at once, so the redundant eARC-HDMI duplicate of an already-active
monitor is disabled via mutter DBus, and restored afterward."""
import os
import subprocess

HMD_OUTPUT_X11 = "DP-4"  # X11 connector name (numbering differs from Wayland)


def is_wayland() -> bool:
    return os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" \
        or bool(os.environ.get("WAYLAND_DISPLAY"))


def run(cmd, timeout=5):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return r.returncode == 0, r.stdout + r.stderr
    except Exception as e:
        return False, str(e)


# --- non_desktop -------------------------------------------------------------

def set_non_desktop(value: int = 1):
    """X11: xrandr --set non-desktop. Wayland: no-op (kernel patch sets it via EDID)."""
    if is_wayland():
        return True, "wayland: non_desktop is set by nvidia-drm kernel patch from EDID (no-op)"
    return run(["xrandr", "--output", HMD_OUTPUT_X11, "--set", "non-desktop", str(value)])


# --- HMD output enable/disable ----------------------------------------------

def detach_hmd():
    """X11: xrandr --output DP-4 --off. Wayland: HMD isn't a desktop monitor (no-op)."""
    if is_wayland():
        return True, "wayland: HMD is leased by Monado, not a desktop output (no-op)"
    return run(["xrandr", "--output", HMD_OUTPUT_X11, "--off"])


def attach_hmd():
    """X11: xrandr --output DP-4 --auto. Wayland: HMD is leased by Monado (no-op)."""
    if is_wayland():
        return True, "wayland: HMD is leased by Monado, not a desktop output (no-op)"
    return run(["xrandr", "--output", HMD_OUTPUT_X11, "--auto"])


# --- VR prepare / restore ----------------------------------------------------

def _wayland_head_to_free():
    """Connector name to disable for VR: profile override or auto duplicate."""
    from core import profiles
    cfg = profiles.load("wayland-display") or {}
    forced = cfg.get("free_head_connector")
    if forced:
        return forced
    from core import mutter
    dup = mutter.find_duplicate_head()
    return dup["connector"] if dup else None


def prepare_for_vr():
    """Pre-VR display setup.

    X11: non_desktop=1 + detach the HMD output.
    Wayland: free a hardware head (disable the redundant eARC-HDMI duplicate) so
    the G2's 2Head1OR mode has the 2 heads it needs. The HMD itself is left for
    Monado to lease; non_desktop is handled by the kernel."""
    if is_wayland():
        from core import mutter
        conn = _wayland_head_to_free()
        if not conn:
            return True, "wayland: no redundant head found to free (4-head budget OK or HMD asleep)"
        try:
            kept = mutter.disable_connectors([conn])
            return True, f"wayland: freed head {conn}; kept {kept}"
        except Exception as e:
            return False, f"wayland: failed to free head {conn}: {e}"
    a, _ = set_non_desktop(1)
    b, _ = detach_hmd()
    return a and b, "x11: non_desktop=1 + HMD off"


def restore_for_desktop():
    """Post-VR restore.

    X11: re-attach HMD output + non_desktop=0.
    Wayland: re-enable the head freed in prepare_for_vr()."""
    if is_wayland():
        from core import mutter
        try:
            ok = mutter.restore()
            return ok, "wayland: restored freed head" if ok else "wayland: no saved head state"
        except Exception as e:
            return False, f"wayland: restore failed: {e}"
    a, _ = attach_hmd()
    b, _ = set_non_desktop(0)
    return a and b, "x11: HMD on + non_desktop=0"


# --- queries -----------------------------------------------------------------

def current_metamode() -> str:
    """X11-only nvidia-settings query. Wayland has no metamodes."""
    if is_wayland():
        return "wayland: nvidia metamodes are X11-only (n/a)"
    return subprocess.getoutput(
        "DISPLAY=:0 nvidia-settings -q CurrentMetaMode 2>&1 | grep 'CurrentMetaMode'")


def hmd_dp_connected() -> bool:
    """Is the HMD's DP link up? X11: xrandr. Wayland: DRM sysfs status of HMD connector."""
    if is_wayland():
        from core import state
        hmd = state.wayland_hmd_connector()  # card1-DP-N or None
        if not hmd:
            return False
        try:
            with open(f"/sys/class/drm/{hmd}/status") as f:
                return f.read().strip() == "connected"
        except OSError:
            return False
    r = subprocess.getoutput(f"xrandr 2>/dev/null | grep {HMD_OUTPUT_X11}")
    return "connected" in r and "disconnected" not in r
