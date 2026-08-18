"""DeviceManager streaming guard — regression for the live abort observed
2026-08-18: concurrent OS;/OA;/ESC.B queries from status polls crossed
replies with the streamer's flow-control handshake and killed a plot
mid-burst. While a stream owns the port, queries must serve cached data
and manual commands must fail closed (never touch the serial line)."""

from __future__ import annotations

import pytest

from app.services.device_manager import DeviceManager


class CountingDriver:
    """Fake driver recording every hardware-touching call."""

    def __init__(self) -> None:
        self.status_calls = 0
        self.move_calls = 0
        self.pen_calls = 0
        self._status = {"status_byte": 0, "pen_down": False}

    # driver surface used by DeviceManager
    def connect(self):
        return "7475A"

    def close(self):
        pass

    @property
    def is_open(self):
        return True

    def status(self):
        self.status_calls += 1
        return dict(self._status)

    def move_abs(self, x, y):
        self.move_calls += 1
        return {"x": x, "y": y, "clamped": False}

    def select_pen(self, pen):
        self.pen_calls += 1


@pytest.fixture()
def manager():
    m = DeviceManager()
    m.connect("/dev/fake", driver_factory=lambda port, settings=None: CountingDriver())
    return m


def test_status_serves_cache_without_port_while_streaming(manager):
    first = manager.status()
    assert manager.streaming is False

    manager.set_streaming(True)
    second = manager.status()

    # cached: no extra hardware call, streaming markers present
    assert second["streaming"] is True
    assert second["stale"] is True
    assert second["status_byte"] == first["status_byte"]
    driver = manager.driver()
    assert driver.status_calls == 1


def test_manual_commands_fail_closed_while_streaming(manager):
    manager.set_streaming(True)
    with pytest.raises(RuntimeError, match="busy"):
        manager.move(100, 100)
    with pytest.raises(RuntimeError, match="busy"):
        manager.select_pen(2)
    driver = manager.driver()
    assert driver.move_calls == 0
    assert driver.pen_calls == 0


def test_guard_releases_after_stream(manager):
    manager.set_streaming(True)
    manager.set_streaming(False)
    assert manager.streaming is False
    manager.status()  # hardware path again (no call has touched it yet)
    assert manager.driver().status_calls == 1


def test_position_and_error_blocked_while_streaming(manager):
    manager.set_streaming(True)
    with pytest.raises(RuntimeError, match="busy"):
        manager.position()
    with pytest.raises(RuntimeError, match="busy"):
        manager.error()
