"""Color→pen mapping: regroup SVG drawables by effective stroke color.

Goal 3e598c6e: many SVGs carry no Inkscape layers — their natural plot
grouping is stroke color. This module rewrites a sanitized SVG into
synthetic Inkscape layers ``1..N`` (one per distinct effective stroke color,
first-seen document order — the SAME order analyzer.stroke_colors reports),
so the existing per-layer pipeline can emit one ``SP`` per color group.

Elements without any resolvable stroke stay at top level; vpype assigns
those to layer 1 (documented behavior: they plot with color 1's pen).

Stroke resolution (own attribute, else nearest ancestor, else None) is
shared with analyzer._stroke_colors via effective_stroke() so the UI's
color list and the pipeline's grouping can never diverge.
"""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from pathlib import Path

DRAWABLE_TAGS = {
    "path", "line", "rect", "circle", "ellipse", "polyline", "polygon"
}

_SVG_NS = "http://www.w3.org/2000/svg"
_INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"


def _style_stroke(el: ET.Element) -> str | None:
    """``stroke`` value from a CSS ``style="..."`` attribute, if any.

    Needed for Inkscape plain-SVG output (goal 47da763c phase 3 F6), which
    serializes glyph strokes as ``style="stroke:#0000ff;..."`` rather than
    presentation attributes — without this the converted paths look
    strokeless to color grouping."""
    style = el.get("style")
    if not style:
        return None
    for decl in style.split(";"):
        prop, sep, value = decl.partition(":")
        if sep and prop.strip().lower() == "stroke":
            return value
    return None


def effective_stroke(stack: list[ET.Element]) -> str | None:
    """Effective stroke for the deepest element of *stack*: own attribute
    (or CSS style attribute — Inkscape plain SVG), else nearest ancestor's,
    else None. Normalized like analyzer (strip+lower); ``none`` counts as
    no stroke."""
    for el in reversed(stack):
        stroke = el.get("stroke")
        if stroke is None:
            stroke = _style_stroke(el)
        if stroke is None:
            continue
        value = stroke.strip().lower()
        if value and value != "none":
            return value
        if value == "none":
            return None  # explicit none overrides inheritance
    return None


def group_by_color(svg_bytes: bytes) -> tuple[bytes, list[str]]:
    """Regroup drawables by effective stroke color.

    Returns ``(grouped_svg_bytes, ordered_colors)`` where layer ``i+1``
    holds every drawable whose effective stroke is ``ordered_colors[i]``.
    Order matches analyzer's stroke_colors for the same document (both walk
    in document order with the same resolution rule).
    """
    root = ET.fromstring(svg_bytes)

    buckets: dict[str, list[ET.Element]] = {}
    ordered: list[str] = []

    def walk(el: ET.Element, stack: list[ET.Element]) -> None:
        stack.append(el)
        tag = el.tag.split("}")[-1]
        if tag in DRAWABLE_TAGS:
            color = effective_stroke(stack)
            if color is not None:
                if color not in buckets:
                    buckets[color] = []
                    ordered.append(color)
                buckets[color].append(el)
        for child in el:
            walk(child, stack)
        stack.pop()

    for child in root:
        walk(child, [root])

    grouped = ET.Element(f"{{{_SVG_NS}}}svg")
    for attr in ("width", "height", "viewBox"):
        if root.get(attr) is not None:
            grouped.set(attr, root.get(attr))  # type: ignore[arg-type]

    for idx, color in enumerate(ordered, start=1):
        g = ET.SubElement(
            grouped,
            f"{{{_SVG_NS}}}g",
            {
                "id": f"color-{idx}",
                f"{{{_INKSCAPE_NS}}}groupmode": "layer",
                f"{{{_INKSCAPE_NS}}}label": str(idx),
            },
        )
        for el in buckets[color]:
            g.append(copy.deepcopy(el))

    # unstroked content: keep at top level (plots in layer 1 with color 1)
    for child in root:
        tag = child.tag.split("}")[-1]
        stack = [root, child]
        if tag in DRAWABLE_TAGS and effective_stroke(stack) is None:
            grouped.append(copy.deepcopy(child))

    ET.register_namespace("", _SVG_NS)
    ET.register_namespace("inkscape", _INKSCAPE_NS)
    out = ET.tostring(grouped, encoding="utf-8", xml_declaration=True)
    return out, ordered


def color_pen_map_to_layers(
    color_pen_map: dict[str, int], ordered_colors: list[str]
) -> dict[str, int]:
    """Translate a color-keyed pen map into vpype-layer-keyed (``"1".."N"``).

    Colors absent from the map are omitted (the pipeline's default pen
    formula then applies, mirroring layer-mode behavior for unmapped layers).
    Unknown color keys raise ValueError — silent no-ops are not acceptable.
    """
    layer_map: dict[str, int] = {}
    unknown = [c for c in color_pen_map if c not in ordered_colors]
    if unknown:
        raise ValueError(
            f"pen_map contains colors not present in the SVG: {unknown}"
        )
    for idx, color in enumerate(ordered_colors, start=1):
        if color in color_pen_map:
            layer_map[str(idx)] = int(color_pen_map[color])
    return layer_map
