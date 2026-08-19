"""Serial transport for the HP 7475A (BUILD_SPEC §10-§12; Prog. Manual Ch.10).

Owns the pyserial port. Sends HP-GL with bounded timeouts and retries, and
enforces flow control so the plotter's 1024-byte input buffer can never
overflow under the preferred SOFTWARE_CHECK strategy (docs/hardware-notes.md
§6: poll ``ESC .B``, send at most ``free - safety_margin`` bytes, chunk only
on instruction boundaries).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum

import serial

from app.services.serial import protocol
from app.services.serial.responder import Responder, ResponderError

logger = logging.getLogger(__name__)


class TransportError(RuntimeError):
    """Base class for serial transport failures (BUILD_SPEC §10)."""


class TransportTimeout(TransportError):
    """Bounded retries exhausted without a usable reply."""


class TransportMalformed(TransportError):
    """Plotter replies stayed unparseable after bounded retries."""


class DeviceDisconnected(TransportError):
    """Serial port vanished (USB unplug / pty closed) — BUILD_SPEC §29."""


class PlotterBufferOverflow(TransportError):
    """Plotter reported input-buffer overflow (ESC .E = 16, Prog. Manual
    §10-29) or a single instruction can never fit the safe window."""


class FlowControl(Enum):
    """Flow-control strategies (BUILD_SPEC §1/§10; hardware-notes §6)."""

    #: Preferred: poll ESC .B free bytes before each chunk (Prog. Manual §10-28).
    SOFTWARE_CHECK = "software_check"
    #: pyserial xonxoff=True; plotter side configured via ESC .I (Prog. §10-33).
    XON_XOFF = "xon_xoff"
    #: Hardwire handshake = DTR pin-20 flag (Prog. Manual §10-27 bit0).
    HARDWARE_DTR = "hardware_dtr"
    #: Fixed chunk + inter-chunk delay; troubleshooting only.
    DIAGNOSTIC = "diagnostic"


@dataclass(frozen=True)
class TransportSettings:
    """Serial + flow-control settings; defaults = HP 7475A PC default 9600
    8N1 (Op. Manual; BUILD_SPEC §7)."""

    baudrate: int = 9600
    bytesize: int = 8
    parity: str = serial.PARITY_NONE
    stopbits: float = 1
    flow_control: FlowControl = FlowControl.SOFTWARE_CHECK
    read_timeout: float = 2.0        # per reply-read attempt, seconds
    write_timeout: float = 2.0       # per write attempt, seconds
    query_retries: int = 3           # bounded retries (BUILD_SPEC §10)
    chunk_size: int = 256            # conservative max HP-GL chunk, bytes
    safety_margin: int = 32          # reserve below ESC .B free (hw-notes §6)
    poll_delay: float = 0.05         # stall re-poll interval, seconds
    stall_timeout: float = 10.0      # give up if buffer never frees, seconds
    diagnostic_chunk: int = 64       # DIAGNOSTIC mode chunk, bytes
    diagnostic_delay: float = 0.05   # DIAGNOSTIC inter-chunk delay, seconds


def split_chunks(data: bytes, max_len: int) -> list[bytes]:
    """Split *data* into chunks of at most ``max_len`` bytes, breaking ONLY
    immediately after ``;`` instruction boundaries so a partially-parsed
    instruction is never split across a re-query (hardware-notes §6.4).

    An instruction longer than ``max_len`` is kept whole (oversized chunk) —
    the SOFTWARE_CHECK sender waits for enough free space or refuses it.
    """
    if max_len <= 0:
        raise ValueError("max_len must be positive")
    if not data:
        return []
    chunks: list[bytes] = []
    start, n = 0, len(data)
    while start < n:
        chunk, start = _next_chunk(data, start, max_len)
        chunks.append(chunk)
    return chunks


def _next_chunk(data: bytes, start: int, room: int) -> tuple[bytes, int]:
    """Return the next boundary-safe chunk from *start* within *room* bytes."""
    room = max(room, 0)
    end = min(start + room, len(data))
    if end == len(data):
        return data[start:end], len(data)
    idx = data.rfind(b";", start, end)
    if idx >= start:
        return data[start : idx + 1], idx + 1
    # No boundary in window: extend to the next ';' (oversized instruction).
    idx = data.find(b";", end)
    stop = len(data) if idx == -1 else idx + 1
    return data[start:stop], stop


class SerialTransport:
    """Owns one pyserial port; write/query with bounded retries, chunked
    flow-controlled sending. Single-threaded by contract — the job runner
    serializes hardware access (BUILD_SPEC §34)."""

    def __init__(self) -> None:
        self._port = None
        self._settings = TransportSettings()
        self._responder: Responder | None = None

    # -- lifecycle ----------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._port is not None

    def open(self, port: str, settings: TransportSettings | None = None) -> None:
        """Open *port*, apply settings, drain stale input (BUILD_SPEC §10).

        Raises TransportError if already open, DeviceDisconnected if the
        device cannot be opened.
        """
        if self._port is not None:
            raise TransportError("transport already open")
        self._settings = settings or TransportSettings()
        s = self._settings
        # HARDWARE_DTR caveat: the 7475A hardwire handshake is a DTR
        # (DB25 pin 20) flag, not RTS/CTS. USB DB9 adapters expose RTS/CTS,
        # and pyserial's rtscts drives those — enabling it without a cable
        # that actually maps the lines would stall or corrupt transfers
        # (BUILD_SPEC §10: "use only when the cable wiring carries the
        # required handshake lines"). So rtscts stays OFF; HARDWARE_DTR
        # falls back to conservative chunking and the wiring caveat is
        # documented here.
        try:
            self._port = serial.Serial(
                port=port,
                baudrate=s.baudrate,
                bytesize=s.bytesize,
                parity=s.parity,
                stopbits=s.stopbits,
                xonxoff=s.flow_control is FlowControl.XON_XOFF,
                rtscts=False,
                timeout=0.1,
                write_timeout=s.write_timeout,
            )
        except serial.SerialException as exc:
            raise DeviceDisconnected(f"cannot open {port!r}: {exc}") from exc
        self._responder = Responder(self._port, timeout=s.read_timeout)
        self.drain()
        logger.info("opened %s @ %d 8%s1 flow=%s", port, s.baudrate, s.parity, s.flow_control.name)

    def close(self) -> None:
        """Close the port (idempotent)."""
        if self._port is not None:
            try:
                self._port.close()
            except Exception:  # pragma: no cover - close best-effort
                logger.debug("port close raised", exc_info=True)
            self._port = None
            logger.info("port closed")

    def drain(self) -> None:
        """Discard stale input on the open port (BUILD_SPEC §10: drain on open)."""
        if self._port is None or self._responder is None:
            return
        try:
            self._port.reset_input_buffer()
        except (AttributeError, serial.SerialException):
            pass
        self._responder.drain()

    # -- low-level I/O ------------------------------------------------------

    def write(self, data: bytes) -> None:
        """Write all of *data*, tolerating partial writes, with a bounded
        stall timeout (BUILD_SPEC §10 "Short writes are handled")."""
        if self._port is None:
            raise TransportError("transport not open")
        view = memoryview(bytes(data))
        deadline = time.monotonic() + self._settings.write_timeout
        while view:
            try:
                n = self._port.write(view)
            except serial.SerialTimeoutException as exc:
                raise TransportTimeout(f"write timed out: {exc}") from exc
            except serial.SerialException as exc:
                raise DeviceDisconnected(f"write failed: {exc}") from exc
            n = n or 0
            if n <= 0:
                if time.monotonic() >= deadline:
                    raise TransportTimeout(f"write stalled after {len(view)} bytes")
                time.sleep(0.005)
                continue
            view = view[n:]

    def query(self, data: bytes, timeout: float | None = None,
              retries: int | None = None, parse=None):
        """Send *data*, return the CR-terminated reply (optionally parsed).

        Retries on timeout AND malformed replies (BUILD_SPEC §10). Raises
        TransportTimeout / TransportMalformed after bounded retries,
        DeviceDisconnected on port loss.
        """
        if self._port is None or self._responder is None:
            raise TransportError("transport not open")
        eff_timeout = self._settings.read_timeout if timeout is None else timeout
        attempts = (self._settings.query_retries if retries is None else retries) + 1
        last_error: Exception | None = None
        last_was_malformed = False
        for _ in range(attempts):
            try:
                self.write(data)
                line = self._responder.read_line(timeout=eff_timeout)
            except serial.SerialException as exc:
                raise DeviceDisconnected(f"read failed: {exc}") from exc
            except ResponderError as exc:
                last_error, last_was_malformed = exc, False
                logger.debug("no reply to %r: %s", data, exc)
            else:
                if parse is None:
                    return line
                try:
                    return parse(line)
                except ResponderError as exc:
                    last_error, last_was_malformed = exc, True
                    logger.warning("malformed reply %r to %r: %s", line, data, exc)
            self.drain()  # drop partial/stale bytes before retrying
        if last_was_malformed:
            raise TransportMalformed(f"unparseable reply after {attempts} attempts: {last_error}")
        raise TransportTimeout(f"no reply after {attempts} attempts: {last_error}")

    # -- flow-controlled streaming ------------------------------------------

    def send_chunked(self, data: bytes) -> None:
        """Send an HP-GL byte stream under the configured flow-control
        strategy (BUILD_SPEC §10). Data must be ASCII HP-GL instructions on
        ``;`` boundaries; escape sequences are sent via query(), not here.
        """
        if self._port is None:
            raise TransportError("transport not open")
        try:
            data.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError("HP-GL stream must be ASCII") from exc
        if not data:
            return
        strategy = self._settings.flow_control
        if strategy is FlowControl.SOFTWARE_CHECK:
            self._send_software_check(data)
        elif strategy is FlowControl.DIAGNOSTIC:
            self._send_fixed_chunks(data, self._settings.diagnostic_chunk,
                                    self._settings.diagnostic_delay)
        else:  # XON_XOFF / HARDWARE_DTR: chunked, no buffer polling
            self._send_fixed_chunks(data, self._settings.chunk_size, 0.0)

    def _send_fixed_chunks(self, data: bytes, chunk: int, delay: float) -> None:
        for part in split_chunks(data, chunk):
            self.write(part)
            if delay:
                time.sleep(delay)

    def _send_software_check(self, data: bytes) -> None:
        """Preferred strategy (hardware-notes §6; Prog. Manual §10-28):

        1. ``ESC .B`` -> free bytes N; never send more than min(N - margin,
           chunk_size) per burst, always on instruction boundaries.
        2. N too small -> bounded wait + re-poll; ``ESC .E`` == 16 aborts.
        3. Final ``ESC .E`` check catches overflow that happened mid-stream.
        """
        s = self._settings
        max_safe = protocol.INPUT_BUFFER_BYTES - s.safety_margin
        pos, waited = 0, 0.0
        while pos < len(data):
            free = self._buffer_space()
            room = min(free - s.safety_margin, s.chunk_size)
            chunk, next_pos = _next_chunk(data, pos, room)
            if chunk and len(chunk) <= room:
                self.write(chunk)
                pos = next_pos
                waited = 0.0
            elif len(chunk) > max_safe:
                raise PlotterBufferOverflow(
                    f"single instruction of {len(chunk)}B can never fit the "
                    f"{max_safe}B safe window"
                )
            else:
                # Buffer too full for the next instruction: check for
                # overflow, then bounded-wait for the plotter to drain.
                code, meaning = self._extended_error()
                if code == 16:
                    raise PlotterBufferOverflow(f"plotter buffer overflow: {meaning}")
                waited += s.poll_delay
                if waited >= s.stall_timeout:
                    raise TransportTimeout(
                        f"plotter buffer did not free up within {s.stall_timeout}s"
                    )
                time.sleep(s.poll_delay)
        code, meaning = self._extended_error()
        if code == 16:
            raise PlotterBufferOverflow(f"plotter buffer overflow: {meaning}")

    def extended_error(self) -> tuple[int, str]:
        """Public ESC .E query for the streamer's overflow watchdog."""
        return self._extended_error()

    def _buffer_space(self) -> int:
        """Query free input-buffer bytes via ESC .B (Prog. Manual §10-28)."""
        assert self._responder is not None
        return self.query(protocol.ESC_OUTPUT_BUFFER_SPACE.encode(),
                          parse=self._responder.parse_buffer_space)

    def _extended_error(self) -> tuple[int, str]:
        """Query RS-232 extended error via ESC .E (Prog. Manual §10-29)."""
        assert self._responder is not None
        return self.query(protocol.ESC_OUTPUT_EXTENDED_ERROR.encode(),
                          parse=self._responder.parse_extended_error)
