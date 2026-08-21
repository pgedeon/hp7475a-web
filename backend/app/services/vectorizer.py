"""Raster → SVG vectorization via the SLD-Vectorization CLI (subprocess).

Goal 950c719c; async jobs + multi-color layers goal a7f70dae. Isolation by
design: the SLD tool (torch/numba/potracer) runs in its OWN venv
(``~/sld-venv``) as a subprocess — the plotter app venv stays clean (no
torch). The subprocess may write ONLY under the app data dir and /tmp
(systemd hardening: ``ReadWritePaths=data``, ``PrivateTmp``), so we pass
cache dirs into /tmp defensively.

Single-color (default):
    SLDvec run INPUT.png --output-path OUTPUT.svg [--thresh 0.6] [--multiple-lines]
Multi-color (``colors >= 2``): our wrapper script runs the SLD pipeline per
quantized color layer and merges one SVG with per-color stroke groups:
    python multicolor.py INPUT.png OUTPUT.svg --colors K
Exit 0 on success. Runtime ~23s (simple) to minutes (complex); stage lines
(``STAGE ...`` on stdout) are streamed to ``on_stage`` when provided.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: SLD CLI lives in its own venv (goal 950c719c phase A). Overridable for tests.
SLD_CLI = os.environ.get("HP7475A_SLD_CLI", "/home/pgedeon/sld-venv/bin/SLDvec")
SLD_PYTHON = os.environ.get("HP7475A_SLD_PYTHON", "/home/pgedeon/sld-venv/bin/python")
#: Multi-color wrapper (runs inside the sld venv).
MULTICOLOR_SCRIPT = Path(__file__).parent / "sld_scripts" / "multicolor.py"

#: Allowed raster input types (by extension).
ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 20 MB

#: Color-layer bounds (1 = plain single-line B/W behavior).
MIN_COLORS, MAX_COLORS = 1, 8

#: Generous ceilings — jobs are backgrounded + cancellable now, so a long run
#: no longer holds an HTTP request hostage (600s caused user-visible failures).
DEFAULT_TIMEOUT_SINGLE = 1800.0
DEFAULT_TIMEOUT_MULTI = 3600.0

#: Concurrency guard: max 2 concurrent vectorizations (CPU-bound, minutes each).
_SEM = threading.Semaphore(2)

#: Defensive env for the subprocess (read-only venv; keep caches in /tmp).
_SUBPROCESS_ENV = {
    "TORCH_HOME": "/tmp/torch_home",
    "NUMBA_CACHE_DIR": "/tmp/numba_cache",
    "PYTHONPYCACHEPREFIX": "/tmp/pycache",
}


class VectorizeError(Exception):
    """Typed error carrying the CLI stderr tail for diagnostics."""

    def __init__(self, message: str, stderr_tail: str = ""):
        super().__init__(message)
        self.stderr_tail = stderr_tail


@dataclass
class VectorizeResult:
    id: str            # vectorize job id (== data/vectorize/<id>/ dir name)
    svg_path: str      # absolute path to the produced SVG
    duration_s: float  # wall-clock seconds
    stderr_tail: str   # last lines of stderr (diagnostics)


def _tail(text: str, n: int = 40) -> str:
    """Last *n* non-empty lines of *text* (for diagnostics)."""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def validate_image(filename: str, content: bytes) -> str:
    """Return the lowercased extension, or raise ValueError if not allowed."""
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_IMAGE_EXTS:
        raise ValueError(
            f"unsupported image type {ext or '(none)'} — "
            f"allowed: {', '.join(sorted(ALLOWED_IMAGE_EXTS))}"
        )
    if len(content) > MAX_IMAGE_BYTES:
        raise ValueError(
            f"image too large ({len(content)} bytes) — max {MAX_IMAGE_BYTES}"
        )
    return ext


def validate_colors(colors: int) -> int:
    """Raise ValueError outside 1..MAX_COLORS; 1 means single-line B/W."""
    if not (MIN_COLORS <= colors <= MAX_COLORS):
        raise ValueError(
            f"colors must be {MIN_COLORS}..{MAX_COLORS} (1 = single-line B/W)"
        )
    return colors

def run_vectorization(
    data_dir: Path,
    input_bytes: bytes,
    filename: str,
    *,
    thresh: float | None = None,
    multiple_lines: bool = False,
    colors: int = 1,
    timeout_s: float | None = None,
    on_stage=None,
    cancel_event: threading.Event | None = None,
) -> VectorizeResult:
    """Run SLD on *input_bytes*; return metadata or raise VectorizeError.

    - Saves input under ``data_dir/vectorize/<id>/input.<ext>``
    - Runs SLDvec (or the multi-color wrapper) → ``data_dir/vectorize/<id>/output.svg``
    - Concurrency-limited to 2 (module-level semaphore).
    - ``on_stage(text)`` fires for each ``STAGE`` line the child prints.
    - ``cancel_event`` set → child killed, VectorizeError("cancelled").

    Raises:
        ValueError: input not an allowed image type / too large / bad colors.
        VectorizeError: CLI missing, non-zero exit, timeout, cancel, or empty output.
    """
    from app.db import new_id  # local import: avoid a cycle at module load

    ext = validate_image(filename, input_bytes)
    validate_colors(colors)
    if timeout_s is None:
        timeout_s = DEFAULT_TIMEOUT_MULTI if colors > 1 else DEFAULT_TIMEOUT_SINGLE
    vid = new_id()
    job_dir = data_dir / "vectorize" / vid
    job_dir.mkdir(parents=True, exist_ok=True)
    input_path = job_dir / f"input{ext}"
    input_path.write_bytes(input_bytes)
    output_path = job_dir / "output.svg"

    if colors > 1:
        # multicolor.py CLI: IN.png OUT.svg [--colors K] (positional output;
        # verified live on dev — 3-color PNG → 3 stroke groups, exit 0)
        cmd = [
            SLD_PYTHON, str(MULTICOLOR_SCRIPT),
            str(input_path), str(output_path),
            "--colors", str(colors),
        ]
    else:
        cmd = [SLD_CLI, "run", str(input_path), "--output-path", str(output_path)]
        if thresh is not None:
            cmd += ["--thresh", str(thresh)]
        if multiple_lines:
            cmd += ["--multiple-lines"]

    env = {**os.environ, **_SUBPROCESS_ENV}

    with _SEM:
        t0 = time.monotonic()
        stderr_tail = ""
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(data_dir),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
            )
        except FileNotFoundError as exc:
            raise VectorizeError(f"SLD CLI not found at {cmd[0]}") from exc

        tail_lines: list[str] = []
        try:
            assert proc.stdout is not None
            for line in iter(proc.stdout.readline, ""):
                line = line.rstrip("\n")
                if line.strip():
                    tail_lines.append(line)
                    if line.startswith("STAGE ") and on_stage is not None:
                        try:
                            on_stage(line[6:].strip())
                        except Exception:  # never let a UI callback kill the job
                            logger.exception("on_stage callback failed")
                if cancel_event is not None and cancel_event.is_set():
                    proc.kill()
                    proc.wait(timeout=5)
                    raise VectorizeError("vectorization cancelled")
            proc.wait(timeout=max(1.0, timeout_s - (time.monotonic() - t0)))
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            proc.wait(timeout=5)
            raise VectorizeError(
                f"vectorization timed out after {timeout_s:.0f}s",
                stderr_tail=_tail("\n".join(tail_lines)),
            ) from exc
        except VectorizeError:
            raise

    duration = time.monotonic() - t0
    stderr_tail = _tail("\n".join(tail_lines))

    if proc.returncode != 0:
        raise VectorizeError(
            f"vectorization failed (exit {proc.returncode})",
            stderr_tail=stderr_tail,
        )
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise VectorizeError(
            "vectorization produced no SVG output",
            stderr_tail=stderr_tail,
        )

    logger.info(
        "vectorize ok: %s -> %s (%.1fs)", input_path.name, output_path, duration
    )
    return VectorizeResult(
        id=vid,
        svg_path=str(output_path),
        duration_s=round(duration, 2),
        stderr_tail=stderr_tail,
    )
