"""Vectorize API tests (goal 950c719c).

POST /api/vectorize + GET /api/vectorize/{svg_id}/svg. The SLD subprocess is
MOCKED in unit tests (no real 23s vectorization); one integration test runs
the real CLI but is env-gated (skip by default).
"""

from __future__ import annotations

import os
import shutil
from unittest.mock import patch

import pytest

from .test_routes import client  # noqa: F401  (TestClient fixture)

from app.services import vectorizer

PNG_BYTES = b"\x89PNG\r\n\x1a\nfakepng"


def _mock_run_ok(stderr="warn line\n"):
    """Patch subprocess.run to fake a successful SLD run (writes the SVG)."""
    def fake_run(cmd, **kw):
        from pathlib import Path
        out = Path(cmd[cmd.index("--output-path") + 1])
        out.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
            '<path d="M10 10 L90 90" stroke="#000"/></svg>'
        )
        m = vectorizer.subprocess.CompletedProcess(cmd, 0)
        m.stderr = stderr
        m.stdout = ""
        return m
    return patch.object(vectorizer.subprocess, "run", side_effect=fake_run)


def _post_vectorize(client, *, filename="cat.png", content=PNG_BYTES,
                    data=None):
    return client.post(
        "/api/vectorize",
        files={"file": (filename, content, "image/png")},
        data=data or {},
    )


# ---------------------------------------------------------------- happy path

def test_vectorize_returns_metadata(client, tmp_path):
    with _mock_run_ok():
        r = _post_vectorize(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["svg_id"]
    assert body["filename"] == "output.svg"
    assert body["path"].startswith("vectorize/")
    assert body["path"].endswith("output.svg")
    assert isinstance(body["duration_s"], (int, float))
    # the SVG is stored under the app data dir
    stored = tmp_path / body["path"]
    assert stored.is_file()


def test_vectorize_svg_endpoint_serves_svg(client):
    with _mock_run_ok():
        r = _post_vectorize(client)
    svg_id = r.json()["svg_id"]
    r2 = client.get(f"/api/vectorize/{svg_id}/svg")
    assert r2.status_code == 200
    assert r2.headers["content-type"].startswith("image/svg+xml")
    assert b"<path" in r2.content


def test_vectorize_params_forwarded(client):
    with patch.object(vectorizer.subprocess, "run") as mr:
        def fake_run(cmd, **kw):
            from pathlib import Path
            Path(cmd[cmd.index("--output-path") + 1]).write_text("<svg/>")
            m = vectorizer.subprocess.CompletedProcess(cmd, 0)
            m.stderr = ""
            m.stdout = ""
            return m
        mr.side_effect = fake_run
        r = _post_vectorize(client, data={"thresh": "0.6", "multiple_lines": "true"})
    assert r.status_code == 200, r.text
    cmd = mr.call_args[0][0]
    assert cmd[cmd.index("--thresh") + 1] == "0.6"
    assert "--multiple-lines" in cmd


# ---------------------------------------------------------------- validation

def test_vectorize_rejects_non_image(client):
    r = _post_vectorize(client, filename="drawing.svg", content=b"<svg/>")
    assert r.status_code == 422
    assert "unsupported image type" in r.text


def test_vectorize_rejects_bad_thresh(client):
    r = _post_vectorize(client, data={"thresh": "1.5"})
    assert r.status_code == 422
    assert "0.01..0.99" in r.text


def test_vectorize_rejects_oversize(client):
    big = b"x" * (vectorizer.MAX_IMAGE_BYTES + 1)
    r = _post_vectorize(client, content=big)
    assert r.status_code == 422
    assert "too large" in r.text


# ---------------------------------------------------------------- failure

def test_vectorize_cli_failure_surfaces_stderr_tail(client):
    def fake_run(cmd, **kw):
        m = vectorizer.subprocess.CompletedProcess(cmd, 1)
        m.stderr = "boom\nCUDA error at line 42\n"
        m.stdout = ""
        return m
    with patch.object(vectorizer.subprocess, "run", side_effect=fake_run):
        r = _post_vectorize(client)
    assert r.status_code == 502
    body = r.json()
    assert "exit 1" in body["detail"]["message"]
    assert "CUDA error" in body["detail"]["stderr_tail"]


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
    # 1x1-ish tiny PNG (8x8 white with a black diagonal) via stdlib zlib/struct
    import struct, zlib

    def tiny_png():
        w = h = 8
        raw = b""
        for y in range(h):
            raw += b"\x00" + bytes(
                0 if x == y else 255 for x in range(w)
            )
        def chunk(typ, data):
            c = struct.pack(">I", len(data)) + typ + data
            return c + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
        ihdr = struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))

    r = _post_vectorize(client, content=tiny_png())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["duration_s"] > 0
    svg = client.get(f"/api/vectorize/{body['svg_id']}/svg")
    assert svg.status_code == 200
    assert svg.headers["content-type"].startswith("image/svg+xml")
    assert b"<path" in svg.content
