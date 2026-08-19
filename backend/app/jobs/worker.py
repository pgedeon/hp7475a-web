"""Single-writer hardware worker (spec §11).

One background thread owns the serial device exclusively. Jobs move through
the state machine; the worker executes: PREPARING (pipeline output already
attached) → READY (await user start) → SENDING/PLOTTING (chunked stream) →
COMPLETING (queued OA sentinel) → COMPLETED. Pause = stop feeding (thread
waits); cancel = stop feeding + mark CANCELLED (+ optional device IN;SP0
reset AFTER buffered motion drains, per docs/hardware-notes.md §9).
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Optional

from app.jobs.models import IllegalTransition, Job, JobState
from app.jobs.store import JobNotFound, JobStore
from app.jobs.streamer import ChunkedStreamer, StreamInterrupted, StreamerFatal
from app.services.serial.transport import DeviceDisconnected

logger = logging.getLogger(__name__)


class WorkerCommand:
    PREPARE = "prepare"
    START = "start"
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"
    RESET_DEVICE = "reset_device"  # IN; + park after drain


class HardwareWorker:
    """Serializes ALL plotter access. API layer enqueues commands; worker
    thread performs them against the driver."""

    def __init__(self, store: JobStore, device_holder, settings):
        self._store = store
        self._devices = device_holder  # DeviceManager (main lane)
        self._settings = settings
        self._queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._cancel = threading.Event()
        self._current_job: str | None = None
        self._lock = threading.RLock()

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="hp7475a-worker", daemon=True)
        self._thread.start()

    def shutdown(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._cancel.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    # -- command interface (called from API handlers) ---------------------------

    def submit(self, command: str, job_id: str) -> None:
        if command in (WorkerCommand.START, WorkerCommand.PREPARE):
            # exclusive: reject if a job is already active
            with self._lock:
                if self._current_job and self._current_job != job_id:
                    active = self._store.get(self._current_job)
                    if active.status in {
                        JobState.PREPARING, JobState.READY, JobState.SENDING,
                        JobState.PLOTTING, JobState.COMPLETING, JobState.PAUSED,
                    }:
                        raise IllegalTransition(
                            f"another job {self._current_job} is {active.status.value}"
                        )
                self._current_job = job_id
        if command == WorkerCommand.PAUSE:
            self._pause.set()
            return
        if command == WorkerCommand.CANCEL:
            self._cancel.set()
            return
        if command == WorkerCommand.RESUME:
            self._pause.clear()
            return
        self._queue.put((command, job_id))

    @property
    def current_job_id(self) -> Optional[str]:
        with self._lock:
            return self._current_job

    # -- worker loop --------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                command, job_id = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                if command == WorkerCommand.PREPARE:
                    self._do_prepare(job_id)
                elif command == WorkerCommand.START:
                    self._do_start(job_id)
                elif command == WorkerCommand.RESET_DEVICE:
                    self._do_reset_device()
                else:
                    logger.error("unknown worker command %r", command)
            except Exception:
                logger.exception("worker command %s(%s) failed", command, job_id)
            finally:
                with self._lock:
                    if self._current_job == job_id and command in (
                        WorkerCommand.START, WorkerCommand.PREPARE
                    ):
                        self._current_job = None

    def _do_prepare(self, job_id: str) -> None:
        """Validate device is connected + HP-GL non-empty → READY."""
        job = self._store.get(job_id)
        self._store.set_state(job_id, JobState.PREPARING)
        try:
            if not self._devices.is_connected():
                raise RuntimeError("device not connected")
            # Paper/plotter containment (2026-08-18 vertical-lines fix):
            # an A3 job on an A4-DIP plotter clamps coords into garbage
            # edge lines. A failed clip QUERY is non-fatal (skip validation)
            # — TransportError IS a RuntimeError subclass, so catch it
            # FIRST or a crossed reply fails the job (observed live:
            # "bad OH reply: '16'" — an OS; poll's reply eaten by OH;).
            from app.services.serial.transport import TransportError

            try:
                clip = self._devices.hard_clip_limits()
                from app.services.serial.paper import clip_fits, get_paper

                if not clip_fits(get_paper(job.paper), tuple(clip["limits"])):
                    raise RuntimeError(
                        f"job paper {job.paper!r} exceeds the plotter's "
                        f"plottable area (plotter reports {clip.get('paper')!r})"
                        f" — coordinates would be clamped; re-create the job "
                        f"with matching paper"
                    )
            except TransportError:
                pass  # query flaked: skip validation, never fail the job
            if not job.hpgl.strip():
                raise RuntimeError("job has no HP-GL payload (run the pipeline first)")
            self._store.update(job_id, bytes_total=len(job.hpgl.encode("ascii")), bytes_sent=0)
            self._store.set_state(job_id, JobState.READY)
        except Exception as exc:
            self._store.set_state(job_id, JobState.FAILED, error=str(exc))

    def _do_start(self, job_id: str) -> None:
        job = self._store.get(job_id)
        if job.status == JobState.PAUSED:
            # resume path
            self._safe_set_state(job_id, JobState.SENDING)
        elif job.status == JobState.READY:
            self._safe_set_state(job_id, JobState.SENDING)
        driver = self._devices.driver()
        if driver is None:
            self._safe_set_state(job_id, JobState.FAILED, error="device not connected")
            return
        payload = job.hpgl
        transport = driver.transport
        streamer = ChunkedStreamer(
            transport,
            safety_margin=self._settings.stream_safety_margin,
            default_chunk=self._settings.stream_default_chunk,
            query_timeout_s=self._settings.stream_query_timeout_s,
            max_retries=self._settings.stream_max_retries,
            on_progress=lambda p: self._store.update(
                job_id, bytes_sent=p.bytes_sent,
                stats={**job.stats, "last_free": p.free_buffer},
            ),
        )
        self._pause.clear()
        self._cancel.clear()
        self._devices.set_streaming(True)  # serial lane owned by the stream
        try:
            self._stream_with_pause_support(streamer, payload, job_id)
            self._safe_set_state(job_id, JobState.PLOTTING)  # all bytes buffered/accepted
            self._safe_set_state(job_id, JobState.COMPLETING)
            driver.complete_plot(timeout=self._settings.completion_timeout_s)
            self._safe_set_state(job_id, JobState.COMPLETED)
            self._store.prune_history()
        except StreamInterrupted as exc:
            reason = str(exc)
            if "paused" in reason:
                self._safe_set_state(job_id, JobState.PAUSED)
                self._wait_while_paused(job_id)
                # resume: re-stream from current offset
                self._resume_stream(job_id)
            elif "cancelled" in reason:
                self._safe_set_state(job_id, JobState.CANCELLED)
            else:
                self._safe_set_state(job_id, JobState.FAILED, error=reason)
        except StreamerFatal as exc:
            self._safe_set_state(job_id, JobState.FAILED, error=str(exc))
        except DeviceDisconnected:
            self._safe_set_state(job_id, JobState.DISCONNECTED, error="device disconnected")
        except Exception as exc:
            self._safe_set_state(job_id, JobState.FAILED, error=f"{type(exc).__name__}: {exc}")
        finally:
            self._devices.set_streaming(False)

    def _safe_set_state(self, job_id: str, state, error: str | None = None) -> None:
        """set_state that tolerates the job being deleted mid-run (a DELETE
        during an active stream is a legal race; the worker must not spew
        tracebacks when its final transition finds the job gone)."""
        try:
            self._store.set_state(job_id, state, error=error)
        except Exception:
            logger.debug("job %s vanished before %s transition", job_id, getattr(state, "value", state))

    def _stream_with_pause_support(self, streamer, payload, job_id) -> None:
        """Run stream() in a helper thread so pause/cancel events can break
        a blocking write/query promptly; re-raises its outcome."""
        outcome: dict = {}

        def runner():
            try:
                streamer.stream(
                    payload,
                    pause_event=self._pause,
                    cancel_event=self._cancel,
                    should_run=lambda: not self._stop.is_set(),
                )
                outcome["ok"] = True
            except (StreamInterrupted, StreamerFatal, DeviceDisconnected) as exc:
                outcome["exc"] = exc
            except Exception as exc:  # unexpected → fatal classification
                outcome["exc"] = StreamerFatal(str(exc))

        t = threading.Thread(target=runner, name=f"stream-{job_id}", daemon=True)
        t.start()
        t.join()
        if "exc" in outcome:
            raise outcome["exc"]

    def _wait_while_paused(self, job_id: str) -> None:
        while self._pause.is_set() and not self._cancel.is_set() and not self._stop.is_set():
            threading.Event().wait(0.2)

    def _resume_stream(self, job_id: str) -> None:
        job = self._store.get(job_id)
        if job.status == JobState.CANCELLED or self._cancel.is_set():
            return
        self._store.set_state(job_id, JobState.SENDING)
        payload = job.hpgl[job.bytes_sent :] if job.bytes_sent < len(job.hpgl) else ""
        driver = self._devices.driver()
        streamer = ChunkedStreamer(
            driver.transport,
            safety_margin=self._settings.stream_safety_margin,
            default_chunk=self._settings.stream_default_chunk,
            on_progress=lambda p: self._store.update(
                job_id, bytes_sent=job.bytes_sent + p.bytes_sent,
                stats={**job.stats, "last_free": p.free_buffer, "resumed": True},
            ),
        )
        try:
            self._stream_with_pause_support(streamer, payload, job_id)
            self._store.set_state(job_id, JobState.PLOTTING)
            self._store.set_state(job_id, JobState.COMPLETING)
            driver.complete_plot(timeout=self._settings.completion_timeout_s)
            self._store.set_state(job_id, JobState.COMPLETED)
        except StreamInterrupted as exc:
            reason = str(exc)
            if "paused" in reason:
                self._store.set_state(job_id, JobState.PAUSED)
            elif "cancelled" in reason:
                self._store.set_state(job_id, JobState.CANCELLED)
            else:
                self._store.set_state(job_id, JobState.FAILED, error=reason)
        except Exception as exc:
            self._store.set_state(job_id, JobState.FAILED, error=str(exc))

    def _do_reset_device(self) -> None:
        driver = self._devices.driver()
        if driver is None:
            return
        try:
            driver.initialize_device()  # IN; (clears error state)
            driver.park()  # PU corner + SP0 after motion drains
        except Exception:
            logger.exception("device reset failed")
