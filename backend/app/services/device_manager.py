"""Device connection manager — owns the single serial connection lifecycle.

Wraps the serial-core lane's discovery/driver (imported lazily so this module
works in tests with injected fakes). The REST layer never touches pyserial
directly; it goes through DeviceManager → HP7475ADevice.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)


class DeviceManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._driver: Any = None  # serial.driver.HP7475ADevice when connected
        self._port: str | None = None
        self._settings_snapshot: dict | None = None

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

    # -- passthrough helpers (guard + delegate) ---------------------------------

    def _require_driver(self):
        driver = self.driver()
        if driver is None:
            raise RuntimeError("device not connected")
        return driver

    def identify(self) -> dict:
        return self._require_driver().identify()

    def status(self) -> dict:
        return self._require_driver().status()

    def error(self) -> dict:
        code, meaning = self._require_driver().errors()
        return {"hpgl": {"code": code, "meaning": meaning}}

    def position(self) -> dict:
        return self._require_driver().position()

    def hard_clip_limits(self) -> dict:
        xmin, ymin, xmax, ymax = self._require_driver().hard_clip_limits()
        from app.services.serial.paper import PAPERS

        match = None
        for name, p in PAPERS.items():
            if (p.x_range[0], p.y_range[0], p.x_range[1], p.y_range[1]) == (xmin, ymin, xmax, ymax):
                match = name
                break
        return {"limits": [xmin, ymin, xmax, ymax], "paper": match}

    def buffer_space(self) -> int:
        return self._require_driver().buffer_space()

    def select_pen(self, pen: int) -> dict:
        return self._require_driver().select_pen(pen)

    def pen_up(self) -> dict:
        return self._require_driver().pen_up()

    def pen_down(self) -> dict:
        return self._require_driver().pen_down()

    def move(self, x: float, y: float) -> dict:
        return self._require_driver().move_abs(x, y)

    def park(self) -> dict:
        return self._require_driver().park()
