"""Server-side text→paths conversion via headless Inkscape (goal 47da763c
phase 3 F6).

Fail-soft by contract: every failure mode (missing binary, non-zero exit,
timeout, oversized output) returns empty bytes + a human-readable reason.
Callers keep the original sanitized SVG and surface the reason as a
warning — conversion must NEVER block an upload. The converted output is
re-sanitized by the caller before storage (fail-closed stays intact).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET

__all__ = ["convert_text_to_paths", "has_text_elements"]

#: Element local names that count as "text" for the conversion trigger.
_TEXT_TAGS = frozenset({"text", "tspan", "textPath"})


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def has_text_elements(svg_bytes: bytes) -> bool:
    """True when the SVG contains any text-bearing element (namespace-insensitive)."""
    try:
        root = ET.fromstring(svg_bytes)
    except ET.ParseError:
        return False
    return any(_localname(el.tag) in _TEXT_TAGS for el in root.iter())


def convert_text_to_paths(
    svg_bytes: bytes, timeout_s: float = 30.0
) -> tuple[bytes, str | None]:
    """Convert text elements to stroked paths with Inkscape (headless).

    Returns ``(converted_bytes, None)`` on success, or ``(b"", reason)``
    on any failure (binary missing, non-zero exit, timeout, empty output).
    Never raises; never touches the network (Inkscape runs with a scrubbed,
    minimal environment and a temp HOME so no font cache/config is read or
    written outside the sandbox dir).
    """
    exe = shutil.which("inkscape")
    if not exe:
        return b"", "Inkscape not installed"

    with tempfile.TemporaryDirectory(prefix="hp7475a-textpath-") as tmp:
        in_path = os.path.join(tmp, "in.svg")
        out_path = os.path.join(tmp, "out.svg")
        with open(in_path, "wb") as fh:
            fh.write(svg_bytes)
        # scrubbed env: no network proxies, temp HOME/cache (fonts config),
        # C locale for stable error text
        env = {
            "PATH": os.path.dirname(exe) + ":/usr/bin:/bin",
            "HOME": tmp,
            "XDG_CACHE_HOME": tmp,
            "XDG_CONFIG_HOME": tmp,
            "LANG": "C",
        }
        cmd = [
            exe, in_path,
            "--export-type=svg",
            "--export-text-to-path",
            "--export-plain-svg",
            "-o", out_path,
        ]
        try:
            proc = subprocess.run(  # noqa: S603 — fixed argv, scrubbed env
                cmd, env=env, cwd=tmp, timeout=timeout_s,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except subprocess.TimeoutExpired:
            return b"", f"Inkscape timed out after {timeout_s:.0f}s"
        except OSError as exc:
            return b"", f"Inkscape failed to run: {exc}"
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()[:300]
            return b"", f"Inkscape exited {proc.returncode}: {err}"
        try:
            converted = open(out_path, "rb").read()
        except OSError:
            return b"", "Inkscape produced no output file"
        if not converted:
            return b"", "Inkscape produced empty output"
        return converted, None
