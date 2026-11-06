"""
Capture-TS: FastAPI application for DVB-T Transport Stream recording.

Stack: FastAPI + Jinja2 + HTMX + WebSockets + TSDuck
Design: Flux Design System (dark-first, burgundy/magenta brand)
"""

import asyncio
import contextlib
import json
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData

from app.services import devices
from app.services import tsduck_service as tsduck
from app.version import APP_VERSION

# ──────────────────────────────────────────────
# Application state
# ──────────────────────────────────────────────

# Recordings live next to the project, wherever the app is started from.
tsduck_service = tsduck.TSDuckService()
connected_websockets: set[WebSocket] = set()
_main_loop: asyncio.AbstractEventLoop | None = None


async def _fan_out(payload: dict) -> None:
    """Send one JSON payload to every connected client, dropping dead ones."""
    global connected_websockets
    data = json.dumps(payload)
    stale = set()
    for ws in list(connected_websockets):
        try:
            await ws.send_text(data)
        except Exception:
            stale.add(ws)
    connected_websockets -= stale


async def broadcast_log(message: str) -> None:
    """Broadcast a log message to all connected WebSocket clients."""
    await _fan_out({"type": "log", "message": message})


async def broadcast_state(state: dict) -> None:
    """Broadcast recording state update to all connected WebSocket clients."""
    await _fan_out({"type": "state", **state})


async def broadcast_report(status: dict) -> None:
    """Announce that the report card has something new to show (S4, L3a)."""
    await _fan_out({"type": "report", **status})


def sync_log_callback(message: str) -> None:
    """Thread-safe log callback — schedules async broadcast."""
    if _main_loop and _main_loop.is_running():
        asyncio.run_coroutine_threadsafe(broadcast_log(message), _main_loop)


def sync_state_callback(state: dict) -> None:
    """Thread-safe state callback — schedules async broadcast."""
    if _main_loop and _main_loop.is_running():
        asyncio.run_coroutine_threadsafe(broadcast_state(state), _main_loop)


def sync_report_callback(status: dict) -> None:
    """Thread-safe report callback — schedules async broadcast."""
    if _main_loop and _main_loop.is_running():
        asyncio.run_coroutine_threadsafe(broadcast_report(status), _main_loop)


async def shutdown_recordings(service: tsduck.TSDuckService | None = None) -> None:
    """Stop a capture that is still running, so tsp cannot outlive the server.

    On Windows a child process survives its parent: closing the terminal in the
    middle of a capture left ``tsp`` writing to disk with nobody watching, and
    nothing in the UI could stop it. The lifespan calls this on shutdown.
    """
    target = service or tsduck_service
    if target.state is tsduck.RecordingState.IDLE:
        return
    await target.stop_recording()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global _main_loop
    _main_loop = asyncio.get_running_loop()
    tsduck_service.set_log_callback(sync_log_callback)
    tsduck_service.set_state_callback(sync_state_callback)
    tsduck_service.set_report_callback(sync_report_callback)
    try:
        yield
    finally:
        await shutdown_recordings()


# ──────────────────────────────────────────────
# FastAPI app
# ──────────────────────────────────────────────

# uvicorn's "auto" resolves to the legacy websockets implementation whenever the
# package is installed, and that implementation has been deprecated since
# websockets 14. Name the current one explicitly so the app (and the tests) run
# on the supported API instead of printing deprecation noise.
WS_IMPLEMENTATION = "websockets-sansio"

app = FastAPI(
    title="Capture-TS",
    description="DVB-T Transport Stream Recorder",
    version=APP_VERSION,
    lifespan=lifespan,
)

BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
# Available to every template, so the footer never hardcodes a version.
templates.env.globals["app_version"] = APP_VERSION
templates.env.filters["filesize"] = tsduck_service._format_size


def _report_context() -> dict:
    """Context for the report card: at most one of its states can render."""
    return {
        "report": tsduck_service.current_report,
        "report_pending_bytes": tsduck_service.report_pending_bytes,
    }


# ──────────────────────────────────────────────
# Pages
# ──────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard page."""
    available = await tsduck_service.list_devices()
    recordings = await tsduck_service.list_recordings()
    selected_device = devices.default_device(available)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "devices": available,
            "selected_adapter": selected_device.index if selected_device else -1,
            "delivery_systems": devices.available_delivery_systems(available),
            "selected_system": devices.default_delivery_system(selected_device),
            "recordings": recordings,
            "recording_state": tsduck_service.state.value,
            "current_recording": tsduck_service.current_recording,
            **_report_context(),
        },
    )


# ──────────────────────────────────────────────
# HTMX API endpoints
# ──────────────────────────────────────────────


class InvalidFormField(ValueError):
    """Raised when a submitted form field is present but not a valid integer."""

    def __init__(self, field: str, raw: object):
        super().__init__(f"Invalid value for '{field}': {raw!r}")
        self.field = field


def _form_int(form: FormData, key: str, default: int) -> int:
    """Parse an integer form field.

    A missing or empty field yields ``default``. A field that is present but
    malformed raises instead of falling back, because a typo in the duration
    must never turn into an unlimited recording.
    """
    raw = form.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(str(raw))
    except (TypeError, ValueError) as exc:
        raise InvalidFormField(key, raw) from exc


def invalid_field_response(error: Exception) -> HTMLResponse:
    """Render a rejected submission where the requested partial would have been swapped in."""
    return HTMLResponse(
        f'<div class="flux-alert flux-alert--error">{escape(str(error))}</div>',
        status_code=400,
    )


async def _resolve_device(
    adapter: int,
    delivery_system: str,
) -> tuple[int, str]:
    """Validate a submitted device/system pair against the devices present now."""
    available = await tsduck_service.list_devices()
    chosen = devices.resolve_device(available, adapter=adapter, delivery_system=delivery_system)
    return chosen.index, delivery_system


@app.post("/api/analyze", response_class=HTMLResponse)
async def analyze_mux(request: Request):
    """Analyze a mux and return the services table as HTML partial."""
    form = await request.form()
    try:
        frequency = _form_int(form, "frequency", 674000000)
        bandwidth = _form_int(form, "bandwidth", 8)
        adapter = _form_int(form, "adapter", 0)
        duration = _form_int(form, "analysis_duration", 10)
    except InvalidFormField as error:
        return invalid_field_response(error)
    delivery_system = str(form.get("delivery_system", "DVB-T"))

    try:
        adapter, delivery_system = await _resolve_device(adapter, delivery_system)
    except devices.DeviceSelectionError as error:
        return invalid_field_response(error)

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
    try:
        frequency = _form_int(form, "frequency", 674000000)
        bandwidth = _form_int(form, "bandwidth", 8)
        adapter = _form_int(form, "adapter", 0)
        # Duration (0 or empty = unlimited)
        duration = _form_int(form, "duration", 0) or None
        # None means "full mux"; an empty list means "selected, but nothing is
        # ticked", which start_recording refuses instead of recording everything.
        service_ids: list[int] | None = None
        if form.get("record_mode", "full_mux") == "selected":
            service_ids = []
            for raw in form.getlist("selected_services"):
                try:
                    service_ids.append(int(str(raw)))
                except (TypeError, ValueError) as exc:
                    raise InvalidFormField("selected_services", raw) from exc
    except InvalidFormField as error:
        return invalid_field_response(error)
    delivery_system = str(form.get("delivery_system", "DVB-T"))

    try:
        adapter, delivery_system = await _resolve_device(adapter, delivery_system)
    except devices.DeviceSelectionError as error:
        return invalid_field_response(error)

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
    except tsduck.NoServicesSelectedError as error:
        return invalid_field_response(error)
    except RuntimeError as e:
        return HTMLResponse(
            f'<div class="flux-alert flux-alert--error">{escape(str(e))}</div>',
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


@app.get("/api/report/card", response_class=HTMLResponse)
async def get_report_card(request: Request):
    """Return the report card for the current report state (S4, L3a).

    The browser asks for this when the WebSocket announces a new report: the card
    cannot be driven by the recording state, because a capture emits its finished
    ``IDLE`` state before the file has been analysed.
    """
    return templates.TemplateResponse(
        request=request,
        name="partials/report_card.html",
        context=_report_context(),
    )


@app.post("/api/report/analyze/{filename}", response_class=HTMLResponse)
async def analyze_named_recording(request: Request, filename: str):
    """Build the report for the recording the operator picked in the library (S10).

    A name that escapes ``recordings_dir`` or does not exist is refused before
    ``tsanalyze`` runs, and the refusal is a 404: the library only offers names it
    listed, so anything else is a hand-written request.
    """
    built = await tsduck_service.analyze_recording(filename)
    if built is None:
        raise HTTPException(status_code=404, detail="Unknown recording")

    return templates.TemplateResponse(
        request=request,
        name="partials/report_card.html",
        context=_report_context(),
    )


@app.post("/api/report/analyze", response_class=HTMLResponse)
async def analyze_capture_report(request: Request):
    """Build the report for the finished capture the operator asked about."""
    await tsduck_service.analyze_pending_capture()
    return templates.TemplateResponse(
        request=request,
        name="partials/report_card.html",
        context=_report_context(),
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
    with contextlib.suppress(Exception):
        await ws.send_text(json.dumps({
            "type": "state",
            **tsduck_service._get_recording_status(),
        }))

    try:
        while True:
            # Keep connection alive, handle client messages
            data = await ws.receive_text()
            msg = json.loads(data)

            if msg.get("type") == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))

    except Exception:
        # WebSocketDisconnect included: either way this peer is gone.
        connected_websockets.discard(ws)
