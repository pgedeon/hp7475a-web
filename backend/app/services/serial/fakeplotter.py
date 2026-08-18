"""Fake HP 7475A on a pseudo-terminal (BUILD_SPEC §35).

Test double for ALL automated tests: opens a pty pair, exposes ``port_path``
for pyserial, and emulates the subset of HP-GL + RS-232 escape behavior the
application relies on:

- output instructions answered **in parse order** as they leave the input
  buffer (Prog. Manual Ch.7/§10 — basis of completion detection),
- finite 1024-byte input buffer with byte-accurate overflow accounting
  (excess bytes dropped, ``ESC .E`` reports 16),
- delayed execution (``set_exec_delay``) so ``ESC .B`` reflects simulated
  occupancy while a "plot" drains,
- fault injection: timeout (silent), malformed (garbage), disconnect (pty
  closed),
- position / pen / velocity / error / status state (IN/DF/SP/PU/PD/PA/PR/VS/
  OI/OA/OC/OE/OS/OH/OP/OO/ESC.B/ESC.E).
"""

from __future__ import annotations

import logging
import os
import select
import threading
import time

from app.services.serial import protocol

logger = logging.getLogger(__name__)

_MOVERS = {"PA", "PR", "PU", "PD", "SP"}  # instructions that take real time

_ESC_BYTE = 0x1B
_TERM_BYTES = (0x3B, 0x3A)  # ';' ':'


def _split_control_channel(stream: bytes) -> tuple[list[str], bytes, bytes]:
    """Separate device-control escapes from HP-GL data in one read block.

    Emulates the plotter's RS-232 handler (Prog. Manual Ch.10): escape
    sequences are intercepted at the control level — they never enter the
    1024-byte graphics buffer — while everything around them stays in
    stream order. Returns (escape_tokens, graphics_bytes,
    incomplete_escape_tail); the tail is withheld (neither buffered nor
    dropped) until its terminator arrives in a later block.
    """
    tokens: list[str] = []
    graphics = bytearray()
    i, n = 0, len(stream)
    while i < n:
        j = stream.find(b"\x1b", i)
        if j < 0:
            graphics += stream[i:]
            break
        end = -1
        for k in range(j + 2, min(n, j + 14)):
            if stream[k] in _TERM_BYTES:
                end = k
                break
        if end < 0:
            if n - j < 14:
                return tokens, bytes(graphics + bytearray(stream[i:j])), stream[j:]
            # stray ESC with no terminator in range: treat as graphics byte
            graphics += stream[i : j + 1]
            i = j + 1
            continue
        graphics += stream[i:j]
        tokens.append(stream[j : end + 1].decode("ascii", errors="replace"))
        i = end + 1
    return tokens, bytes(graphics), b""


class FakeHP7475A:
    """Thread-safe pty-backed HP 7475A emulator."""

    def __init__(self, buffer_size: int = protocol.INPUT_BUFFER_BYTES,
                 exec_delay: float = 0.0) -> None:
        self._buffer_size = buffer_size
        self._exec_delay = float(exec_delay)
        self._lock = threading.Lock()
        self._inbuf = bytearray()
        self._running = False
        self._master = -1
        self._slave = -1
        self._reply_mode = "normal"  # normal | timeout | malformed
        self._pending_ctl = bytearray()  # incomplete escape bytes (control ch.)

        # -- emulated device state (guarded by _lock) --
        self.x = 0
        self.y = 0
        self.commanded = (0, 0)
        self.pen_down = False
        self.pen = 0
        self.velocity = protocol.VELOCITY_MAX_CM_S
        self.hard_clip = (0, 0, 11040, 7721)  # A4 metric (Prog. Manual §7-2)
        self.p1p2 = self.hard_clip
        self.hpgl_error = 0
        self.rs232_error = 0
        self.status_initialized = True  # power-up bit 3 until first OS
        #: every executed instruction token, in order (for test asserts)
        self.commands: list[str] = []
        #: per-arrival buffer accounting: (occ_before, bytes, occ_after, dropped)
        self.occupancy_log: list[tuple[int, bytes, int, int]] = []
        self.port_path: str | None = None
        self._threads: list[threading.Thread] = []

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> "FakeHP7475A":
        """Open the pty pair and start reader/executor threads."""
        self._master, self._slave = os.openpty()
        self.port_path = os.ttyname(self._slave)
        self._running = True
        for target in (self._reader_loop, self._executor_loop):
            t = threading.Thread(target=target, daemon=True,
                                 name=f"fake7475a-{target.__name__}")
            t.start()
            self._threads.append(t)
        logger.debug("fake plotter on %s", self.port_path)
        return self

    def stop(self) -> None:
        """Stop threads and close the pty (idempotent)."""
        self._running = False
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads = []
        for fd_name in ("_master", "_slave"):
            fd = getattr(self, fd_name)
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, fd_name, -1)
        self.port_path = None

    # -- fault injection (BUILD_SPEC §35) ------------------------------------

    def fault_timeout(self) -> None:
        """Stop replying to anything (host sees read timeouts)."""
        with self._lock:
            self._reply_mode = "timeout"

    def fault_malformed(self) -> None:
        """Reply with garbage instead of well-formed answers."""
        with self._lock:
            self._reply_mode = "malformed"

    def fault_disconnect(self) -> None:
        """Close the pty master: client I/O fails (USB-unplug analogue)."""
        self._running = False
        with self._lock:
            if self._master >= 0:
                try:
                    os.close(self._master)
                except OSError:
                    pass
                self._master = -1

    def set_exec_delay(self, seconds: float) -> None:
        """Seconds each motion instruction takes to execute; the buffer only
        frees as instructions execute, so ESC .B reflects occupancy."""
        with self._lock:
            self._exec_delay = float(seconds)

    # -- introspection helpers for tests -------------------------------------

    @property
    def occupancy(self) -> int:
        """Current input-buffer occupancy in bytes."""
        with self._lock:
            return len(self._inbuf)

    @property
    def reply_mode(self) -> str:
        with self._lock:
            return self._reply_mode

    # -- synchronization helpers for tests ------------------------------------

    def wait_received(self, n: int, timeout: float = 5.0) -> bool:
        """Wait until >= *n* bytes have arrived from the host."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                rx = sum(len(entry[1]) for entry in self.occupancy_log)
            if rx >= n:
                return True
            time.sleep(0.005)
        return False

    def wait_idle(self, quiet: float = 0.15, timeout: float = 5.0) -> bool:
        """Wait until arrivals stop AND the input buffer is empty (all
        queued instructions executed). Guards the arrival race a bare
        ``occupancy == 0`` check misses."""
        deadline = time.monotonic() + timeout
        last_rx, quiet_start = -1, None
        while time.monotonic() < deadline:
            with self._lock:
                rx = sum(len(entry[1]) for entry in self.occupancy_log)
                busy = bool(self._inbuf)
            if rx != last_rx:
                last_rx, quiet_start = rx, None
            if not busy:
                if quiet_start is None:
                    quiet_start = time.monotonic()
                elif time.monotonic() - quiet_start >= quiet:
                    return True
            time.sleep(0.01)
        return False

    # -- threads --------------------------------------------------------------

    def _reader_loop(self) -> None:
        """Moves bytes from the pty into the emulated input buffer, dropping
        (and flagging overflow on) whatever does not fit."""
        while self._running:
            try:
                ready, _, _ = select.select([self._master], [], [], 0.05)
                if not ready:
                    continue
                data = os.read(self._master, 4096)
            except OSError:
                break  # master closed (disconnect/stop)
            if not data:
                continue
            with self._lock:
                # RS-232 handler semantics (Prog. Manual Ch.10): device-control
                # escapes are intercepted by the control parser BEFORE the
                # graphics buffer — they never consume its 1024 bytes and are
                # answered in stream order. Anything after a complete escape
                # (and any incomplete escape tail) is control-channel data.
                stream = bytes(self._pending_ctl) + data
                self._pending_ctl = bytearray()
                escapes, remainder, pending = _split_control_channel(stream)
                self._pending_ctl = bytearray(pending)
                occ = len(self._inbuf)
                take = min(len(remainder), self._buffer_size - occ)
                dropped = len(remainder) - take
                if dropped:
                    self.rs232_error = 16  # input buffer overflow (Prog. §10-29)
                self._inbuf.extend(remainder[:take])
                self.occupancy_log.append((occ, data, len(self._inbuf), dropped))
                replies = [
                    self._format_reply_locked(
                        self._escape_reply_locked(token)
                    )
                    for token in escapes
                ]
            for text in replies:
                if text is not None:
                    self._emit(text)

    def _answer_escapes_locked(self) -> list[str]:
        """Legacy in-buffer escape extraction — with control-channel
        splitting at read time this is a no-op safety net (kept for direct
        _inbuf injection in tests). Caller holds the lock."""
        out: list[str] = []
        esc = protocol.ESC.encode("ascii")
        while True:
            i = self._inbuf.find(esc)
            if i < 0:
                break
            end = -1
            for k in range(i + 2, min(len(self._inbuf), i + 14)):
                if self._inbuf[k : k + 1] in (b";", b":"):
                    end = k
                    break
            if end < 0:
                break  # incomplete escape — wait for the rest
            token = bytes(self._inbuf[i : end + 1]).decode("ascii", errors="replace")
            del self._inbuf[i : end + 1]
            reply = self._escape_reply_locked(token)
            if reply is None:
                continue
            if self._reply_mode == "timeout":
                continue
            if self._reply_mode == "malformed":
                reply = "##garbage##"
            out.append(reply + protocol.OUTPUT_TERMINATOR)
        return out

    # executor loop unchanged
    def _format_reply_locked(self, raw: str | None) -> str | None:
        """Apply fault modes to one control reply; None = stay silent."""
        if raw is None:
            return None
        if self._reply_mode == "timeout":
            return None
        if self._reply_mode == "malformed":
            return "##garbage##"
        return raw + protocol.OUTPUT_TERMINATOR

    def _escape_reply_locked(self, token: str) -> str | None:
        """Reply for one escape token; free space counts bytes still
        queued (the token itself is already out of the buffer)."""
        if token.startswith(protocol.ESC_OUTPUT_BUFFER_SPACE.rstrip(";")):
            return str(max(0, self._buffer_size - len(self._inbuf)))
        if token.startswith(protocol.ESC_OUTPUT_EXTENDED_ERROR.rstrip(";")):
            code, self.rs232_error = self.rs232_error, 0  # ESC .E clears
            return str(code)
        return None  # other escapes acknowledged by silence

    def _executor_loop(self) -> None:
        """Pops one complete instruction at a time, executing it and replying
        to output instructions in parse order."""
        while self._running:
            with self._lock:
                token = self._pop_token()
            if token is None:
                time.sleep(0.002)
                continue
            is_motion, reply = self._execute(token)
            if reply is not None:
                with self._lock:
                    if self._reply_mode == "timeout":
                        reply = None
                    elif self._reply_mode == "malformed":
                        reply = "##garbage##"
                if reply is not None:
                    self._emit(reply + protocol.OUTPUT_TERMINATOR)
            if is_motion and self._exec_delay > 0:
                time.sleep(self._exec_delay)

    def _pop_token(self) -> str | None:
        """Pop one ';'-terminated instruction (escapes included) from the
        buffer, freeing its bytes. Caller holds the lock."""
        idx = self._inbuf.find(b";")
        if idx < 0:
            return None
        token = bytes(self._inbuf[: idx + 1]).decode("ascii", errors="replace")
        del self._inbuf[: idx + 1]
        return token

    def _emit(self, text: str) -> None:
        try:
            os.write(self._master, text.encode("ascii"))
        except OSError:
            logger.debug("emit after close: %r", text)

    # -- instruction execution -------------------------------------------------

    def _execute(self, token: str) -> tuple[bool, str | None]:
        """Execute one instruction token. Returns (is_motion, reply)."""
        with self._lock:
            self.commands.append(token)
            mnem = token[:2].upper()
            params = token[2:].rstrip(";").strip()
            reply: str | None = None
            is_motion = mnem in _MOVERS
            if mnem == "OI":
                reply = protocol.IDENTIFICATION
            elif mnem in ("OA", "OC"):
                pos = (self.x, self.y) if mnem == "OA" else self.commanded
                reply = f"{int(pos[0])},{int(pos[1])},{1 if self.pen_down else 0}"
            elif mnem == "OE":
                reply = str(self.hpgl_error)
                self.hpgl_error = 0  # OE clears the error (Prog. Manual Ch.7)
            elif mnem == "OS":
                reply = str(self._status_byte_locked())
            elif mnem == "OH":
                reply = ",".join(str(v) for v in self.hard_clip)
            elif mnem == "OP":
                reply = ",".join(str(v) for v in self.p1p2)
            elif mnem == "OO":
                reply = "0,1,0,0,1,0,0,0"  # pen-select + arcs (Prog. Manual §7-6)
            elif mnem in ("IN", "DF"):
                # IN/DF: default state; does NOT move the pen (Prog. §1-13).
                self.p1p2 = self.hard_clip
                self.velocity = protocol.VELOCITY_MAX_CM_S
                self.hpgl_error = 0
                self.pen_down = False
            elif mnem == "SP":
                self.pen = self._int_param(params, 0)
            elif mnem == "VS":
                self.velocity = float(params) if params else protocol.VELOCITY_MAX_CM_S
            elif mnem in ("PU", "PD"):
                self.pen_down = mnem == "PD"
                self._move(params, relative=False)
            elif mnem == "PA":
                self._move(params, relative=False)
            elif mnem == "PR":
                self._move(params, relative=True)
            else:
                self.hpgl_error = 1  # instruction not recognized
            return is_motion, reply

    def _move(self, params: str, relative: bool) -> None:
        """Apply PU/PD/PA/PR coordinate lists (last pair wins), clamped to
        the hard-clip limits. Caller holds the lock."""
        if not params:
            return
        try:
            nums = [float(v) for v in params.split(",")]
        except ValueError:
            self.hpgl_error = 2  # wrong number of parameters
            return
        if len(nums) < 2 or len(nums) % 2:
            self.hpgl_error = 2
            return
        x, y = nums[-2], nums[-1]
        if relative:
            x, y = self.x + x, self.y + y
        xmin, ymin, xmax, ymax = self.hard_clip
        self.x = min(max(x, xmin), xmax)
        self.y = min(max(y, ymin), ymax)
        self.commanded = (self.x, self.y)

    def _status_byte_locked(self) -> int:
        """Status byte per Prog. Manual §7-7; reading OS clears bit 3."""
        value = 0
        if self.pen_down:
            value |= protocol.STATUS_PEN_DOWN
        if self.status_initialized:
            value |= protocol.STATUS_INITIALIZED
            self.status_initialized = False
        value |= protocol.STATUS_READY
        if self.hpgl_error or self.rs232_error:
            value |= protocol.STATUS_ERROR
        return value

    @staticmethod
    def _int_param(params: str, default: int) -> int:
        try:
            return int(params)
        except ValueError:
            return default
