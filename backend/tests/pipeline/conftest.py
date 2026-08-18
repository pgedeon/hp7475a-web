"""Shared helpers for pipeline tests."""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SVG_FIXTURES = REPO_ROOT / "fixtures" / "svg"
HPGL_FIXTURES = REPO_ROOT / "fixtures" / "hpgl"


def svg_bytes(name: str) -> bytes:
    return (SVG_FIXTURES / name).read_bytes()


def hpgl_text(name: str) -> str:
    return (HPGL_FIXTURES / name).read_text()
