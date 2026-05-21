"""Manage monado-service: start/stop with env-var config."""
import os
import signal
import subprocess
import time
import json
from pathlib import Path

HOME = Path.home()
MONADO_BIN = HOME / ".local/bin/monado-service"
LD_LIB = str(HOME / ".local/lib")
RUNTIME_JSON = HOME / ".config/openxr/1/active_runtime.json"
IPC_SOCK = Path(f"/run/user/{os.getuid()}/monado_comp_ipc")
LOG_FILE = Path("/tmp/g2-studio-monado.log")
FIFO_PATH = Path("/tmp/g2-studio-m-fifo")


# Env-var defaults — session-agnostic; X11-only backend selectors are injected
# per-session in start() (Wayland leases via wp_drm_lease instead).
# Sources: Collabora Monado 25.1 docs, LVRA wiki, freedesktop u_pacing source
DEFAULT_CONFIG = {
    # --- Rendering / buffering ---
    "XRT_COMPOSITOR_PREFERRED_IMAGE_COUNT": "3",  # triple buffer
    "XRT_COMPOSITOR_DEFAULT_FRAMERATE": "90",
    "XRT_COMPOSITOR_PRINT_MODES": "1",

    # --- Rendering ---
    "XRT_COMPOSITOR_COMPUTE": "1",              # compute path = lower latency
    "XRT_COMPOSITOR_SCALE_PERCENTAGE": "100",   # 100 = match render to display; raise to 130-150 for SSAA

    # --- Pacing (research-recommended for NVIDIA + G2) ---
    "XRT_COMPOSITOR_USE_PRESENT_WAIT": "1",     # use VK_KHR_present_wait (low-latency on 565+)
    "U_PACING_COMP_TIME_FRACTION_PERCENT": "90", # LVRA-recommended for NVIDIA
    "U_PACING_COMP_MIN_TIME_MS": "4",
    "U_PACING_COMP_PRESENT_TO_DISPLAY_OFFSET_MS": "2.5",  # G2 NVIDIA real value, trims latency

    # --- Logging / metrics ---
    "XRT_LOG": "info",
    "XRT_COMPOSITOR_LOG": "info",
    "XRT_COMP_FRAME_LAG_LOG_AS_LEVEL": "info",
    "XRT_APP_FRAME_LAG_LOG_AS_LEVEL": "info",
    "XRT_METRICS_FILE": "/tmp/g2-studio-monado-metrics.proto",
    "XRT_METRICS_EARLY_FLUSH": "true",
    # "U_PACING_LIVE_STATS": "1",  # enable for benchmarking
}


def _is_monado_proc(pid: int) -> bool:
    """Verify a PID is actually our monado-service binary, not just a process whose cmdline mentions it."""
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
        return exe.endswith("/monado-service")
    except (OSError, FileNotFoundError, PermissionError):
        return False


def is_running():
    return get_pid() is not None


def get_pid():
    """Find our actual running monado-service (not stale or grep matches)."""
    out = subprocess.getoutput("pgrep -x monado-service")  # -x = exact name match
    for p in out.split():
        if p.isdigit() and _is_monado_proc(int(p)):
            return int(p)
    return None


def start(config: dict = None, nice: int = -10) -> dict:
    """Start monado-service with the given env config. FIFO stdin to keep epoll happy."""
    if is_running():
        return {"ok": False, "msg": "monado-service is already running"}

    config = {**DEFAULT_CONFIG, **(config or {})}

    session_wayland = (os.environ.get("XDG_SESSION_TYPE") == "wayland"
                       or bool(os.environ.get("WAYLAND_DISPLAY")))

    vr_display_applied = None
    if session_wayland:
        # Wayland: free display ISO bandwidth for the G2 (dip desktop refresh per
        # the negotiated, cached per-monitor config) so the 32bpp present isn't
        # bandwidth-downgraded. Wakes the G2 first if asleep.
        try:
            from . import vr_display
            vr_display_applied = vr_display.enter_vr()
        except Exception as e:
            print(f"[monado] vr_display.enter_vr skipped: {e}")
    else:
        # X11: select the RandR-lease backend (Wayland uses wp_drm_lease instead).
        config.setdefault("XRT_COMPOSITOR_FORCE_RANDR", "true")
        config.setdefault("XRT_COMPOSITOR_DESIRED_MODE", "1")

    # Pin GPU to P0 clocks + power cap and the CPU performance governor (O-4/O-5).
    try:
        from . import gpu
        gpu.apply_vr_optimizations()
    except Exception as e:
        print(f"[monado] gpu.apply_vr_optimizations skipped: {e}")

    # Clean up stale state
    if IPC_SOCK.exists():
        try:
            IPC_SOCK.unlink()
        except Exception:
            pass
    if FIFO_PATH.exists():
        FIFO_PATH.unlink()
    os.mkfifo(str(FIFO_PATH))

    # Open both ends of FIFO so stdin stays alive
    fifo_rd = os.open(str(FIFO_PATH), os.O_RDONLY | os.O_NONBLOCK)
    fifo_wr = os.open(str(FIFO_PATH), os.O_WRONLY)

    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = LD_LIB
    for k, v in config.items():
        env[k] = str(v)

    log = open(LOG_FILE, "w")
    proc = subprocess.Popen(
        [str(MONADO_BIN)],
        stdin=fifo_rd,
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )

    # Wait for socket to appear (or fail)
    for _ in range(40):  # 4 seconds
        if IPC_SOCK.exists():
            break
        if proc.poll() is not None:
            return {"ok": False, "msg": f"monado-service exited early (code {proc.returncode}). Check log.",
                    "pid": proc.pid, "log": str(LOG_FILE)}
        time.sleep(0.1)

    if not IPC_SOCK.exists():
        return {"ok": False, "msg": "monado-service didn't create IPC socket in 4s. Check log.",
                "pid": proc.pid, "log": str(LOG_FILE)}

    # Renice for perf
    if nice is not None:
        subprocess.run(["sudo", "renice", "-n", str(nice), "-p", str(proc.pid)],
                       check=False, capture_output=True)

    return {"ok": True, "pid": proc.pid, "log": str(LOG_FILE), "config": config,
            "display": vr_display_applied}


def stop() -> dict:
    """Stop any running monado-service. Force-kill if needed."""
    pid = get_pid()
    if not pid:
        # Cleanup stale state anyway
        if IPC_SOCK.exists():
            try:
                IPC_SOCK.unlink()
            except Exception:
                pass
        if FIFO_PATH.exists():
            FIFO_PATH.unlink()
        return {"ok": True, "msg": "monado not running"}
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            time.sleep(0.1)
            if not is_running():
                break
        if is_running():
            os.kill(pid, signal.SIGKILL)
            time.sleep(0.5)
    except ProcessLookupError:
        pass

    if IPC_SOCK.exists():
        try:
            IPC_SOCK.unlink()
        except Exception:
            pass
    if FIFO_PATH.exists():
        FIFO_PATH.unlink()

    # Wayland: restore the full desktop layout dipped by enter_vr().
    try:
        from . import vr_display
        vr_display.exit_vr()
    except Exception as e:
        print(f"[monado] vr_display.exit_vr skipped: {e}")

    # Restore GPU clocks + CPU governor.
    try:
        from . import gpu
        gpu.revert_optimizations()
    except Exception as e:
        print(f"[monado] gpu.revert_optimizations skipped: {e}")

    return {"ok": True, "msg": "monado stopped"}


def tail_log(n: int = 50) -> str:
    if not LOG_FILE.exists():
        return ""
    try:
        return subprocess.getoutput(f"tail -n {n} {LOG_FILE}")
    except Exception:
        return ""
