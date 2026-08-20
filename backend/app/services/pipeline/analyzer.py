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

__all__ = ["SvgAnalysis", "analyze_svg", "FIT_MARGIN_MM", "hints_for"]

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
        unsupported: BLOCKERS — content the vector pipeline cannot plot as-is
            (text, raster images, filters, gradients, markers, clip-paths,
            masks, patterns). Plotting proceeds only after conversion/removal.
        warnings: NON-BLOCKING quirks (goal 47da763c phase 3): currently
            fills — vpype extracts their outlines, so the plot proceeds with
            outline-only rendering of filled shapes.
        hints: per-warning action hint (goal 47da763c phase 3 F7) — keys
            mirror ``unsupported``/``warnings`` strings exactly; value = what
            to do.
        est_paper_fit: per paper name (PAPERS keys) whether the estimated
            bbox fits inside the hard-clip area minus FIT_MARGIN_MM, in
            either orientation.
        fit_rotate90: per paper name whether the bbox fits only after a
            90-degree rotation (swapped w/h) — powers the UI "fits when
            rotated" tip (goal 47da763c). Suggestion only, never applied.
    """

    bbox_mm: tuple[float, float, float, float] | None
    stroke_colors: list[str] = field(default_factory=list)
    layers: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    hints: dict[str, str] = field(default_factory=dict)
    est_paper_fit: dict[str, bool] = field(default_factory=dict)
    fit_rotate90: dict[str, bool] = field(default_factory=dict)


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
    blockers, warnings = _unsupported(root)
    hints = {**hints_for(blockers), **hints_for(warnings)}
    bbox = _bbox_mm(root, clean_bytes)
    fit, fit_rot = _paper_fit(bbox)

    return SvgAnalysis(
        bbox_mm=bbox,
        stroke_colors=colors,
        layers=layers,
        unsupported=blockers,
        warnings=warnings,
        hints=hints,
        est_paper_fit=fit,
        fit_rotate90=fit_rot,
    )

# F7: actionable companion for each unsupported/warning string (key = the
# warning description before the ": N" count suffix).
_ACTION_HINTS: dict[str, str] = {
    "text elements (convert to paths before plotting)":
        "enable 'Convert text to paths' when uploading (server-side Inkscape)",
    "raster <image> elements (cannot be plotted as vectors)":
        "rasters cannot be plotted — embed as separate reference or trace to vectors",
    "filled shapes (outline-only pipeline plots their outlines)":
        "outline-only: fills are ignored; use stroked shapes for filled areas",
    "filter definitions/effects":
        "remove filter effects — they cannot be plotted",
    "elements with filter applied":
        "remove filter effects — they cannot be plotted",
    "gradient definitions":
        "flatten gradients to solid stroke colors",
    "fill gradient/paint-server references":
        "flatten gradients to solid stroke colors",
    "stroke gradient/paint-server references":
        "flatten gradients to solid stroke colors",
    "marker definitions":
        "remove markers (line-end decorations are not plotted)",
    "elements with markers applied":
        "remove markers (line-end decorations are not plotted)",
    "clip-path definitions":
        "remove clip-paths or pre-clip the geometry",
    "elements with clip-path applied":
        "remove clip-paths or pre-clip the geometry",
    "mask definitions":
        "remove masks — masked geometry cannot be plotted",
    "pattern definitions":
        "remove patterns or convert patterned areas to stroked outlines",
}


def hints_for(warnings: list[str]) -> dict[str, str]:
    """Map each unsupported/warning string to its action hint (F7)."""
    out: dict[str, str] = {}
    for warning in warnings:
        desc = warning.rsplit(": ", 1)[0]
        if desc in _ACTION_HINTS:
            out[warning] = _ACTION_HINTS[desc]
    return out


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

def _unsupported(root: ET.Element) -> tuple[list[str], list[str]]:
    """(blockers, warnings) for non-ideal content (spec §15: never silently
    dropped). Blockers stop nothing at upload time but mean the content will
    NOT be plotted as-is; warnings are quirks the pipeline absorbs (fills →
    outlines)."""
    counts: dict[str, int] = {}   # blockers
    warns: dict[str, int] = {}    # non-blocking

    def bump(key: str, warning: bool = False) -> None:
        (warns if warning else counts)[key] = (
            (warns if warning else counts).get(key, 0) + 1
        )

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
                bump(
                    "filled shapes (outline-only pipeline plots their outlines)",
                    warning=True,
                )
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

    fmt = lambda d: [f"{desc}: {n}" for desc, n in d.items()]  # noqa: E731
    return fmt(counts), fmt(warns)


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
    # svgelements returns np.float64 for bezier bbox extrema (plain floats
    # for straight-line shapes); coerce so the dataclass honors its float
    # contract and downstream fit comparisons yield plain Python bools
    # (np.bool_ cannot subclass bool — json.dumps TypeError, 2026-08-20).
    return (
        round(float(x0) * _PX_TO_MM, 3),
        round(float(y0) * _PX_TO_MM, 3),
        round(float(x1) * _PX_TO_MM, 3),
        round(float(y1) * _PX_TO_MM, 3),
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

def _paper_fit(
    bbox_mm: tuple[float, float, float, float] | None,
) -> tuple[dict[str, bool], dict[str, bool]]:
    """Fit estimate per PAPERS: (either-orientation, rotated-only).

    A design exactly the size of the paper fits (the pipeline scales to
    fit inside its safety margin — est_paper_fit answers 'can this go on
    that sheet at all', not 'with margin'). Regression (2026-08-18): a
    210×297 design reported a4:false and the UI auto-picked A3 while the
    plotter was DIP-switched to A4 → clamped coords → vertical lines."""
    if bbox_mm is None:
        return {name: False for name in PAPERS}, {name: False for name in PAPERS}
    x0, y0, x1, y1 = bbox_mm
    w, h = x1 - x0, y1 - y0
    result: dict[str, bool] = {}
    rotated: dict[str, bool] = {}
    for name, paper in PAPERS.items():
        pw, ph = paper.size_mm
        # bool(): numpy comparisons must not leak np.bool_ into API/persisted
        # state — it is not a bool subclass and crashes json.dumps.
        result[name] = bool((w <= pw and h <= ph) or (w <= ph and h <= pw))
        rotated[name] = bool(w <= ph and h <= pw)
    return result, rotated


def paper_for(name: str) -> Paper:
    """Resolve paper by name/alias — thin re-export for API callers."""
    return get_paper(name)


def bbox_units(bbox_mm: tuple[float, float, float, float]) -> tuple[float, ...]:
    """Convert an mm bbox to plotter units (helper for extent math)."""
    from app.services.serial.paper import mm_to_plotter_units

    return tuple(mm_to_plotter_units(v) for v in bbox_mm)  # type: ignore[return-value]


__all__ += ["paper_for", "bbox_units", "plotter_units_to_mm"]
