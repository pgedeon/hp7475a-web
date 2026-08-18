"""Long-plot + pause/resume stress on the PTY fake (goal 3e598c6e)."""

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
        stream_default_chunk=256,
        stream_query_timeout_s=1.0,
        completion_timeout_s=120.0,
    )
    db = Database(settings.db_path)
    jobs = JobStore(db, history_keep=10)
    devices = DeviceManager()
    devices.connect(fake.port_path, {"baudrate": 9600}, driver_factory=_fd)
    worker = HardwareWorker(jobs, devices, settings)
    worker.start()
    yield settings, jobs, worker
    worker.shutdown()
    devices.disconnect()
    db.close()


def _fd(port_path, settings=None):
    from app.services.serial.driver import HP7475ADevice

    return HP7475ADevice(port_path, settings)


def _wait(jobs, job_id, want, timeout=120.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = jobs.get(job_id)
        if job.status in want:
            return job
        time.sleep(0.05)
    raise AssertionError(f"stuck in {jobs.get(job_id).status}, wanted {want}")


def _big_payload(n=10000):
    rows = [f"PD{100 + (i % 9000)},{100 + ((i * 7) % 7000)};" for i in range(n)]
    return "IN;SP1;PU100,100;" + "".join(rows) + "PU0,0;SP0;"


def test_long_plot_10k_instructions(stack, fake):
    """~75KB HP-GL, hundreds of ESC.B round-trips, must complete without
    buffer overflow (rs232_error stays 0) and with exact byte accounting."""
    settings, jobs, worker = stack
    payload = _big_payload(10000)
    job = jobs.create(name="stress-10k", hpgl=payload, paper="a4")
    worker.submit("prepare", job.id)
    _wait(jobs, job.id, {JobState.READY, JobState.FAILED})
    worker.submit("start", job.id)
    done = _wait(jobs, job.id, {JobState.COMPLETED, JobState.FAILED, JobState.DISCONNECTED})
    assert done.status == JobState.COMPLETED, done.error
    assert done.bytes_sent == len(payload.encode())
    fake.wait_idle(timeout=15.0)
    assert fake.rs232_error == 0


def test_pause_resume_mid_plot_completes(stack, fake):
    settings, jobs, worker = stack
    fake.set_exec_delay(0.01)
    payload = _big_payload(2000)
    job = jobs.create(name="pause-resume", hpgl=payload, paper="a4")
    worker.submit("prepare", job.id)
    _wait(jobs, job.id, {JobState.READY, JobState.FAILED})
    worker.submit("start", job.id)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if jobs.get(job.id).bytes_sent > 500:
            break
        time.sleep(0.02)
    worker.submit("pause", job.id)
    _wait(jobs, job.id, {JobState.PAUSED, JobState.COMPLETED}, timeout=30)
    time.sleep(0.3)
    worker.submit("resume", job.id)
    done = _wait(jobs, job.id, {JobState.COMPLETED, JobState.FAILED, JobState.DISCONNECTED}, timeout=180)
    assert done.status == JobState.COMPLETED, done.error
    assert done.bytes_sent == len(payload.encode())


def test_three_jobs_run_serially(stack, fake):
    settings, jobs, worker = stack
    payload = _big_payload(800)
    ids = [jobs.create(name=f"q{i}", hpgl=payload, paper="a4").id for i in range(3)]
    for jid in ids:
        worker.submit("prepare", jid)
        _wait(jobs, jid, {JobState.READY, JobState.FAILED})
        worker.submit("start", jid)
        done = _wait(jobs, jid, {JobState.COMPLETED, JobState.FAILED, JobState.DISCONNECTED}, timeout=120)
        assert done.status == JobState.COMPLETED, done.error


def test_giant_single_instruction_plots(stack, fake):
    """Regression (live 2026-08-18): vpype-optimized output contains single
    PD polylines > the 1024B plotter buffer. The 7475A parses HP-GL
    incrementally — such instructions MUST plot (mid-instruction chunking),
    not abort. Mirrors the failed a4_impossible_geometry job: a 1542B PD
    at offset 32617 previously raised StreamerFatal."""
    settings, jobs, worker = stack
    # one ~5.6KB PD instruction (vpype linemerge-style giant polyline)
    giant = "PD" + ",".join(
        f"{100 + (i % 9000)},{100 + ((i * 7) % 7000)}" for i in range(400)
    ) + ";"
    assert len(giant) > 1024
    payload = "IN;SP1;PU100,100;" + giant + "PU0,0;SP0;"
    job = jobs.create(name="giant-polyline", hpgl=payload, paper="a4")
    worker.submit("prepare", job.id)
    _wait(jobs, job.id, {JobState.READY, JobState.FAILED})
    worker.submit("start", job.id)
    done = _wait(jobs, job.id, {JobState.COMPLETED, JobState.FAILED,
                                JobState.DISCONNECTED}, timeout=120)
    assert done.status == JobState.COMPLETED, done.error
    assert done.bytes_sent == len(payload.encode())
    assert fake.rs232_error == 0
    # the plotter executed the full polyline: one PD token in its log
    pds = [c for c in fake.commands if c.startswith("PD")]
    assert len(pds) == 1 and len(pds[0]) == len(giant)
