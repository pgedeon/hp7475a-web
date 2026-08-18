"""SQLite persistence layer (stdlib sqlite3, WAL mode).

Deliberately no ORM: the schema is small (jobs + settings), fully covered by
tests, and a thin repository keeps the dependency surface minimal for a
hardware-adjacent service. Migrations are forward-only, versioned in
`schema_meta`.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    file_id TEXT,
    hpgl TEXT NOT NULL DEFAULT '',
    paper TEXT NOT NULL DEFAULT 'a4',
    pen_map TEXT NOT NULL DEFAULT '{}',
    options TEXT NOT NULL DEFAULT '{}',
    stats TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    bytes_total INTEGER NOT NULL DEFAULT 0,
    bytes_sent INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
"""


class Database:
    """Thread-safe wrapper over a single sqlite3 connection (WAL)."""

    def __init__(self, path: Path | str):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute("SELECT version FROM schema_meta").fetchone()
            if row is None:
                self._conn.execute("INSERT INTO schema_meta (version) VALUES (?)", (SCHEMA_VERSION,))
                self._conn.commit()
            elif row["version"] != SCHEMA_VERSION:
                # Forward-only migrations would go here; v1 is initial.
                raise RuntimeError(
                    f"DB schema version {row['version']} != expected {SCHEMA_VERSION}"
                )

    # -- generic helpers ---------------------------------------------------

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- settings kv ---------------------------------------------------------

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.query("SELECT value FROM settings WHERE key = ?", (key,))
        if not row:
            return default
        return json.loads(row[0]["value"])

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def now() -> float:
    return time.time()
