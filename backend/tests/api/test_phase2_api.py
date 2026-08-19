"""Phase 2 API-level tests (goal 47da763c phase 2).

F2: estimate surfaced on the job JSON after prepare (lifted from
stats.pipeline.estimate). F3: WS reconnect receives a resume snapshot for
the active job. F4: oversize copies → 422 with max-fit hint.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from .test_routes import client, _upload_svg, requires_pipeline  # noqa: F401


def _wait_status(client, job_id, want, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in want:
            return job
        time.sleep(0.05)
    raise AssertionError(f"stuck in {job['status']}, wanted {want}")


@requires_pipeline
def test_estimate_lands_on_job_json_after_prepare(client):
    """AC2: READY job JSON gains a top-level estimate dict."""
    file_id = _upload_svg(client)
    r = client.post(
        "/api/jobs",
        json={"file_id": file_id, "paper": "a4",
              "options": {"velocity_cm_s": 10.0}},
    )
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]
    # pre-prepare: no estimate yet
    assert "estimate" not in client.get(f"/api/jobs/{job_id}").json()
    client.post("/api/device/connect", json={"port": "/dev/null-fake"})
    assert client.post(f"/api/jobs/{job_id}/prepare").status_code == 200
    job = _wait_status(client, job_id, {"READY", "FAILED"})
    assert job["status"] == "READY", job
    est = job["estimate"]
    assert set(est) == {"drawn_mm", "travel_mm", "velocity_cm_s", "est_seconds"}
    assert est["velocity_cm_s"] == 9.88  # quantized at prepare time
    assert est["est_seconds"] > 0 and est["travel_mm"] >= 0


@requires_pipeline
def test_oversize_copies_prepare_422_with_max_fit_hint(client):
    """AC4: oversize grid surfaces as a 422 naming the max that fits."""
    file_id = _upload_svg(client)
    r = client.post(
        "/api/jobs",
        json={"file_id": file_id, "paper": "a4",
              "options": {"copies": {"rows": 20, "cols": 20}}},
    )
    job_id = r.json()["id"]
    client.post("/api/device/connect", json={"port": "/dev/null-fake"})
    prep = client.post(f"/api/jobs/{job_id}/prepare")
    assert prep.status_code == 422
    assert "max that fits" in prep.text


@requires_pipeline
def test_ws_connect_sends_resume_snapshot_for_active_job(client):
    """AC3: a WS (re)connect while a job is active receives a resume event
    with the buffered progress so the client can restore its bar."""
    file_id = _upload_svg(client)
    job_id = client.post(
        "/api/jobs", json={"file_id": file_id, "paper": "a4"}
    ).json()["id"]
    client.post("/api/device/connect", json={"port": "/dev/null-fake"})
    assert client.post(f"/api/jobs/{job_id}/prepare").status_code == 200
    _wait_status(client, job_id, {"READY", "FAILED"})

    # stub the worker's active-job pointer (no real plot in API tests)
    container = client.app.state.container
    real_worker = container.worker

    class StubWorker:
        current_job_id = job_id

    container.worker = StubWorker()
    try:
        with client.websocket_connect("/api/ws/status") as ws:
            msg = json.loads(ws.receive_text())
        assert msg["type"] == "job"
        assert msg["event"] == "resume"
        assert msg["job_id"] == job_id
        assert msg["acked_bytes"] == 0
        assert "total_bytes" in msg
        assert "pen_down" in msg
    finally:
        container.worker = real_worker


_FILL_ONLY_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    b'<path d="M10 10 L90 10 L90 90 L10 90 Z" fill="#0000ff"/></svg>'
)

_NAMED_GROUPS_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    b'<g id="cut"><path d="M5 5 L20 5" stroke="#000" fill="none"/></g>'
    b'<g id="engrave"><path d="M5 10 L20 10" stroke="#f00" fill="none"/></g>'
    b'</svg>'
)


@requires_pipeline
def test_analysis_reports_fill_warning_not_blocker(client):
    """2026-08-19 user report: fills must not appear under unsupported."""
    file_id = _upload_svg(client, _FILL_ONLY_SVG)
    a = client.get(f"/api/files/{file_id}/analysis").json()
    assert a["unsupported"] == []
    assert len(a["warnings"]) == 1 and "filled shapes" in a["warnings"][0]
    assert a["layers"] == [] and a["stroke_colors"] == []


@requires_pipeline
def test_fill_only_svg_prepares_with_single_outline_layer(client):
    """Fill-only artwork plots as outlines; job with pen_map {"1": 2} must
    reach READY and emit SP2 (not the default pen 1)."""
    file_id = _upload_svg(client, _FILL_ONLY_SVG)
    job_id = client.post(
        "/api/jobs",
        json={"file_id": file_id, "paper": "a4", "pen_map": {"1": 2}},
    ).json()["id"]
    client.post("/api/device/connect", json={"port": "/dev/null-fake"})
    assert client.post(f"/api/jobs/{job_id}/prepare").status_code == 200
    job = _wait_status(client, job_id, {"READY", "FAILED"})
    assert job["status"] == "READY", job
    assert "SP2;" in job["hpgl"] and "SP1;" not in job["hpgl"]


@requires_pipeline
def test_named_layer_pen_map_translated_to_vpype_ids(client):
    """UI keys pen maps by analysis layer label; prepare must translate to
    vpype's numeric ids (document order) so assignments are honored."""
    file_id = _upload_svg(client, _NAMED_GROUPS_SVG)
    a = client.get(f"/api/files/{file_id}/analysis").json()
    assert a["layers"] == ["cut", "engrave"]
    job_id = client.post(
        "/api/jobs",
        json={"file_id": file_id, "paper": "a4",
              "pen_map": {"cut": 3, "engrave": 5}},
    ).json()["id"]
    client.post("/api/device/connect", json={"port": "/dev/null-fake"})
    assert client.post(f"/api/jobs/{job_id}/prepare").status_code == 200
    job = _wait_status(client, job_id, {"READY", "FAILED"})
    assert job["status"] == "READY", job
    hpgl = job["hpgl"]
    assert "SP3;" in hpgl and "SP5;" in hpgl
    assert "SP1;" not in hpgl  # old behavior: silent default-pen fallback
