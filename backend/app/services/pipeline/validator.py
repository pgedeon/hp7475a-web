"""HP-GL job-payload validator — the gate before anything reaches the serial port.

Implements BUILD_SPEC §24 (HP-GL inspector validation), §39 (HP-GL safety
validator) for *job payloads*. Policy:

- allowlist = ``protocol.SAFE_HPGL_COMMANDS`` **minus** every output
  instruction (OA/OC/OD/OE/OF/OH/OI/OO/OP/OS/OW): a job payload must never
  contain queries — replies would corrupt the streaming handshake (they are
  injected exclusively by the transport layer, hardware-notes §5/§6).
- ``IN``/``DF`` allowed only at the very start of the job.
- pen numbers must be 0..6 (``protocol.MIN_PEN``..``MAX_PEN``).
- coordinates are tracked through PA/PR mode switches and checked against
  the paper hard-clip ranges from ``paper.PAPERS``: overshoot of at most
  ``CLIP_TOLERANCE_UNITS`` (200u ≈ 5 mm) is a warning, anything beyond is
  an error (spec §39 "out-of-range detection").
- no ESC (or any other control) bytes anywhere — HP-GL/2/PCL escape
  sequences are rejected outright (spec §39).
- every instruction must be semicolon-terminated.
- total payload capped at ``MAX_JOB_BYTES`` (5 MB).
- ``FS``/``PW`` warn "ineffective on 7475A" (``protocol.INEFFECTIVE_HPGL_COMMANDS``).

Spec references: BUILD_SPEC §24, §39, §45; hardware-notes §3, §9.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.services.serial import protocol
from app.services.serial.paper import Paper, get_paper

__all__ = ["ValidationReport", "validate_hpgl", "MAX_JOB_BYTES", "CLIP_TOLERANCE_UNITS"]

MAX_JOB_BYTES = 5 * 1024 * 1024  #: hard cap on job payload size
CLIP_TOLERANCE_UNITS = 200  #: ≈5 mm; warning-only overshoot band

#: Output instructions are transport-layer-only — forbidden in job payloads.
_OUTPUT_INSTRUCTIONS = frozenset(
    {"OA", "OC", "OD", "OE", "OF", "OH", "OI", "OO", "OP", "OS", "OW"}
)

_ALLOWED = frozenset(protocol.SAFE_HPGL_COMMANDS) - _OUTPUT_INSTRUCTIONS

#: Motion commands whose parameters are coordinate pairs.
_MOTION = frozenset({"PU", "PD", "PA", "PR"})

#: Commands accepting arbitrary free-form payloads (tokenized leniently).
_FREESTYLE = frozenset({"LB", "DT", "SM"})

_NUMBER_RE = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)$")
_ABSURD = 10_000_000  #: numeric magnitude guard


@dataclass
class ValidationReport:
    """Result of validate_hpgl()."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_hpgl(text: str, paper: str | Paper | None) -> ValidationReport:
    """Validate an HP-GL job payload for the HP 7475A.

    Args:
        text: the full HP-GL job text (uploaded or generated).
        paper: paper name (or :class:`Paper`) used for hard-clip extent
            checks. ``None`` skips extent checks (used at upload time, before
            a paper is chosen; prepare-time re-validation supplies the paper).

    Returns:
        :class:`ValidationReport`. ``errors`` empty means safe to (queue for)
        transmission; ``warnings`` are advisory.
    """
    report = ValidationReport()
    p = None if paper is None else (paper if isinstance(paper, Paper) else get_paper(paper))

    if len(text.encode("utf-8", errors="replace")) > MAX_JOB_BYTES:
        report.errors.append(
            f"job exceeds {MAX_JOB_BYTES} byte cap"
        )
        return report

    # control bytes: ESC anywhere is an error; other C0 except \t\n\r too
    for i, ch in enumerate(text):
        code = ord(ch)
        if code == 0x1B:
            report.errors.append(f"ESC byte at offset {i} (HP-GL/2 escape — not for job payloads)")
        elif code < 0x20 and ch not in "\t\n\r":
            report.errors.append(f"control byte 0x{code:02x} at offset {i}")
    if report.errors:
        return report

    tokens = _tokens(text, report)

    absolute_mode = True  # IN/DF default
    x = y = 0.0  # pen position assumption; generated jobs always PU-abs first
    pen = 0
    pen_down = False
    seen_body = False  # any non-(IN/DF) instruction seen

    for idx, raw in enumerate(tokens):
        instr = raw.strip()
        if not instr:
            continue
        mnemonic = instr[:2].upper() if len(instr) >= 2 and instr[:2].isalpha() else ""
        if not mnemonic:
            report.errors.append(f"instruction #{idx + 1}: {instr!r} has no HP-GL mnemonic")
            continue
        args_blob = instr[2:].strip()

        if mnemonic in _OUTPUT_INSTRUCTIONS:
            report.errors.append(
                f"{mnemonic} (output instruction) forbidden in job payloads"
            )
            continue
        if mnemonic not in _ALLOWED:
            report.errors.append(f"instruction {mnemonic!r} not in HP 7475A safe subset")
            continue
        if mnemonic in protocol.INEFFECTIVE_HPGL_COMMANDS:
            report.warnings.append(f"{mnemonic} has no effect on the HP 7475A")

        if mnemonic in ("IN", "DF"):
            if seen_body:
                report.errors.append(f"{mnemonic} allowed only at the start of the job")
            absolute_mode = True
            continue
        seen_body = True

        if mnemonic == "SP":
            if args_blob:
                pen, err = _int_arg(mnemonic, args_blob, report)
                if not err and not (protocol.MIN_PEN <= pen <= protocol.MAX_PEN):
                    report.errors.append(f"pen number {pen} outside 0..6")
                if not err and pen == 0:
                    pen_down = False
            continue
        if mnemonic == "VS":
            if args_blob:
                vals, err = _num_args(mnemonic, args_blob, report, expect=1)
                if not err and vals is not None:
                    v = vals[0]
                    if not (0 <= v <= protocol.VELOCITY_MAX_CM_S):
                        report.errors.append(
                            f"VS {v} outside 0..{protocol.VELOCITY_MAX_CM_S} cm/s"
                        )
                    elif abs(v / protocol.VELOCITY_STEP_CM_S - round(v / protocol.VELOCITY_STEP_CM_S)) > 1e-6 and v != 0:
                        report.warnings.append(
                            f"VS {v} not a multiple of {protocol.VELOCITY_STEP_CM_S} cm/s"
                        )
            continue
        if mnemonic in _FREESTYLE:
            continue  # payload arbitrary; no coordinate semantics to check

        if mnemonic in ("PA", "PR"):
            absolute_mode = mnemonic == "PA"

        if mnemonic in _MOTION:
            coords, err = _num_args(mnemonic, args_blob, report, expect=None, even=True)
            if err or coords is None:
                continue
            for j in range(0, len(coords), 2):
                absolute = mnemonic == "PA" or (mnemonic != "PR" and absolute_mode)
                if absolute:
                    nx, ny = coords[j], coords[j + 1]
                else:
                    nx, ny = x + coords[j], y + coords[j + 1]
                x, y = nx, ny
                if p is not None:
                    _check_extent(p, nx, ny, report)
            if mnemonic == "PD":
                pen_down = True
            elif mnemonic == "PU":
                pen_down = False
            continue
        if mnemonic in ("AA",):
            coords, err = _num_args(mnemonic, args_blob, report, expect=3)
            if not err and coords:
                x, y = coords[0], coords[1]
                if p is not None:
                    _check_extent(p, x, y, report)
            continue
        if mnemonic in ("AR",):
            coords, err = _num_args(mnemonic, args_blob, report, expect=3)
            if not err and coords:
                x, y = x + coords[0], y + coords[1]
                if p is not None:
                    _check_extent(p, x, y, report)
            continue
        if mnemonic == "CI":
            _num_args(mnemonic, args_blob, report, expect=1)
            continue
        if mnemonic == "HM":
            x, y = 0.0, 0.0
            pen_down = False
            continue
        # remaining safe-set commands (LT/PT/AS/IP/IW/SC/RO/TL/XT/YT/CP/DR/SI/SR/IM/FS/PW/…):
        # numeric sanity only
        _num_args(mnemonic, args_blob, report, expect=None)

    if pen_down:
        report.warnings.append("job ends with pen down (no final PU/SP0)")
    if pen != 0:
        report.warnings.append("job ends without SP0 (pen not stored)")
    return report


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _tokens(text: str, report: ValidationReport) -> list[str]:
    """Split on ';' and flag a non-whitespace unterminated tail."""
    parts = text.split(";")
    tail = parts.pop()
    if tail.strip():
        report.errors.append(f"unterminated instruction: {tail.strip()!r} (missing ';')")
    return parts


def _parse_nums(blob: str) -> list[float] | None:
    blob = blob.strip()
    if not blob:
        return []
    vals: list[float] = []
    for part in blob.replace(" ", "").split(","):
        if not _NUMBER_RE.match(part):
            return None
        vals.append(float(part))
    return vals


def _num_args(
    mnemonic: str,
    blob: str,
    report: ValidationReport,
    expect: int | None,
    even: bool = False,
) -> tuple[list[float] | None, bool]:
    vals = _parse_nums(blob)
    if vals is None:
        report.errors.append(f"{mnemonic}: non-numeric parameter {blob!r}")
        return None, True
    if any(abs(v) > _ABSURD for v in vals):
        report.errors.append(f"{mnemonic}: unreasonable numeric value")
        return None, True
    if expect is not None and len(vals) != expect:
        report.errors.append(f"{mnemonic}: expected {expect} parameter(s), got {len(vals)}")
        return vals, True
    if even and len(vals) % 2 != 0:
        report.errors.append(f"{mnemonic}: odd number of coordinates")
        return vals, True
    return vals, False


def _int_arg(
    mnemonic: str, blob: str, report: ValidationReport
) -> tuple[int, bool]:
    vals = _parse_nums(blob)
    if vals is None or len(vals) != 1:
        report.errors.append(f"{mnemonic}: bad parameter {blob!r}")
        return -1, True
    if float(vals[0]).is_integer():
        return int(vals[0]), False
    report.errors.append(f"{mnemonic}: pen number must be an integer")
    return -1, True


def _check_extent(p: Paper, x: float, y: float, report: ValidationReport) -> None:
    """Hard-clip check with CLIP_TOLERANCE_UNITS warning band."""
    xmin, xmax = p.x_range
    ymin, ymax = p.y_range
    over = max(xmin - x, x - xmax, ymin - y, y - ymax)
    if over > CLIP_TOLERANCE_UNITS:
        report.errors.append(
            f"coordinate ({x:.0f},{y:.0f}) exceeds hard-clip "
            f"[{xmin}..{xmax}]x[{ymin}..{ymax}] by {over:.0f}u (paper {p.name})"
        )
    elif over > 0:
        report.warnings.append(
            f"coordinate ({x:.0f},{y:.0f}) within {over:.0f}u of/beyond hard-clip "
            f"edge (≤{CLIP_TOLERANCE_UNITS}u tolerance)"
        )
