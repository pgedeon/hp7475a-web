"""Manual HP-GL assembly fallback for the HP 7475A.

Produces plain absolute ``PU/PD`` HP-GL from vpype ``LineCollection``-like
geometry (iterables of complex points: x=real, y=imag, in *plotter units*).
Used when the vpype-based pipeline (:mod:`app.services.pipeline.vpy`) is
unavailable or when the job runner needs to assemble geometry directly.
The vpype path remains the primary writer — this module shares its header/
park conventions so output is byte-compatible in shape:

    ``IN;DF; [VS v;] SP<n>; …PU/PD streams… PU<corner>;SP0;``

The park corner is taken from vpype's ``hp7475a`` device config
(``final_pu_params``) so there is exactly one source for that constant.

Spec references: BUILD_SPEC §18 (pipeline), §39 (pen-up/final-safe-state
suffix); hardware-notes §9 (IN does not move the pen; SP0 stores it).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from app.services.serial import protocol
from app.services.serial.paper import Paper, get_paper

__all__ = ["build_hpgl", "park_position"]


def park_position(paper: str | Paper) -> tuple[int, int]:
    """Device park corner (plotter units) for a paper.

    Reads ``final_pu_params`` from the vpype ``hp7475a`` device config —
    the same value vpype itself emits before ``SP0;`` (e.g. ``11040,7721``
    for A4). Raises if the config is missing (fail loud, never invent).
    """
    import vpype as vp

    name = paper.name if isinstance(paper, Paper) else get_paper(paper).name
    config = vp.config_manager.get_plotter_config("hp7475a")
    if config is None:
        raise RuntimeError("vpype hp7475a device config not found")
    for paper_config in config.paper_configs:
        if paper_config.name == name and paper_config.final_pu_params:
            x_s, y_s = paper_config.final_pu_params.split(",")
            return int(x_s), int(y_s)
    raise RuntimeError(f"no final_pu_params for paper {name!r} in hp7475a config")


def build_hpgl(
    geometries: Mapping[int, Iterable],
    paper: str | Paper,
    pen_map: Mapping[str, int],
    options: object | None = None,
) -> str:
    """Assemble an HP-GL job from raw line collections.

    Args:
        geometries: mapping layer_id -> iterable of lines; each line is an
            iterable of points (complex or (x, y)-indexable) in *plotter
            units* (vpype convention after device scaling). Lines with
            fewer than 2 points are skipped, mirroring vpype's writer.
        paper: paper name or :class:`Paper`.
        pen_map: layer id (as string) -> physical pen 1..6. Layers without
            an entry get vpype's default ``1 + (id-1) % 6``.
        options: optional object with ``velocity_cm_s`` (float|None);
            ignored when None.

    Returns:
        HP-GL text: ``IN;DF;`` header, optional ``VS``, one ``SP<n>;`` +
        absolute PU/PD stream per layer (ascending layer id), final park
        ``PU<corner>;SP0;``.

    Raises:
        ValueError: on a pen number outside 1..6 or no geometry at all.
    """
    p = paper if isinstance(paper, Paper) else get_paper(paper)
    velocity = getattr(options, "velocity_cm_s", None) if options is not None else None

    if not geometries:
        raise ValueError("no geometry to plot")

    px, py = park_position(p)

    parts: list[str] = []
    for layer_id in sorted(geometries.keys()):
        pen = int(pen_map.get(str(layer_id), 1 + (layer_id - 1) % protocol.PEN_COUNT))
        if not (1 <= pen <= protocol.PEN_COUNT):
            raise ValueError(f"pen {pen} for layer {layer_id} outside 1..6")
        chunk: list[str] = [protocol.HPGL_SELECT_PEN_FMT.format(pen=pen)]
        for line in geometries[layer_id]:
            pts = [_point(q) for q in line]
            if len(pts) < 2:
                continue
            chunk.append(
                "PU" + ",".join(pts[:1]) + ";PD" + ",".join(pts[1:]) + ";"
            )
        if len(chunk) > 1:  # layer had plottable lines
            parts.append("".join(chunk))

    if not parts:
        raise ValueError("no plottable geometry (all lines shorter than 2 points)")

    header = protocol.HPGL_INIT + protocol.HPGL_DEFAULTS
    if velocity is not None:
        header += protocol.HPGL_VELOCITY_FMT.format(velocity=_fmt_num(float(velocity)))
    trailer = f"PU{px},{py};" + protocol.HPGL_SELECT_PEN_FMT.format(pen=0)

    return "".join([header, *parts, trailer])


def _point(q: object) -> str:
    """Format one point as 'x,y' (plotter units, rounded to int)."""
    if hasattr(q, "real") and hasattr(q, "imag"):
        x, y = q.real, q.imag  # type: ignore[union-attr]
    else:
        x, y = q[0], q[1]  # type: ignore[index]
    return f"{int(round(float(x)))},{int(round(float(y)))}"


def _fmt_num(v: float) -> str:
    return f"{v:g}"
