"""SVG analyzer — structural facts needed for plot preparation.

Implements BUILD_SPEC §15 (supported geometry / unsupported reporting),
§20 (layer/pen separation data) and §22 (page-fit checks) for *sanitized*
SVG bytes (feed it the output of ``sanitizer.sanitize_svg``).

Approximation note (documented per contract): ``bbox_mm`` is computed with
``svgelements`` (the same library vpype's SVG reader uses) with transform
reification enabled; if that parse fails the method falls back to the
svg ``width``/``height``/``viewBox`` declaration, and finally returns None.
So the bbox is exact for ordinary SVGs and a declared-size approximation
for pathological ones. It is an *estimate* used for paper-fit warnings —
the authoritative geometry always comes from the vpype pipeline itself.

Spec references: BUILD_SPEC §15, §16, §17, §20, §22, §36 (fixture tests).
"""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from app.services.serial.paper import PAPERS, Paper, get_paper, plotter_units_to_mm

__all__ = ["SvgAnalysis", "analyze_svg", "FIT_MARGIN_MM"]

#: Margin used by the paper-fit *estimate* (spec §22 uses a conservative
#: default; the pipeline itself takes its own configurable margin).
FIT_MARGIN_MM = 5.0

_PX_TO_MM = 25.4 / 96.0

_INKSCAPE_NS = "{http://www.inkscape.org/namespaces/inkscape}"

_FILLABLE = frozenset(
    {"path", "rect", "circle", "ellipse", "polygon", "polyline", "line"}
)


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


@dataclass
class SvgAnalysis:
    """Structural analysis of one sanitized SVG.

    Attributes:
        bbox_mm: estimated geometry bounding box (x0, y0, x1, y1) in mm in
            SVG user coordinates (y down), or None when unknowable.
        stroke_colors: stroke color values in first-seen document order.
        layers: Inkscape layer labels (``inkscape:groupmode="layer"``), or
            ids of top-level ``<g>`` elements when no Inkscape layers exist.
        unsupported: warnings for content the vector pipeline cannot plot
            (text, raster images, filters, non-none fills, gradients,
            markers, clip-paths, masks, patterns).
        est_paper_fit: per paper name (PAPERS keys) whether the estimated
            bbox fits inside the hard-clip area minus FIT_MARGIN_MM, in
            either orientation.
    """

    bbox_mm: tuple[float, float, float, float] | None
    stroke_colors: list[str] = field(default_factory=list)
    layers: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    est_paper_fit: dict[str, bool] = field(default_factory=dict)


def analyze_svg(clean_bytes: bytes) -> SvgAnalysis:
    """Analyze sanitized SVG bytes.

    Args:
        clean_bytes: output of :func:`sanitizer.sanitize_svg` (assumed
            well-formed; analysis is best-effort and never raises on
            content quirks).

    Returns:
        :class:`SvgAnalysis`.
    """
    root = ET.fromstring(clean_bytes)

    colors = _stroke_colors(root)
    layers = _layer_names(root)
    unsupported = _unsupported(root)
    bbox = _bbox_mm(root, clean_bytes)
    fit = _paper_fit(bbox)

    return SvgAnalysis(
        bbox_mm=bbox,
        stroke_colors=colors,
        layers=layers,
        unsupported=unsupported,
        est_paper_fit=fit,
    )


# --------------------------------------------------------------------------
# stroke colors
# --------------------------------------------------------------------------

def _stroke_colors(root: ET.Element) -> list[str]:
    """Ordered first-seen EFFECTIVE stroke values (excluding 'none').

    Inheritance-aware: a drawable without its own stroke inherits the
    nearest ancestor stroke (explicit ``stroke="none"`` overrides).
    Shares resolution with colormap.effective_stroke so the UI color list
    and pipeline color grouping can never diverge.
    """
    from app.services.pipeline.colormap import effective_stroke

    seen: list[str] = []

    def walk(el: ET.Element, stack: list[ET.Element]) -> None:
        stack.append(el)
        tag = el.tag.split("}")[-1]
        if tag in {
            "path", "line", "rect", "circle", "ellipse", "polyline", "polygon"
        }:
            color = effective_stroke(stack)
            if color is not None and color not in seen:
                seen.append(color)
        for child in el:
            walk(child, stack)
        stack.pop()

    for child in root:
        walk(child, [root])
    return seen


# --------------------------------------------------------------------------
# layers
# --------------------------------------------------------------------------

def _layer_names(root: ET.Element) -> list[str]:
    """Inkscape layers if present, else top-level <g id> elements."""
    inkscape_layers: list[str] = []
    top_groups: list[str] = []
    for el in root:
        if _localname(el.tag) != "g":
            continue
        gid = el.get("id") or ""
        if el.get(f"{_INKSCAPE_NS}groupmode") == "layer":
            inkscape_layers.append(
                el.get(f"{_INKSCAPE_NS}label") or gid or f"layer-{len(inkscape_layers) + 1}"
            )
        elif gid and gid not in top_groups:
            top_groups.append(gid)
    return inkscape_layers if inkscape_layers else top_groups


# --------------------------------------------------------------------------
# unsupported content
# --------------------------------------------------------------------------

def _unsupported(root: ET.Element) -> list[str]:
    """Warnings for non-plottable content (spec §15: never silently dropped)."""
    counts: dict[str, int] = {}

    def bump(key: str) -> None:
        counts[key] = counts.get(key, 0) + 1

    for el in root.iter():
        name = _localname(el.tag)
        if name in ("text", "tspan", "textPath"):
            bump("text elements (convert to paths before plotting)")
        elif name == "image":
            bump("raster <image> elements (cannot be plotted as vectors)")
        elif name in ("filter",):
            bump("filter definitions/effects")
        elif name in ("linearGradient", "radialGradient"):
            bump("gradient definitions")
        elif name == "marker":
            bump("marker definitions")
        elif name == "clipPath":
            bump("clip-path definitions")
        elif name in ("mask",):
            bump("mask definitions")
        elif name in ("pattern",):
            bump("pattern definitions")

        if name in _FILLABLE:
            fill = (el.get("fill") or "").strip().lower()
            if fill and fill != "none":
                bump("non-'none' fills (outline-only pipeline ignores fills)")
        if el.get("filter") is not None:
            bump("elements with filter applied")
        if el.get("clip-path"):
            bump("elements with clip-path applied")
        if el.get("marker-start") or el.get("marker-mid") or el.get("marker-end") or el.get("marker"):
            bump("elements with markers applied")
        for attr in ("fill", "stroke"):
            ref = el.get(attr, "")
            if ref.strip().lower().startswith("url("):
                bump(f"{attr} gradient/paint-server references")

    return [f"{desc}: {n}" for desc, n in counts.items()]


# --------------------------------------------------------------------------
# bbox estimate
# --------------------------------------------------------------------------

def _bbox_mm(root: ET.Element, clean_bytes: bytes) -> tuple[float, float, float, float] | None:
    """Estimated geometry bbox in mm via svgelements; declared-size fallback."""
    bbox_px = _svgelements_bbox(clean_bytes)
    if bbox_px is None:
        bbox_px = _declared_bbox_px(root)
    if bbox_px is None:
        return None
    x0, y0, x1, y1 = bbox_px
    return (
        round(x0 * _PX_TO_MM, 3),
        round(y0 * _PX_TO_MM, 3),
        round(x1 * _PX_TO_MM, 3),
        round(y1 * _PX_TO_MM, 3),
    )


def _svgelements_bbox(clean_bytes: bytes) -> tuple[float, float, float, float] | None:
    """Geometry bbox in scene px via svgelements (best effort, never raises)."""
    try:
        from svgelements import SVG, Shape

        svg = SVG.parse(io.BytesIO(clean_bytes))
        boxes = [
            el.bbox()
            for el in svg.elements()
            if isinstance(el, Shape)
            and getattr(el, "stroke", None) is not None  # plottable strokes only
        ]
        boxes = [b for b in boxes if b is not None]
        if not boxes:
            return None
        return (
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        )
    except Exception:  # noqa: BLE001 — estimate only, fall back below
        return None


def _declared_bbox_px(root: ET.Element) -> tuple[float, float, float, float] | None:
    """Fallback: svg declared viewport in px (width/height or viewBox)."""
    import re

    def px(value: str | None) -> float | None:
        if not value:
            return None
        m = re.fullmatch(r"\s*([0-9.]+)\s*(mm|cm|in|pt|px)?\s*", value)
        if not m:
            return None
        n = float(m.group(1))
        unit = m.group(2) or "px"
        factor = {"mm": 96 / 25.4, "cm": 96 / 2.54, "in": 96.0, "pt": 96 / 72.0, "px": 1.0}
        return n * factor[unit]

    vb = root.get("viewBox")
    if vb:
        parts = vb.replace(",", " ").split()
        if len(parts) == 4:
            x, y, w, h = (float(p) for p in parts)
            # scale user units to px using declared size when available
            dw, dh = px(root.get("width")), px(root.get("height"))
            if dw and w:
                return (x * dw / w, y * dh / h if dh else y, (x + w) * dw / w, (y + h) * (dh / h) if (dh and h) else y + h)
            return (x, y, x + w, y + h)
    dw, dh = px(root.get("width")), px(root.get("height"))
    if dw and dh:
        return (0.0, 0.0, dw, dh)
    return None


# --------------------------------------------------------------------------
# paper fit
# --------------------------------------------------------------------------

def _paper_fit(bbox_mm: tuple[float, float, float, float] | None) -> dict[str, bool]:
    """Fit estimate per PAPERS with FIT_MARGIN_MM (orientation-agnostic)."""
    if bbox_mm is None:
        return {name: False for name in PAPERS}
    x0, y0, x1, y1 = bbox_mm
    w, h = x1 - x0, y1 - y0
    result: dict[str, bool] = {}
    for name, paper in PAPERS.items():
        pw = paper.size_mm[0] - 2 * FIT_MARGIN_MM
        ph = paper.size_mm[1] - 2 * FIT_MARGIN_MM
        result[name] = (w <= pw and h <= ph) or (w <= ph and h <= pw)
    return result


def paper_for(name: str) -> Paper:
    """Resolve paper by name/alias — thin re-export for API callers."""
    return get_paper(name)


def bbox_units(bbox_mm: tuple[float, float, float, float]) -> tuple[float, ...]:
    """Convert an mm bbox to plotter units (helper for extent math)."""
    from app.services.serial.paper import mm_to_plotter_units

    return tuple(mm_to_plotter_units(v) for v in bbox_mm)  # type: ignore[return-value]


__all__ += ["paper_for", "bbox_units", "plotter_units_to_mm"]
