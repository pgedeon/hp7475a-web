"""E2E: a rotate_90 pipeline output plots clean on the fake plotter."""

from __future__ import annotations

import pytest

from app.jobs.models import JobState
from app.services.pipeline.vpy import PipelineOptions, run_pipeline
from tests.pipeline.conftest import SVG_FIXTURES

from .test_pty_e2e import _wait_status, fake, stack  # noqa: F401


def test_rotated_job_plots_with_zero_errors(stack, fake):  # noqa: F811
    settings, db, jobs, devices, worker = stack
    result = run_pipeline(
        SVG_FIXTURES / "benign.svg", "a4", PipelineOptions(rotate_90=True), {"1": 1, "2": 2, "3": 3}
    )
    job = jobs.create(name="rot-e2e", hpgl=result.hpgl, paper="a4")
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
    fake.wait_idle(timeout=10.0)
    assert fake.hpgl_error == 0  # OE-style HP-GL error never raised
    assert fake.rs232_error == 0
