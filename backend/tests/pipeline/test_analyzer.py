"""Analyzer tests — BUILD_SPEC §15/§20/§22 on fixtures."""
from __future__ import annotations

import pytest

from app.services.pipeline.analyzer import analyze_svg
from app.services.pipeline.sanitizer import sanitize_svg
from app.services.serial.paper import PAPERS
from conftest import svg_bytes


def _analyze(name: str):
    clean, report = sanitize_svg(svg_bytes(name))
    assert report.ok, report.reasons
    return analyze_svg(clean)


def test_benign_fields():
    a = _analyze("benign.svg")
    assert a.layers == ["Layer 1", "Layer 2", "Layer 3"]
    assert a.stroke_colors == ["#ff0000", "#0000ff", "#00aa00"]
    assert a.unsupported == []
    assert a.bbox_mm is not None
    x0, y0, x1, y1 = a.bbox_mm
    assert x0 == pytest.approx(10, abs=0.5)
    assert y0 == pytest.approx(10, abs=0.5)
    assert x1 == pytest.approx(90, abs=0.5)
    assert y1 == pytest.approx(72, abs=0.5)


def test_benign_paper_fit():
    a = _analyze("benign.svg")
    assert set(a.est_paper_fit) == set(PAPERS)
    assert a.est_paper_fit["a4"] is True
    assert a.est_paper_fit["a"] is True


def test_a4_fit_fits_a4_not_ansi_a():
    fit = _analyze("a4-fit.svg").est_paper_fit
    assert fit["a4"] is True
    assert fit["a3"] is True
    assert fit["a"] is False


def test_a3_only_fits_exactly_a3():
    # Exact-fit semantics (2026-08-18): fit = "can go on the sheet at all";
    # safety margins are the pipeline's concern (it scale-fits). The 380×272
    # design fits A3 and also imperial B (431.8×279.4) — geometrically true.
    fit = _analyze("a3-only.svg").est_paper_fit
    assert fit == {"a4": False, "a3": True, "a": False, "b": True}


def test_huge_coords_fit_nowhere():
    fit = _analyze("huge-coords.svg").est_paper_fit
    assert not any(fit.values())


def test_unsupported_text_and_image_reported():
    a = _analyze("text-and-image.svg")
    joined = " ".join(a.unsupported).lower()
    assert "text" in joined
    assert "raster" in joined or "image" in joined
    assert "gradient" in joined
    assert "marker" in joined
    assert "clip-path" in joined
    assert "fill" in joined


def test_no_viewbox_bbox_from_declared_size():
    a = _analyze("no-viewbox.svg")
    assert a.bbox_mm is not None
    x0, y0, x1, y1 = a.bbox_mm
    assert x1 > x0 and y1 > y0
    # geometry 10..110 user units == px -> ~2.6..29.1 mm
    assert x0 == pytest.approx(10 * 25.4 / 96, abs=0.5)
    assert x1 == pytest.approx(110 * 25.4 / 96, abs=0.5)


def test_layers_fall_back_to_top_level_groups():
    data = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="50mm" height="50mm">'
        b'<g id="alpha"><line x1="1" y1="1" x2="2" y2="2" stroke="black"/></g>'
        b'<g id="beta"><line x1="3" y1="3" x2="4" y2="4" stroke="red"/></g></svg>'
    )
    clean, _ = sanitize_svg(data)
    assert analyze_svg(clean).layers == ["alpha", "beta"]


def test_exact_a4_design_fits_a4():
    """Regression (2026-08-18 vertical-lines bug): a 210×297 design must
    report a4 fit True — the old margin-deducted estimate said False, the
    UI auto-picked A3, and an A4-DIP plotter clamped the coords into
    garbage vertical lines."""
    import io

    from app.services.pipeline.analyzer import analyze_svg

    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="297mm" '
        b'viewBox="0 0 210 297"><g stroke="black" fill="none">'
        b'<rect x="10" y="10" width="190" height="277"/></g></svg>'
    )
    fit = analyze_svg(svg).est_paper_fit
    assert fit["a4"] is True
    assert fit["a3"] is True
