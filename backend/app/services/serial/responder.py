"""Response reading/parsing layer for the HP 7475A serial link.

Sits directly on top of a pyserial port. All plotter replies on RS-232-C are
ASCII lines terminated by CR (default output terminator, Prog. Manual Ch.7).
This module never sends data; it only reads and interprets replies, with
bounded timeouts and retries handled by the caller (transport).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.services.serial import protocol

logger = logging.getLogger(__name__)


class ResponderError(RuntimeError):
    """Raised when the plotter's reply cannot be obtained or parsed."""


@dataclass(frozen=True)
class StatusReport:
    status_byte: int
    pen_down: bool
    p1p2_changed: bool
    digitize_ready: bool
    initialized: bool
    ready: bool
    error: bool
    rsv: bool

    @classmethod
    def from_byte(cls, value: int) -> "StatusReport":
        return cls(
            status_byte=value,
            pen_down=bool(value & protocol.STATUS_PEN_DOWN),
            p1p2_changed=bool(value & protocol.STATUS_P1P2_CHANGED),
            digitize_ready=bool(value & protocol.STATUS_DIGITIZE_READY),
            initialized=bool(value & protocol.STATUS_INITIALIZED),
            ready=bool(value & protocol.STATUS_READY),
            error=bool(value & protocol.STATUS_ERROR),
            rsv=bool(value & protocol.STATUS_RSV),
        )


_INT_RE = re.compile(r"^\s*(-?\d+)\s*$")
_FLOAT_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*$")
_POSITION_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?),\s*(\d+)\s*$")
_OPTIONS_RE = re.compile(r"^\s*\d+(?:\s*,\s*\d+){7}\s*$")


class Responder:
    """Reads one CR-terminated reply line at a time from the serial port."""

    def __init__(self, port, timeout: float = 2.0):
        """``port`` is an open pyserial Serial instance (or test double with
        the same read() semantics)."""
        self._port = port
        self._timeout = timeout
        self._buf = bytearray()

    # -- low level ---------------------------------------------------------

    def read_line(self, timeout: float | None = None) -> str:
        """Read until CR (or LF), strip it, return the payload.

        Raises ResponderError on timeout. Never loops forever: bounded by
        timeout, not by buffer size.
        """
        eff_timeout = self._timeout if timeout is None else timeout
        deadline = None if eff_timeout is None else _monotonic() + eff_timeout
        while True:
            idx = self._find_terminator()
            if idx >= 0:
                line = bytes(self._buf[:idx])
                del self._buf[: idx + 1]
                return line.decode("ascii", errors="replace").strip("\r\n")
            remaining = None if deadline is None else deadline - _monotonic()
            if remaining is not None and remaining <= 0:
                raise ResponderError(
                    f"Timeout after {eff_timeout}s waiting for plotter reply "
                    f"(buffer has {len(self._buf)} bytes)"
                )
            to_read = max(1, self._port.inWaiting()) if hasattr(self._port, "inWaiting") else 1
            chunk_timeout = remaining if remaining is not None else eff_timeout
            if chunk_timeout is not None and chunk_timeout < 0:
                chunk_timeout = 0
            data = self._port.read(max(1, min(to_read, 64)))
            if data:
                self._buf.extend(data)

    def _find_terminator(self) -> int:
        for i, b in enumerate(self._buf):
            if b in (0x0D, 0x0A):
                return i
        return -1

    def drain(self) -> None:
        """Discard any buffered/pending input (noise, stale replies)."""
        self._buf.clear()
        try:
            waiting = self._port.inWaiting()
        except Exception:
            waiting = 0
        if waiting:
            self._port.read(waiting)

    # -- typed parsers (send nothing; parse one reply line each) -----------

    def parse_identification(self, line: str) -> str:
        return line.strip()

    def parse_position(self, line: str) -> tuple[float, float, bool]:
        m = _POSITION_RE.match(line)
        if not m:
            raise ResponderError(f"Malformed position reply: {line!r}")
        x, y, p = float(m.group(1)), float(m.group(2)), m.group(3) == "1"
        return x, y, p

    def parse_int(self, line: str, field: str) -> int:
        m = _INT_RE.match(line)
        if not m:
            raise ResponderError(f"Malformed {field} reply: {line!r}")
        return int(m.group(1))

    def parse_float(self, line: str, field: str) -> float:
        m = _FLOAT_RE.match(line)
        if not m:
            raise ResponderError(f"Malformed {field} reply: {line!r}")
        return float(m.group(1))

    def parse_status(self, line: str) -> StatusReport:
        value = self.parse_int(line, "status")
        if not 0 <= value <= 255:
            raise ResponderError(f"Status byte out of range 0-255: {value}")
        return StatusReport.from_byte(value)

    def parse_buffer_space(self, line: str) -> int:
        value = self.parse_int(line, "buffer space")
        if not 0 <= value <= protocol.INPUT_BUFFER_BYTES:
            raise ResponderError(
                f"Buffer space reply out of range 0-{protocol.INPUT_BUFFER_BYTES}: {value}"
            )
        return value

    def parse_extended_error(self, line: str) -> tuple[int, str]:
        value = self.parse_int(line, "extended error")
        meaning = protocol.RS232_ERRORS.get(value)
        if meaning is None:
            raise ResponderError(f"Unknown extended error code: {value}")
        return value, meaning

    def parse_hpgl_error(self, line: str) -> tuple[int, str]:
        value = self.parse_int(line, "HP-GL error")
        meaning = protocol.HPGL_ERRORS.get(value)
        if meaning is None:
            raise ResponderError(f"Unknown HP-GL error code: {value}")
        return value, meaning

    def parse_options(self, line: str) -> list[int]:
        if not _OPTIONS_RE.match(line):
            raise ResponderError(f"Malformed options reply: {line!r}")
        return [int(v.strip()) for v in line.split(",")]


def _monotonic() -> float:
    import time

    return time.monotonic()
