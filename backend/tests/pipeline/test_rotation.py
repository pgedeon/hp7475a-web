"""Rotation + safe-area placement tests (goal 47da763c).

Pins: rotate_90 swaps artwork w/h in page/plotter coords, stays inside the
paper clip, preserves geometry fidelity (path length), honors the safe
rect + margin model, and the analyzer's rotated-fit hint data.
"""

from __future__ import annotations

import re

import pytest

from app.services.pipeline.vpy import PipelineOptions, run_pipeline, safe_page_rect_mm
from app.services.serial import protocol
from app.services.serial.paper import PAPERS, get_paper
from conftest import SVG_FIXTURES

BENIGN = SVG_FIXTURES / "benign.svg"
_U = protocol.PLOTTER_UNIT_MM  # mm per plotter unit

def _geom_extents(hpgl: str) -> tuple[float, float, float, float]:
    """(xmin, ymin, xmax, ymax) in plotter units, final park excluded."""
    h = re.sub(r"PU-?\d+,-?\d+;SP0;$", "", hpgl)
    pts = re.findall(r"(?:PU|PD|PA)(-?\d+),(-?\d+)", h)
    assert pts, "no coordinates found"
    xs, ys = [int(p[0]) for p in pts], [int(p[1]) for p in pts]
    return min(xs), min(ys), max(xs), max(ys)

def _extents_mm(hpgl: str) -> tuple[float, float]:
    x0, y0, x1, y1 = _geom_extents(hpgl)
    return (x1 - x0) * _U, (y1 - y0) * _U

def _total_length_mm(result) -> float:
    return sum(l["length_mm"] for l in result.stats["layers"].values())

def _inside_clip(hpgl: str, paper: str) -> bool:
    p = get_paper(paper)
    x0, y0, x1, y1 = _geom_extents(hpgl)
    return p.x_range[0] <= x0 and p.x_range[1] >= x1 and p.y_range[0] <= y0 and p.y_range[1] >= y1

# ---------------------------------------------------------------- rotation

def test_rotate_90_swaps_artwork_extents():
    """Rotated extents = swapped artwork dims x its own fit scale."""
    plain = run_pipeline(BENIGN, "a4", PipelineOptions(), {})
    rot = run_pipeline(BENIGN, "a4", PipelineOptions(rotate_90=True), {})
    pw, ph = _extents_mm(plain.hpgl)
    rw, rh = _extents_mm(rot.hpgl)
    # artwork dims (mm) recovered per-run via its fit scale
    s_p, s_r = plain.stats["fit_scale"], rot.stats["fit_scale"]
    bw, bh = pw / s_p, ph / s_p
    assert abs(rw / s_r - bh) < 0.5, "rotated width must equal unrotated height"
    assert abs(rh / s_r - bw) < 0.5, "rotated height must equal unrotated width"
    # orientation flips on the page (artwork is wider than tall)
    assert pw > ph and rw < rh
    assert _inside_clip(plain.hpgl, "a4")
    assert _inside_clip(rot.hpgl, "a4")

def test_rotate_90_preserves_geometry_length():
    """Rotation is rigid: base (pre-scale) path length is invariant."""
    plain = run_pipeline(BENIGN, "a4", PipelineOptions(), {})
    rot = run_pipeline(BENIGN, "a4", PipelineOptions(rotate_90=True), {})
    base_p = _total_length_mm(plain) / plain.stats["fit_scale"]
    base_r = _total_length_mm(rot) / rot.stats["fit_scale"]
    assert abs(base_p - base_r) < 0.5  # mm

def test_rotate_90_output_validates():
    result = run_pipeline(BENIGN, "a3", PipelineOptions(rotate_90=True), {"1": 1})
    from app.services.pipeline.validator import validate_hpgl

    assert validate_hpgl(result.hpgl, "a3").errors == []

def test_rotate_90_stats_reported():
    result = run_pipeline(BENIGN, "a4", PipelineOptions(rotate_90=True), {})
    assert result.stats["rotate_90"] is True
    plain = run_pipeline(BENIGN, "a4", PipelineOptions(), {})
    assert plain.stats["rotate_90"] is False

def test_rotation_applies_in_color_pipeline_too():
    from app.services.pipeline.vpy import run_pipeline_color

    res = run_pipeline_color(
        BENIGN, "a4", PipelineOptions(rotate_90=True), {"#ff0000": 1, "#0000ff": 2, "#00aa00": 3}
    )
    assert res.stats["rotate_90"] is True
    assert _inside_clip(res.hpgl, "a4")

# ---------------------------------------------------------------- safe area

def test_safe_page_rect_inside_clip_and_sized():
    for name, want in (("a4", (274.0, 192.0)), ("a3", (399.0, 271.0))):
        p = get_paper(name)
        x0, y0, x1, y1 = safe_page_rect_mm(p)
        assert abs((x1 - x0) - want[0]) < 0.5
        assert abs((y1 - y0) - want[1]) < 0.5
        # fully inside the hard clip with the small published inset
        clip_w, clip_h = p.width_mm, p.height_mm
        assert clip_w >= x1 - x0 and clip_h >= y1 - y0

def test_placement_targets_safe_area_not_full_clip():
    """Fit-to-safe: extents must stay within safe rect minus margin."""
    p = get_paper("a3")
    sx0, sy0, sx1, sy1 = safe_page_rect_mm(p)
    result = run_pipeline(BENIGN, "a3", PipelineOptions(margin_mm=10.0), {})
    w, h = _extents_mm(result.hpgl)
    assert w <= (sx1 - sx0) - 2 * 10.0 + 0.5
    assert h <= (sy1 - sy0) - 2 * 10.0 + 0.5

def test_margin_change_moves_extents_toward_edge():
    small = run_pipeline(BENIGN, "a4", PipelineOptions(margin_mm=10.0), {})
    tight = run_pipeline(BENIGN, "a4", PipelineOptions(margin_mm=5.0), {})
    w_s, h_s = _extents_mm(small.hpgl)
    w_t, h_t = _extents_mm(tight.hpgl)
    # margin 5 frees 2x5 mm per axis; the height-filling artwork grows ~10mm
    assert w_t > w_s + 4.0 and h_t > h_s + 4.0

def test_margin_bounds_enforced():
    with pytest.raises(ValueError):
        PipelineOptions.from_dict({"margin_mm": 40.0})
    with pytest.raises(ValueError):
        PipelineOptions.from_dict({"margin_mm": -1.0})
    assert PipelineOptions.from_dict({"margin_mm": 5}).margin_mm == 5.0

def test_from_dict_rotate_90_coercion():
    assert PipelineOptions.from_dict({"rotate_90": True}).rotate_90 is True
    assert PipelineOptions.from_dict({"rotate_90": "false"}).rotate_90 is False
    assert PipelineOptions.from_dict({}).rotate_90 is False

def test_papers_safe_area_metadata():
    assert PAPERS["a4"].safe_area_mm == (274.0, 192.0)
    assert PAPERS["a3"].safe_area_mm == (399.0, 271.0)
    assert PAPERS["a4"].loads_orientation == "landscape"
    assert PAPERS["a3"].loads_orientation == "portrait"
    # derived ANSI safe areas = 95% of clip
    for name in ("a", "b"):
        p = PAPERS[name]
        assert p.safe_area_mm is not None
        assert abs(p.safe_area_mm[0] - 0.95 * p.width_mm) < 1.0
        assert abs(p.safe_area_mm[1] - 0.95 * p.height_mm) < 1.0
    # inset property: per-axis inset of safe inside clip
    ins = PAPERS["a4"].safe_inset_mm
    assert abs(ins[0] - (PAPERS["a4"].width_mm - 274.0) / 2) < 1e-9
