"""vpype-based SVG → HP-GL pipeline for the HP 7475A.

Implements BUILD_SPEC §18 (SVG-to-HP-GL pipeline) and §19 (plot
optimization) using vpype 1.15 as a *library* (``vpype_cli.execute`` /
``vp.read_multilayer_svg`` / ``vp.write_hpgl`` / ``vp.write_svg``) — no
subprocess, no shell (spec §14 "prefer a direct Python library
integration").

Stages: read (Inkscape layers → vpype layers) → optional linemerge /
linesimplify / linesort / reloop → fit+center onto the paper's *hard-clip*
area with a configurable margin → per-layer HP-GL via the vpype
``hp7475a`` device (absolute coordinates) → re-assembly with explicit
``SP<n>;`` per ``pen_map`` → final park ``PU<corner>;SP0;``. The result is
self-validated with :mod:`app.services.pipeline.validator`; any validation
error raises — the pipeline never emits HP-GL its own gate would reject.

⚠ ESCALATION-NOTE (vpype hp7475a emits ``PS``, which the frozen
``protocol.SAFE_HPGL_COMMANDS`` allowlist deliberately does *not* include):
vpype's ``write_hpgl`` prefixes every job with ``PS4;`` (a4/a) or ``PS0;``
(a3/b) — a paper-size command from later plotters that the HP 7475A manual
does not document (it would raise "instruction not recognized", HP-GL
error 1). The validator allowlist stays untouched: this module strips the
``PS`` instruction during post-assembly and records
``stats["ps_stripped"]`` so the neutralization is visible, never silent.
Reported to the goal tracker 2026-08-18.

Placement model: vpype's ``layout`` command computes margins against the
*paper* size, but the 7475A hard-clip area is inset from the paper by
10–14 mm (hardware-notes §2), so ``layout`` margins alone let geometry
land in the un-plottable band where vpype would silently crop it (spec
§45: "do not silently crop geometry"). This module therefore computes the
plottable rectangle in page coordinates from the vpype device config
(origin_location + x/y_range) and places the scaled geometry inside it
manually — ``vp.write_hpgl``'s crop then never has anything to cut.

Spec references: BUILD_SPEC §18, §19, §20, §22, §23 (preview from
post-processed geometry), §39; hardware-notes §2, §9.
"""

from __future__ import annotations

import io
import math
import re
import shlex
import tempfile
import xml.etree.ElementTree as ET
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

import vpype as vp
import vpype_cli

from app.services.pipeline.hpgl_writer import park_position
from app.services.pipeline.validator import validate_hpgl
from app.services.serial import protocol
from app.services.serial.paper import Paper, get_paper

__all__ = ["PipelineOptions", "PipelineResult", "run_pipeline"]

_PX_PER_MM = 96.0 / 25.4
_UNIT_MM = protocol.PLOTTER_UNIT_MM  # mm per plotter unit

# Empirical pen-change overhead per distinct pen in a job (carousel swap
# + settle), used by the plot-time estimate. Documented in
# docs/hardware-notes.md S12. Tune when real timings say so.
PEN_CHANGE_OVERHEAD_S = 2.0

# Sane cap for the copies grid rows/cols.
_MAX_GRID_DIM = 20

# vpype write_hpgl per-layer chunk framing that we strip during re-assembly:
_CHUNK_PREFIX_RE = re.compile(r"^IN;DF;(?:VS[0-9.]+;)?(?:PS\d+;)?SP\d+;")
_CHUNK_SUFFIX_RE = re.compile(r"(?:PA;)?PU(?:\d+,\d+)?;SP0;IN;\s*$")


@dataclass
class PipelineOptions:
    """User-selectable optimization/layout knobs (spec §19, §22).

    Attributes:
        linemerge: merge collinear/joinable paths.
        linesimplify: reduce point count (Douglas-Peucker style).
        linesort: order paths to minimize pen-up travel.
        reloop: rotate closed paths to start near the previous path's end.
        margin_mm: safety margin from the *hard-clip* edge (not paper edge).
        quantization_mm: max segment length when flattening curves.
        velocity_cm_s: optional VS velocity (0.38..38.1, protocol §3-3).
        landscape: paper orientation (True = long axis on X, matching the
            7475A carriage; paper.py x_range spans the long dimension).
        rotate_90: rotate the artwork 90 degrees around its own bbox
            center in page space BEFORE fit+center (goal 47da763c).
            Swaps artwork w/h on the page; never uses HP-GL RO.
    """

    linemerge: bool = False
    linesimplify: bool = False
    linesort: bool = False
    reloop: bool = False
    margin_mm: float = 10.0
    #: user resize control: final geometry = fit-to-paper × scale
    #: (1.0 = largest safe fit inside margin; 0.5 = half that). Bounds-
    #: checked 0.25–1.0 at from_dict.
    scale: float = 1.0
    quantization_mm: float = 0.1
    velocity_cm_s: float | None = None
    landscape: bool = True
    rotate_90: bool = False
    # multi-copy tiling (phase 2): {"rows": int>=1, "cols": int>=1,
    # "spacing_mm": float>=0}. Default/1x1 keeps the single-copy path,
    # byte-identical to phase-1 output. Grid is fit-checked against the
    # safe area -- never silently shrunk (lower ``scale`` explicitly).
    copies: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Ctor-level validation (from_dict re-checks after coercion)."""
        if self.velocity_cm_s is not None and not (
            protocol.VELOCITY_MIN_CM_S
            <= float(self.velocity_cm_s)
            <= protocol.VELOCITY_MAX_CM_S
        ):
            raise ValueError(
                f"velocity_cm_s must be "
                f"{protocol.VELOCITY_MIN_CM_S}..{protocol.VELOCITY_MAX_CM_S}"
            )
        c = self.copies or {}
        rows, cols = int(c.get("rows", 1)), int(c.get("cols", 1))
        sp = float(c.get("spacing_mm", 0.0))
        if not (1 <= rows <= _MAX_GRID_DIM and 1 <= cols <= _MAX_GRID_DIM):
            raise ValueError(f"copies rows/cols must be 1..{_MAX_GRID_DIM}")
        if not 0.0 <= sp <= 100.0:
            raise ValueError("copies spacing_mm must be 0..100")

    @classmethod
    def from_dict(cls, raw: dict | "PipelineOptions | None") -> "PipelineOptions":
        """Coerce an API/UI options dict (or None) into PipelineOptions.

        Unknown keys are ignored; booleans/floats coerced defensively — the
        HTTP layer stores whatever JSON the client sent.
        """
        if raw is None or isinstance(raw, cls):
            return raw or cls()
        fields = {f for f in cls.__dataclass_fields__}
        clean: dict = {}
        for k, v in raw.items():
            if k not in fields or v is None:
                continue
            if k in ("margin_mm", "quantization_mm"):
                f = float(v)
                if k == "margin_mm" and not 0.0 <= f <= 30.0:
                    raise ValueError("margin_mm must be between 0 and 30")
                clean[k] = f
            elif k == "scale":
                s = float(v)
                if not 0.25 <= s <= 1.0:
                    raise ValueError("scale must be between 0.25 and 1.0")
                clean[k] = s
            elif k == "velocity_cm_s":
                clean[k] = float(v) if v is not None else None
            elif k == "copies":
                c = v if isinstance(v, dict) else {}
                try:
                    rows = int(c.get("rows", 1))
                    cols = int(c.get("cols", 1))
                    sp = float(c.get("spacing_mm", 0.0))
                except (TypeError, ValueError):
                    raise ValueError("copies rows/cols/spacing_mm must be numeric")
                if not (1 <= rows <= _MAX_GRID_DIM) or not (1 <= cols <= _MAX_GRID_DIM):
                    raise ValueError(f"copies rows/cols must be 1..{_MAX_GRID_DIM}")
                if not 0.0 <= sp <= 100.0:
                    raise ValueError("copies spacing_mm must be 0..100")
                clean[k] = {"rows": rows, "cols": cols, "spacing_mm": sp}
            else:
                if isinstance(v, str):  # tolerate "false"/"0"/"off" truthy trap
                    v = v.strip().lower() not in ("false", "0", "no", "off", "")
                clean[k] = bool(v)
        return cls(**clean)


@dataclass
class PipelineResult:
    """Output of run_pipeline().

    Attributes:
        hpgl: complete, self-validated HP-GL job text.
        preview_svg_path: path to an SVG rendering of the *post-processed*
            geometry (what the plotter will actually draw; spec §23). The
            caller owns this temp file.
        stats: pipeline statistics dict (paths per layer/pen, travel,
            bounds, byte size, neutralizations applied).
    """

    hpgl: str
    preview_svg_path: str
    stats: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# device placement math (single source: vpype hp7475a config)
# ---------------------------------------------------------------------------

def _paper_config(paper: Paper):
    config = vp.config_manager.get_plotter_config("hp7475a")
    if config is None:
        raise RuntimeError("vpype hp7475a device config not found")
    for pc in config.paper_configs:
        if pc.name == paper.name:
            if pc.paper_orientation is not None or pc.rotate_180 or not pc.y_axis_up:
                raise RuntimeError(
                    f"unexpected hp7475a config flags for paper {pc.name!r} — "
                    "placement math assumes y_axis_up, no rotate_180, no orientation"
                )
            return pc
    raise RuntimeError(f"paper {paper.name!r} not in vpype hp7475a config")


def _plottable_rect_mm(pc) -> tuple[float, float, float, float]:
    """Hard-clip rectangle in *page* mm coordinates (x0, y0, x1, y1), y down.

    vpype maps page (px, y-down) → plotter units via
    plotter_x = (x - origin_x)*units_per_mm, plotter_y = (origin_y - y)*upmm.
    The clip ranges therefore translate to the page rectangle:
    x ∈ [origin_x + xmin*u, origin_x + xmax*u],
    y ∈ [origin_y - ymax*u, origin_y - ymin*u].
    """
    units = lambda u: u * _UNIT_MM
    ox, oy = pc.origin_location  # px, y-down, from page top-left
    ox_mm, oy_mm = ox / _PX_PER_MM, oy / _PX_PER_MM
    xmin, xmax = pc.x_range
    ymin, ymax = pc.y_range
    return (
        ox_mm + units(xmin),
        oy_mm - units(ymax),
        ox_mm + units(xmax),
        oy_mm - units(ymin),
    )


def safe_page_rect_mm(paper: Paper) -> tuple[float, float, float, float]:
    """Usable plotting rect in *page* mm (x0, y0, x1, y1; y down).

    The safe area (paper.safe_area_mm) centered inside the hard-clip rect
    from the vpype device config. Falls back to the clip rect itself when
    no safe area is defined. Used both by placement (margin applies inside
    it) and by the preview annotation (red dashed rect).
    """
    clip = _plottable_rect_mm(_paper_config(paper))
    if paper.safe_area_mm is None:
        return clip
    sw = min(paper.safe_area_mm[0], paper.width_mm)
    sh = min(paper.safe_area_mm[1], paper.height_mm)
    cx0, cy0, cx1, cy1 = clip
    w, h = cx1 - cx0, cy1 - cy0
    return (cx0 + (w - sw) / 2, cy0 + (h - sh) / 2, cx0 + (w + sw) / 2, cy0 + (h + sh) / 2)

def _rotate_artwork(doc: vp.Document, angle_rad: float) -> None:
    """Rotate all layers around the artwork bbox center (page space).

    vpype rotates about the origin only, so compose translate-rotate-
    translate per layer. Path lengths are preserved exactly; bbox w/h
    swap for 90-degree angles.
    """
    b = doc.bounds()
    if b is None:
        return
    cx, cy = (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0
    for layer in doc.layers.values():
        layer.translate(-cx, -cy)
        layer.rotate(angle_rad)
        layer.translate(cx, cy)

def quantize_velocity(v: float) -> float:
    """Snap a pen velocity onto the 0.38 cm/s VS grid (Prog. Manual S3-3),
    clamped to the 0.38..38.1 range. 38.1 (the documented default/max) is
    not itself on the 0.38 grid — inputs within half a step of it snap to
    exactly 38.1 so "default" never emits a redundant VS."""
    step = protocol.VELOCITY_STEP_CM_S
    vmax = protocol.VELOCITY_MAX_CM_S
    v = min(max(float(v), protocol.VELOCITY_MIN_CM_S), vmax)
    if abs(v - vmax) <= step / 2:
        return vmax
    return round(v / step) * step

def _greedy_sort(lc: vp.LineCollection) -> None:
    """In-place greedy pen-up travel sort (the same algorithm as vpype's
    ``linesort`` default: nearest-next + line flip, no two-opt). Phase 2:
    re-sort AFTER tiling so travel is optimized over the whole page."""
    if len(lc) < 2:
        return
    index = vp.LineIndex(lc[1:], reverse=True)
    new_lines = vp.LineCollection([lc[0]], metadata=lc.metadata)
    while len(index) > 0:
        idx, reverse = index.find_nearest(new_lines[-1][-1])
        line = index.pop(idx)
        if line is not None:
            if reverse:
                line = np.flip(line)
            new_lines.append(line)
    if lc.pen_up_length()[0] >= new_lines.pen_up_length()[0]:
        del lc[:]
        lc.extend(new_lines)

def _tile_layers(
    doc: vp.Document, rows: int, cols: int, step_x_mm: float, step_y_mm: float,
    sort: bool,
) -> vp.Document:
    """Replicate every layer onto a rows x cols grid (cell offsets
    step_x/step_y already include the spacing). Cell (0,0) reuses the
    placed original; every other cell is a translated deep copy."""
    tiled = vp.Document()
    for lid, lc in doc.layers.items():
        out = vp.LineCollection(metadata=lc.metadata)
        out.extend(lc)  # cell (0,0)
        for r in range(rows):
            for c in range(cols):
                if r == 0 and c == 0:
                    continue
                cp = vp.LineCollection(
                    (line.copy() for line in lc), metadata=lc.metadata
                )
                cp.translate(c * step_x_mm * _PX_PER_MM, r * step_y_mm * _PX_PER_MM)
                out.extend(cp)
        if sort:
            _greedy_sort(out)
        tiled.add(out, lid, with_metadata=True)
    return tiled

def _place_on_paper(
    doc: vp.Document, paper: Paper, options: PipelineOptions
) -> tuple[float, dict | None, vp.Document]:
    """Scale-to-fit + center geometry inside the safe rect (in-place).

    Returns (applied absolute fit scale, grid info dict or None, document).
    The returned document is the same object unless tiling replicated it.
    With copies > 1x1 the SINGLE copy keeps its normal placed size and is
    then replicated onto a centered grid; a grid that would exceed the
    safe area raises instead of shrinking (lower ``options.scale``)."""
    # Safe-area model (goal 47da763c): placement targets the paper's
    # usable rect (safe_area_mm) centered in the hard clip; margin_mm is
    # the ADDITIONAL user margin inside that rect:
    # effective_avail = safe_rect inset by margin_mm.
    cx0, cy0, cx1, cy1 = safe_page_rect_mm(paper)
    avail_w = max(0.0, (cx1 - cx0) - 2 * options.margin_mm)
    avail_h = max(0.0, (cy1 - cy0) - 2 * options.margin_mm)

    bounds = doc.bounds()
    if bounds is None:
        raise ValueError("SVG contains no plottable geometry")
    (bx0, by0, bx1, by1) = (b / _PX_PER_MM for b in bounds)  # px → mm
    bw, bh = bx1 - bx0, by1 - by0
    if bw <= 0 or bh <= 0:
        raise ValueError("degenerate geometry bounds")

    fit = min(avail_w / bw, avail_h / bh)
    scale = fit * options.scale
    scale_px = scale  # mm/mm is dimensionless; apply directly to px coords
    doc.scale(scale_px)

    new_w, new_h = bw * scale, bh * scale
    grid_info = None
    rows, cols, sp = (
        int((options.copies or {}).get("rows", 1)),
        int((options.copies or {}).get("cols", 1)),
        float((options.copies or {}).get("spacing_mm", 0.0)),
    )
    if rows * cols > 1:
        step_x, step_y = new_w + sp, new_h + sp
        grid_w = cols * new_w + (cols - 1) * sp
        grid_h = rows * new_h + (rows - 1) * sp
        if grid_w > avail_w + 1e-6 or grid_h > avail_h + 1e-6:
            max_cols = max(1, int((avail_w + sp) // step_x))
            max_rows = max(1, int((avail_h + sp) // step_y))
            raise ValueError(
                f"copies {rows}x{cols} needs {grid_w:.0f}x{grid_h:.0f} mm but the "
                f"plottable area is only {avail_w:.0f}x{avail_h:.0f} mm (paper "
                f"{paper.name}, margin {options.margin_mm:.0f} mm); max that fits "
                f"at this artwork size: {max_rows}x{max_cols} - reduce copies, "
                f"spacing, or lower the scale option"
            )
        gx0 = cx0 + options.margin_mm + (avail_w - grid_w) / 2
        gy0 = cy0 + options.margin_mm + (avail_h - grid_h) / 2
        doc.translate(
            (gx0 - bx0 * scale) * _PX_PER_MM, (gy0 - by0 * scale) * _PX_PER_MM
        )
        tiled = _tile_layers(doc, rows, cols, step_x, step_y, sort=options.linesort)
        page = doc.page_size
        doc = tiled
        doc.page_size = page
        grid_info = {
            "rows": rows, "cols": cols, "spacing_mm": sp,
            "grid_mm": [float(round(grid_w, 1)), float(round(grid_h, 1))],
        }
    else:
        # target top-left so geometry is centered in the margin-inset rect
        tx_mm = cx0 + options.margin_mm + (avail_w - new_w) / 2 - bx0 * scale
        ty_mm = cy0 + options.margin_mm + (avail_h - new_h) / 2 - by0 * scale
        doc.translate(tx_mm * _PX_PER_MM, ty_mm * _PX_PER_MM)

    # page size for preview + vpype write inference: full paper, landscape
    page_mm = (paper.size_mm[0], paper.size_mm[1]) if options.landscape else (
        paper.size_mm[1], paper.size_mm[0]
    )
    out_doc = doc if grid_info is None else tiled
    out_doc.page_size = (page_mm[0] * _PX_PER_MM, page_mm[1] * _PX_PER_MM)
    return scale, grid_info, out_doc

def _travel_segments(doc: vp.Document, layer_order: list[int]) -> list[tuple]:
    """Pen-up travel moves (page px, y-down) in plot order: within each
    pen layer, last point of path k → first point of path k+1; across
    layers, last point of pen k → first point of pen k+1. Read-only —
    never touches geometry, so HP-GL output cannot change (F1)."""
    segs: list[tuple] = []
    prev_end = None
    for lid in layer_order:
        for line in doc.layers[lid]:
            pts = np.asarray(line).ravel()  # vpype 1.15: complex points
            if pts.size < 2:
                continue
            start, end = complex(pts[0]), complex(pts[-1])
            if prev_end is not None and prev_end != start:
                segs.append((prev_end, start))
            prev_end = end
    return segs

def _inject_travel_group(svg_path: Path, segs: list[tuple]) -> None:
    """Insert a <g class="travel-group"> of pen-up polylines into the
    preview SVG written by vp.write_svg (same page-px coordinates)."""
    if not segs:
        return
    polys = "".join(
        f'<polyline class="travel" fill="none" stroke="#5aa0d6" '
        f'stroke-opacity="0.3" stroke-width="0.5" stroke-dasharray="2 3" '
        f'points="{a.real:.2f},{a.imag:.2f} {b.real:.2f},{b.imag:.2f}"/>'
        for a, b in segs
    )
    text = svg_path.read_text(encoding="utf-8")
    marker = "</svg>"
    idx = text.rfind(marker)
    if idx == -1:
        return
    svg_path.write_text(
        text[:idx] + f'<g class="travel-group">{polys}</g>' + text[idx:],
        encoding="utf-8",
    )

def _estimate_seconds(
    drawn_mm: float, travel_mm: float, velocity_cm_s: float, n_pens: int,
) -> float:
    """t = (drawn + travel) / v + n_pens × PEN_CHANGE_OVERHEAD_S (F2)."""
    mm_per_s = max(velocity_cm_s, 0.01) * 10.0
    return (drawn_mm + travel_mm) / mm_per_s + n_pens * PEN_CHANGE_OVERHEAD_S


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------

def run_pipeline_color(
    svg_path: str | Path,
    paper: str,
    options: PipelineOptions | dict | None,
    color_pen_map: dict[str, int],
) -> PipelineResult:
    """Color-mapped variant of run_pipeline (goal 3e598c6e).

    Regroups the SVG by effective stroke color into synthetic layers
    1..N (colormap.group_by_color), translates *color_pen_map* (keys =
    color strings exactly as analysis.stroke_colors reports them) into a
    layer-keyed map, then delegates to :func:`run_pipeline`.

    Raises ValueError for pen-map colors not present in the file.
    """
    import tempfile

    from app.services.pipeline.colormap import color_pen_map_to_layers, group_by_color

    raw = Path(svg_path).read_bytes()
    grouped_bytes, ordered = group_by_color(raw)
    if not ordered:
        raise ValueError("SVG has no stroked drawable elements to map by color")
    layer_map = color_pen_map_to_layers(color_pen_map, ordered)
    with tempfile.NamedTemporaryFile(
        suffix=".svg", prefix="colormap-", delete=False
    ) as tmp:
        tmp.write(grouped_bytes)
        tmp_path = tmp.name
    try:
        return run_pipeline(tmp_path, paper, options, layer_map)
    finally:
        Path(tmp_path).unlink(missing_ok=True)


_INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"

def _vpype_layer_id(label: str | None, gid: str | None, idx: int) -> int:
    """Mirror of vpype ``read_multilayer_svg``'s layer-id rule: first
    contiguous digit group in ``inkscape:label`` (then ``id``) wins; 0 → 1;
    no digits at all → appearance order (idx + 1)."""
    m = re.search(r"\d+", label or "") or re.search(r"\d+", gid or "")
    if not m:
        return idx + 1
    lid = int(m.group())
    return 1 if lid == 0 else lid

def translate_named_pen_map(
    svg_bytes: bytes, pen_map: dict[str, int]
) -> dict[str, int]:
    """Translate named layer keys to vpype's numeric layer ids.

    The UI builds pen maps keyed by ``analysis.layers`` labels (Inkscape
    layer labels when any group is an Inkscape layer, else top-level
    ``<g id>`` — analyzer._layer_names rule). vpype assigns numeric ids per
    its own rule; this mirrors both so named assignments are honored instead
    of silently falling back to the default pen (goal 47da763c phase 3).
    Numeric keys pass through; names absent from the file stay as-is
    (default-pen fallback, same as today's unmapped layers).
    """
    if not any(not str(k).isdigit() for k in pen_map):
        return dict(pen_map)

    root = ET.fromstring(svg_bytes)
    groups = [g for g in root if g.tag.rsplit("}", 1)[-1] == "g"]
    has_ink_layers = any(
        (g.get(f"{{{_INKSCAPE_NS}}}groupmode") or "") == "layer" for g in groups
    )

    name_to_id: dict[str, int] = {}
    ink_idx = 0
    for i, g in enumerate(groups):
        label = g.get(f"{{{_INKSCAPE_NS}}}label") or ""
        gid = g.get("id") or ""
        is_ink_layer = (g.get(f"{{{_INKSCAPE_NS}}}groupmode") or "") == "layer"
        if has_ink_layers:
            # analyzer._layer_names rule: only Inkscape layers count
            if not is_ink_layer:
                continue
            ink_idx += 1
            name = label or gid or f"layer-{ink_idx}"
        else:
            if not gid:
                continue
            name = gid
        name_to_id.setdefault(name, _vpype_layer_id(label, gid, i))

    out: dict[str, int] = {}
    for key, pen in pen_map.items():
        k = str(key)
        if k.isdigit() or k not in name_to_id:
            out[k] = int(pen)
        else:
            out.setdefault(str(name_to_id[k]), int(pen))
    return out

def run_pipeline(
    svg_path: str | Path,
    paper: str,
    options: PipelineOptions | None = None,
    pen_map: dict[str, int] | None = None,
) -> PipelineResult:
    """Convert a (pre-sanitized) SVG file into a validated HP-GL job.

    Args:
        svg_path: path to the sanitized SVG on disk.
        paper: paper name/alias from ``paper.PAPERS`` (a4/a3/a/b…).
        options: optimization/layout options; defaults when None.
        pen_map: mapping of vpype layer id (as string, e.g. ``"1"``) to
            physical pen 1..6. Missing layers fall back to vpype's default
            ``1 + (id-1) % 6``.

    Returns:
        :class:`PipelineResult` with self-validated HP-GL and a preview SVG.

    Raises:
        ValueError: unknown paper, no plottable geometry, bad pen numbers.
        RuntimeError: vpype emitted something the validator rejects.
    """
    options = PipelineOptions.from_dict(options) if not isinstance(
        options, PipelineOptions) else options
    pen_map = pen_map or {}
    # Named layer keys (UI maps pens by analysis.layers labels) → vpype's
    # numeric ids so assignments are honored (goal 47da763c phase 3).
    if any(not str(k).isdigit() for k in pen_map):
        try:
            pen_map = translate_named_pen_map(Path(svg_path).read_bytes(), pen_map)
        except Exception:  # stored SVG is sanitized; parse failure → old behavior
            pass  # ponytail: fail-open; raise if it ever bites
    p = get_paper(paper)

    quant_px = options.quantization_mm * _PX_PER_MM
    # click 8.4 quirk: options must precede the FILE argument in execute()
    cmds = ["read", "--quantization", f"{quant_px:g}", shlex.quote(str(svg_path))]
    if options.linemerge:
        cmds.append("linemerge")
    if options.linesimplify:
        cmds.append("linesimplify")
    if options.linesort:
        cmds.append("linesort")
    if options.reloop:
        cmds.append("reloop")
    doc = vpype_cli.execute(" ".join(cmds))
    if doc.is_empty():
        raise ValueError("SVG contains no plottable geometry")

    if options.rotate_90:
        _rotate_artwork(doc, math.pi / 2)

    _place_scale, _grid_info, doc = _place_on_paper(doc, p, options)

    # ---- per-layer HP-GL via vpype hp7475a writer ----
    chunks: list[tuple[int, int, str]] = []  # (layer_id, pen, geometry-only HP-GL)
    ps_stripped = 0
    for layer_id in sorted(doc.layers.keys()):
        single = vp.Document()
        single.page_size = doc.page_size
        single.add(doc.layers[layer_id], layer_id, with_metadata=True)
        buf = io.StringIO()
        vp.write_hpgl(
            output=buf,
            document=single,
            page_size=p.name,
            landscape=options.landscape,
            center=False,
            device="hp7475a",
            velocity=None,
            absolute=True,
            quiet=True,
        )
        raw = buf.getvalue()
        geom = _CHUNK_PREFIX_RE.sub("", raw, count=1)
        ps_stripped += raw.count("PS")
        geom = _CHUNK_SUFFIX_RE.sub("", geom)
        geom = geom.strip()
        if not geom:
            continue  # layer had no lines with ≥2 points — nothing to plot
        pen = int(pen_map.get(str(layer_id), 1 + (layer_id - 1) % protocol.PEN_COUNT))
        if not (1 <= pen <= protocol.PEN_COUNT):
            raise ValueError(f"pen {pen} for layer {layer_id} outside 1..6")
        chunks.append((layer_id, pen, geom))

    if not chunks:
        raise ValueError("SVG contains no plottable geometry")

    # ---- assembly: header, SP per layer, final park ----
    header = protocol.HPGL_INIT + protocol.HPGL_DEFAULTS
    if options.velocity_cm_s is not None:
        v = quantize_velocity(options.velocity_cm_s)
        if not (protocol.VELOCITY_MIN_CM_S <= options.velocity_cm_s
                <= protocol.VELOCITY_MAX_CM_S):
            raise ValueError(
                f"velocity {options.velocity_cm_s} outside "
                f"{protocol.VELOCITY_MIN_CM_S}..{protocol.VELOCITY_MAX_CM_S} cm/s"
            )
        if abs(v - protocol.VELOCITY_MAX_CM_S) > 1e-9:  # default 38.1 → omit VS
            header += f"VS{v:g};"
    corner_x, corner_y = park_position(p)
    hpgl = (
        header
        + "".join(f"SP{pen};{geom}" for _lid, pen, geom in chunks)
        + f"PU{corner_x},{corner_y};SP0;"
    )

    report = validate_hpgl(hpgl, p)
    if report.errors:
        raise RuntimeError(
            "generated HP-GL failed its own validator: " + "; ".join(report.errors)
        )

    # ---- preview: post-processed geometry, full paper page ----
    preview = tempfile.NamedTemporaryFile(
        "w", suffix=".svg", prefix="hp7475a_preview_", delete=False, encoding="utf-8"
    )
    vp.write_svg(
        output=preview,
        document=doc,
        page_size=doc.page_size,
        center=False,
        set_date=False,
    )
    preview.close()
    _inject_travel_group(Path(preview.name), _travel_segments(doc, [lid for lid, _p, _g in chunks]))

    # ---- stats ----
    layer_stats = {
        str(lid): {
            "pen": pen,
            "paths": len(doc.layers[lid]),
            "length_mm": round(float(doc.layers[lid].length()) / _PX_PER_MM, 2),
        }
        for lid, pen, _g in chunks
    }
    drawn_mm = sum(s["length_mm"] for s in layer_stats.values())
    travel_mm = round(float(doc.pen_up_length()) / _PX_PER_MM, 2)
    eff_v = (
        quantize_velocity(options.velocity_cm_s)
        if options.velocity_cm_s is not None else protocol.VELOCITY_MAX_CM_S
    )
    estimate = {
        "drawn_mm": round(drawn_mm, 1),
        "travel_mm": travel_mm,
        "velocity_cm_s": eff_v,
        "est_seconds": round(
            _estimate_seconds(drawn_mm, travel_mm, eff_v, len({pen for _l, pen, _g in chunks})), 1
        ),
    }
    stats = {
        "paper": p.name,
        "fit_scale": round(_place_scale, 4),
        "user_scale": options.scale,
        "rotate_90": options.rotate_90,
        "margin_mm": options.margin_mm,
        "safe_area_mm": p.safe_area_mm,
        "layers": layer_stats,
        "total_paths": sum(s["paths"] for s in layer_stats.values()),
        "pen_up_travel_mm": round(float(doc.pen_up_length()) / _PX_PER_MM, 2),
        "estimate": estimate,
        "copies": _grid_info,
        "hpgl_bytes": len(hpgl.encode()),
        "warnings": report.warnings,
        "ps_stripped": ps_stripped,  # vpype PS instruction neutralized (see module note)
    }
    return PipelineResult(hpgl=hpgl, preview_svg_path=preview.name, stats=stats)
