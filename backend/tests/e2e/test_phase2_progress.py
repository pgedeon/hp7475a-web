"""Phase 2 F3 — live progress + pen state over the PTY fake plotter.

Runs the REAL production path (HardwareWorker → ChunkedStreamer →
SerialTransport → pty → FakeHP7475A) with a collecting publish hook in
place of the WS hub. Pins the sentinel-safety contract of the brief:
progress during SENDING is buffer-accounting only (pen_down=None, zero
OA/OS traffic), OS;-derived pen state appears only during COMPLETING
(replies queue behind the OA sentinel), and completion still lands.
"""

from __future__ import annotations

import threading
import time

import pytest

from app.config import Settings
from app.db import Database
from app.jobs.models import JobState
from app.jobs.store import JobStore
from app.jobs.worker import HardwareWorker
from app.services.device_manager import DeviceManager
from app.services.serial.fakeplotter import FakeHP7475A
from app.services.serial.transport import TransportSettings

# ~11k instructions: long enough for many chunk-level progress events and
# (with exec delay) a COMPLETING phase spanning several OS; poll intervals.
PAYLOAD = "IN;DF;VS20.14;SP1;PU500,500;" + "".join(
    f"PD{500 + i * 10},{600 + (i % 7) * 10};" for i in range(11000)
) + "PU0,0;SP0;"


@pytest.fixture()
def fake(request):
    fake = FakeHP7475A()
    fake.start()
    request.addfinalizer(fake.stop)
    return fake


@pytest.fixture()
def stack(tmp_path, fake):
    events: list[dict] = []
    lock = threading.Lock()

    def publish(msg: dict) -> None:
        with lock:
            events.append(msg)

    settings = Settings(
        data_dir=tmp_path,
        stream_default_chunk=256,      # many chunks → many progress events
        stream_query_timeout_s=1.0,
        completion_timeout_s=60.0,     # exec-delayed drain must fit
    )
    db = Database(settings.db_path)
    jobs = JobStore(db, history_keep=10)
    devices = DeviceManager()
    devices.connect(fake.port_path, {"baudrate": 9600},
                    driver_factory=_fake_driver)
    worker = HardwareWorker(jobs, devices, settings, publish=publish)
    worker.start()
    yield settings, db, jobs, devices, worker, events, lock
    worker.shutdown()
    devices.disconnect()
    db.close()


def _fake_driver(port_path, settings=None):
    from app.services.serial.driver import HP7475ADevice

    return HP7475ADevice(
        port_path,
        TransportSettings(
            **({k: v for k, v in (settings.__dict__ if settings else {}).items()
                if k in TransportSettings.__dataclass_fields__})
        ) if settings else None,
    )


def _wait(jobs, job_id, want, timeout=90.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if jobs.get(job_id).status in want:
            return jobs.get(job_id)
        time.sleep(0.05)
    raise AssertionError(f"stuck in {jobs.get(job_id).status}, wanted {want}")


def test_progress_events_pen_state_and_sentinel_intact(stack, fake):
    settings, db, jobs, devices, worker, events, lock = stack
    fake.set_exec_delay(0.0012)  # ~13s drain → COMPLETING spans OS; polls

    job = jobs.create(name="p2-progress", hpgl=PAYLOAD, paper="a4")
    worker.submit("prepare", job.id)
    _wait(jobs, job.id, {JobState.READY, JobState.FAILED})
    worker.submit("start", job.id)
    done = _wait(jobs, job.id,
                 {JobState.COMPLETED, JobState.FAILED, JobState.DISCONNECTED})
    assert done.status == JobState.COMPLETED, done.error

    with lock:
        prog = [e for e in events
                if e.get("event") == "progress" and e["job_id"] == job.id]
    assert len(prog) >= 2, f"expected throttled progress stream, got {len(prog)}"

    # SENDING-phase events: buffer accounting only — pen_down always None
    total = len(PAYLOAD.encode())
    sending = [e for e in prog if e["acked_bytes"] < total]
    assert all(e["pen_down"] is None for e in sending), \
        "mid-stream pen_down would imply OA/OS polling (sentinel hazard)"
    assert sending[0]["acked_bytes"] <= sending[-1]["acked_bytes"]
    assert any(e["acked_bytes"] == total for e in prog), "no final 100% event"

    # COMPLETING-phase: at least one OS;-derived pen event (bool, not None)
    assert any(isinstance(e["pen_down"], bool) for e in prog), \
        "no pen state observed during COMPLETING OS; polls"

    # sentinel intact: zero error conditions on the device, pen parked
    fake.wait_idle(timeout=10.0)
    assert fake.hpgl_error == 0 and fake.rs232_error == 0
    assert fake.pen == 0
    # the round-tripped VS stuck (fake accepts it, no error)
    assert fake.velocity == pytest.approx(20.14)
