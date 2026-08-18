"""Phase 7 — end-to-end over the PTY fake plotter.

Unlike the API tests (fake streamer), these run the REAL production path:
HardwareWorker → ChunkedStreamer → SerialTransport → pty → FakeHP7475A.
No HTTP layer; this pins the hardware-facing behavior (spec §35, §45).
"""

from __future__ import annotations

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


@pytest.fixture()
def fake(request):
    fake = FakeHP7475A()
    fake.start()
    request.addfinalizer(fake.stop)
    return fake


@pytest.fixture()
def stack(tmp_path, fake):
    settings = Settings(
        data_dir=tmp_path,
        stream_default_chunk=64,       # force many chunks
        stream_query_timeout_s=1.0,
        completion_timeout_s=10.0,
    )
    db = Database(settings.db_path)
    jobs = JobStore(db, history_keep=10)
    devices = DeviceManager()
    devices.connect(
        fake.port_path, {"baudrate": 9600}, driver_factory=_fake_driver
    )
    worker = HardwareWorker(jobs, devices, settings)
    worker.start()
    yield settings, db, jobs, devices, worker
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


def _wait_status(jobs, job_id, want, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = jobs.get(job_id)
        if job.status in want:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job stuck in {jobs.get(job_id).status}, wanted {want}")


PAYLOAD = "IN;SP1;PU500,500;" + "".join(
    f"PD{500 + i * 10},{600 + (i % 7) * 10};" for i in range(60)
) + "PU0,0;SP0;"


def test_e2e_happy_path_completes_after_device_execution(stack, fake):
    settings, db, jobs, devices, worker = stack
    job = jobs.create(name="e2e", hpgl=PAYLOAD, paper="a4")
    worker.submit("prepare", job.id)
    _wait_status(jobs, job.id, {JobState.READY, JobState.FAILED})
    assert jobs.get(job.id).status == JobState.READY

    worker.submit("start", job.id)
    done = _wait_status(
        jobs, job.id,
        {JobState.COMPLETED, JobState.FAILED, JobState.DISCONNECTED},
        timeout=30.0,
    )
    assert done.status == JobState.COMPLETED, done.error
    # bytes accounting: everything sent
    assert done.bytes_sent == len(PAYLOAD.encode())
    # device actually executed the payload (fake drains by exec delay)
    fake.wait_idle(timeout=10.0)
    assert fake.pen == 0  # parked


def test_e2e_cancel_during_plot(stack, fake):
    settings, db, jobs, devices, worker = stack
    fake.set_exec_delay(0.15)  # slow execution → cancel window
    job = jobs.create(name="cancel", hpgl=PAYLOAD, paper="a4")
    worker.submit("prepare", job.id)
    _wait_status(jobs, job.id, {JobState.READY, JobState.FAILED})
    worker.submit("start", job.id)
    # wait until sending has begun, then cancel
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if jobs.get(job.id).bytes_sent > 0:
            break
        time.sleep(0.02)
    worker.submit("cancel", job.id)
    done = _wait_status(
        jobs, job.id, {JobState.CANCELLED, JobState.FAILED, JobState.COMPLETED},
        timeout=20.0,
    )
    # cancel must win unless the plot had already finished
    assert done.status in (JobState.CANCELLED, JobState.COMPLETED)


def test_e2e_disconnect_marks_disconnected(stack, fake):
    settings, db, jobs, devices, worker = stack
    fake.set_exec_delay(0.1)
    job = jobs.create(name="disc", hpgl=PAYLOAD, paper="a4")
    worker.submit("prepare", job.id)
    _wait_status(jobs, job.id, {JobState.READY, JobState.FAILED})
    worker.submit("start", job.id)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if jobs.get(job.id).bytes_sent > 0:
            break
        time.sleep(0.02)
    fake.fault_disconnect()
    done = _wait_status(
        jobs, job.id,
        {JobState.DISCONNECTED, JobState.FAILED, JobState.CANCELLED},
        timeout=20.0,
    )
    assert done.status == JobState.DISCONNECTED, f"{done.status}: {done.error}"


def test_e2e_timeout_fault_fails_job(stack, fake):
    settings, db, jobs, devices, worker = stack
    fake.fault_timeout()
    job = jobs.create(name="tmo", hpgl=PAYLOAD, paper="a4")
    worker.submit("prepare", job.id)
    _wait_status(jobs, job.id, {JobState.READY, JobState.FAILED})
    worker.submit("start", job.id)
    done = _wait_status(
        jobs, job.id, {JobState.FAILED, JobState.DISCONNECTED}, timeout=30.0
    )
    assert done.status == JobState.FAILED
    assert done.error


def test_e2e_status_polls_during_stream_do_not_touch_port(stack, fake):
    """Live regression (2026-08-18): browser status polls mid-plot injected
    OS;/buffer queries into the stream, crossing replies and aborting the
    job after ~900 bytes. With the streaming guard, polls during SENDING
    return cached data and the plot must still complete with zero
    rs232-error conditions on the device."""
    settings, db, jobs, devices, worker = stack
    def _wait(job_id, want, timeout=120.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            j = jobs.get(job_id)
            if j.status in want:
                return j
            time.sleep(0.05)
        raise AssertionError(f"stuck in {jobs.get(job_id).status}, wanted {want}")

    payload = "IN;SP1;PU100,100;" + "PD200,300;PD300,100;" * 400 + "PU0,0;SP0;"
    job = jobs.create(name="poll-under-stream", hpgl=payload, paper="a4")
    worker.submit("prepare", job.id)
    _wait(job.id, {JobState.READY, JobState.FAILED})
    worker.submit("start", job.id)

    # hammer the status route's manager path while the stream runs
    import threading
    stop = threading.Event()
    polled = {"n": 0, "touched_port": 0}

    def poller():
        while not stop.is_set():
            try:
                s = devices.status()
                polled["n"] += 1
                if not s.get("stale"):
                    polled["touched_port"] += 1
            except Exception:
                pass
            time.sleep(0.01)

    t = threading.Thread(target=poller, daemon=True)
    t.start()
    done = _wait(job.id, {JobState.COMPLETED, JobState.FAILED,
                          JobState.DISCONNECTED})
    stop.set()
    t.join(timeout=2)

    assert done.status == JobState.COMPLETED, done.error
    assert done.bytes_sent == len(payload.encode())
    assert fake.rs232_error == 0
    assert polled["n"] > 0  # polls actually ran during the stream
