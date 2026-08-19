"""HP 7475A device driver (BUILD_SPEC §9; Prog. Manual Ch.1-3, 7).

Typed, safety-checked façade over :class:`SerialTransport`. Constant source
for every byte sequence is :mod:`app.services.serial.protocol` (spec §9: no
raw escape/HP-GL literals here).

Safety contract:
- ``connect()`` performs drain + identify verification only — **no pen
  motion** (BUILD_SPEC §8: never draw during connection).
- ``move_abs`` clamps to the configured paper's hard-clip limits
  (Prog. Manual §7-2) and reports the clamp.
- completion detection uses the parse-order OA sentinel (hardware-notes §5).
"""

from __future__ import annotations

import logging
import time

import serial
from dataclasses import dataclass

from app.services.serial import paper as paper_mod
from app.services.serial import protocol
from app.services.serial.responder import ResponderError, StatusReport
from app.services.serial.transport import (
    DeviceDisconnected,
    SerialTransport,
    TransportMalformed,
    TransportSettings,
    TransportTimeout,
)

logger = logging.getLogger(__name__)

#: Single-point form of ``PA{x0},{y0},...;`` (Prog. Manual §3-4): the
#: constant's ellipsis marks the repeatable-coordinate form; one coordinate
#: pair needs no continuation.
_PA_POINT_FMT = protocol.HPGL_PLOT_ABSOLUTE_FMT.replace(",...", "")


class DeviceError(RuntimeError):
    """Base class for device-level failures."""


class DeviceIdentificationError(DeviceError):
    """Plotter did not answer OI; with "7475A" (BUILD_SPEC §8 step 3)."""


@dataclass(frozen=True)
class MoveResult:
    """Result of an absolute move after hard-clip clamping."""

    x: int
    y: int
    clamped: bool


class HP7475ADevice:
    """High-level HP 7475A control: lifecycle, queries, pen, motion,
    velocity, park, initialize, completion await.

    Not thread-safe by itself — serialize through the hardware worker
    (BUILD_SPEC §34).
    """

    def __init__(self, port_path: str, settings: TransportSettings | None = None,
                 paper: str = "a4") -> None:
        """Args:
            port_path: device node or /dev/serial/by-id path.
            settings: transport settings (defaults: 9600 8N1, SOFTWARE_CHECK).
            paper: paper name/alias for hard-clip clamping (the UI knows what
                is loaded; default A4 metric).
        """
        self._transport = SerialTransport()
        self._port_path = port_path
        self._settings = settings
        self._paper = paper_mod.get_paper(paper)

    # -- lifecycle ----------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._transport.is_open

    @property
    def transport(self) -> "SerialTransport":
        """The underlying transport (used by the job streamer)."""
        return self._transport

    @property
    def paper(self) -> paper_mod.Paper:
        return self._paper

    def connect(self) -> str:
        """Open the port, drain, and verify identity via OI;.

        Queries only — no pen motion (BUILD_SPEC §8/§45). Returns the
        identification string. Raises DeviceIdentificationError on wrong
        identity, TransportTimeout/Malformed/DeviceDisconnected on comm
        failure.
        """
        self._transport.open(self._port_path, self._settings)
        try:
            ident = self.identify()
            if ident != protocol.IDENTIFICATION:
                raise DeviceIdentificationError(
                    f"device at {self._port_path} identified as {ident!r}, "
                    f"expected {protocol.IDENTIFICATION!r}"
                )
        except Exception:
            self._transport.close()
            raise
        logger.info("connected to HP 7475A at %s (paper %s)",
                    self._port_path, self._paper.name)
        return ident

    def close(self) -> None:
        """Close the transport (idempotent)."""
        self._transport.close()

    def __enter__(self) -> "HP7475ADevice":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- queries (Prog. Manual Ch.7) ------------------------------------------

    def identify(self) -> str:
        """OI; → identification string ("7475A", Prog. Manual §7-6)."""
        return self._query(protocol.HPGL_OUTPUT_IDENTIFICATION,
                           parse=self._responder().parse_identification)

    def status(self) -> StatusReport:
        """OS; → typed status report (Prog. Manual §7-7)."""
        return self._query(protocol.HPGL_OUTPUT_STATUS,
                           parse=self._responder().parse_status)

    def position(self) -> tuple[float, float, bool]:
        """OA; → (x, y, pen_down) in plotter units (Prog. Manual §7-2)."""
        return self._query(protocol.HPGL_OUTPUT_ACTUAL_POSITION,
                           parse=self._responder().parse_position)

    def hard_clip_limits(self) -> tuple[int, int, int, int]:
        """OH; → (xmin, ymin, xmax, ymax) plotter units (Prog. Manual Ch.2).

        Reveals the paper size configured via the plotter's rear DIP
        switches — lets the UI verify the loaded sheet matches the job.
        """
        line = self._query(protocol.HPGL_OUTPUT_HARD_CLIP, parse=None)
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 4 or not all(p.lstrip("-").isdigit() for p in parts):
            raise TransportMalformed(f"bad OH reply: {line!r}")
        xmin, ymin, xmax, ymax = (int(p) for p in parts)
        return xmin, ymin, xmax, ymax

    def buffer_space(self) -> int:
        """ESC .B; → free input-buffer bytes 0..1024 (Prog. Manual §10-28)."""
        return self._query(protocol.ESC_OUTPUT_BUFFER_SPACE,
                           parse=self._responder().parse_buffer_space)

    def errors(self) -> tuple[int, str]:
        """OE; → (hpgl_error_code, meaning); reading clears the error
        (Prog. Manual Ch.7)."""
        return self._query(protocol.HPGL_OUTPUT_ERROR,
                           parse=self._responder().parse_hpgl_error)

    # -- pen (Prog. Manual §3-2) ----------------------------------------------

    def select_pen(self, pen: int) -> None:
        """SP n; — select carousel pen 1..6 (validated; SP 0 is park-only,
        see :meth:`park`)."""
        if not 1 <= pen <= protocol.PEN_COUNT:
            raise ValueError(f"pen must be 1-{protocol.PEN_COUNT}, got {pen}")
        self._transport.write(
            protocol.HPGL_SELECT_PEN_FMT.format(pen=pen).encode())

    def pen_up(self) -> None:
        """PU; — raise the pen (Prog. Manual §3-2)."""
        self._transport.write(protocol.HPGL_PEN_UP.encode())

    def pen_down(self) -> None:
        """PD; — lower the pen (Prog. Manual §3-2)."""
        self._transport.write(protocol.HPGL_PEN_DOWN.encode())

    # -- motion ----------------------------------------------------------------

    def move_abs(self, x: float, y: float) -> MoveResult:
        """PA x,y; — move to absolute plotter-unit coordinates, clamped to
        the paper hard-clip limits (Prog. Manual §7-2). Draws only if the
        pen is down. Returns the clamped destination."""
        xmin, xmax = self._paper.x_range
        ymin, ymax = self._paper.y_range
        rx, ry = round(x), round(y)
        cx = int(min(max(rx, xmin), xmax))
        cy = int(min(max(ry, ymin), ymax))
        clamped = (cx != rx) or (cy != ry)
        self._transport.write(_PA_POINT_FMT.format(x0=cx, y0=cy).encode())
        return MoveResult(x=cx, y=cy, clamped=clamped)

    def set_velocity(self, cm_s: float) -> float:
        """VS v; — pen velocity in cm/s, validated to 0.38–38.1 and
        quantized to the plotter's 0.38 cm/s increments (Prog. Manual §3-3).
        Returns the quantized value actually commanded."""
        if not protocol.VELOCITY_MIN_CM_S <= cm_s <= protocol.VELOCITY_MAX_CM_S:
            raise ValueError(
                f"velocity must be {protocol.VELOCITY_MIN_CM_S}-"
                f"{protocol.VELOCITY_MAX_CM_S} cm/s, got {cm_s}"
            )
        steps = max(1, min(round(cm_s / protocol.VELOCITY_STEP_CM_S),
                           int(protocol.VELOCITY_MAX_CM_S / protocol.VELOCITY_STEP_CM_S)))
        quantized = round(steps * protocol.VELOCITY_STEP_CM_S, 2)
        self._transport.write(
            protocol.HPGL_VELOCITY_FMT.format(velocity=quantized).encode())
        return quantized

    def park(self) -> None:
        """Park: PU to the hard-clip lower-left corner, then SP0 (store pen
        in carousel). Both are real motions (hardware-notes §9)."""
        x, y = self._paper.x_range[0], self._paper.y_range[0]
        self._transport.write(
            protocol.HPGL_PEN_UP_FMT.format(x=x, y=y).encode())
        self._transport.write(
            protocol.HPGL_SELECT_PEN_FMT.format(pen=0).encode())

    def initialize_device(self) -> None:
        """IN; — initialize plotter state; clears errors, does not move the
        pen (Prog. Manual §1-13)."""
        self._transport.write(protocol.HPGL_INIT.encode())

    # -- completion (hardware-notes §5) ----------------------------------------

    def abort_and_park(self) -> None:
        """Immediate abort (ESC.K discards buffered graphics) + pen up.

        Escape sequences are processed at the RS-232 control level, ahead
        of any buffered HP-GL, so this halts pending motion at once. Used
        on cancellation (2026-08-19: a cancelled plot's buffer kept
        executing, drawing on the sheet after the user stopped it)."""
        self._transport.write(b"\x1b.K")
        self._transport.write(b"PU;")

    def complete_plot(self, timeout: float, on_status=None,
                      poll_interval: float = 1.0) -> tuple[float, float, bool]:
        """Queue an OA; sentinel AFTER the streamed job payload and block
        until its reply arrives — the parse-order completion proof
        (hardware-notes §5; Prog. Manual Ch.7/10).

        The sentinel reply is *recognized by shape* (X,Y,P position line),
        not by value: the caller cannot know the final position in advance.
        Optional OS; status polls are injected between reads; their replies
        queue behind the sentinel in parse order.

        Returns (x, y, pen_down) from the sentinel reply.
        Raises TransportTimeout on deadline; DeviceDisconnected on loss.
        """
        self._transport.write(protocol.HPGL_OUTPUT_ACTUAL_POSITION.encode())
        responder = self._responder()
        deadline = time.monotonic() + timeout
        # poll OS; immediately at entry (mirrors await_completion): the
        # sentinel OA; was written first, so its reply still leads the
        # queue and the trailing OS; reply yields the pen state.
        next_poll = time.monotonic() if on_status is not None else None
        while True:
            now = time.monotonic()
            if now >= deadline:
                raise TransportTimeout(
                    f"completion OA reply not received within {timeout}s"
                )
            if next_poll is not None and now >= next_poll:
                self._transport.write(protocol.HPGL_OUTPUT_STATUS.encode())
                next_poll = now + poll_interval
            try:
                line = responder.read_line(timeout=0.5)
            except serial.SerialException as exc:
                raise DeviceDisconnected(f"read failed: {exc}") from exc
            except ResponderError:
                continue  # transient; deadline re-checked above
            try:
                parsed = responder.parse_position(line)
                if on_status is not None:
                    # best-effort pen state from the OS; reply queued
                    # behind the (already-consumed) sentinel
                    self._consume_trailing_status(responder, on_status)
                return parsed
            except ResponderError:
                self._maybe_status(line, on_status)

    def await_completion(self, sentinel_reply: str, timeout: float,
                         on_status=None, poll_interval: float = 1.0) -> None:
        """Wait for a previously queued sentinel reply (an OA; appended
        after the final job instruction — output instructions are answered
        only after earlier buffered commands execute, Prog. Manual Ch.7).

        Blocks until a reply line equals *sentinel_reply*. Optionally polls
        OS; every *poll_interval* seconds, passing each parsed StatusReport
        to *on_status* (OS replies queue behind the sentinel in parse order,
        so they mostly surface at completion).

        Raises TransportTimeout if the sentinel does not arrive in time
        (job runner classifies FAILED); DeviceDisconnected on port loss.
        """
        responder = self._responder()
        deadline = time.monotonic() + timeout
        next_poll = time.monotonic() if on_status is not None else None
        while True:
            now = time.monotonic()
            if now >= deadline:
                raise TransportTimeout(
                    f"completion sentinel {sentinel_reply!r} not received "
                    f"within {timeout}s"
                )
            if next_poll is not None and now >= next_poll:
                self._transport.write(protocol.HPGL_OUTPUT_STATUS.encode())
                next_poll = now + poll_interval
            read_for = min(deadline - now, 0.5)
            if next_poll is not None:
                read_for = min(read_for, max(next_poll - now, 0.05))
            try:
                line = responder.read_line(timeout=max(read_for, 0.05))
            except ResponderError:
                continue  # transient read timeout; deadline re-checked above
            if line == sentinel_reply:
                if on_status is not None:
                    self._consume_trailing_status(responder, on_status)
                return
            self._maybe_status(line, on_status)

    # -- internals ---------------------------------------------------------------

    def _responder(self):
        """Transport's Responder (shared port + typed parsers)."""
        return self._transport._responder

    def _query(self, instruction: str, parse):
        return self._transport.query(instruction.encode(), parse=parse)

    def _maybe_status(self, line: str, on_status) -> None:
        if on_status is None:
            return
        try:
            on_status(self._responder().parse_status(line))
        except (ResponderError, ValueError):
            logger.debug("non-status line while awaiting sentinel: %r", line)

    def _consume_trailing_status(self, responder, on_status) -> None:
        """Best-effort read of the OS reply queued behind the sentinel."""
        try:
            line = responder.read_line(timeout=0.2)
        except ResponderError:
            return
        self._maybe_status(line, on_status)
