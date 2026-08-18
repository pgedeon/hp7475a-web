"""API-layer tests: jobs CRUD + state machine + worker commands against fakes.

Uses FastAPI TestClient with a real AppState backed by a temp DB and FAKE
device/pipeline (child lanes are tested in their own suites; here we pin the
HTTP contract of spec §33).
"""

from __future__ import annotations

import importlib.util
import threading
import time

import pytest
from fastapi.testclient import TestClient

# Child lanes (serial-core, svg-pipeline) are built by parallel agents; these
# tests activate automatically once those modules land.
SERIAL_LANE = importlib.util.find_spec("app.services.serial.discovery") is not None
PIPELINE_LANE = importlib.util.find_spec("app.services.pipeline.validator") is not None

requires_serial = pytest.mark.skipif(not SERIAL_LANE, reason="serial lane pending")
requires_pipeline = pytest.mark.skipif(
    not PIPELINE_LANE, reason="svg-pipeline lane pending"
)

from app.config import Settings
from app.db import Database
from app.jobs.models import JobState
from app.jobs.store import JobStore
from app.jobs.worker import HardwareWorker
from app.main import create_app
from app.api.ws import WSHub
from app.registry import AppState
from app.services.device_manager import DeviceManager
from app.services.files import FileRegistry


class FakeDriver:
    """Driver double mirroring the REAL driver's return types
    (dataclasses / tuples / None / str) so the DeviceManager shaping layer
    is what the API tests exercise — dict-returning fakes once hid 4 bugs."""

    def __init__(self):
        self.transport = None
        self.open = True
        self.log: list[str] = []

    def is_open(self) -> bool:
        return self.open

    def connect(self):
        self.log.append("connect")
        return "7475A"

    def close(self):
        self.open = False
        self.log.append("close")

    def identify(self):
        return "7475A"

    def status(self):
        from dataclasses import dataclass

        @dataclass
        class _SR:
            status_byte: int = 24
            pen_down: bool = False
            ready: bool = True
            error: bool = False

        return _SR()

    def errors(self):
        return (0, "No error")

    def position(self):
        return (0.0, 0.0, False)

    def hard_clip_limits(self):
        # A4 DIP-switch configuration — same shape as the real driver's
        # OH; tuple return; lets the paper-containment validation run.
        return (0, 0, 11040, 7721)

    def select_pen(self, n):
        self.log.append(f"SP{n}")
        return None

    def pen_up(self):
        return None

    def pen_down(self):
        return None

    def move_abs(self, x, y):
        from dataclasses import dataclass

        @dataclass
        class _MR:
            x: float
            y: float
            clamped: bool = False

        return _MR(x, y)

    def park(self):
        return None

    def initialize_device(self):
        return {}

    def complete_plot(self, timeout=600.0, **kwargs):
        return (0.0, 0.0, False)

    def await_completion(self, timeout=600.0):
        return True


class FakeStreamer:
    """Replaces ChunkedStreamer in worker for API tests."""

    def __init__(self, transport, **kwargs):
        pass

    def stream(self, payload, *, pause_event, cancel_event, should_run=None):
        for _ in range(2):  # simulate two progress steps
            if cancel_event.is_set():
                from app.jobs.streamer import StreamInterrupted

                raise StreamInterrupted("cancelled by user")
            if pause_event.is_set():
                from app.jobs.streamer import StreamInterrupted

                raise StreamInterrupted("paused by user")
            time.sleep(0.01)
        return len(payload)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, host="127.0.0.1", port=8750)
    app = create_app(settings)

    # Swap real lanes for fakes BEFORE lifespan runs by patching constructors
    # used inside lifespan: we instead build our own container post-startup.
    with TestClient(app) as c:
        container = app.state.container
        # fake device manager
        fake = DeviceManager()
        container.devices = fake
        container.worker._devices = fake
        # make connect use FakeDriver
        orig_connect = DeviceManager.connect

        def fake_connect(self, port, settings_dict=None, *, driver_factory=None):
            return orig_connect(
                self, port, settings_dict, driver_factory=lambda p, s: FakeDriver()
            )

        monkeypatch.setattr(DeviceManager, "connect", fake_connect)
        # fake streamer
        import app.jobs.worker as worker_mod

        monkeypatch.setattr(worker_mod, "ChunkedStreamer", FakeStreamer)
        yield c


def _upload_hpgl(client: TestClient, hpgl: str = "IN;SP1;PU100,100;PD200,200;SP0;") -> str:
    r = client.post(
        "/api/files/hpgl",
        files={"file": ("test.hpgl", hpgl.encode("ascii"), "application/octet-stream")},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _upload_svg(client: TestClient, svg: bytes | None = None) -> str:
    """Upload a benign SVG (or *svg* override) and return its file id."""
    payload = svg if svg is not None else _BENIGN_SVG
    r = client.post(
        "/api/files/svg",
        files={"file": ("test.svg", payload, "image/svg+xml")},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


@requires_pipeline
def test_hpgl_upload_rejects_forbidden_output_instruction(client):
    r = client.post(
        "/api/files/hpgl",
        files={"file": ("bad.hpgl", b"OI;", "application/octet-stream")},
    )
    assert r.status_code == 422


@requires_pipeline
def test_hpgl_upload_rejects_esc(client):
    r = client.post(
        "/api/files/hpgl",
        files={"file": ("bad.hpgl", b"\x1b.B;", "application/octet-stream")},
    )
    assert r.status_code == 422


@requires_serial
def test_device_connect_and_queries(client):
    r = client.get("/api/serial/ports")
    assert r.status_code == 200
    r = client.post("/api/device/connect", json={"port": "/dev/null-fake"})
    assert r.status_code == 200, r.text
    assert client.get("/api/device/status").json()["connected"] is True
    assert client.post("/api/device/identify").status_code == 200
    assert client.post("/api/device/pen/3").status_code == 200
    assert client.post("/api/device/pen/9").status_code == 422
    assert client.post("/api/device/pen-up").status_code == 200
    assert client.post("/api/device/park").status_code == 200


@requires_serial
def test_device_move_mm_converts_to_plotter_units(client):
    client.post("/api/device/connect", json={"port": "/dev/null-fake"})
    r = client.post("/api/device/move", json={"x": 25.0, "y": 25.0, "units": "mm"})
    assert r.status_code == 200
    assert abs(r.json()["x"] - 25.0 / 0.02488) < 1.0


@requires_pipeline
def test_job_lifecycle_happy_path(client):
    file_id = _upload_hpgl(client)
    r = client.post(
        "/api/jobs",
        json={"file_id": file_id, "name": "t", "paper": "a4", "pen_map": {"layer1": 1}},
    )
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]
    client.post("/api/device/connect", json={"port": "/dev/null-fake"})
    assert client.post(f"/api/jobs/{job_id}/prepare").status_code == 200
    # worker runs async; poll until READY
    for _ in range(50):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] == "READY":
            break
        time.sleep(0.05)
    assert job["status"] == "READY", job
    assert client.post(f"/api/jobs/{job_id}/start").status_code == 200
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] == "COMPLETED":
            break
        time.sleep(0.05)
    assert job["status"] == "COMPLETED", job


@requires_pipeline
def test_job_start_requires_connection(client):
    file_id = _upload_hpgl(client)
    job_id = client.post("/api/jobs", json={"file_id": file_id, "paper": "a4"}).json()["id"]
    r = client.post(f"/api/jobs/{job_id}/prepare")
    # device not connected → prepare transitions to FAILED
    for _ in range(50):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("FAILED", "READY"):
            break
        time.sleep(0.05)
    assert job["status"] == "FAILED"
    assert "connected" in job["error"]


@requires_pipeline
def test_invalid_paper_rejected(client):
    file_id = _upload_hpgl(client)
    r = client.post("/api/jobs", json={"file_id": file_id, "paper": "c5"})
    assert r.status_code == 422


@requires_pipeline
def test_delete_active_job_conflict(client):
    file_id = _upload_hpgl(client)
    job_id = client.post("/api/jobs", json={"file_id": file_id}).json()["id"]
    client.post("/api/device/connect", json={"port": "/dev/null-fake"})
    client.post(f"/api/jobs/{job_id}/prepare")
    for _ in range(50):
        if client.get(f"/api/jobs/{job_id}").json()["status"] == "READY":
            break
        time.sleep(0.05)
    # START immediately then DELETE should be disallowed while active
    client.post(f"/api/jobs/{job_id}/start")
    r = client.delete(f"/api/jobs/{job_id}")
    assert r.status_code in (409, 200)  # depends on timing; both legal per spec
    if r.status_code == 200:
        # job already terminal and deleted — re-delete must 404, nothing more
        assert client.delete(f"/api/jobs/{job_id}").status_code == 404
        return
    # still active (409): cancel, wait terminal, then delete must succeed
    client.post(f"/api/jobs/{job_id}/cancel")
    for _ in range(50):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("CANCELLED", "COMPLETED", "FAILED"):
            break
        time.sleep(0.05)
    assert client.delete(f"/api/jobs/{job_id}").status_code == 200


def test_settings_roundtrip(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    r = client.put("/api/settings", json={"custom": {"theme": "dark"}})
    assert r.status_code == 200
    assert client.get("/api/settings").json()["custom"]["theme"] == "dark"


_BENIGN_SVG = b'''<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" width="100mm" height="80mm" viewBox="0 0 100 80">
 <g id="layer1" inkscape:label="1"><path d="M10,10 L90,10 L90,70 Z" fill="none" stroke="#ff0000"/></g>
 <g id="layer2" inkscape:label="2"><rect x="20" y="20" width="30" height="30" fill="none" stroke="#00ff00"/></g>
</svg>'''


@requires_pipeline
def test_svg_upload_returns_sanitize_dict(client):
    """Regression: SanitizeReport dataclass once blew JSON serialization
    (HTTP 500 'Upload failed' in the browser)."""
    r = client.post(
        "/api/files/svg",
        files={"file": ("benign.svg", _BENIGN_SVG, "image/svg+xml")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body["sanitize"], dict)
    return body["id"]


@requires_pipeline
def test_svg_upload_rejects_unparseable(client):
    """Fail-closed: unparseable XML must 422, never store an empty file."""
    r = client.post(
        "/api/files/svg",
        files={"file": ("broken.svg", b"<svg><oops", "image/svg+xml")},
    )
    assert r.status_code == 422


@requires_pipeline
def test_analysis_endpoint_returns_dict(client):
    """Regression: SvgAnalysis dataclass once not JSON serializable."""
    r = client.post(
        "/api/files/svg",
        files={"file": ("benign.svg", _BENIGN_SVG, "image/svg+xml")},
    )
    assert r.status_code == 200, r.text
    fid = r.json()["id"]
    r = client.get(f"/api/files/{fid}/analysis")
    assert r.status_code == 200, r.text
    analysis = r.json()
    assert isinstance(analysis, dict)
    assert isinstance(analysis.get("layers"), list)


@requires_serial
def test_device_endpoints_shape_driver_types(client):
    """Regression: MoveResult/None returns once failed response validation
    (26× jog 500s). All endpoints must return JSON dicts now."""
    client.post("/api/device/connect", json={"port": "/dev/null-fake"})
    r = client.post("/api/device/identify")
    assert r.status_code == 200 and r.json()["identity"] == "7475A"
    r = client.post("/api/device/move", json={"x": 10, "y": 10, "units": "mm"})
    assert r.status_code == 200 and isinstance(r.json(), dict)
    assert "clamped" in r.json()
    assert client.post("/api/device/pen/2").json() == {"pen": 2}
    assert client.post("/api/device/pen-up").json() == {"pen_down": False}
    assert client.post("/api/device/park").json() == {"parked": True}
    status = client.get("/api/device/status").json()
    assert isinstance(status["status"], dict)


def test_ws_status_smoke(client):
    """Regression: websocket route once required a Request arg FastAPI
    never injects — every WS connect 500ed."""
    with client.websocket_connect("/api/ws/status") as ws:
        # trigger a publish from the app side (device event)
        client.post("/api/device/disconnect")
        msg = ws.receive_text()
        assert "type" in msg


@requires_pipeline
def test_prepare_rejects_paper_larger_than_plotter(client):
    """Vertical-lines regression (2026-08-18): A3 job on an A4-DIP plotter
    clamps beyond-clip coordinates into garbage lines — prepare must 422."""
    file_id = _upload_svg(client)
    job_id = client.post(
        "/api/jobs", json={"file_id": file_id, "paper": "a3"}
    ).json()["id"]
    client.post("/api/device/connect", json={"port": "/dev/null-fake"})
    r = client.post(f"/api/jobs/{job_id}/prepare")
    assert r.status_code == 422, r.text
    assert "exceeds the plotter" in r.text
    assert "a4" in r.text.lower()


@requires_pipeline
def test_job_scale_flows_to_pipeline_and_preview(client):
    file_id = _upload_svg(client)
    r = client.post(
        "/api/jobs", json={"file_id": file_id, "paper": "a4", "scale": 0.5}
    )
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]
    assert r.json()["options"]["scale"] == 0.5
    # scale out of range rejected by validation
    bad = client.post(
        "/api/jobs", json={"file_id": file_id, "paper": "a4", "scale": 1.5}
    )
    assert bad.status_code == 422
    client.post("/api/device/connect", json={"port": "/dev/null-fake"})
    assert client.post(f"/api/jobs/{job_id}/prepare").status_code == 200
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("READY", "FAILED"):
            break
        time.sleep(0.05)
    assert job["status"] == "READY", job
    assert job["stats"]["pipeline"]["user_scale"] == 0.5
    pv = client.get(f"/api/jobs/{job_id}/preview")
    assert pv.status_code == 200
    assert "A4" in pv.text and "50%" in pv.text
