"""API-level rotation/margin flow tests (goal 47da763c)."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from .test_routes import client, _upload_svg, requires_pipeline  # noqa: F401

@requires_pipeline
def test_rotate_and_margin_flow_to_pipeline_and_preview(client):  # noqa: F811
    file_id = _upload_svg(client)
    r = client.post(
        "/api/jobs",
        json={
            "file_id": file_id,
            "paper": "a4",
            "options": {"rotate_90": True, "margin_mm": 5},
        },
    )
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]
    assert r.json()["options"]["rotate_90"] is True
    assert r.json()["options"]["margin_mm"] == 5
    client.post("/api/device/connect", json={"port": "/dev/null-fake"})
    assert client.post(f"/api/jobs/{job_id}/prepare").status_code == 200
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("READY", "FAILED"):
            break
        time.sleep(0.05)
    assert job["status"] == "READY", job
    stats = job["stats"]["pipeline"]
    assert stats["rotate_90"] is True
    assert stats["margin_mm"] == 5.0
    assert stats["safe_area_mm"] == [274.0, 192.0]
    pv = client.get(f"/api/jobs/{job_id}/preview")
    assert pv.status_code == 200
    assert "ROTATED" in pv.text
    assert "Y paper motion" in pv.text

@requires_pipeline
def test_bad_margin_rejected_at_pipeline(client):  # noqa: F811
    file_id = _upload_svg(client)
    r = client.post(
        "/api/jobs",
        json={"file_id": file_id, "paper": "a4", "options": {"margin_mm": 99}},
    )
    assert r.status_code == 200  # job creation stores options verbatim…
    client.post("/api/device/connect", json={"port": "/dev/null-fake"})
    prep = client.post(f"/api/jobs/{r.json()['id']}/prepare")
    assert prep.status_code == 422  # …pipeline rejects at prepare time
    assert "margin_mm" in prep.text

@requires_pipeline
def test_papers_endpoint_exposes_safe_area(client):  # noqa: F811
    r = client.get("/api/papers")
    assert r.status_code == 200
    papers = r.json()
    assert papers["a4"]["safe_area_mm"] == [274.0, 192.0]
    assert papers["a3"]["loads_orientation"] == "portrait"
    assert papers["b"]["safe_area_mm"][0] < papers["b"]["x_range"][1] * 0.025
