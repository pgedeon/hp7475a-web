"""Shared fixtures/mocks for the serial-layer tests (BUILD_SPEC §36)."""

from __future__ import annotations

from collections import deque

import pytest
import serial

from app.services.serial.fakeplotter import FakeHP7475A
from app.services.serial.transport import TransportSettings
from app.services.serial.driver import HP7475ADevice


class MockSerial:
    """pyserial test double: scripted replies, partial writes, fault flags.

    ``reply_script`` maps request bytes (e.g. ``ESC .B``) to a list of reply
    strings, consumed one per matching write.
    """

    def __init__(self, **kwargs):
        self.kwargs = dict(kwargs)
        self.tx = bytearray()
        self.writes: list[bytes] = []
        self.rx: deque[bytes] = deque()
        self.reply_script: dict[bytes, list[str]] = {}
        self.write_limit: int | None = None
        self.fail_writes = False
        self.fail_reads = False
        self.closed = False

    def write(self, data) -> int:
        data = bytes(data)
        if self.fail_writes:
            raise serial.SerialException("device gone")
        if self.write_limit is not None:
            data = data[: self.write_limit]
        self.writes.append(data)
        self.tx.extend(data)
        for request, replies in self.reply_script.items():
            if data == request and replies:
                reply = replies.pop(0)
                if reply is not None:  # None = stay silent (timeout)
                    self.rx.append(reply.encode() + b"\r")
        return len(data)

    def read(self, size: int = 1) -> bytes:
        if self.fail_reads:
            raise serial.SerialException("read error")
        out = bytearray()
        while self.rx and len(out) < size:
            item = self.rx[0]
            take = item[: size - len(out)]
            rest = item[len(take):]
            if rest:
                self.rx[0] = rest
            else:
                self.rx.popleft()
            out += take
        return bytes(out)

    def inWaiting(self) -> int:  # noqa: N802 - pyserial API name
        return sum(len(item) for item in self.rx)

    def reset_input_buffer(self) -> None:
        self.rx.clear()

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def mock_serial_cls(monkeypatch):
    """Patch pyserial Serial with MockSerial; yield the factory."""
    instances: list[MockSerial] = []

    def factory(**kwargs):
        mock = MockSerial(**kwargs)
        instances.append(mock)
        return mock

    monkeypatch.setattr(serial, "Serial", factory)
    return factory


@pytest.fixture
def fake_plotter():
    fp = FakeHP7475A().start()
    yield fp
    fp.stop()


@pytest.fixture
def fast_settings() -> TransportSettings:
    return TransportSettings(read_timeout=0.5, write_timeout=1.0, query_retries=1)


@pytest.fixture
def driver(fake_plotter, fast_settings) -> HP7475ADevice:
    dev = HP7475ADevice(fake_plotter.port_path, settings=fast_settings)
    dev.connect()
    yield dev
    dev.close()
