"""Analyzer fit_rotate90 + preview-annotation tests (goal 47da763c)."""

from __future__ import annotations

from app.services.pipeline.analyzer import analyze_svg
from app.services.pipeline.sanitizer import sanitize_svg
from app.api.routes import _annotate_preview
from app.services.serial.paper import PAPERS
from conftest import SVG_FIXTURES

# portrait-only artwork: 200 wide x 280 tall mm — fits A4 only rotated
_PORTRAIT_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="200mm" height="280mm" '
    b'viewBox="0 0 200 280">'
    b'<g fill="none" stroke="#000000" stroke-width="0.5">'
    b'<rect x="10" y="10" width="180" height="260"/>'
    b"</g></svg>"
)

def _analyze(name: str):
    data = (SVG_FIXTURES / name).read_bytes()
    clean, _ = sanitize_svg(data)
    return analyze_svg(clean)

def test_fit_rotate90_reported_for_all_papers():
    a = _analyze("benign.svg")
    assert set(a.fit_rotate90) == set(a.est_paper_fit)

def test_portrait_artwork_fits_a4_only_when_rotated():
    clean, _ = sanitize_svg(_PORTRAIT_SVG)
    a = analyze_svg(clean)
    # A4 (297x210): normal fit impossible (280 > 210), rotated fits
    assert a.est_paper_fit["a4"] is True  # orientation-agnostic
    assert a.fit_rotate90["a4"] is True
    w = a.bbox_mm[2] - a.bbox_mm[0]
    h = a.bbox_mm[3] - a.bbox_mm[1]
    pw, ph = PAPERS["a4"].size_mm
    assert not (w <= pw and h <= ph)  # unrotated genuinely does not fit

def test_benign_landscape_fits_unrotated():
    a = _analyze("benign.svg")  # ~80x62mm artwork
    assert a.est_paper_fit["a4"] is True
    # landscape-ish artwork: rotated also fits (small), but unrotated is the
    # natural orientation — both flags may be true; the UI prefers unrotated
    assert a.fit_rotate90["a4"] is True

# ---------------------------------------------------------- preview overlay

_MINIMAL_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="1122.5" height="793.7" '
    'viewBox="0 0 1122.5 793.7"><path d="M 100,100 L 1000,700"/></svg>'
)

def test_annotate_preview_draws_sheet_and_safe_rects():
    out = _annotate_preview(_MINIMAL_SVG, "a4", 1.0)
    assert out.count("<rect") == 2
    assert 'stroke="#adb5bd"' in out          # sheet outline (grey solid)
    assert 'stroke-dasharray' in out and 'stroke="#ff5252"' in out  # safe (red dashed)
    # axis indicators
    assert "X pen carriage" in out
    assert "Y paper motion" in out
    # caption content
    assert "A4" in out and "LANDSCAPE" in out
    assert "sheet 297" in out and "safe 274" in out
    assert "100%" in out
    assert "ROTATED" not in out

def test_annotate_preview_rotated_badge_and_orientation_word():
    out = _annotate_preview(_MINIMAL_SVG, "a3", 0.5, rotated=True)
    assert "ROTATED" in out
    assert "A3" in out and "PORTRAIT" in out
    assert "50%" in out
    assert "safe 399" in out
