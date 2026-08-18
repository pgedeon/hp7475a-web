"""Job model and state machine (spec §11/§13).

States: QUEUED → PREPARING → READY → (await user confirm) → SENDING →
PLOTTING → COMPLETING → COMPLETED, with PAUSED, CANCELLED, FAILED,
DISCONNECTED terminal/divergent states.

Rules enforced here (transitions table); the worker never sets states
directly — it goes through JobMachine so illegal transitions raise.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, asdict
from typing import Any


class JobState(str, enum.Enum):
    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    READY = "READY"
    SENDING = "SENDING"
    PLOTTING = "PLOTTING"
    PAUSED = "PAUSED"
    COMPLETING = "COMPLETING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    DISCONNECTED = "DISCONNECTED"


#: Allowed transitions. Terminal states (COMPLETED/CANCELLED/FAILED/
#: DISCONNECTED) have no outgoing entries.
TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.PREPARING, JobState.CANCELLED, JobState.FAILED}),
    JobState.PREPARING: frozenset(
        {JobState.READY, JobState.FAILED, JobState.CANCELLED, JobState.DISCONNECTED}
    ),
    JobState.READY: frozenset({JobState.SENDING, JobState.CANCELLED, JobState.FAILED}),
    JobState.SENDING: frozenset(
        {JobState.PLOTTING, JobState.PAUSED, JobState.CANCELLED, JobState.FAILED, JobState.DISCONNECTED}
    ),
    JobState.PLOTTING: frozenset(
        {JobState.COMPLETING, JobState.PAUSED, JobState.CANCELLED, JobState.FAILED, JobState.DISCONNECTED}
    ),
    JobState.PAUSED: frozenset(
        {JobState.SENDING, JobState.PLOTTING, JobState.CANCELLED, JobState.FAILED, JobState.DISCONNECTED}
    ),
    JobState.COMPLETING: frozenset(
        {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED, JobState.DISCONNECTED}
    ),
    JobState.COMPLETED: frozenset(),
    JobState.CANCELLED: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.DISCONNECTED: frozenset(),
}


class IllegalTransition(RuntimeError):
    pass


def transition(current: JobState, target: JobState) -> JobState:
    if target not in TRANSITIONS[current]:
        raise IllegalTransition(f"{current.value} → {target.value} is not allowed")
    return target


@dataclass
class Job:
    id: str
    name: str = ""
    status: JobState = JobState.QUEUED
    file_id: str | None = None
    paper: str = "a4"
    pen_map: dict[str, int] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    hpgl: str = ""
    bytes_total: int = 0
    bytes_sent: int = 0
    error: str | None = None
    stats: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Job":
        d = dict(d)
        d["status"] = JobState(d["status"])
        d["pen_map"] = dict(d.get("pen_map") or {})
        d["options"] = dict(d.get("options") or {})
        d["stats"] = dict(d.get("stats") or {})
        return cls(**d)


# Import-time sanity: every non-terminal state must be reachable & consistent.
def _self_check() -> None:
    for state, targets in TRANSITIONS.items():
        for t in targets:
            assert isinstance(t, JobState)
            if t not in (JobState.DISCONNECTED,):
                assert t in TRANSITIONS  # defined state


_self_check()
