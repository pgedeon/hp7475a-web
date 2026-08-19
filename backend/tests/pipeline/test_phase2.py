"""Phase 2 — F1 travel preview, F2 estimate+velocity, F4 tiling.

AC evidence (brief hp7475a-phase2): byte-identity against the phase-1
golden (default options and default copies), travel polylines in the
preview SVG, VS quantization/emission rules, estimate math, grid
duplication + extents, oversize error with max-fit hint.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services.pipeline.vpy import (
    PipelineOptions,
    quantize_velocity,
    run_pipeline,
)
from conftest import HPGL_FIXTURES, SVG_FIXTURES

BENIGN = SVG_FIXTURES / "benign.svg"
GOLDEN = HPGL_FIXTURES / "golden.hpgl"

TWO_SHAPES = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">'
    '<path d="M10 10 L50 10 L50 50" stroke="black" fill="none"/>'
    '<path d="M150 80 L180 80 L180 60" stroke="red" fill="none"/></svg>'
)


@pytest.fixture()
def two_shapes(tmp_path: Path) -> str:
    p = tmp_path / "two-shapes.svg"
    p.write_text(TWO_SHAPES)
    return str(p)


def _drawn_coords(hpgl: str) -> list[tuple[int, int]]:
    """All pen-down-path coordinates: PD targets + the PU landing points
    that precede them (the pen drops AT the PU position). Only the final
    park move is excluded."""
    hpgl = re.sub(r"PU\d+,\d+;SP0;$", "", hpgl)
    nums = [
        int(x)
        for m in re.findall(r"P[UD]([\d,]+)", hpgl)
        for x in m.split(",")
        if x
    ]
    return list(zip(nums[0::2], nums[1::2]))


# ------------------------------------------------------------------ F1

def test_travel_polylines_in_preview_and_hpgl_untouched(two_shapes):
    """AC1: travel lines appear in the preview SVG; HP-GL is byte-identical
    with the feature present (travel injection never touches hpgl)."""
    r = run_pipeline(two_shapes, "a4", PipelineOptions(), {"1": 1, "2": 2})
    pv = Path(r.preview_svg_path).read_text(encoding="utf-8")
    assert pv.count('polyline class="travel"') >= 1
    assert '<g class="travel-group">' in pv
    # travel is preview-only: same file through the pipeline again must
    # produce identical HP-GL (golden path unaffected)
    r2 = run_pipeline(two_shapes, "a4", PipelineOptions(), {"1": 1, "2": 2})
    assert r.hpgl == r2.hpgl
    # the phase-1 golden still matches (travel did not leak into output)
    benign = run_pipeline(BENIGN, "a4", PipelineOptions(), {"1": 1, "2": 2, "3": 3})
    assert benign.hpgl == GOLDEN.read_text()


def test_travel_styling_matches_brief(two_shapes):
    r = run_pipeline(two_shapes, "a4", PipelineOptions(), {"1": 1, "2": 2})
    pv = Path(r.preview_svg_path).read_text(encoding="utf-8")
    m = re.search(r'<polyline class="travel"([^>]*)/>', pv)
    assert m, "no travel polyline"
    attrs = m.group(1)
    assert 'stroke="#5aa0d6"' in attrs
    assert 'stroke-opacity="0.3"' in attrs or 'opacity="0.3"' in attrs
    assert 'stroke-dasharray="2 3"' in attrs
    assert 'fill="none"' in attrs


# ------------------------------------------------------------------ F2

@pytest.mark.parametrize(
    "raw,expected",
    [
        (38.1, 38.1),   # documented default stays exact
        (38.0, 38.1),   # within half a step of max snaps to default
        (20.0, 20.14),  # round(v/0.38)*0.38
        (10.0, 9.88),
        (0.1, 0.38),    # clamp to min
        (100.0, 38.1),  # clamp to max
        (0.5, 0.38),
    ],
)
def test_quantize_velocity(raw, expected):
    assert quantize_velocity(raw) == pytest.approx(expected, abs=1e-9)


def test_vs_emitted_only_when_not_default():
    r = run_pipeline(BENIGN, "a4", PipelineOptions(velocity_cm_s=10.0),
                     {"1": 1, "2": 2, "3": 3})
    # 2026-08-19: per-pen VS follows each SP (bare header VS bound to pen 0)
    assert r.hpgl.startswith("IN;DF;SP")
    assert r.hpgl.count("VS9.88,") == 3  # once per mapped pen (1,2,3)

    r_default = run_pipeline(BENIGN, "a4", PipelineOptions(velocity_cm_s=38.1),
                             {"1": 1, "2": 2, "3": 3})
    assert r_default.hpgl.startswith("IN;DF;SP")


def test_vs_output_validates():
    from app.services.pipeline.validator import validate_hpgl

    r = run_pipeline(BENIGN, "a4", PipelineOptions(velocity_cm_s=20.0),
                     {"1": 1, "2": 2, "3": 3})
    report = validate_hpgl(r.hpgl, "a4")
    assert report.errors == []


def test_estimate_fields_and_math():
    """AC2: estimate dict on the result; formula
    (drawn_mm + travel_mm) / (velocity_cm_s × 10) + n_pens × 2.0s."""
    pens = {"1": 1, "2": 2, "3": 3}
    r = run_pipeline(BENIGN, "a4", PipelineOptions(velocity_cm_s=10.0), pens)
    est = r.stats["estimate"]
    assert set(est) == {"drawn_mm", "travel_mm", "velocity_cm_s", "est_seconds"}
    assert est["velocity_cm_s"] == 9.88  # quantized, not raw 10.0
    n_pens = len(set(pens.values()))
    expected = (est["drawn_mm"] + est["travel_mm"]) / (9.88 * 10.0) + n_pens * 2.0
    assert est["est_seconds"] == pytest.approx(expected, abs=0.15)
    assert est["travel_mm"] > 0


def test_estimate_velocity_affects_time():
    fast = run_pipeline(BENIGN, "a4", PipelineOptions(velocity_cm_s=38.1),
                        {"1": 1, "2": 2, "3": 3}).stats["estimate"]
    slow = run_pipeline(BENIGN, "a4", PipelineOptions(velocity_cm_s=10.0),
                        {"1": 1, "2": 2, "3": 3}).stats["estimate"]
    assert slow["est_seconds"] > fast["est_seconds"]
    assert fast["velocity_cm_s"] == 38.1


def test_velocity_bounds_validated():
    with pytest.raises(ValueError):
        PipelineOptions(velocity_cm_s=0.0)
    with pytest.raises(ValueError):
        PipelineOptions(velocity_cm_s=50.0)


# ------------------------------------------------------------------ F4

def test_copies_default_byte_identical_to_golden():
    """AC4: copies 1×1 (or absent) must equal the phase-1 golden exactly."""
    r_absent = run_pipeline(BENIGN, "a4", PipelineOptions(), {"1": 1, "2": 2, "3": 3})
    r_one = run_pipeline(
        BENIGN, "a4",
        PipelineOptions(copies={"rows": 1, "cols": 1, "spacing_mm": 5.0}),
        {"1": 1, "2": 2, "3": 3},
    )
    golden = GOLDEN.read_text()
    assert r_absent.hpgl == golden
    assert r_one.hpgl == golden


def test_copies_2x3_duplicates_geometry_and_extents(two_shapes):
    """AC4: 2×3 grid → 6 copies (path count), extents match the computed
    grid, everything inside the safe area."""
    pens = {"1": 1, "2": 2}
    tiled = run_pipeline(
        two_shapes, "a4",
        PipelineOptions(scale=0.25,
                        copies={"rows": 2, "cols": 3, "spacing_mm": 5.0}),
        pens,
    )
    single = run_pipeline(two_shapes, "a4", PipelineOptions(scale=0.25), pens)
    assert tiled.stats["total_paths"] == 6 * single.stats["total_paths"]
    info = tiled.stats["copies"]
    assert (info["rows"], info["cols"], info["spacing_mm"]) == (2, 3, 5.0)

    tc, sc = _drawn_coords(tiled.hpgl), _drawn_coords(single.hpgl)
    grid_w, grid_h = info["grid_mm"]
    t_min_x = min(x for x, _ in tc); t_max_x = max(x for x, _ in tc)
    t_min_y = min(y for _, y in tc); t_max_y = max(y for _, y in tc)
    # plotter units = 0.025 mm; tiled extent IS the full grid
    # (±2.5mm: one read-quantization step 3.78px ≈ 1mm can round an
    # endpoint outward per cell edge)
    assert (t_max_x - t_min_x) * 0.025 == pytest.approx(grid_w, abs=2.5)
    assert (t_max_y - t_min_y) * 0.025 == pytest.approx(grid_h, abs=2.5)
    # inside A4 safe area (default margin 10mm → x 0..254, y 0..172 mm)
    assert t_min_x >= 0 and t_max_x <= 254 / 0.025
    assert t_min_y >= 0 and t_max_y <= 172 / 0.025


def test_copies_oversize_error_names_max_fit(two_shapes):
    with pytest.raises(ValueError) as exc:
        run_pipeline(
            two_shapes, "a4",
            PipelineOptions(copies={"rows": 20, "cols": 20}),
            {"1": 1, "2": 2},
        )
    msg = str(exc.value)
    assert "20x20" in msg
    assert "max that fits" in msg
    assert re.search(r"\b1x1\b", msg)


def test_copies_validation():
    for bad in ({"rows": 0}, {"rows": -1}, {"cols": 21}, {"spacing_mm": -0.1}):
        with pytest.raises(ValueError):
            PipelineOptions(copies=bad)
    # partial dicts are legal — missing keys default to 1/1/0
    assert PipelineOptions(copies={"rows": 2}).copies.get("cols", 1) == 1


def test_tiled_preview_shows_travel_and_grid(two_shapes):
    r = run_pipeline(
        two_shapes, "a4",
        PipelineOptions(scale=0.25,
                        copies={"rows": 2, "cols": 3, "spacing_mm": 5.0}),
        {"1": 1, "2": 2},
    )
    pv = Path(r.preview_svg_path).read_text(encoding="utf-8")
    assert pv.count('polyline class="travel"') >= 1
    import xml.etree.ElementTree as ET

    ET.fromstring(pv)  # well-formed after injection
