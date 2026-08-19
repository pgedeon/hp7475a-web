"""Phase 3 F6: server-side text→paths conversion via headless Inkscape
(goal 47da763c). Unit level: has_text_elements + convert_text_to_paths,
including the failure paths (missing binary, non-zero exit, timeout).
"""

from __future__ import annotations

import os
import shutil
import stat
import xml.etree.ElementTree as ET

import pytest

from app.services.pipeline.textpath import convert_text_to_paths, has_text_elements

TEXT_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="50mm" '
    b'viewBox="0 0 100 50">'
    b'<rect x="5" y="5" width="20" height="10" fill="none" stroke="#ff0000"/>'
    b'<text x="30" y="30" font-size="10" fill="none" stroke="#0000ff">Hi</text>'
    b"</svg>"
)

NO_TEXT_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="50mm" '
    b'viewBox="0 0 100 50"><rect x="5" y="5" width="20" height="10" '
    b'fill="none" stroke="#ff0000"/></svg>'
)

requires_inkscape = pytest.mark.skipif(
    shutil.which("inkscape") is None, reason="Inkscape not installed"
)


def _local_tags(svg: bytes) -> set[str]:
    root = ET.fromstring(svg)
    return {el.tag.rsplit("}", 1)[-1] for el in root.iter()}


def test_has_text_elements():
    assert has_text_elements(TEXT_SVG) is True
    assert has_text_elements(NO_TEXT_SVG) is False
    assert has_text_elements(b"<svg xmlns='http://www.w3.org/2000/svg'/>") is False
    assert has_text_elements(b"not xml at all") is False  # unparseable → False


@requires_inkscape
def test_convert_removes_text_keeps_geometry_and_strokes():
    out, err = convert_text_to_paths(TEXT_SVG)
    assert err is None
    tags = _local_tags(out)
    assert "text" not in tags and "tspan" not in tags and "textPath" not in tags
    assert "path" in tags
    # stroke colors survive (glyph strokes land in style= on plain SVG)
    assert b"#ff0000" in out
    assert b"0000ff" in out


@requires_inkscape
def test_convert_output_is_parseable_svg_with_viewbox():
    out, err = convert_text_to_paths(TEXT_SVG)
    assert err is None
    root = ET.fromstring(out)
    assert root.tag.rsplit("}", 1)[-1] == "svg"
    assert root.get("viewBox") == "0 0 100 50"


def test_missing_binary_fail_soft(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent-phase3-test")
    out, err = convert_text_to_paths(TEXT_SVG)
    assert out == b""
    assert err is not None and "Inkscape not installed" in err


def _fake_inkscape(tmp_path, body: str) -> str:
    """Write an executable named `inkscape` into tmp dir, return its PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / "inkscape"
    exe.write_text(body)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return str(bindir)


def test_nonzero_exit_fail_soft(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "PATH", _fake_inkscape(tmp_path, "#!/bin/sh\necho boom >&2\nexit 3\n")
    )
    out, err = convert_text_to_paths(TEXT_SVG)
    assert out == b""
    assert "exited 3" in err and "boom" in err


def test_timeout_fail_soft(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", _fake_inkscape(tmp_path, "#!/bin/sh\nsleep 10\n"))
    out, err = convert_text_to_paths(TEXT_SVG, timeout_s=1)
    assert out == b""
    assert "timed out" in err
