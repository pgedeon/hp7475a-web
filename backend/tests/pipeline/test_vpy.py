"""Pipeline tests — BUILD_SPEC §18/§36: vpype roundtrip, SP ordering, golden."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import vpype as vp

from app.services.pipeline.hpgl_writer import build_hpgl
from app.services.pipeline.validator import validate_hpgl
from app.services.pipeline.vpy import PipelineOptions, run_pipeline
from conftest import HPGL_FIXTURES, SVG_FIXTURES

BENIGN = SVG_FIXTURES / "benign.svg"


def test_roundtrip_default_options_valid():
    result = run_pipeline(BENIGN, "a4", PipelineOptions(), {"1": 1, "2": 2, "3": 3})
    report = validate_hpgl(result.hpgl, "a4")
    assert report.errors == []


def test_roundtrip_with_all_optimizations_valid():
    opts = PipelineOptions(
        linemerge=True, linesimplify=True, linesort=True, reloop=True, margin_mm=8.0
    )
    result = run_pipeline(BENIGN, "a4", opts, {"1": 1, "2": 2, "3": 3})
    assert validate_hpgl(result.hpgl, "a4").errors == []


def test_sp_order_follows_layer_order_and_pen_map():
    result = run_pipeline(BENIGN, "a4", PipelineOptions(), {"1": 3, "2": 1, "3": 6})
    h = result.hpgl
    i3, i1, i6 = h.index("SP3;"), h.index("SP1;"), h.index("SP6;")
    assert -1 < i3 < i1 < i6
    assert h.endswith("SP0;")


def test_ps_instruction_never_in_output_and_reported():
    result = run_pipeline(BENIGN, "a4", PipelineOptions(), {"1": 1, "2": 2, "3": 3})
    assert "PS" not in result.hpgl
    assert result.stats["ps_stripped"] >= 1


def test_preview_svg_written_and_parseable():
    result = run_pipeline(BENIGN, "a4", PipelineOptions(), {"1": 1, "2": 2, "3": 3})
    p = Path(result.preview_svg_path)
    assert p.is_file()
    import xml.etree.ElementTree as ET

    ET.parse(p)  # must be well-formed
    assert p.stat().st_size > 0


def test_deterministic_output():
    a = run_pipeline(BENIGN, "a4", PipelineOptions(), {"1": 1, "2": 2, "3": 3})
    b = run_pipeline(BENIGN, "a4", PipelineOptions(), {"1": 1, "2": 2, "3": 3})
    assert a.hpgl == b.hpgl


def test_stats_content():
    result = run_pipeline(BENIGN, "a4", PipelineOptions(), {"1": 2, "2": 2, "3": 5})
    s = result.stats
    assert s["paper"] == "a4"
    assert s["total_paths"] >= 3
    assert set(s["layers"]) == {"1", "2", "3"}
    assert s["layers"]["3"]["pen"] == 5
    assert s["hpgl_bytes"] == len(result.hpgl.encode())


def test_velocity_option_emits_vs():
    result = run_pipeline(
        BENIGN, "a4", PipelineOptions(velocity_cm_s=10.0), {"1": 1, "2": 2, "3": 3}
    )
    # phase 2: VS is quantized to the 0.38 cm/s grid (brief F2)
    assert "VS9.88;" in result.hpgl
    assert validate_hpgl(result.hpgl, "a4").errors == []


def test_bad_pen_map_rejected():
    with pytest.raises(ValueError):
        run_pipeline(BENIGN, "a4", PipelineOptions(), {"1": 9})


def test_unknown_paper_rejected():
    with pytest.raises(KeyError):
        run_pipeline(BENIGN, "a5", PipelineOptions(), {})


def test_golden_matches_pipeline_output():
    golden = (HPGL_FIXTURES / "golden.hpgl").read_text()
    result = run_pipeline(BENIGN, "a4", PipelineOptions(), {"1": 1, "2": 2, "3": 3})
    assert result.hpgl == golden


def test_geometry_stays_inside_hard_clip_with_margin():
    # margin 10mm == ~402u: all coordinates must sit >= ~390u inside clip edges
    result = run_pipeline(BENIGN, "a4", PipelineOptions(margin_mm=10.0), {})
    report = validate_hpgl(result.hpgl, "a4")
    assert report.errors == []
    assert report.warnings == []  # not even soft-clip warnings expected


def test_build_hpgl_fallback_valid():
    lines1 = vp.LineCollection([np.array([100 + 0j, 2000 + 500j, 3000 + 100j])])
    lines2 = vp.LineCollection([np.array([500 + 500j, 1500 + 2000j])])
    text = build_hpgl({1: lines1, 2: lines2}, "a4", {"1": 2, "2": 4})
    assert text.startswith("IN;DF;")
    assert "SP2;" in text and "SP4;" in text and text.index("SP2;") < text.index("SP4;")
    assert text.endswith("PU11040,7721;SP0;")
    assert validate_hpgl(text, "a4").errors == []


def test_build_hpgl_velocity():
    lines = vp.LineCollection([np.array([0 + 0j, 100 + 100j])])

    class Opts:
        velocity_cm_s = 5.0

    text = build_hpgl({1: lines}, "a4", {"1": 1}, Opts())
    assert "VS5;" in text
