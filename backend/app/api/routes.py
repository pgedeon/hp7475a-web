"""REST API routes (spec §33 endpoint table).

All handlers are thin: they validate input, delegate to AppState services,
and translate domain errors to proper HTTP codes. No business logic here.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, UploadFile, File, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.jobs.models import IllegalTransition, JobState
from app.jobs.store import JobNotFound
from app.jobs.worker import WorkerCommand
from app.services.serial.paper import PAPERS

logger = logging.getLogger(__name__)


def get_state(request: Request):
    return request.app.state.container


router = APIRouter(prefix="/api")


# ---------------------------------------------------------------- health

@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------- serial

@router.get("/serial/ports")
async def serial_ports(request: Request) -> dict:
    from app.services.serial.discovery import list_ports  # serial lane (lazy)

    state = get_state(request)
    try:
        ports = list_ports()
    except Exception as exc:
        raise HTTPException(500, f"port discovery failed: {exc}")
    return {
        "ports": [p.to_dict() if hasattr(p, "to_dict") else p for p in ports],
        "selected": state.devices.port,
    }


# ---------------------------------------------------------------- device

class ConnectBody(BaseModel):
    port: str
    baudrate: int = 9600
    bytesize: int = Field(default=8, ge=5, le=8)
    parity: str = "N"
    stopbits: int = Field(default=1, ge=1, le=2)


@router.post("/device/connect")
async def device_connect(body: ConnectBody, request: Request) -> dict:
    state = get_state(request)
    try:
        info = state.devices.connect(
            body.port,
            {
                "baudrate": body.baudrate,
                "bytesize": body.bytesize,
                "parity": body.parity,
                "stopbits": body.stopbits,
            },
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"connection failed: {exc}")
    state.ws_hub.publish({"type": "device", "event": "connected", "port": body.port})
    return {"connected": True, "port": body.port, "info": info}


@router.post("/device/disconnect")
async def device_disconnect(request: Request) -> dict:
    state = get_state(request)
    state.devices.disconnect()
    state.ws_hub.publish({"type": "device", "event": "disconnected"})
    return {"connected": False}


def _device_call(request: Request, fn, *args) -> dict:
    state = get_state(request)
    try:
        return fn(*args)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    except Exception as exc:
        logger.exception("device call failed")
        raise HTTPException(502, f"device error: {exc}")


@router.post("/device/identify")
async def device_identify(request: Request) -> dict:
    return _device_call(request, get_state(request).devices.identify)


@router.get("/device/status")
async def device_status(request: Request) -> dict:
    state = get_state(request)
    info = state.devices.connection_info()
    if not info["connected"]:
        return {"connected": False, "port": None, "settings": None, "status": None}
    try:
        status = state.devices.status()
    except Exception as exc:
        status = {"error": str(exc)}
    return {**info, "status": status}


@router.get("/device/error")
async def device_error(request: Request) -> dict:
    return _device_call(request, get_state(request).devices.error)


class PenBody(BaseModel):
    pen: int = Field(ge=1, le=6)


@router.post("/device/pen/{number}")
async def device_pen(number: int, request: Request) -> dict:
    if not 1 <= number <= 6:
        raise HTTPException(422, "pen must be 1..6")
    return _device_call(request, get_state(request).devices.select_pen, number)


@router.post("/device/pen-up")
async def device_pen_up(request: Request) -> dict:
    return _device_call(request, get_state(request).devices.pen_up)


@router.post("/device/pen-down")
async def device_pen_down(request: Request) -> dict:
    return _device_call(request, get_state(request).devices.pen_down)


class MoveBody(BaseModel):
    x: float
    y: float
    units: str = "mm"  # mm | plotter


@router.post("/device/move")
async def device_move(body: MoveBody, request: Request) -> dict:
    from app.services.serial.paper import mm_to_plotter_units

    x, y = body.x, body.y
    if body.units == "mm":
        x, y = mm_to_plotter_units(x), mm_to_plotter_units(y)
    return _device_call(request, get_state(request).devices.move, x, y)


@router.post("/device/park")
async def device_park(request: Request) -> dict:
    return _device_call(request, get_state(request).devices.park)


# ---------------------------------------------------------------- files

@router.post("/files/svg")
async def upload_svg(request: Request, file: UploadFile = File(...)) -> dict:
    from app.services.pipeline.sanitizer import sanitize_svg  # pipeline lane

    state = get_state(request)
    raw = await file.read()
    try:
        clean, report = sanitize_svg(raw, state.settings.max_upload_bytes)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    meta = state.files.save(
        kind="svg", name=file.filename or "upload.svg", content=clean,
        extra={"sanitize_report": report},
    )
    return {"id": meta.id, "name": meta.name, "size": meta.size_bytes,
            "sanitize": report}


@router.post("/files/hpgl")
async def upload_hpgl(request: Request, file: UploadFile = File(...)) -> dict:
    from app.services.pipeline.validator import validate_hpgl  # pipeline lane

    state = get_state(request)
    raw = await file.read()
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise HTTPException(422, "HP-GL must be ASCII")
    validation = validate_hpgl(text, None)
    if validation.errors:
        raise HTTPException(422, {"message": "HP-GL rejected", "validation": validation})
    meta = state.files.save(
        kind="hpgl", name=file.filename or "upload.hpgl", content=raw,
        extra={"validation": validation},
    )
    return {"id": meta.id, "name": meta.name, "size": meta.size_bytes,
            "validation": validation}


@router.get("/files/{file_id}/analysis")
async def file_analysis(file_id: str, request: Request) -> dict:
    from app.services.pipeline.analyzer import analyze_svg  # pipeline lane

    state = get_state(request)
    try:
        meta = state.files.get(file_id)
    except FileNotFoundError:
        raise HTTPException(404, "file not found")
    if meta.kind != "svg":
        raise HTTPException(422, "analysis is for SVG files")
    if meta.analysis:
        return meta.analysis
    analysis = analyze_svg(state.files.read_bytes(file_id))
    meta.analysis = analysis
    state.files.update(meta)
    return analysis


# ---------------------------------------------------------------- jobs

class JobCreateBody(BaseModel):
    file_id: str
    name: str = ""
    paper: str = "a4"
    pen_map: dict[str, int] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)


@router.post("/jobs")
async def create_job(body: JobCreateBody, request: Request) -> dict:
    state = get_state(request)
    if body.paper not in PAPERS:
        raise HTTPException(422, f"paper must be one of {sorted(PAPERS)}")
    try:
        meta = state.files.get(body.file_id)
    except FileNotFoundError:
        raise HTTPException(404, "file not found")
    for layer, pen in body.pen_map.items():
        if not 1 <= pen <= 6:
            raise HTTPException(422, f"pen for layer {layer!r} must be 1..6")
    job = state.jobs.create(
        name=body.name or meta.name, file_id=body.file_id, paper=body.paper,
        pen_map=body.pen_map, options=body.options,
    )
    return job.to_dict()


@router.get("/jobs")
async def list_jobs(request: Request) -> dict:
    state = get_state(request)
    return {"jobs": [j.to_dict() for j in state.jobs.list()],
            "active_job_id": state.worker.current_job_id if state.worker else None}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, request: Request) -> dict:
    try:
        return get_state(request).jobs.get(job_id).to_dict()
    except JobNotFound:
        raise HTTPException(404, "job not found")


def _job_command(request: Request, job_id: str, command: str) -> dict:
    state = get_state(request)
    try:
        state.jobs.get(job_id)
    except JobNotFound:
        raise HTTPException(404, "job not found")
    try:
        state.worker.submit(command, job_id)
    except IllegalTransition as exc:
        raise HTTPException(409, str(exc))
    return {"accepted": True, "command": command, "job_id": job_id}


@router.post("/jobs/{job_id}/prepare")
async def prepare_job(job_id: str, request: Request) -> dict:
    state = get_state(request)
    # If the job's file is SVG, run the pipeline first to attach HP-GL.
    try:
        job = state.jobs.get(job_id)
    except JobNotFound:
        raise HTTPException(404, "job not found")
    if not job.hpgl and job.file_id:
        from app.services.pipeline.vpy import run_pipeline  # pipeline lane

        try:
            meta = state.files.get(job.file_id)
        except FileNotFoundError:
            raise HTTPException(404, "source file vanished")
        if meta.kind == "svg":
            try:
                result = run_pipeline(
                    state.files.get(job.file_id).stored_path, job.paper,
                    job.options, job.pen_map,
                )
            except Exception as exc:
                raise HTTPException(422, f"pipeline failed: {exc}")
            state.jobs.update(job_id, hpgl=result.hpgl,
                              stats={"pipeline": result.stats})
    return _job_command(request, job_id, WorkerCommand.PREPARE)


@router.post("/jobs/{job_id}/start")
async def start_job(job_id: str, request: Request) -> dict:
    return _job_command(request, job_id, WorkerCommand.START)


@router.post("/jobs/{job_id}/pause")
async def pause_job(job_id: str, request: Request) -> dict:
    return _job_command(request, job_id, WorkerCommand.PAUSE)


@router.post("/jobs/{job_id}/resume")
async def resume_job(job_id: str, request: Request) -> dict:
    return _job_command(request, job_id, WorkerCommand.RESUME)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request) -> dict:
    return _job_command(request, job_id, WorkerCommand.CANCEL)


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str, request: Request) -> dict:
    state = get_state(request)
    try:
        state.jobs.delete(job_id)
    except JobNotFound:
        raise HTTPException(404, "job not found")
    except IllegalTransition as exc:
        raise HTTPException(409, str(exc))
    return {"deleted": job_id}


# ---------------------------------------------------------------- settings

@router.get("/settings")
async def get_settings(request: Request) -> dict:
    state = get_state(request)
    return {
        "host": state.settings.host,
        "port": state.settings.port,
        "stream": {
            "safety_margin": state.settings.stream_safety_margin,
            "default_chunk": state.settings.stream_default_chunk,
            "query_timeout_s": state.settings.stream_query_timeout_s,
            "max_retries": state.settings.stream_max_retries,
            "completion_timeout_s": state.settings.completion_timeout_s,
        },
        "job_history_keep": state.settings.job_history_keep,
        "custom": state.db.get_setting("custom", {}),
    }


class CustomSettingsBody(BaseModel):
    custom: dict[str, Any] = Field(default_factory=dict)


@router.put("/settings")
async def put_settings(body: CustomSettingsBody, request: Request) -> dict:
    state = get_state(request)
    state.db.set_setting("custom", body.custom)
    return {"saved": True}


# ---------------------------------------------------------------- papers (helper for UI)

@router.get("/papers")
async def papers() -> dict:
    return {
        name: {
            "size_mm": p.size_mm,
            "x_range": p.x_range,
            "y_range": p.y_range,
            "dip_mode": p.dip_mode,
            "info": p.info,
        }
        for name, p in PAPERS.items()
    }


# ---------------------------------------------------------------- websocket

@router.websocket("/ws/status")
async def ws_status(ws: WebSocket, request: Request) -> None:
    state = get_state(request)
    await state.ws_hub.connect(ws)
    cid = state.ws_hub._next_id - 1
    try:
        while True:
            # Client pings keep the socket alive; content ignored.
            await ws.receive_text()
    except WebSocketDisconnect:
        state.ws_hub.disconnect(cid)
