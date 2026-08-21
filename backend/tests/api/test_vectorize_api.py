"""Vectorize API tests (goals 950c719c + a7f70dae).

POST /api/vectorize starts a BACKGROUND JOB (202 {job_id}); the UI polls
GET /api/vectorize/{job_id}/status and can DELETE to cancel. The SLD
subprocess is MOCKED at the ``subprocess.Popen`` level (no real 23s
vectorization); one integration test runs the real CLI but is env-gated.
"""

from __future__ import annotations

import io
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from .test_routes import client  # noqa: F401  (TestClient fixture)

from app.services import vectorizer

PNG_BYTES = b"\x89PNG\r\n\x1a\nfakepng"

SVG_OUT = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    '<path d="M10 10 L90 90" stroke="#000"/></svg>'
)

class FakeProc:
    """Mimics subprocess.Popen: stdout lines, wait writes the SVG."""

    def __init__(self, out="", returncode=0, write_to=None):
        self.stdout = io.StringIO(out)
        self.returncode = returncode
        self._write_to = write_to
        self.killed = False

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        if self.killed:
            self.returncode = -9
            return self.returncode
        if self._write_to:
            Path(self._write_to).write_text(SVG_OUT)
        return self.returncode

def _mock_popen(out="", returncode=0):
    """Patch Popen so the child 'writes' the SVG at its --output-path."""
    def fake(cmd, **kw):
        out_path = Path(cmd[cmd.index("--output-path") + 1])
        return FakeProc(out=out, returncode=returncode, write_to=str(out_path))
    return fake

def _post(client, *, filename="cat.png", content=PNG_BYTES, data=None):
    return client.post(
        "/api/vectorize",
        files={"file": (filename, content, "image/png")},
        data=data or {},
    )

def _poll_done(client, job_id, tries=100):
    """Poll a job until it leaves queued/running; return final payload."""
    for _ in range(tries):
        r = client.get(f"/api/vectorize/{job_id}/status")
        assert r.status_code == 200, r.text
        body = r.json()
        if body["status"] in ("done", "error"):
            return body
        time.sleep(0.02)
    raise AssertionError("job did not finish in time")

# ---------------------------------------------------------------- happy path

def test_vectorize_start_returns_job_id(client, tmp_path):
    with patch.object(vectorizer.subprocess, "Popen", side_effect=_mock_popen()):
        r = _post(client)
    assert r.status_code == 202, r.text
    assert r.json()["job_id"]

def test_vectorize_job_completes_and_serves_svg(client, tmp_path):
    with patch.object(vectorizer.subprocess, "Popen", side_effect=_mock_popen()):
        job_id = _post(client).json()["job_id"]
        body = _poll_done(client, job_id)
    assert body["status"] == "done"
    res = body["result"]
    assert res["svg_id"]
    assert res["filename"] == "output.svg"
    assert res["path"].startswith("vectorize/")
    assert isinstance(res["duration_s"], (int, float))
    stored = tmp_path / res["path"]
    assert stored.is_file()
    r2 = client.get(f"/api/vectorize/{res['svg_id']}/svg")
    assert r2.status_code == 200
    assert r2.headers["content-type"].startswith("image/svg+xml")
    assert b"<path" in r2.content

def test_vectorize_status_reports_stage_lines(client):
    with patch.object(
        vectorizer.subprocess, "Popen",
        side_effect=_mock_popen(out="noise\nSTAGE loading image\nSTAGE ordering strokes\n"),
    ):
        job_id = _post(client).json()["job_id"]
        body = _poll_done(client, job_id)
    assert body["status"] == "done"

def test_vectorize_params_forwarded(client):
    with patch.object(vectorizer.subprocess, "Popen", side_effect=_mock_popen()) as mp:
        job_id = _post(client, data={"thresh": "0.6", "multiple_lines": "true"}).json()["job_id"]
        _poll_done(client, job_id)
    cmd = mp.call_args[0][0]
    assert cmd[cmd.index("--thresh") + 1] == "0.6"
    assert "--multiple-lines" in cmd

def test_vectorize_colors_forwarded_to_wrapper(client):
    captured = {}
    def fake(cmd, **kw):
        captured["cmd"] = cmd
        out = Path(cmd[3])  # multicolor CLI: IN OUT --colors K
        return FakeProc(write_to=str(out))
    with patch.object(vectorizer.subprocess, "Popen", side_effect=fake):
        job_id = _post(client, data={"colors": "3"}).json()["job_id"]
        body = _poll_done(client, job_id)
    assert body["status"] == "done"
    cmd = captured["cmd"]
    assert cmd[1] == str(vectorizer.MULTICOLOR_SCRIPT)
    assert cmd[cmd.index("--colors") + 1] == "3"

# ---------------------------------------------------------------- validation

def test_vectorize_rejects_non_image(client):
    r = _post(client, filename="drawing.svg", content=b"<svg/>")
    assert r.status_code == 422
    assert "unsupported image type" in r.text

def test_vectorize_rejects_bad_thresh(client):
    r = _post(client, data={"thresh": "1.5"})
    assert r.status_code == 422
    assert "0.01..0.99" in r.text

def test_vectorize_rejects_bad_colors(client):
    for bad in ("0", "9", "-2"):
        r = _post(client, data={"colors": bad})
        assert r.status_code == 422, bad
        assert "colors must be" in r.text

def test_vectorize_rejects_oversize(client):
    big = b"x" * (vectorizer.MAX_IMAGE_BYTES + 1)
    r = _post(client, content=big)
    assert r.status_code == 422
    assert "too large" in r.text

# ---------------------------------------------------------------- failure

def test_vectorize_cli_failure_surfaces_stderr_tail(client):
    with patch.object(
        vectorizer.subprocess, "Popen",
        side_effect=_mock_popen(out="boom\nCUDA error at line 42\n", returncode=1),
    ):
        job_id = _post(client).json()["job_id"]
        body = _poll_done(client, job_id)
    assert body["status"] == "error"
    err = body["error"]
    assert "exit 1" in err["message"]
    assert "CUDA error" in err["stderr_tail"]

def test_vectorize_status_unknown_job_404(client):
    assert client.get("/api/vectorize/nonexistent/status").status_code == 404

def test_vectorize_cancel_unknown_job_404(client):
    assert client.delete("/api/vectorize/nonexistent").status_code == 404

def test_vectorize_svg_unknown_id_404(client):
    assert client.get("/api/vectorize/nonexistent/svg").status_code == 404

def test_vectorize_svg_path_traversal_rejected(client):
    for bad in ("..%2F..%2Fetc", "..", "a/b"):
        r = client.get(f"/api/vectorize/{bad}/svg")
        assert r.status_code in (404, 422), f"{bad}: {r.status_code}"

# ---------------------------------------------------------------- integration
# Real SLD CLI run (~25-90s on dev). Opt-in via HP7475A_TEST_REAL_VECTORIZE=1
# and only when the CLI exists (dev box).

requires_real = pytest.mark.skipif(
    os.environ.get("HP7475A_TEST_REAL_VECTORIZE") != "1"
    or not os.path.exists(vectorizer.SLD_CLI),
    reason="real SLD CLI run disabled (set HP7475A_TEST_REAL_VECTORIZE=1 on dev)",
)

@requires_real
def test_vectorize_real_cli_integration(client):
    """End-to-end: real SLDvec run on a tiny generated PNG."""
    import struct, zlib

    def tiny_png():
        w = h = 8
        raw = b""
        for y in range(h):
            raw += b"\x00" + bytes(0 if x == y else 255 for x in range(w))
        def chunk(typ, data):
            c = struct.pack(">I", len(data)) + typ + data
            return c + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
        ihdr = struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))

    r = _post(client, content=tiny_png())
    assert r.status_code == 202, r.text
    body = _poll_done(client, r.json()["job_id"], tries=2000)
    assert body["status"] == "done", body
    svg = client.get(f"/api/vectorize/{body['result']['svg_id']}/svg")
    assert svg.status_code == 200
    assert svg.headers["content-type"].startswith("image/svg+xml")
    assert b"<path" in svg.content
