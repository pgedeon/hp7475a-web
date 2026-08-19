"""Phase 3 API tests (goal 47da763c): F5 raw file endpoint, F6 upload-time
text→paths conversion (Inkscape, fail-soft), F7 analysis hints, plus the
prepare-failure wart fix (error recorded on QUEUED job).
"""

from __future__ import annotations

import shutil
import stat
import xml.etree.ElementTree as ET

import pytest

from .test_routes import client, _upload_svg, requires_pipeline  # noqa: F401

TEXT_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="50mm" '
    b'viewBox="0 0 100 50">'
    b'<rect x="5" y="5" width="20" height="10" fill="none" stroke="#ff0000"/>'
    b'<text x="30" y="30" font-size="10" fill="none" stroke="#0000ff">Hi</text>'
    b"</svg>"
)

requires_inkscape = pytest.mark.skipif(
    shutil.which("inkscape") is None, reason="Inkscape not installed"
)


def _upload_text_svg(client, convert: bool = False) -> dict:
    r = client.post(
        "/api/files/svg",
        files={"file": ("t.svg", TEXT_SVG, "image/svg+xml")},
        data={"convert_text": "true" if convert else "false"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _tags(raw: bytes) -> set[str]:
    return {el.tag.rsplit("}", 1)[-1] for el in ET.fromstring(raw).iter()}


# ---------------------------------------------------------------- F7 hints

@requires_pipeline
def test_analysis_gains_hints_for_unsupported(client):
    filled = TEXT_SVG.replace(b'fill="none" stroke="#ff0000"', b'fill="#00ff00" stroke="#ff0000"')
    r = client.post(
        "/api/files/svg", files={"file": ("filled.svg", filled, "image/svg+xml")}
    )
    assert r.status_code == 200, r.text
    meta = r.json()
    a = client.get(f"/api/files/{meta['id']}/analysis").json()
    assert any("text elements" in w for w in a["unsupported"])
    # backward compatible: unsupported kept as [str]; hints maps warning → action
    assert isinstance(a["unsupported"], list)
    text_warn = next(w for w in a["unsupported"] if "text elements" in w)
    assert "Convert text to paths" in a["hints"][text_warn]
    # fills are non-blocking (phase-3 split): they live in warnings, not unsupported
    fill_warn = next(w for w in a["warnings"] if "filled shapes" in w)
    assert "outline-only" in a["hints"][fill_warn]


# ---------------------------------------------------------------- F6 convert

@requires_pipeline
@requires_inkscape
def test_upload_with_convert_stores_text_free_svg(client):
    meta = _upload_text_svg(client, convert=True)
    assert meta["text_converted"] is True
    assert meta["conversion"]["warning"] is None
    raw = client.get(f"/api/files/{meta['id']}/raw")
    assert raw.status_code == 200
    assert raw.headers["content-type"].startswith("image/svg+xml")
    tags = _tags(raw.content)
    assert "text" not in tags and "path" in tags
    # analysis runs on the CONVERTED file: text warning gone, strokes alive
    a = client.get(f"/api/files/{meta['id']}/analysis").json()
    assert not any("text elements" in w for w in a["unsupported"])
    assert "#ff0000" in a["stroke_colors"] and "#0000ff" in a["stroke_colors"]


@requires_pipeline
def test_upload_without_convert_keeps_original_and_warns(client):
    meta = _upload_text_svg(client, convert=False)
    assert meta["text_converted"] is False
    assert meta["conversion"]["attempted"] is False
    raw = client.get(f"/api/files/{meta['id']}/raw").content
    assert b"<text" in raw  # original kept verbatim (sanitized)
    a = client.get(f"/api/files/{meta['id']}/analysis").json()
    assert any("text elements" in w for w in a["unsupported"])


@requires_pipeline
def test_inkscape_missing_fails_soft_keeps_original(client, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent-phase3-test")
    meta = _upload_text_svg(client, convert=True)
    assert meta["text_converted"] is False
    assert "Inkscape not installed" in meta["conversion"]["warning"]
    raw = client.get(f"/api/files/{meta['id']}/raw").content
    assert b"<text" in raw  # original kept


def _fake_inkscape(tmp_path, body: str) -> str:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / "inkscape"
    exe.write_text(body)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return str(bindir)


@requires_pipeline
def test_malicious_conversion_output_rejected_sanitizer_keeps_original(
    client, tmp_path, monkeypatch
):
    """Fuzz one: fake Inkscape emits an XXE payload — the sanitizer must
    reject it and the upload must keep the original file (fail-closed)."""
    fake = (
        "#!/bin/sh\n"
        'out=""; prev=""; for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done\n'
        'cat > "$out" <<\'EOF\'\n'
        '<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>\n'
        '<svg xmlns="http://www.w3.org/2000/svg">&xxe;</svg>\n'
        "EOF\n"
    )
    monkeypatch.setenv("PATH", _fake_inkscape(tmp_path, fake))
    meta = _upload_text_svg(client, convert=True)
    assert meta["text_converted"] is False
    assert "rejected by sanitizer" in meta["conversion"]["warning"]
    raw = client.get(f"/api/files/{meta['id']}/raw").content
    assert b"<text" in raw  # original kept, never the malicious output


# ---------------------------------------------------------------- F5 raw

@requires_pipeline
def test_raw_serves_sanitized_not_uploaded(client):
    """The stored bytes are the SANITIZED upload — a script tag must not
    survive into /raw even though the upload was accepted."""
    dirty = TEXT_SVG.replace(
        b"</svg>", b'<script>alert(1)</script></svg>'
    )
    r = client.post(
        "/api/files/svg",
        files={"file": ("d.svg", dirty, "image/svg+xml")},
    )
    assert r.status_code == 200, r.text
    fid = r.json()["id"]
    raw = client.get(f"/api/files/{fid}/raw")
    assert raw.status_code == 200
    assert b"<script" not in raw.content
    assert b"<text" in raw.content


def test_raw_404_for_unknown_file(client):  # noqa: F811 — client fixture
    assert client.get("/api/files/does-not-exist/raw").status_code == 404


# ---------------------------------------------------------------- wart fix

@requires_pipeline
def test_prepare_pipeline_failure_records_error_on_queued_job(client):
    """Tiling-oversize 422: job stays QUEUED but now carries the reason on
    its record, and stays re-preparable (a sane re-prepare succeeds)."""
    file_id = _upload_svg(client)
    job_id = client.post(
        "/api/jobs",
        json={"file_id": file_id, "paper": "a4",
              "options": {"copies": {"rows": 20, "cols": 20}}},
    ).json()["id"]
    client.post("/api/device/connect", json={"port": "/dev/null-fake"})
    r = client.post(f"/api/jobs/{job_id}/prepare")
    assert r.status_code == 422
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "QUEUED"
    assert job["error"] and "pipeline failed" in job["error"]
    # re-prepare after fixing options is still possible → succeeds
    r2 = client.post(
        "/api/jobs", json={"file_id": file_id, "paper": "a4"}
    ).json()
    client.post("/api/device/connect", json={"port": "/dev/null-fake"})
    assert client.post(f"/api/jobs/{r2['id']}/prepare").status_code == 200
