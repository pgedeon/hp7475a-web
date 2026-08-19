"""Normalized paper/device model for the HP 7475A.

Single source of truth for paper sizes, hard-clip limits, and plotter-unit
conversions. Verified against the HP 7475A Interfacing and Programming Manual
(§7-2 hard-clip table) and vpype 1.15 ``hp7475a`` device config — both match.

All geometry in this module is in *plotter units* unless a field name says
otherwise. 1 plotter unit = 0.02488 mm.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.serial.protocol import PLOTTER_UNIT_MM

__all__ = ["Paper", "PAPERS", "get_paper", "plotter_units_to_mm", "mm_to_plotter_units"]


def plotter_units_to_mm(units: float) -> float:
    return units * PLOTTER_UNIT_MM


def mm_to_plotter_units(mm: float) -> float:
    return mm / PLOTTER_UNIT_MM


@dataclass(frozen=True)
class Paper:
    """One paper configuration of the HP 7475A.

    Attributes:
        name: canonical short name (matches vpype paper names).
        aka: alternate names accepted from the API/UI.
        size_mm: (width, height) in mm for landscape carriage orientation
            as configured on the plotter (paper switches).
        x_range: (xmin, xmax) hard-clip limits in plotter units.
        y_range: (ymin, ymax) hard-clip limits in plotter units.
        dip_mode: rear-panel DIP switch mode required for this paper.
        info: user-facing note.
        safe_area_mm: practical usable area (width, height) in mm in the
            same axis convention as size_mm (X = carriage / long edge,
            Y = paper motion). Defaults from the user-measured plot-area
            doc (goal 47da763c): A4 274x192, A3 399x271 (the doc's
            "271x399" portrait frame rotated into this convention);
            ANSI A/B derived at 95% of the hard clip. None = treat the
            full hard-clip rect as the safe area.
        loads_orientation: how the sheet is loaded per the user doc
            (caption word only - no geometry effect): A4 landscape,
            A3 portrait ("297 mm edge across the carriage").
    """

    name: str
    aka: tuple[str, ...]
    size_mm: tuple[float, float]
    x_range: tuple[int, int]
    y_range: tuple[int, int]
    dip_mode: str
    info: str = ""
    safe_area_mm: tuple[float, float] | None = None
    loads_orientation: str = "landscape"

    @property
    def width_units(self) -> int:
        return self.x_range[1] - self.x_range[0]

    @property
    def height_units(self) -> int:
        return self.y_range[1] - self.y_range[0]

    @property
    def width_mm(self) -> float:
        return plotter_units_to_mm(self.width_units)

    @property
    def height_mm(self) -> float:
        return plotter_units_to_mm(self.height_units)

    @property
    def safe_size_mm(self) -> tuple[float, float]:
        """Usable (width, height) in mm - safe area if defined, else clip."""
        if self.safe_area_mm is None:
            return (self.width_mm, self.height_mm)
        return self.safe_area_mm

    @property
    def safe_inset_mm(self) -> tuple[float, float]:
        """Per-axis inset (x, y) of the safe area inside the hard clip, mm."""
        if self.safe_area_mm is None:
            return (0.0, 0.0)
        return (
            (self.width_mm - self.safe_area_mm[0]) / 2.0,
            (self.height_mm - self.safe_area_mm[1]) / 2.0,
        )

    def contains(self, x: float, y: float, *, margin_units: int = 0) -> bool:
        """True if point (x, y) lies inside hard-clip limits (minus margin)."""
        xmin, xmax = self.x_range
        ymin, ymax = self.y_range
        return (
            xmin + margin_units <= x <= xmax - margin_units
            and ymin + margin_units <= y <= ymax - margin_units
        )


# Hard-clip values: HP 7475A Prog. Manual §7-2 == vpype hp7475a papers.
PAPERS: dict[str, Paper] = {
    p.name: p
    for p in (
        Paper(
            name="a4",
            aka=("A4",),
            size_mm=(297.0, 210.0),
            x_range=(0, 11040),
            y_range=(0, 7721),
            dip_mode="metric",
            info="Plotter must be configured in Metric mode (rear DIP).",
            safe_area_mm=(274.0, 192.0),
            loads_orientation="landscape",
        ),
        Paper(
            name="a3",
            aka=("A3",),
            size_mm=(420.0, 297.0),
            x_range=(0, 16158),
            y_range=(0, 11040),
            dip_mode="metric",
            info="Plotter must be configured in Metric mode (rear DIP).",
            # user doc "271x399" is in the portrait/load frame; stored here
            # landscape-first (same convention as size_mm) to stay inside
            # the X=carriage hard clip this module pins.
            safe_area_mm=(399.0, 271.0),
            loads_orientation="portrait",
        ),
        Paper(
            name="a",
            aka=("ansi_a", "letter", "ANSI A", "Letter"),
            size_mm=(279.4, 215.9),
            x_range=(0, 10365),
            y_range=(0, 7962),
            dip_mode="imperial",
            info="Plotter must be configured in Imperial mode (rear DIP).",
            safe_area_mm=(245.0, 188.2),
        ),
        Paper(
            name="b",
            aka=("ansi_b", "tabloid", "ANSI B", "Tabloid"),
            size_mm=(431.8, 279.4),
            x_range=(0, 16640),
            y_range=(0, 10365),
            dip_mode="imperial",
            info="Plotter must be configured in Imperial mode (rear DIP).",
            safe_area_mm=(393.3, 245.0),
        ),
    )
}

_ALIASES: dict[str, str] = {
    alias.lower(): paper.name for paper in PAPERS.values() for alias in paper.aka
}


def get_paper(name: str) -> Paper:
    """Resolve a paper by canonical name or alias (case-insensitive)."""
    key = name.strip().lower()
    if key in PAPERS:
        return PAPERS[key]
    if key in _ALIASES:
        return PAPERS[_ALIASES[key]]
    raise KeyError(
        f"Unknown paper {name!r}. Valid: {sorted(PAPERS)} (aliases: {sorted(_ALIASES)})"
    )


def clip_fits(paper: Paper, limits: tuple[int, int, int, int]) -> bool:
    """True when *paper*'s hard-clip rect fits inside the device's actual
    clip *limits* (xmin, ymin, xmax, ymax from OH;).

    Guards against the clamp-artifact failure (2026-08-18): preparing a
    job for A3 while the plotter's DIP switches select A4 silently clamps
    every coordinate beyond the real clip — drawing vertical garbage lines
    at the sheet edge. """
    xmin, ymin, xmax, ymax = limits
    px0, px1 = paper.x_range
    py0, py1 = paper.y_range
    return px0 >= xmin and py0 >= ymin and px1 <= xmax and py1 <= ymax
