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
import re
import shlex
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import vpype as vp
import vpype_cli

from app.services.pipeline.hpgl_writer import park_position
from app.services.pipeline.validator import validate_hpgl
from app.services.serial import protocol
from app.services.serial.paper import Paper, get_paper

__all__ = ["PipelineOptions", "PipelineResult", "run_pipeline"]

_PX_PER_MM = 96.0 / 25.4
_UNIT_MM = protocol.PLOTTER_UNIT_MM  # mm per plotter unit

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
                clean[k] = float(v)
            elif k == "scale":
                s = float(v)
                if not 0.25 <= s <= 1.0:
                    raise ValueError("scale must be between 0.25 and 1.0")
                clean[k] = s
            elif k == "velocity_cm_s":
                clean[k] = float(v) if v is not None else None
            else:
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


def _place_on_paper(doc: vp.Document, paper: Paper, options: PipelineOptions) -> float:
    """Scale-to-fit + center geometry inside the plottable rect (in-place).

    Returns the applied absolute fit scale (mm/mm, before the user
    ``options.scale`` multiplier is included in the returned value as-is
    — see callers: stats report both)."""
    pc = _paper_config(paper)
    clip = _plottable_rect_mm(pc)
    cx0, cy0, cx1, cy1 = clip
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

    # target top-left so geometry is centered in the margin-inset rect
    new_w, new_h = bw * scale, bh * scale
    tx_mm = cx0 + options.margin_mm + (avail_w - new_w) / 2 - bx0 * scale
    ty_mm = cy0 + options.margin_mm + (avail_h - new_h) / 2 - by0 * scale
    doc.translate(tx_mm * _PX_PER_MM, ty_mm * _PX_PER_MM)

    # page size for preview + vpype write inference: full paper, landscape
    page_mm = (paper.size_mm[0], paper.size_mm[1]) if options.landscape else (
        paper.size_mm[1], paper.size_mm[0]
    )
    doc.page_size = (page_mm[0] * _PX_PER_MM, page_mm[1] * _PX_PER_MM)
    return scale


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

    _place_scale = _place_on_paper(doc, p, options)

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
        v = options.velocity_cm_s
        if not (protocol.VELOCITY_MIN_CM_S <= v <= protocol.VELOCITY_MAX_CM_S):
            raise ValueError(
                f"velocity {v} outside {protocol.VELOCITY_MIN_CM_S}..{protocol.VELOCITY_MAX_CM_S} cm/s"
            )
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

    # ---- stats ----
    layer_stats = {
        str(lid): {
            "pen": pen,
            "paths": len(doc.layers[lid]),
            "length_mm": round(float(doc.layers[lid].length()) / _PX_PER_MM, 2),
        }
        for lid, pen, _g in chunks
    }
    stats = {
        "paper": p.name,
        "fit_scale": round(_place_scale, 4),
        "user_scale": options.scale,
        "layers": layer_stats,
        "total_paths": sum(s["paths"] for s in layer_stats.values()),
        "pen_up_travel_mm": round(float(doc.pen_up_length()) / _PX_PER_MM, 2),
        "hpgl_bytes": len(hpgl.encode()),
        "warnings": report.warnings,
        "ps_stripped": ps_stripped,  # vpype PS instruction neutralized (see module note)
    }
    return PipelineResult(hpgl=hpgl, preview_svg_path=preview.name, stats=stats)
