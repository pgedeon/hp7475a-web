"""Uploaded-file registry: stored under data/files/<id>/ with metadata.

SVG uploads are sanitized at upload time (fail-closed); HP-GL uploads are
validated. Analysis is computed lazily and cached in meta.json.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.db import new_id


@dataclass
class FileMeta:
    id: str
    kind: str  # "svg" | "hpgl"
    name: str
    size_bytes: int
    stored_path: str
    created_at: float
    sanitize_report: dict | None = None
    analysis: dict | None = None
    validation: dict | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class FileRegistry:
    def __init__(self, base: Path, max_bytes: int):
        self._base = base
        self._base.mkdir(parents=True, exist_ok=True)
        self._max = max_bytes

    @property
    def max_bytes(self) -> int:
        return self._max

    def _dir(self, file_id: str) -> Path:
        d = self._base / file_id
        if not d.is_dir():
            raise FileNotFoundError(file_id)
        return d

    def save(self, *, kind: str, name: str, content: bytes, extra: dict | None = None) -> FileMeta:
        if len(content) > self._max:
            raise ValueError(f"file exceeds {self._max} byte cap")
        file_id = new_id()
        d = self._base / file_id
        d.mkdir(parents=True)
        suffix = ".svg" if kind == "svg" else ".hpgl"
        stored = d / f"source{suffix}"
        stored.write_bytes(content)
        meta = FileMeta(
            id=file_id,
            kind=kind,
            name=name or f"upload{suffix}",
            size_bytes=len(content),
            stored_path=str(stored),
            created_at=time.time(),
            **(extra or {}),
        )
        self._write_meta(meta)
        return meta

    def get(self, file_id: str) -> FileMeta:
        d = self._dir(file_id)
        raw = json.loads((d / "meta.json").read_text())
        return FileMeta(
            id=raw["id"], kind=raw["kind"], name=raw["name"],
            size_bytes=raw["size_bytes"], stored_path=raw["stored_path"],
            created_at=raw["created_at"], sanitize_report=raw.get("sanitize_report"),
            analysis=raw.get("analysis"), validation=raw.get("validation"),
        )

    def update(self, meta: FileMeta) -> None:
        self._dir(meta.id)
        self._write_meta(meta)

    def read_bytes(self, file_id: str) -> bytes:
        return Path(self.get(file_id).stored_path).read_bytes()

    def _write_meta(self, meta: FileMeta) -> None:
        d = self._base / meta.id
        (d / "meta.json").write_text(json.dumps(meta.to_dict(), indent=1))
