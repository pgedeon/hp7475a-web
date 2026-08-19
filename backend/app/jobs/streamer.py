"""Buffer-safe HP-GL streamer (spec §12-13, docs/hardware-notes.md §6).

Feeds a job's HP-GL payload to the plotter using the software-checking
handshake: query ESC.B → send at most (free − margin) bytes ending on an
instruction boundary → repeat. Progress reported via callback; pause/cancel
via threading.Event. Completion is *device-level*: after the last byte we
queue an OA sentinel and wait for its reply (output instructions answer in
parse order — Prog. Manual Ch.7/10), bounded by completion_timeout_s.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable, Protocol

from app.services.serial import protocol
from app.services.serial.transport import DeviceDisconnected

logger = logging.getLogger(__name__)


class StreamInterrupted(RuntimeError):
    """Raised when streaming stops early (cancelled, paused, or fatal)."""


class StreamerFatal(StreamInterrupted):
    """Unrecoverable stream failure (timeout retries exhausted, overflow,
    disconnect)."""


@dataclass
class StreamProgress:
    bytes_sent: int
    bytes_total: int
    free_buffer: int | None = None


class TransportLike(Protocol):
    def query(self, data: bytes, timeout: float, retries: int) -> str: ...
    def write(self, data: bytes) -> int: ...
    def extended_error(self) -> tuple[int, str]: ...


def split_chunk(payload: str, start: int, max_bytes: int) -> int:
    """Return end index (exclusive) of the next chunk starting at `start`.

    Prefer ending just past a ';' inside the window (clean pause/resume
    points); if none fits, split mid-instruction — the 7475A parses HP-GL
    incrementally (Prog. Manual §buffering), so an instruction may be far
    larger than the 1024B buffer; only its unprocessed tail needs room.
    Returns -1 only when the window is empty (caller waits for the buffer
    to drain)."""
    window_end = min(start + max_bytes, len(payload))
    if window_end == start:
        return -1
    cut = payload.rfind(";", start, window_end)
    if cut != -1:
        return cut + 1
    # No terminator in window: NEVER split mid-instruction. Field evidence
    # (2026-08-19 zigzag canary): a long PD delivered in mid-instruction
    # pieces with ESC.B polls between them was misparsed by the real 7475A
    # as OE error 2 ("wrong number of parameters") despite byte-perfect
    # delivery; every plot since the phase-2 streamer landed was corrupted
    # this way. An instruction is atomic — wait for a bigger window.
    return -1


class ChunkedStreamer:
    def __init__(
        self,
        transport: TransportLike,
        *,
        safety_margin: int = 32,
        default_chunk: int = 512,
        query_timeout_s: float = 2.0,
        max_retries: int = 3,
        zero_free_poll_s: float = 0.25,
        zero_free_max_wait_s: float = 60.0,
        on_progress: Callable[[StreamProgress], None] | None = None,
    ):
        self._transport = transport
        self._margin = safety_margin
        self._chunk = default_chunk
        self._query_timeout = query_timeout_s
        self._retries = max_retries
        self._zero_poll = zero_free_poll_s
        self._zero_max_wait = zero_free_max_wait_s
        self._on_progress = on_progress

    def stream(
        self,
        payload: str,
        *,
        pause_event: threading.Event,
        cancel_event: threading.Event,
        should_run: Callable[[], bool] | None = None,
    ) -> int:
        """Send `payload` buffer-safely. Returns bytes sent. Raises
        StreamInterrupted(paused/cancelled) or StreamerFatal."""
        data = payload.encode("ascii")
        total = len(data)
        sent = 0
        text = payload
        zero_waited = 0.0
        try:
            while sent < total:
                if cancel_event.is_set():
                    raise StreamInterrupted("cancelled by user")
                if pause_event.is_set():
                    raise StreamInterrupted("paused by user")
                if should_run is not None and not should_run():
                    raise StreamInterrupted("worker stopped")
                free = self._query_free()
                self._report(sent, total, free)
                budget = min(free - self._margin, self._chunk)
                if budget <= 0:
                    # Buffer full: overflow check, then bounded wait.
                    self._check_overflow()
                    if zero_waited >= self._zero_max_wait:
                        raise StreamerFatal(
                            f"plotter buffer stayed full for {zero_waited:.0f}s; aborting"
                        )
                    threading.Event().wait(self._zero_poll)
                    zero_waited += self._zero_poll
                    continue
                end = split_chunk(text, sent, budget)
                if end == -1:
                    # The default chunk can't reach a boundary; retry with
                    # the FULL safe window (free - margin) — a longer chunk
                    # is fine when the buffer actually has room (phase-1
                    # fallback semantics, boundary-only).
                    end = split_chunk(text, sent, max(free - self._margin, budget))
                if end == -1:
                    # No instruction boundary fits ANY window. If the next
                    # instruction can never fit even a drained buffer, fail
                    # loudly (raw uploads with monster instructions — the
                    # pipeline pre-splits to <=240B, raw ones now do too).
                    nxt = text.find(";", sent)
                    instr_len = (nxt + 1 if nxt != -1 else len(text)) - sent
                    if instr_len > protocol.INPUT_BUFFER_BYTES - self._margin:
                        raise StreamerFatal(
                            f"single instruction of {instr_len}B exceeds the "
                            f"{protocol.INPUT_BUFFER_BYTES - self._margin}B safe "
                            f"window; split the file's long PD/PU commands"
                        )
                    if zero_waited >= self._zero_max_wait:
                        raise StreamerFatal(
                            f"plotter buffer stayed full for {zero_waited:.0f}s; aborting"
                        )
                    self._check_overflow()
                    threading.Event().wait(self._zero_poll)
                    zero_waited += self._zero_poll
                    continue
                zero_waited = 0.0
                chunk = data[sent:end]
                try:
                    self._transport.write(chunk)  # transport guarantees full write
                except DeviceDisconnected:
                    raise
                except Exception as exc:
                    raise StreamerFatal(f"write failed at byte {sent}: {exc}") from exc
                sent = end
                self._report(sent, total, free)
            self._check_overflow()  # final: catch any mid-stream overflow
            return sent
        except StreamInterrupted as exc:
            if "cancelled" in str(exc) and sent < total:
                # A cancel can land mid-instruction; close the dangling
                # instruction so the plotter's parser resyncs cleanly at the
                # next job (best-effort — never masks the cancellation).
                try:
                    self._transport.write(b";")
                except Exception:
                    pass
            raise

    def _check_overflow(self) -> None:
        """ESC .E watchdog: error 16 = input buffer overflow (Prog. Manual
        §10-29). The phase-1 sender aborted on it; restoring that here —
        a silent overflow mid-PD corrupts the plot (horizontal garbage
        lines, 2026-08-19 incident). Best-effort: query failure alone
        never kills the stream; a definitive 16 does."""
        try:
            code, meaning = self._transport.extended_error()
        except Exception:
            return
        if code == 16:
            raise StreamerFatal(f"plotter input buffer overflow (ESC.E 16): {meaning}")

    def _query_free(self) -> int:
        """ESC.B query with bounded retries. Returns free bytes 0..1024.
        Retries transient timeouts; a reply we cannot parse is fatal."""
        last_exc: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                reply = self._transport.query(
                    protocol.ESC_OUTPUT_BUFFER_SPACE.encode("ascii"),
                    timeout=self._query_timeout,
                    retries=0,
                )
                value = int(reply.strip())
                if not 0 <= value <= protocol.INPUT_BUFFER_BYTES:
                    raise StreamerFatal(
                        f"ESC.B reply out of range: {value}"
                    )
                return value
            except StreamerFatal:
                raise
            except DeviceDisconnected:
                raise
            except Exception as exc:
                last_exc = exc
                logger.warning("ESC.B attempt %d failed: %s", attempt + 1, exc)
                threading.Event().wait(0.1 * (attempt + 1))
        raise StreamerFatal(f"ESC.B query failed after {self._retries + 1} attempts: {last_exc}")

    def _report(self, sent: int, total: int, free: int | None) -> None:
        if self._on_progress:
            self._on_progress(StreamProgress(sent, total, free))
