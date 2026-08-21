"""REST API routes (spec §33 endpoint table).

All handlers are thin: they validate input, delegate to AppState services,
and translate domain errors to proper HTTP codes. No business logic here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import asdict, is_dataclass
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request, Response, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pathlib import Path
from pydantic import BaseModel, Field

from app.jobs.models import IllegalTransition, JobState
from app.jobs.store import JobNotFound
from app.db import new_id
from app.jobs.worker import WorkerCommand
from app.services.serial.paper import PAPERS

logger = logging.getLogger(__name__)


def _jsonable(obj: Any) -> Any:
    """Route-boundary serializer: dataclasses → dicts (SanitizeReport,
    SvgAnalysis, etc.); dicts/lists pass through."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    return obj


def get_state(request: Request):
    return request.app.state.container

def _job_json(job) -> dict:
    """Job dict + top-level convenience fields (phase 2 F2): ``estimate``
    lifted from stats.pipeline.estimate when the pipeline produced one."""
    d = job.to_dict()
    est = (job.stats or {}).get("pipeline", {}).get("estimate")
    if est:
        d["estimate"] = est
    return d


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
    result = {**info, "status": status}
    try:
        result["buffer_free"] = state.devices.buffer_space()
    except Exception:
        pass  # optional enrichment; monitor UI renders when present
    return result


@router.get("/device/hard-clip")
async def device_hard_clip(request: Request) -> dict:
    return _device_call(request, get_state(request).devices.hard_clip_limits)


@router.get("/device/buffer")
async def device_buffer(request: Request) -> dict:
    value = _device_call(request, get_state(request).devices.buffer_space)
    return {"free": value}


@router.get("/device/error")
async def device_error(request: Request) -> dict:
    return _device_call(request, get_state(request).devices.error)


class PenBody(BaseModel):
    pen: int = Field(ge=1, le=6)


@router.get("/device/io-error")
async def device_io_error(request: Request) -> dict:
    """RS-232 extended error (ESC .E). Reading it CLEARS the front-panel
    ERROR light when the latch is I/O-side (e.g. 10 overlapping output
    request from crossed queries, 15 framing, 16 buffer overflow)."""
    state = get_state(request)
    driver = state.devices.driver()
    if driver is None:
        raise HTTPException(409, "device not connected")
    try:
        code, meaning = driver.transport.extended_error()
    except Exception as exc:
        raise HTTPException(409, f"io error query failed: {exc}")
    return {"io": {"code": code, "meaning": meaning}}


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
async def upload_svg(
    request: Request, file: UploadFile = File(...),
    convert_text: bool = Form(False),
) -> dict:
    """Upload + sanitize an SVG (goal 47da763c phase 3 F6).

    Optional ``convert_text``: when checked and the sanitized SVG contains
    text elements, run headless Inkscape text-to-path conversion, re-sanitize
    its output and store THAT. Fail-soft everywhere: Inkscape missing,
    erroring, timing out or producing sanitizer-rejected output keeps the
    original sanitized file plus a warning in ``conversion`` — never
    blocks the upload."""
    from app.services.pipeline.sanitizer import sanitize_svg  # pipeline lane

    state = get_state(request)
    raw = await file.read()
    try:
        clean, report = sanitize_svg(raw, state.settings.max_upload_bytes)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if getattr(report, "rejected", False) or not clean:
        # fail-closed: rejected input is NEVER stored (sanitizer returns
        # empty bytes on rejection — callers must not proceed)
        raise HTTPException(
            422, {"message": "SVG rejected", "sanitize": _jsonable(report)}
        )
    conversion = {"attempted": False, "converted": False, "warning": None}
    if convert_text:
        conversion["attempted"] = True
        from app.services.pipeline.textpath import convert_text_to_paths, has_text_elements

        if has_text_elements(clean):
            converted, err = convert_text_to_paths(clean)
            if err is not None:
                conversion["warning"] = f"text-to-path conversion unavailable ({err})"
            else:
                re_clean, re_report = sanitize_svg(converted, state.settings.max_upload_bytes)
                if getattr(re_report, "rejected", False) or not re_clean:
                    conversion["warning"] = (
                        "text-to-path conversion unavailable (converted output "
                        "rejected by sanitizer: "
                        + "; ".join(getattr(re_report, "reasons", []) or ["unknown"])
                        + ") — original kept"
                    )
                else:
                    clean, report = re_clean, re_report
                    conversion["converted"] = True
    meta = state.files.save(
        kind="svg", name=file.filename or "upload.svg", content=clean,
        extra={"sanitize_report": _jsonable(report),
               "text_converted": conversion["converted"],
               "conversion": conversion},
    )
    return {"id": meta.id, "name": meta.name, "size": meta.size_bytes,
            "sanitize": _jsonable(report),
            "text_converted": conversion["converted"],
            "conversion": conversion}


@router.post("/files/hpgl")
async def upload_hpgl(request: Request, file: UploadFile = File(...)) -> dict:
    from app.services.pipeline.validator import validate_hpgl  # pipeline lane

    state = get_state(request)
    raw = await file.read()
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise HTTPException(422, "HP-GL must be ASCII")
    from app.services.pipeline.pd_split import split_long_pd

    text = split_long_pd(text)  # monster PD/PU -> <=240B (boundary-safe)
    raw = text.encode("ascii")
    validation = validate_hpgl(text, None)
    if validation.errors:
        raise HTTPException(422, {"message": "HP-GL rejected", "validation": {"errors": validation.errors, "warnings": validation.warnings}})
    meta = state.files.save(
        kind="hpgl", name=file.filename or "upload.hpgl", content=raw,
        extra={"validation": {"errors": validation.errors, "warnings": validation.warnings}},
    )
    return {"id": meta.id, "name": meta.name, "size": meta.size_bytes,
            "validation": {"errors": validation.errors, "warnings": validation.warnings}}


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
        return _jsonable(meta.analysis) if not isinstance(meta.analysis, dict) else meta.analysis
    data = state.files.read_bytes(file_id)
    if not data:
        raise HTTPException(422, "stored file is empty (rejected upload?)")
    analysis = _jsonable(analyze_svg(data))
    meta.analysis = analysis
    state.files.update(meta)
    return analysis


@router.get("/files/{file_id}/raw")
async def file_raw(file_id: str, request: Request) -> Response:
    """Serve the STORED (sanitized / text-converted) file bytes — artwork
    preview source, goal 47da763c phase 3 F5. Never the raw upload."""
    state = get_state(request)
    try:
        meta = state.files.get(file_id)
    except FileNotFoundError:
        raise HTTPException(404, "file not found")
    data = state.files.read_bytes(file_id)
    if not data:
        raise HTTPException(422, "stored file is empty (rejected upload?)")
    media = "image/svg+xml" if meta.kind == "svg" else "text/plain"
    return Response(content=data, media_type=media)


# ---------------------------------------------------------------- vectorize

def _vectorize_dir(request: Request, svg_id: str) -> Path:
    """Resolve a vectorize job dir, refusing path traversal (404 on bad id)."""
    if not svg_id or "/" in svg_id or "\\" in svg_id or ".." in svg_id:
        raise HTTPException(404, "vectorize result not found")
    d = get_state(request).settings.data_dir / "vectorize" / svg_id
    if not d.is_dir():
        raise HTTPException(404, "vectorize result not found")
    return d

# Vectorize background jobs (goal a7f70dae): POST returns immediately, the
# subprocess runs in a worker thread, the UI polls status and can cancel.
# In-memory store is fine: jobs are minutes-lived, results persist on disk.
_VJOBS: dict[str, dict] = {}
_VJOBS_LOCK = threading.Lock()
_VJOB_TTL_S = 3600.0  # finished-job records pruned lazily

def _vjob_sweep() -> None:
    """Drop finished job records older than the TTL (called under lock)."""
    now = time.monotonic()
    stale = [
        jid for jid, j in _VJOBS.items()
        if j["status"] in ("done", "error") and now - j["ended_at"] > _VJOB_TTL_S
    ]
    for jid in stale:
        _VJOBS.pop(jid, None)

def _run_vjob(job_id: str, data_dir: Path, raw: bytes, filename: str,
              thresh: float | None, multiple_lines: bool, colors: int) -> None:
    from app.services.vectorizer import VectorizeError, run_vectorization

    job = _VJOBS[job_id]
    try:
        job["status"] = "running"
        result = run_vectorization(
            data_dir, raw, filename,
            thresh=thresh,
            multiple_lines=multiple_lines,
            colors=colors,
            on_stage=lambda text: job.update(stage=text),
            cancel_event=job["cancel"],
        )
        rel = Path(result.svg_path).relative_to(data_dir)
        job.update(
            status="done", stage=None,
            result={
                "svg_id": result.id,
                "filename": Path(result.svg_path).name,
                "path": str(rel),
                "duration_s": result.duration_s,
            },
        )
    except VectorizeError as exc:
        job.update(status="error", error={"message": str(exc), "stderr_tail": exc.stderr_tail})
    except ValueError as exc:
        job.update(status="error", error={"message": str(exc), "stderr_tail": ""})
    except Exception as exc:  # defensive: never leave a job stuck "running"
        logger.exception("vectorize job %s crashed", job_id)
        job.update(status="error", error={"message": f"internal error: {exc}", "stderr_tail": ""})
    finally:
        job["ended_at"] = time.monotonic()

@router.post("/vectorize", status_code=202)
async def vectorize(
    request: Request,
    file: UploadFile = File(...),
    thresh: float | None = Form(None),
    multiple_lines: bool = Form(False),
    colors: int = Form(1),
) -> dict:
    """Raster drawing → SVG via SLD-Vectorization, as a BACKGROUND JOB.

    Returns 202 {job_id} immediately; poll GET /vectorize/{job_id}/status.
    ``colors`` 1 = single-line B/W (thresh/multiple_lines apply); 2–8 =
    multi-color layered vectorization. Concurrency-limited to 2; extra jobs
    queue (status stays "queued"). DELETE /vectorize/{job_id} cancels."""
    from app.services.vectorizer import MAX_COLORS, MIN_COLORS, validate_image

    state = get_state(request)
    raw = await file.read()
    try:
        # Sync validation: bad uploads fail fast with 422 instead of
        # creating a job that immediately errors.
        validate_image(file.filename or "", raw)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if thresh is not None and not (0.01 <= thresh <= 0.99):
        raise HTTPException(422, "thresh must be 0.01..0.99 (or omit for auto)")
    if not (MIN_COLORS <= colors <= MAX_COLORS):
        raise HTTPException(422, f"colors must be {MIN_COLORS}..{MAX_COLORS} (1 = single-line B/W)")

    with _VJOBS_LOCK:
        _vjob_sweep()
        job_id = new_id()
        _VJOBS[job_id] = {
            "status": "queued", "stage": None, "result": None, "error": None,
            "started_at": time.monotonic(), "ended_at": 0.0,
            "cancel": threading.Event(),
        }
    threading.Thread(
        target=_run_vjob,
        args=(job_id, state.settings.data_dir, raw, file.filename or "upload.png",
              thresh, multiple_lines, colors),
        name=f"vjob-{job_id}",
        daemon=True,
    ).start()
    return {"job_id": job_id}

@router.get("/vectorize/{job_id}/status")
async def vectorize_status(job_id: str, request: Request) -> dict:
    """Poll a vectorize job: queued/running (+stage, elapsed) / done / error."""
    with _VJOBS_LOCK:
        job = _VJOBS.get(job_id)
        if job is None:
            raise HTTPException(404, "vectorize job not found")
        payload = {
            "status": job["status"],
            "stage": job["stage"],
            "elapsed_s": round((job["ended_at"] or time.monotonic()) - job["started_at"], 1),
            "result": job["result"],
            "error": job["error"],
        }
    return payload

@router.delete("/vectorize/{job_id}")
async def vectorize_cancel(job_id: str, request: Request) -> dict:
    """Cancel a queued/running vectorize job (kills the subprocess)."""
    with _VJOBS_LOCK:
        job = _VJOBS.get(job_id)
        if job is None:
            raise HTTPException(404, "vectorize job not found")
    job["cancel"].set()
    return {"status": "cancelling"}

@router.get("/vectorize/{svg_id}/svg")
async def vectorize_svg(svg_id: str, request: Request) -> Response:
    """Serve a vectorized SVG (image/svg+xml)."""
    d = _vectorize_dir(request, svg_id)
    svg = d / "output.svg"
    if not svg.is_file():
        raise HTTPException(404, "vectorize result not found")
    return Response(
        content=svg.read_bytes(), media_type="image/svg+xml"
    )


# ---------------------------------------------------------------- jobs

class JobCreateBody(BaseModel):
    file_id: str
    name: str = ""
    paper: str = "a4"
    scale: float = Field(default=1.0, ge=0.25, le=1.0)
    pen_map: dict[str, int] = Field(default_factory=dict)
    pen_map_mode: str = "layers"  # "layers" | "colors" (goal 3e598c6e)
    options: dict[str, Any] = Field(default_factory=dict)


@router.post("/jobs")
async def create_job(body: JobCreateBody, request: Request) -> dict:
    state = get_state(request)
    if body.paper not in PAPERS:
        raise HTTPException(422, f"paper must be one of {sorted(PAPERS)}")
    if body.pen_map_mode not in ("layers", "colors"):
        raise HTTPException(422, "pen_map_mode must be 'layers' or 'colors'")
    try:
        meta = state.files.get(body.file_id)
    except FileNotFoundError:
        raise HTTPException(404, "file not found")
    for layer, pen in body.pen_map.items():
        if not 1 <= pen <= 6:
            raise HTTPException(422, f"pen for layer {layer!r} must be 1..6")
    job = state.jobs.create(
        name=body.name or meta.name, file_id=body.file_id, paper=body.paper,
        pen_map=body.pen_map,
        options={**body.options, "pen_map_mode": body.pen_map_mode,
                 "scale": body.scale},
    )
    return _job_json(job)


@router.get("/jobs")
async def list_jobs(request: Request) -> dict:
    state = get_state(request)
    return {"jobs": [_job_json(j) for j in state.jobs.list()],
            "active_job_id": state.worker.current_job_id if state.worker else None}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, request: Request) -> dict:
    try:
        return _job_json(get_state(request).jobs.get(job_id))
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


def _annotate_preview(svg_text: str, paper: str, user_scale: float, rotated: bool = False) -> str:
    """Overlay sheet outline, safe-area rect, axis arrows + caption.

    Goal 47da763c: TWO rects — full sheet (grey, subtle) + safe/plot area
    (red dashed) — plus pen-carriage/paper-motion axis indicators and a
    caption (paper, loading orientation, sheet/safe mm, scale %, ROTATED
    badge). Pure static SVG, no scripts."""
    import re
    from app.services.pipeline.vpy import safe_page_rect_mm
    from app.services.serial.paper import get_paper as _gp

    m = re.search(
        r'viewBox="([\d.eE+-]+) ([\d.eE+-]+) ([\d.eE+-]+) ([\d.eE+-]+)"', svg_text
    )
    if not m:
        return svg_text
    w, h = float(m.group(3)), float(m.group(4))
    sw = max(1.0, w / 500)
    p = _gp(paper)
    # safe rect: page mm -> viewBox units (vpype preview is 96 dpi)
    px = lambda mm: mm * 96.0 / 25.4
    sx0, sy0, sx1, sy1 = (px(v) for v in safe_page_rect_mm(p))
    fs = w / 55  # caption font size
    overlay = (
        # full sheet outline — subtle grey, solid
        f'<rect x="{sw:.2f}" y="{sw:.2f}" width="{w - 2 * sw:.1f}" height="{h - 2 * sw:.1f}" '
        f'fill="none" stroke="#adb5bd" stroke-width="{sw:.2f}"/>'
        # safe plot area — red dashed
        f'<rect x="{sx0:.1f}" y="{sy0:.1f}" width="{sx1 - sx0:.1f}" height="{sy1 - sy0:.1f}" '
        f'fill="none" stroke="#ff5252" stroke-width="{sw:.2f}" '
        f'stroke-dasharray="{w / 90:.1f},{w / 220:.1f}"/>'
        # axis indicators: X = pen carriage (horizontal), Y = paper motion
        f'<g stroke="#868e96" stroke-width="{sw:.2f}" fill="#868e96" '
        f'font-family="sans-serif" font-size="{fs:.1f}">'
        f'<line x1="{w * 0.03:.1f}" y1="{h * 0.94:.1f}" x2="{w * 0.10:.1f}" y2="{h * 0.94:.1f}"/>'
        f'<polygon points="{w * 0.10:.1f},{h * 0.94 - fs * 0.35:.1f} '
        f'{w * 0.10 + fs * 0.7:.1f},{h * 0.94:.1f} {w * 0.10:.1f},{h * 0.94 + fs * 0.35:.1f}" '
        f'stroke="none"/>'
        f'<text x="{w * 0.115:.1f}" y="{h * 0.94 + fs * 0.35:.1f}" stroke="none">X pen carriage</text>'
        f'<line x1="{w * 0.03:.1f}" y1="{h * 0.90:.1f}" x2="{w * 0.03:.1f}" y2="{h * 0.83:.1f}"/>'
        f'<polygon points="{w * 0.03 - fs * 0.35:.1f},{h * 0.83:.1f} '
        f'{w * 0.03:.1f},{h * 0.83 - fs * 0.7:.1f} {w * 0.03 + fs * 0.35:.1f},{h * 0.83:.1f}" '
        f'stroke="none"/>'
        f'<text x="{w * 0.045:.1f}" y="{h * 0.845:.1f}" stroke="none">Y paper motion</text>'
        f'</g>'
        # caption
        f'<text x="{w * 0.02:.1f}" y="{h * 0.035:.1f}" font-family="sans-serif" '
        f'font-size="{fs:.1f}" fill="#868e96">'
        f'{p.name.upper()} \u00b7 {p.loads_orientation.upper()} \u00b7 sheet '
        f'{p.size_mm[0]:.0f}\u00d7{p.size_mm[1]:.0f} mm \u00b7 safe '
        f'{p.safe_size_mm[0]:.0f}\u00d7{p.safe_size_mm[1]:.0f} mm \u00b7 '
        f'{round(user_scale * 100)}%'
        + (' \u00b7 <tspan fill="#ff5252" font-weight="bold">ROTATED</tspan>' if rotated else "")
        + '</text>'
    )
    cut = svg_text.index(">", svg_text.index("<svg")) + 1
    return svg_text[:cut] + overlay + svg_text[cut:]


def _validate_paper_against_plotter(state, paper_name: str) -> None:
    """Refuse papers larger than the plotter's DIP-switched clip (422).

    Vertical-lines fix (2026-08-18): an A3 job on an A4-configured plotter
    clamps every coordinate beyond the real clip into garbage edge lines.
    Skipped when the device is disconnected or the clip query fails —
    prepare's device check still gates the rest."""
    if not state.devices.is_connected():
        return
    try:
        clip = state.devices.hard_clip_limits()
    except Exception:
        return
    from app.services.serial.paper import clip_fits, get_paper

    if not clip_fits(get_paper(paper_name), tuple(clip["limits"])):
        detected = clip.get("paper") or "unknown"
        raise HTTPException(
            422,
            f"job paper {paper_name!r} exceeds the plotter's plottable area "
            f"(plotter reports {detected!r} — check rear DIP switches). "
            f"Beyond-clip coordinates get clamped into vertical garbage "
            f"lines. Re-create the job with paper={detected!r}.",
        )


@router.post("/jobs/{job_id}/prepare")
async def prepare_job(job_id: str, request: Request) -> dict:
    state = get_state(request)
    # If the job's file is SVG, run the pipeline first to attach HP-GL.
    try:
        job = state.jobs.get(job_id)
    except JobNotFound:
        raise HTTPException(404, "job not found")
    _validate_paper_against_plotter(state, job.paper)
    if not job.hpgl and job.file_id:
        from app.services.pipeline.vpy import PipelineOptions, run_pipeline  # pipeline lane

        try:
            meta = state.files.get(job.file_id)
        except FileNotFoundError:
            raise HTTPException(404, "source file vanished")
        if meta.kind == "svg":
            try:
                opts = PipelineOptions.from_dict(job.options)
                if job.options.get("pen_map_mode") == "colors":
                    from app.services.pipeline.vpy import run_pipeline_color

                    result = run_pipeline_color(
                        meta.stored_path, job.paper, opts, job.pen_map,
                    )
                else:
                    result = run_pipeline(meta.stored_path, job.paper, opts, job.pen_map)
            except Exception as exc:
                # record WHY on the job (stays QUEUED → re-preparable;
                # goal 47da763c phase-3 wart fix), then 422 the caller
                try:
                    state.jobs.update(job_id, error=f"pipeline failed: {exc}")
                except Exception:
                    logger.debug("could not record pipeline error on job", exc_info=True)
                raise HTTPException(422, f"pipeline failed: {exc}")
            preview_dir = state.settings.data_dir / "previews"
            preview_dir.mkdir(parents=True, exist_ok=True)
            preview_path = preview_dir / f"{job_id}.svg"
            try:
                pv_text = Path(result.preview_svg_path).read_text(encoding="utf-8")
                _stats = result.stats or {}
                pv_text = _annotate_preview(
                    pv_text, job.paper, float(_stats.get("user_scale", 1.0)),
                    rotated=bool(job.options.get("rotate_90")),
                )
                preview_path.write_text(pv_text)
            except OSError:
                preview_path = preview_dir / job_id  # placeholder on failure
                preview_path.write_text(
                    "<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'/>"
                )
            state.jobs.update(job_id, hpgl=result.hpgl,
                              stats={"pipeline": result.stats,
                                     "preview": str(preview_path)})
        elif meta.kind == "hpgl":
            payload = state.files.read_bytes(job.file_id).decode("ascii")
            # Honor the velocity option for RAW HP-GL too (2026-08-19: the
            # slider silently did nothing for raw files). Per-pen VS after
            # each SPn — a bare VS would bind to the current pen (pen 0 in
            # a header, i.e. nobody) and be ignored, same bug as the SVG
            # pipeline's old header-VS.
            _vel = job.options.get("velocity_cm_s", job.options.get("velocity"))
            if _vel is not None:
                from app.services.serial.protocol import (
                    VELOCITY_MAX_CM_S, VELOCITY_MIN_CM_S,
                )
                from app.services.pipeline.vpy import quantize_velocity
                v = float(_vel)
                if not (VELOCITY_MIN_CM_S <= v <= VELOCITY_MAX_CM_S):
                    raise HTTPException(422, f"velocity {v} outside "
                                             f"{VELOCITY_MIN_CM_S}..{VELOCITY_MAX_CM_S} cm/s")
                q = quantize_velocity(v)
                if abs(q - VELOCITY_MAX_CM_S) > 1e-9:  # default → no VS
                    import re as _re
                    payload = _re.sub(
                        r"(SP([1-6]);)",
                        lambda m: f"{m.group(1)}VS{q:g},{m.group(2)};",
                        payload,
                    )
            from app.services.pipeline.validator import validate_hpgl as _vh
            from app.services.serial.paper import get_paper as _gp
            vr = _vh(payload, _gp(job.paper))
            if vr.errors:
                raise HTTPException(422, {"message": "stored HP-GL failed re-validation for this paper", "validation": {"errors": vr.errors, "warnings": vr.warnings}})
            state.jobs.update(job_id, hpgl=payload)
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


@router.get("/jobs/{job_id}/preview")
async def job_preview(job_id: str, request: Request):
    """Post-processing preview SVG (what will actually be plotted)."""
    state = get_state(request)
    try:
        job = state.jobs.get(job_id)
    except JobNotFound:
        raise HTTPException(404, "job not found")
    preview = (state.settings.data_dir / "previews" / f"{job_id}.svg")
    if not preview.is_file():
        raise HTTPException(404, "preview not ready (prepare the job first)")
    return FileResponse(preview, media_type="image/svg+xml")


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
            "safe_area_mm": p.safe_area_mm,
            "loads_orientation": p.loads_orientation,
        }
        for name, p in PAPERS.items()
    }


# ---------------------------------------------------------------- websocket

@router.websocket("/ws/status")
async def ws_status(ws: WebSocket) -> None:
    """Live status socket. NOTE: FastAPI websocket routes do NOT receive a
    Request object — app state is reached via ws.app (regression-tested
    after this exact bug 500'd every WS connect on device)."""
    state = ws.app.state.container
    cid = await state.ws_hub.connect(ws)
    try:
        # F3: on (re)connect, resume event for the active job so a client
        # that dropped mid-plot restores its progress bar immediately.
        active = state.worker.current_job_id
        if active:
            try:
                job = state.jobs.get(active)
                await ws.send_text(json.dumps({
                    "type": "job", "event": "resume", "job_id": job.id,
                    "status": job.status.value,
                    "acked_bytes": job.bytes_sent,
                    "total_bytes": job.bytes_total,
                    "pen_down": None,
                }))
            except Exception:
                logger.debug("ws resume snapshot failed", exc_info=True)
        while True:
            # Client pings keep the socket alive; content ignored.
            await ws.receive_text()
    except WebSocketDisconnect:
        state.ws_hub.disconnect(cid)
