"""G2 Studio — VR configurator backend.

Run: python3 server.py
Open: http://localhost:8765
"""
import asyncio
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core import state, monado, display, gpu, profiles, steamvr

ROOT = Path(__file__).parent
STATIC = ROOT / "static"

app = FastAPI(title="G2 Studio")


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text()


@app.get("/api/status")
def get_status():
    """Full live system snapshot."""
    return state.system_status()


@app.get("/api/monado/config")
def get_monado_config():
    return monado.DEFAULT_CONFIG


class MonadoStartReq(BaseModel):
    config: Optional[dict] = None
    nice: Optional[int] = -10


@app.post("/api/monado/start")
def monado_start(req: MonadoStartReq):
    return monado.start(req.config or {}, nice=req.nice)


@app.post("/api/monado/stop")
def monado_stop():
    return monado.stop()


@app.get("/api/monado/log")
def monado_log(n: int = 100):
    return {"log": monado.tail_log(n)}


@app.post("/api/steamvr/start")
def steamvr_start():
    """Launch SteamVR over the Monado bridge (Wayland): frees the G2, dips the
    desktop, pins perf, launches SteamVR, restores on stop."""
    return steamvr.start()


@app.post("/api/steamvr/stop")
def steamvr_stop():
    return steamvr.stop()


@app.get("/api/steamvr/status")
def steamvr_status():
    return {"running": steamvr.is_running()}


class DisplayOp(BaseModel):
    op: str  # "non_desktop_1", "non_desktop_0", "off", "auto", "vr_prep", "vr_restore"


@app.post("/api/display/op")
def display_op(req: DisplayOp):
    ops = {
        "non_desktop_1": lambda: display.set_non_desktop(1),
        "non_desktop_0": lambda: display.set_non_desktop(0),
        "off": display.detach_hmd,
        "auto": display.attach_hmd,
        "vr_prep": display.prepare_for_vr,
        "vr_restore": display.restore_for_desktop,
    }
    if req.op not in ops:
        return {"ok": False, "msg": f"unknown op: {req.op}"}
    result = ops[req.op]()
    if isinstance(result, tuple):
        ok, out = result
        return {"ok": ok, "out": out}
    return {"ok": bool(result)}


class GpuOp(BaseModel):
    op: str
    min_clock: Optional[int] = 2400
    max_clock: Optional[int] = 2820
    power_watts: Optional[int] = 320


@app.post("/api/gpu/op")
def gpu_op(req: GpuOp):
    ops = {
        "persistence_on": lambda: gpu.set_persistence(True),
        "persistence_off": lambda: gpu.set_persistence(False),
        "lock_clocks": lambda: gpu.set_clock_lock(req.min_clock, req.max_clock),
        "reset_clocks": gpu.reset_clocks,
        "set_power": lambda: gpu.set_power_limit(req.power_watts),
        "apply_vr": gpu.apply_vr_optimizations,
        "revert": gpu.revert_optimizations,
    }
    if req.op not in ops:
        return {"ok": False, "msg": f"unknown op: {req.op}"}
    result = ops[req.op]()
    if isinstance(result, tuple):
        return {"ok": result[0], "out": result[1]}
    return {"ok": bool(result)}


@app.get("/api/profiles")
def list_profiles():
    return {"profiles": profiles.list_profiles()}


@app.get("/api/profiles/{name}")
def load_profile(name: str):
    return profiles.load(name)


class ProfileSave(BaseModel):
    name: str
    config: dict


@app.post("/api/profiles/save")
def save_profile(req: ProfileSave):
    path = profiles.save(req.name, req.config)
    return {"ok": True, "path": path}


@app.delete("/api/profiles/{name}")
def delete_profile(name: str):
    return {"ok": profiles.delete(name)}


@app.websocket("/ws/logs")
async def ws_logs(ws: WebSocket):
    await ws.accept()
    log = monado.LOG_FILE
    try:
        # Send last 50 lines immediately
        if log.exists():
            tail = monado.tail_log(50)
            await ws.send_text(tail)
        # Then follow new lines
        last_size = log.stat().st_size if log.exists() else 0
        while True:
            await asyncio.sleep(0.5)
            if not log.exists():
                continue
            size = log.stat().st_size
            if size > last_size:
                with open(log) as f:
                    f.seek(last_size)
                    new = f.read()
                last_size = size
                if new:
                    await ws.send_text(new)
            elif size < last_size:
                # log rotated/truncated
                last_size = 0
    except Exception:
        pass


if __name__ == "__main__":
    import uvicorn
    print()
    print("┌─────────────────────────────────────────┐")
    print("│  G2 Studio                              │")
    print("│  http://localhost:8765                  │")
    print("└─────────────────────────────────────────┘")
    print()
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
