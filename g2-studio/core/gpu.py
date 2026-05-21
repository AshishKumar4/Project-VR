"""GPU + CPU performance tuning for VR (O-5 GPU clocks, O-4 governor)."""
import subprocess
from pathlib import Path

SAVED_GOV = Path("/tmp/g2_saved_cpu_governor")


def run(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=8, check=False)
        return r.returncode == 0, r.stdout + r.stderr
    except Exception as e:
        return False, str(e)


def set_persistence(on: bool):
    return run(["sudo", "nvidia-smi", "-pm", "1" if on else "0"])


def set_clock_lock(min_mhz: int = 2820, max_mhz: int = 3105):
    """Lock the graphics clock to the P0 range. RTX 4080: 2820 MHz floor (P0),
    3105 MHz boost ceiling — keeping the floor at P0 avoids first-heavy-frame
    boost latency. (Was 2400, which sat below P0.)"""
    return run(["sudo", "nvidia-smi", "-lgc", f"{min_mhz},{max_mhz}"])


def reset_clocks():
    return run(["sudo", "nvidia-smi", "-rgc"])


def set_power_limit(watts: int = 320):
    return run(["sudo", "nvidia-smi", "-pl", str(watts)])


def _cpu_governor() -> str:
    try:
        return Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor").read_text().strip()
    except Exception:
        return ""


def set_cpu_governor(gov: str):
    """Set the CPU frequency governor on all cores (needs root). VR wants
    'performance' — the default 'powersave' adds frame-time variance."""
    ok, out = run(["sudo", "cpupower", "frequency-set", "-g", gov])
    if not ok:  # fallback: write sysfs directly
        ok, out = run(["sudo", "sh", "-c",
            f'for f in /sys/devices/system/cpu/*/cpufreq/scaling_governor; do echo {gov} > "$f"; done'])
    return ok, out


def apply_vr_optimizations():
    """All-in performance setup for VR: persistence, P0 GPU clocks + power cap,
    and the CPU performance governor (original governor saved for restore)."""
    cur = _cpu_governor()
    if cur and cur != "performance":
        SAVED_GOV.write_text(cur)
    set_persistence(True)
    set_clock_lock(2820, 3105)
    set_power_limit(320)
    set_cpu_governor("performance")
    return True


def revert_optimizations():
    set_persistence(False)
    reset_clocks()
    gov = SAVED_GOV.read_text().strip() if SAVED_GOV.exists() else "powersave"
    set_cpu_governor(gov)
    if SAVED_GOV.exists():
        SAVED_GOV.unlink()
    return True
