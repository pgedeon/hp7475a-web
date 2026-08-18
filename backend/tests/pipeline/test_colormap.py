"""Color→pen mapping tests (goal 3e598c6e)."""

from __future__ import annotations

import pytest

from app.services.pipeline.analyzer import analyze_svg
from app.services.pipeline.colormap import (
    color_pen_map_to_layers,
    effective_stroke,
    group_by_color,
)
from app.services.pipeline.validator import validate_hpgl
from app.services.pipeline.vpy import run_pipeline_color
from app.services.serial.paper import get_paper

import xml.etree.ElementTree as ET

_COLORS_ONLY = "fixtures/svg/colors-only.svg"


@pytest.fixture()
def colors_svg_bytes():
    return open(_COLORS_ONLY, "rb").read()


def test_effective_stroke_resolution_and_none_override(colors_svg_bytes):
    root = ET.fromstring(colors_svg_bytes)
    # find the stroke="none" rect inside the blue group
    none_rect = [el for el in root.iter() if el.tag.endswith("rect") and el.get("stroke") == "none"][0]
    # simulate its ancestor chain manually: root > g(blue) > rect
    blue_g = [g for g in root.iter() if g.tag.endswith("g") and g.get("stroke") == "#0000ff"][0]
    assert effective_stroke([root, blue_g, none_rect]) is None  # explicit none wins
    yellow_poly = [el for el in root.iter() if el.tag.endswith("polyline")][0]
    inner_g = yellow_poly  # its parent chain: g(blue) > g > polyline
    # build real stack via iteration
    def find_stack(target, stack):
        for el in stack[-1]:
            new = stack + [el]
            if el is target:
                return new
            r = find_stack(target, new)
            if r:
                return r
        return None
    stack = find_stack(yellow_poly, [root])
    assert effective_stroke(stack) == "#ffcc00"  # own attr beats blue ancestor


def test_group_by_color_matches_analyzer_order(colors_svg_bytes):
    analysis = analyze_svg(colors_svg_bytes)
    _, ordered = group_by_color(colors_svg_bytes)
    assert ordered == analysis.stroke_colors
    # inheritance: red children + green rect + blue circle + yellow polyline
    assert ordered[0] == "#ff0000"
    assert "#ffcc00" in ordered and "#0000ff" in ordered


def test_group_by_color_labels_layers_1_to_n(colors_svg_bytes):
    grouped_bytes, ordered = group_by_color(colors_svg_bytes)
    root = ET.fromstring(grouped_bytes)
    labels = []
    for g in root:
        if g.tag.endswith("}g"):
            label = g.get("{http://www.inkscape.org/namespaces/inkscape}label")
            labels.append(label)
    assert labels == [str(i) for i in range(1, len(ordered) + 1)]


def test_color_pen_map_translation_and_unknown(colors_svg_bytes):
    _, ordered = group_by_color(colors_svg_bytes)
    m = color_pen_map_to_layers(
        {ordered[0]: 3, ordered[2]: 1}, ordered
    )
    assert m == {"1": 3, "3": 1}
    with pytest.raises(ValueError, match="not present"):
        color_pen_map_to_layers({"#abcdef": 2}, ordered)


def test_run_pipeline_color_produces_valid_sp_mapped_hpgl(colors_svg_bytes):
    import tempfile, pathlib
    with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as f:
        f.write(colors_svg_bytes)
        p = f.name
    try:
        result = run_pipeline_color(
            p, "a4",
            {"linemerge": True, "linesimplify": False, "linesort": True},
            {"#ff0000": 3, "#00ff00": 5, "#0000ff": 2, "#ffcc00": 6},
        )
    finally:
        pathlib.Path(p).unlink(missing_ok=True)
    vr = validate_hpgl(result.hpgl, get_paper("a4"))
    assert vr.errors == []
    # pen order follows first-seen color order (SP3 before SP5 before SP2/SP6)
    i3 = result.hpgl.find("SP3")
    i5 = result.hpgl.find("SP5")
    i2 = result.hpgl.find("SP2")
    i6 = result.hpgl.find("SP6")
    assert -1 < i3 < i5 < i2 < i6
    assert "SP0;" in result.hpgl  # park


def test_run_pipeline_color_rejects_unknown_color(colors_svg_bytes):
    import tempfile, pathlib
    with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as f:
        f.write(colors_svg_bytes)
        p = f.name
    try:
        with pytest.raises(ValueError, match="not present"):
            run_pipeline_color(p, "a4", None, {"#deadbe": 1})
    finally:
        pathlib.Path(p).unlink(missing_ok=True)
