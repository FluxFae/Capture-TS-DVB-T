"""
Capture-TS: FastAPI application for DVB-T Transport Stream recording.

Stack: FastAPI + Jinja2 + HTMX + WebSockets + TSDuck
Design: Flux Design System (dark-first, burgundy/magenta brand)
"""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.services.tsduck_service import TSDuckService, RecordingState

# ──────────────────────────────────────────────
# Application state
# ──────────────────────────────────────────────

tsduck_service = TSDuckService(recordings_dir="recordings")
connected_websockets: set[WebSocket] = set()
_main_loop: asyncio.AbstractEventLoop | None = None


async def broadcast_log(message: str) -> None:
    """Broadcast a log message to all connected WebSocket clients."""
    data = json.dumps({"type": "log", "message": message})
    stale = set()
    for ws in connected_websockets:
        try:
            await ws.send_text(data)
        except Exception:
            stale.add(ws)
    connected_websockets -= stale


async def broadcast_state(state: dict) -> None:
    """Broadcast recording state update to all connected WebSocket clients."""
    data = json.dumps({"type": "state", **state})
    stale = set()
    for ws in connected_websockets:
        try:
            await ws.send_text(data)
        except Exception:
            stale.add(ws)
    connected_websockets -= stale


def sync_log_callback(message: str) -> None:
    """Thread-safe log callback — schedules async broadcast."""
    if _main_loop and _main_loop.is_running():
        asyncio.run_coroutine_threadsafe(broadcast_log(message), _main_loop)


def sync_state_callback(state: dict) -> None:
    """Thread-safe state callback — schedules async broadcast."""
    if _main_loop and _main_loop.is_running():
        asyncio.run_coroutine_threadsafe(broadcast_state(state), _main_loop)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global _main_loop
    _main_loop = asyncio.get_running_loop()
    tsduck_service.set_log_callback(sync_log_callback)
    tsduck_service.set_state_callback(sync_state_callback)
    yield


# ──────────────────────────────────────────────
# FastAPI app
# ──────────────────────────────────────────────

app = FastAPI(
    title="Capture-TS",
    description="DVB-T Transport Stream Recorder",
    version="1.0.0",
    lifespan=lifespan,
)

BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


# ──────────────────────────────────────────────
# Pages
# ──────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard page."""
    devices = await tsduck_service.list_devices()
    recordings = await tsduck_service.list_recordings()
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "devices": devices,
            "recordings": recordings,
            "recording_state": tsduck_service.state.value,
            "current_recording": tsduck_service.current_recording,
        },
    )


# ──────────────────────────────────────────────
# HTMX API endpoints
# ──────────────────────────────────────────────

@app.post("/api/analyze", response_class=HTMLResponse)
async def analyze_mux(request: Request):
    """Analyze a mux and return the services table as HTML partial."""
    form = await request.form()
    frequency = int(form.get("frequency", 674000000))
    bandwidth = int(form.get("bandwidth", 8))
    adapter = int(form.get("adapter", 1))
    delivery_system = form.get("delivery_system", "DVB-T")
    duration = int(form.get("analysis_duration", 10))

    mux_info = await tsduck_service.analyze_mux(
        frequency=frequency,
        bandwidth=bandwidth,
        adapter=adapter,
        delivery_system=delivery_system,
        duration_seconds=duration,
    )

    return templates.TemplateResponse(
        request=request,
        name="partials/mux_info.html",
        context={"mux": mux_info},
    )


@app.post("/api/record/start", response_class=HTMLResponse)
async def start_recording(request: Request):
    """Start a recording and return updated recorder panel."""
    form = await request.form()
    frequency = int(form.get("frequency", 674000000))
    bandwidth = int(form.get("bandwidth", 8))
    adapter = int(form.get("adapter", 1))
    delivery_system = form.get("delivery_system", "DVB-T")

    # Parse selected services
    service_ids = []
    record_mode = form.get("record_mode", "full_mux")

    if record_mode == "selected":
        selected = form.getlist("selected_services")
        service_ids = [int(s) for s in selected if s]

    # Duration (0 = unlimited)
    duration = int(form.get("duration", 0)) or None

    try:
        recording = await tsduck_service.start_recording(
            frequency=frequency,
            bandwidth=bandwidth,
            adapter=adapter,
            delivery_system=delivery_system,
            service_ids=service_ids,
            duration_seconds=duration,
        )
        return templates.TemplateResponse(
            request=request,
            name="partials/recorder_panel.html",
            context={
                "recording_state": tsduck_service.state.value,
                "current_recording": recording,
            },
        )
    except RuntimeError as e:
        return HTMLResponse(
            f'<div class="flux-alert flux-alert--error">{e}</div>',
            status_code=400,
        )


@app.post("/api/record/stop", response_class=HTMLResponse)
async def stop_recording(request: Request):
    """Stop the current recording and return updated panel."""
    recording = await tsduck_service.stop_recording()
    recordings = await tsduck_service.list_recordings()
    return templates.TemplateResponse(
        request=request,
        name="partials/recorder_panel.html",
        context={
            "recording_state": tsduck_service.state.value,
            "current_recording": recording,
            "recordings": recordings,
        },
    )


@app.get("/api/record/panel", response_class=HTMLResponse)
async def get_recorder_panel(request: Request):
    """Return recorder panel as HTML partial."""
    return templates.TemplateResponse(
        request=request,
        name="partials/recorder_panel.html",
        context={
            "recording_state": tsduck_service.state.value,
            "current_recording": tsduck_service.current_recording,
        },
    )


@app.get("/api/recordings", response_class=HTMLResponse)
async def list_recordings(request: Request):
    """Return recordings table as HTML partial."""
    recordings = await tsduck_service.list_recordings()
    return templates.TemplateResponse(
        request=request,
        name="partials/recordings_table.html",
        context={"recordings": recordings},
    )


@app.delete("/api/recordings/{filename}", response_class=HTMLResponse)
async def delete_recording(request: Request, filename: str):
    """Delete a recording and return updated table."""
    await tsduck_service.delete_recording(filename)
    recordings = await tsduck_service.list_recordings()
    return templates.TemplateResponse(
        request=request,
        name="partials/recordings_table.html",
        context={"recordings": recordings},
    )


@app.get("/api/devices", response_class=JSONResponse)
async def list_devices():
    """Return available DVB devices as JSON."""
    devices = await tsduck_service.list_devices()
    return [{"index": d.index, "name": d.name, "systems": d.delivery_systems} for d in devices]


# ──────────────────────────────────────────────
# WebSocket for real-time updates
# ──────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """WebSocket for real-time log streaming and recording state updates."""
    await ws.accept()
    connected_websockets.add(ws)

    # Send initial state
    try:
        await ws.send_text(json.dumps({
            "type": "state",
            **tsduck_service._get_recording_status(),
        }))
    except Exception:
        pass

    try:
        while True:
            # Keep connection alive, handle client messages
            data = await ws.receive_text()
            msg = json.loads(data)

            if msg.get("type") == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))

    except WebSocketDisconnect:
        connected_websockets.discard(ws)
    except Exception:
        connected_websockets.discard(ws)
