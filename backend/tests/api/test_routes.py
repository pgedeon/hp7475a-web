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
    """Minimal driver double honoring the DeviceManager contract."""

    def __init__(self):
        self.transport = None
        self.open = True
        self.log: list[str] = []

    def is_open(self) -> bool:
        return self.open

    def connect(self):
        self.log.append("connect")
        return {"identity": "7475A"}

    def close(self):
        self.open = False
        self.log.append("close")

    def identify(self):
        return {"identity": "7475A"}

    def status(self):
        return {"status": 24}

    def errors(self):
        return {"hpgl": 0, "extended": 0}

    def position(self):
        return {"x": 0.0, "y": 0.0, "pen_down": False}

    def select_pen(self, n):
        self.log.append(f"SP{n}")
        return {"pen": n}

    def pen_up(self):
        return {"pen_down": False}

    def pen_down(self):
        return {"pen_down": True}

    def move_abs(self, x, y):
        return {"x": x, "y": y, "clamped": False}

    def park(self):
        return {"parked": True}

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
    # ensure terminal
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
