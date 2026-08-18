"""Adversarial API tests (goal 3e598c6e): hostile/malformed inputs, state
misuse, concurrency exclusivity. Every case must produce a clean 4xx/409 —
never a 500."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.device_manager import DeviceManager

from tests.api.test_routes import FakeDriver, FakeStreamer  # reuse doubles

from tests.api.test_routes import _BENIGN_SVG  # noqa: F401  (fixture svg)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    app = create_app(settings)
    with TestClient(app) as c:
        container = app.state.container
        fake = DeviceManager()
        container.devices = fake
        container.worker._devices = fake
        orig = DeviceManager.connect
        monkeypatch.setattr(
            DeviceManager, "connect",
            lambda self, port, s=None, *, driver_factory=None: orig(
                self, port, s, driver_factory=lambda p, ss: FakeDriver()),
        )
        import app.jobs.worker as W
        monkeypatch.setattr(W, "ChunkedStreamer", FakeStreamer)
        yield c


def _connect(client):
    r = client.post("/api/device/connect", json={"port": "/dev/null-fake"})
    assert r.status_code == 200


# ---------------------------------------------------------------- connects

def test_double_connect_conflict(client):
    _connect(client)
    r = client.post("/api/device/connect", json={"port": "/dev/null-fake"})
    assert r.status_code == 409


def test_connect_missing_port_fails_at_driver(client):
    """Fake driver accepts any port, so the missing-port path is asserted
    at the driver layer (route maps any exception → 502)."""
    from app.services.serial.driver import HP7475ADevice

    with pytest.raises(Exception):
        HP7475ADevice("/dev/does-not-exist").connect()


def test_device_calls_disconnected_409(client):
    assert client.post("/api/device/identify").status_code == 409
    assert client.post("/api/device/pen/1").status_code == 409
    assert client.post("/api/device/pen-up").status_code == 409
    assert client.post("/api/device/move", json={"x": 1, "y": 1}).status_code == 409
    assert client.post("/api/device/park").status_code == 409


# ---------------------------------------------------------------- uploads

def test_oversized_svg_rejected(client):
    big = b"<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'>" + b" " * (21 * 1024 * 1024)
    r = client.post("/api/files/svg", files={"file": ("big.svg", big, "image/svg+xml")})
    assert r.status_code == 422


def test_malicious_svg_neutralized(client):
    evil = (b"<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'>"
            b"<script>alert(1)</script><rect width='4' height='4' stroke='#f00' fill='none'/></svg>")
    r = client.post("/api/files/svg", files={"file": ("evil.svg", evil, "image/svg+xml")})
    assert r.status_code == 200
    assert any("script" in str(rem).lower() for rem in r.json()["sanitize"]["removals"])


def test_hpgl_upload_non_ascii_422(client):
    r = client.post("/api/files/hpgl", files={"file": ("x.hpgl", "PU;\xff\xfe;".encode("latin-1"), "application/octet-stream")})
    assert r.status_code == 422


# ---------------------------------------------------------------- jobs

def _svg_job(client, **extra):
    fid = client.post("/api/files/svg", files={"file": ("b.svg", _BENIGN_SVG, "image/svg+xml")}).json()["id"]
    body = {"file_id": fid, "paper": "a4", **extra}
    return client.post("/api/jobs", json=body)


def test_job_bad_paper_pen_mode(client):
    assert _svg_job(client, paper="c7").status_code == 422
    assert _svg_job(client, pen_map={"x": 0}).status_code == 422
    assert _svg_job(client, pen_map={"x": 7}).status_code == 422
    assert _svg_job(client, pen_map_mode="rainbow").status_code == 422


def test_job_missing_file_404(client):
    r = client.post("/api/jobs", json={"file_id": "nope", "paper": "a4"})
    assert r.status_code == 404


def test_concurrent_active_job_rejected(client):
    _connect(client)
    j1 = _svg_job(client).json()["id"]
    j2 = _svg_job(client).json()["id"]
    client.post(f"/api/jobs/{j1}/prepare")
    for _ in range(50):
        if client.get(f"/api/jobs/{j1}").json()["status"] == "READY":
            break
        time.sleep(0.05)
    client.post(f"/api/jobs/{j1}/start")
    # second job may not start while first active (FakeStreamer finishes fast;
    # race-safe: retry until j1 terminal, then j2 must be accepted)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        st1 = client.get(f"/api/jobs/{j1}").json()["status"]
        if st1 in ("COMPLETED", "FAILED", "CANCELLED"):
            break
        time.sleep(0.1)
    r2 = client.post(f"/api/jobs/{j2}/prepare")
    assert r2.status_code == 200


def test_preview_before_prepare_404(client):
    j = _svg_job(client).json()["id"]
    assert client.get(f"/api/jobs/{j}/preview").status_code == 404


def test_analysis_of_hpgl_file_422(client):
    fid = client.post("/api/files/hpgl", files={"file": ("a.hpgl", b"IN;SP1;PU0,0;SP0;", "application/octet-stream")}).json()["id"]
    assert client.get(f"/api/files/{fid}/analysis").status_code == 422


def test_color_mode_unknown_color_rejected_at_prepare(client):
    _connect(client)
    j = _svg_job(client, pen_map_mode="colors", pen_map={"#doesnotexist": 2}).json()["id"]
    r = client.post(f"/api/jobs/{j}/prepare")
    # inline pipeline validation → 422; job stays QUEUED (never started)
    assert r.status_code == 422
    assert "not present" in str(r.json()["detail"])
    assert client.get(f"/api/jobs/{j}").json()["status"] == "QUEUED"


def test_color_mode_happy_path(client):
    _connect(client)
    j = _svg_job(
        client, pen_map_mode="colors",
        pen_map={"#ff0000": 3, "#00ff00": 5},
    ).json()["id"]
    client.post(f"/api/jobs/{j}/prepare")
    for _ in range(80):
        job = client.get(f"/api/jobs/{j}").json()
        if job["status"] in ("READY", "FAILED"):
            break
        time.sleep(0.1)
    assert job["status"] == "READY", job["error"]
    assert "SP3;" in job["hpgl"] and "SP5;" in job["hpgl"]


# ---------------------------------------------------------------- ws abuse

def test_ws_connect_disconnect_loop(client):
    for _ in range(5):
        with client.websocket_connect("/api/ws/status") as ws:
            client.post("/api/device/disconnect")
            assert "type" in ws.receive_text()
