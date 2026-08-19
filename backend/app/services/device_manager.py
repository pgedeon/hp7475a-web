"""Device connection manager — owns the single serial connection lifecycle.

Also the serialization boundary: the serial-lane driver returns typed
values (MoveResult/StatusReport dataclasses, tuples, None, str); this layer
converts every passthrough into JSON-safe dicts for the API. Routes never
see driver types.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, is_dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


class DeviceManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._driver: Any = None  # serial.driver.HP7475ADevice when connected
        self._port: str | None = None
        self._settings_snapshot: dict | None = None
        # Streaming guard: while a job is streaming HP-GL, NO other code may
        # touch the port — concurrent queries (OS;/OA;/ESC.B) interleave bytes
        # and cross replies with the streamer's flow-control handshake, which
        # aborts plots mid-burst (observed live 2026-08-18). Query methods
        # serve cached data; manual pen/move commands fail closed.
        self._streaming = False
        self._cached_status: dict | None = None
        # I/O lane: serializes EVERY hardware query even at idle — without
        # it, a status poll's OS; reply can be consumed by a concurrent
        # OH; (observed live 2026-08-18: "bad OH reply: '16'" — '16' is an
        # OS; status byte). One serial port = one query at a time.
        self._io_lock = threading.Lock()

    # -- connection state -----------------------------------------------------

    def is_connected(self) -> bool:
        with self._lock:
            return self._driver is not None and getattr(self._driver, "is_open", lambda: True)()

    @property
    def port(self) -> Optional[str]:
        with self._lock:
            return self._port

    def driver(self) -> Any:
        with self._lock:
            return self._driver

    def connection_info(self) -> dict:
        with self._lock:
            return {
                "connected": self._driver is not None,
                "port": self._port,
                "settings": self._settings_snapshot,
            }

    # -- connect / disconnect ---------------------------------------------------

    def connect(
        self,
        port: str,
        settings: dict | None = None,
        *,
        driver_factory=None,
    ) -> dict:
        """Open the device. `driver_factory(port, settings)` defaults to the
        real HP7475ADevice from the serial lane (lazy import keeps tests
        independent of pyserial hardware presence)."""
        from app.services.serial.driver import HP7475ADevice  # lazy: serial lane
        from app.services.serial.transport import TransportSettings

        factory = driver_factory or HP7475ADevice
        with self._lock:
            if self._driver is not None:
                raise RuntimeError(
                    f"already connected to {self._port}; disconnect first"
                )
            raw = settings or {}
            valid = {f for f in TransportSettings.__dataclass_fields__}
            coerced = {k: v for k, v in raw.items() if k in valid}
            ts = TransportSettings(**coerced) if coerced else None
            driver = factory(port, ts)
            info = driver.connect()  # drains + identifies; NO pen motion
            self._driver = driver
            self._port = port
            self._settings_snapshot = raw
            return info

    def disconnect(self) -> None:
        with self._lock:
            driver, self._driver, self._port, self._settings_snapshot = (
                self._driver, None, None, None,
            )
        if driver is not None:
            try:
                driver.close()
            except Exception:
                logger.exception("error closing device")

    # -- passthrough helpers (guard + delegate + shape) -------------------------

    def _require_driver(self):
        driver = self.driver()
        if driver is None:
            raise RuntimeError("device not connected")
        return driver

    @staticmethod
    def _dc(obj: Any) -> dict:
        """Dataclass/None/dict-tolerant → dict shaping for API responses."""
        if obj is None:
            return {}
        if is_dataclass(obj) and not isinstance(obj, type):
            return asdict(obj)
        if isinstance(obj, dict):
            return obj
        raise TypeError(f"unshapable driver result: {obj!r}")

    # -- streaming guard -------------------------------------------------------

    def set_streaming(self, active: bool) -> None:
        """Mark the serial lane as owned by an active plot stream.
        The job worker toggles this around streamer.send; everything else
        must then avoid the port (cached status / DeviceBusy errors)."""
        with self._lock:
            self._streaming = active

    @property
    def streaming(self) -> bool:
        with self._lock:
            return self._streaming

    def _check_hardware_free(self, action: str) -> None:
        """Raise when a plot stream owns the port (fail-closed for manual
        commands; query methods use cached paths instead)."""
        if self.streaming:
            raise RuntimeError(
                f"device busy: plot in progress ({action} blocked until it "
                f"finishes, pauses, or is cancelled)"
            )

    # -- passthrough helpers (guard + delegate) ---------------------------------

    def identify(self) -> dict:
        self._check_hardware_free("identify")
        with self._io_lock:
            result = self._require_driver().identify()
        return {"identity": result} if isinstance(result, str) else self._dc(result)

    def status(self) -> dict:
        """OS; status report. During streaming: last cached report (never
        touches the port — a concurrent OS; would corrupt the handshake)."""
        if self.streaming:
            cached = self._cached_status or {}
            return {"streaming": True, "stale": True, **cached}
        with self._io_lock:
            result = self._dc(self._require_driver().status())
        self._cached_status = result
        return result

    def error(self) -> dict:
        self._check_hardware_free("error query")
        with self._io_lock:
            code, meaning = self._require_driver().errors()
        return {"hpgl": {"code": code, "meaning": meaning}}

    def position(self) -> dict:
        self._check_hardware_free("position query")
        with self._io_lock:
            result = self._require_driver().position()
        if isinstance(result, tuple):
            return {"x": result[0], "y": result[1], "pen_down": result[2]}
        return self._dc(result)

    def select_pen(self, pen: int) -> dict:
        self._check_hardware_free("select pen")
        with self._io_lock:
            self._require_driver().select_pen(pen)
        return {"pen": pen}

    def pen_up(self) -> dict:
        self._check_hardware_free("pen up")
        with self._io_lock:
            self._require_driver().pen_up()
        return {"pen_down": False}

    def pen_down(self) -> dict:
        self._check_hardware_free("pen down")
        with self._io_lock:
            self._require_driver().pen_down()
        return {"pen_down": True}

    def move(self, x: float, y: float) -> dict:
        self._check_hardware_free("move")
        with self._io_lock:
            result = self._require_driver().move_abs(x, y)
        return self._dc(result)

    def park(self) -> dict:
        self._check_hardware_free("park")
        with self._io_lock:
            self._require_driver().park()
        return {"parked": True}

    def hard_clip_limits(self) -> dict:
        with self._io_lock:
            xmin, ymin, xmax, ymax = self._require_driver().hard_clip_limits()
        from app.services.serial.paper import PAPERS

        match = None
        for name, p in PAPERS.items():
            if (p.x_range[0], p.y_range[0], p.x_range[1], p.y_range[1]) == (xmin, ymin, xmax, ymax):
                match = name
                break
        return {"limits": [xmin, ymin, xmax, ymax], "paper": match}

    def buffer_space(self) -> int:
        self._check_hardware_free("buffer query")
        with self._io_lock:
            return self._require_driver().buffer_space()
