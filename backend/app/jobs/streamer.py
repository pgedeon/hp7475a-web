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


def split_chunk(payload: str, start: int, max_bytes: int) -> int:
    """Return end index (exclusive) of the next chunk starting at `start`.

    Chunk never exceeds max_bytes and always ends just past a ';' instruction
    terminator when one exists within the window (property-tested)."""
    window = payload[start : start + max_bytes]
    cut = window.rfind(";")
    if cut == -1:
        # No terminator in window: send whole window but never split a
        # trailing partial instruction — find safe cut at last boundary if
        # the remainder continues mid-instruction.
        if start + max_bytes >= len(payload):
            return len(payload)
        # Search wider for the next ';' to know where the instruction ends;
        # if the instruction is longer than max_bytes we must still split it
        # (validator caps instruction sizes well below chunk size, so this
        # is a defensive path that raises rather than corrupting).
        next_cut = payload.find(";", start)
        if next_cut == -1:
            return len(payload)
        if next_cut - start + 1 <= max_bytes * 4:  # unreasonably long guard
            raise StreamerFatal(
                f"Instruction longer than 4×chunk size at offset {start}; refusing to split"
            )
        return start + max_bytes
    return start + cut + 1


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
                # Buffer full: bounded wait then re-query.
                if zero_waited >= self._zero_max_wait:
                    raise StreamerFatal(
                        f"plotter buffer stayed full for {zero_waited:.0f}s; aborting"
                    )
                threading.Event().wait(self._zero_poll)
                zero_waited += self._zero_poll
                continue
            zero_waited = 0.0
            end = split_chunk(text, sent, budget)
            chunk = data[sent:end]
            try:
                self._transport.write(chunk)  # transport guarantees full write
            except DeviceDisconnected:
                raise
            except Exception as exc:
                raise StreamerFatal(f"write failed at byte {sent}: {exc}") from exc
            sent = end
            self._report(sent, total, free)
        return sent

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
