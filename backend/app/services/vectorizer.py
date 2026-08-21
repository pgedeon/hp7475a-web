"""Raster → SVG vectorization via the SLD-Vectorization CLI (subprocess).

Goal 950c719c. Isolation by design: the SLD tool (torch/numba/potracer) runs
in its OWN venv (``~/sld-venv``) as a subprocess — the plotter app venv stays
clean (no torch). The subprocess may write ONLY under the app data dir and
/tmp (systemd hardening: ``ReadWritePaths=data``, ``PrivateTmp``), so we pass
cache dirs into /tmp defensively.

The CLI (verified on dev):
    SLDvec run INPUT.png --output-path OUTPUT.svg [--thresh 0.6] [--multiple-lines]
Exit 0 on success; writes an svgwrite SVG (baseProfile tiny, one/few <path>,
no layers). Runtime ~23s (simple) to ~3min (complex) on CPU.
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

#: Allowed raster input types (by extension).
ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 20 MB

#: Concurrency guard: max 2 concurrent vectorizations (CPU-bound, 23s–3min each).
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


def run_vectorization(
    data_dir: Path,
    input_bytes: bytes,
    filename: str,
    *,
    thresh: float | None = None,
    multiple_lines: bool = False,
    timeout_s: float = 600.0,
) -> VectorizeResult:
    """Run the SLD CLI on *input_bytes*; return metadata or raise VectorizeError.

    - Saves input under ``data_dir/vectorize/<id>/input.<ext>``
    - Runs SLDvec → ``data_dir/vectorize/<id>/output.svg``
    - Concurrency-limited to 2 (module-level semaphore).

    Raises:
        ValueError: input not an allowed image type / too large.
        VectorizeError: CLI missing, non-zero exit, timeout, or empty output.
    """
    from app.db import new_id  # local import: avoid a cycle at module load

    ext = validate_image(filename, input_bytes)
    vid = new_id()
    job_dir = data_dir / "vectorize" / vid
    job_dir.mkdir(parents=True, exist_ok=True)
    input_path = job_dir / f"input{ext}"
    input_path.write_bytes(input_bytes)
    output_path = job_dir / "output.svg"

    cmd = [SLD_CLI, "run", str(input_path), "--output-path", str(output_path)]
    if thresh is not None:
        cmd += ["--thresh", str(thresh)]
    if multiple_lines:
        cmd += ["--multiple-lines"]

    env = {**os.environ, **_SUBPROCESS_ENV}

    with _SEM:
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(data_dir),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise VectorizeError(
                f"vectorization timed out after {timeout_s:.0f}s",
                stderr_tail=_tail(exc.stderr or ""),
            ) from exc
        except FileNotFoundError as exc:
            raise VectorizeError(f"SLD CLI not found at {SLD_CLI}") from exc

    duration = time.monotonic() - t0
    stderr_tail = _tail(proc.stderr)

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
