"""Thread-safe job repository on top of Database, with pub/sub for WS."""

from __future__ import annotations

import threading
from typing import Callable

from app.db import Database, new_id, now
from app.jobs.models import IllegalTransition, Job, JobState, transition


class JobNotFound(KeyError):
    pass


class JobStore:
    """CRUD + state transitions + listeners. All mutations serialized by the
    DB lock; listeners called after commit (must not raise)."""

    def __init__(self, db: Database, history_keep: int = 100):
        self._db = db
        self._keep = history_keep
        self._listeners: list[Callable[[Job], None]] = []
        self._lock = threading.RLock()

    # -- subscriptions -------------------------------------------------------

    def subscribe(self, fn: Callable[[Job], None]) -> None:
        with self._lock:
            self._listeners.append(fn)

    def _emit(self, job: Job) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(job)
            except Exception:  # listener bugs never break the store
                pass

    # -- CRUD ----------------------------------------------------------------

    def create(self, **kwargs) -> Job:
        job = Job(id=new_id(), created_at=now(), updated_at=now(), **kwargs)
        self._db.execute(
            "INSERT INTO jobs (id, created_at, updated_at, name, status, file_id, hpgl,"
            " paper, pen_map, options, stats, error, bytes_total, bytes_sent)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job.id, job.created_at, job.updated_at, job.name, job.status.value,
                job.file_id, job.hpgl, job.paper, _dumps(job.pen_map), _dumps(job.options),
                _dumps(job.stats), job.error, job.bytes_total, job.bytes_sent,
            ),
        )
        self._emit(job)
        return job

    def get(self, job_id: str) -> Job:
        rows = self._db.query("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if not rows:
            raise JobNotFound(job_id)
        return _row_to_job(rows[0])

    def list(self, limit: int = 50) -> list[Job]:
        rows = self._db.query(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return [_row_to_job(r) for r in rows]

    def delete(self, job_id: str) -> None:
        job = self.get(job_id)  # raises if missing
        if job.status in {
            JobState.SENDING, JobState.PLOTTING, JobState.COMPLETING, JobState.PAUSED,
        }:
            raise IllegalTransition("cannot delete an active/paused job; cancel it first")
        self._db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    # -- state machine ---------------------------------------------------------

    def set_state(self, job_id: str, target: JobState, *, error: str | None = None) -> Job:
        with self._lock:
            job = self.get(job_id)
            job.status = transition(job.status, target)
            job.error = error
            job.updated_at = now()
            self._db.execute(
                "UPDATE jobs SET status=?, error=?, updated_at=? WHERE id=?",
                (job.status.value, job.error, job.updated_at, job.id),
            )
        self._emit(job)
        return job

    def update(self, job_id: str, **fields) -> Job:
        """Update whitelisted fields (name/paper/pen_map/options/hpgl/
        bytes_total/bytes_sent/stats/file_id)."""
        allowed = {"name", "paper", "pen_map", "options", "hpgl", "bytes_total",
                   "bytes_sent", "stats", "file_id"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"cannot update fields: {sorted(unknown)}")
        with self._lock:
            job = self.get(job_id)
            for k, v in fields.items():
                setattr(job, k, v)
            job.updated_at = now()
            self._db.execute(
                "UPDATE jobs SET name=?, paper=?, pen_map=?, options=?, hpgl=?,"
                " bytes_total=?, bytes_sent=?, stats=?, file_id=?, updated_at=? WHERE id=?",
                (
                    job.name, job.paper, _dumps(job.pen_map), _dumps(job.options), job.hpgl,
                    job.bytes_total, job.bytes_sent, _dumps(job.stats), job.file_id,
                    job.updated_at, job.id,
                ),
            )
        self._emit(job)
        return job

    def prune_history(self) -> None:
        """Keep only the most recent `history_keep` terminal jobs."""
        rows = self._db.query(
            "SELECT id FROM jobs WHERE status IN (?,?,?,?,?) ORDER BY created_at DESC",
            tuple(s.value for s in (
                JobState.COMPLETED, JobState.CANCELLED, JobState.FAILED, JobState.DISCONNECTED,
            )),
        )
        for row in rows[self._keep :]:
            self._db.execute("DELETE FROM jobs WHERE id = ?", (row["id"],))


def _dumps(obj) -> str:
    import json

    return json.dumps(obj)


def _row_to_job(row) -> Job:
    import json

    return Job.from_dict(
        {
            "id": row["id"],
            "name": row["name"],
            "status": row["status"],
            "file_id": row["file_id"],
            "hpgl": row["hpgl"],
            "paper": row["paper"],
            "pen_map": json.loads(row["pen_map"]),
            "options": json.loads(row["options"]),
            "stats": json.loads(row["stats"]),
            "bytes_total": row["bytes_total"],
            "bytes_sent": row["bytes_sent"],
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
    )
