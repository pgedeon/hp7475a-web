"""Responder parser tests: valid/malformed/timeout replies via a mock port."""

from __future__ import annotations

import pytest

from app.services.serial import protocol
from app.services.serial.responder import Responder, ResponderError
from conftest import MockSerial


def _responder(rx_lines=(), timeout=0.2):
    port = MockSerial()
    port.rx.extend(line.encode() + b"\r" for line in rx_lines)
    return Responder(port, timeout=timeout), port


def test_read_line_valid():
    responder, _ = _responder(["7475A"])
    assert responder.read_line() == "7475A"


def test_read_line_sequences_multiple_replies():
    responder, _ = _responder(["24", "16"])
    assert responder.read_line() == "24"
    assert responder.read_line() == "16"


def test_read_line_timeout_raises():
    responder, _ = _responder([])
    with pytest.raises(ResponderError, match="[Tt]imeout"):
        responder.read_line()


def test_parse_status_power_up():
    responder, _ = _responder()
    report = responder.parse_status("24")
    assert report.initialized and report.ready
    assert not report.pen_down and not report.error


def test_parse_status_bits():
    responder, _ = _responder()
    report = responder.parse_status("49")  # 1+16+32
    assert report.pen_down and report.ready and report.error


def test_parse_position_valid_and_malformed():
    responder, _ = _responder()
    assert responder.parse_position("100,200,1") == (100.0, 200.0, True)
    assert responder.parse_position("0,0,0") == (0.0, 0.0, False)
    for bad in ("100,200", "x,y,z", "", "1,2,3,4"):
        with pytest.raises(ResponderError):
            responder.parse_position(bad)


def test_parse_buffer_space_bounds():
    responder, _ = _responder()
    assert responder.parse_buffer_space("1024") == protocol.INPUT_BUFFER_BYTES
    assert responder.parse_buffer_space("0") == 0
    for bad in ("-1", "1025", "abc"):
        with pytest.raises(ResponderError):
            responder.parse_buffer_space(bad)


def test_parse_extended_error_known_codes_only():
    responder, _ = _responder()
    assert responder.parse_extended_error("0") == (0, protocol.RS232_ERRORS[0])
    code, meaning = responder.parse_extended_error("16")
    assert code == 16 and "overflow" in meaning.lower()
    for bad in ("9", "17", "x"):
        with pytest.raises(ResponderError):
            responder.parse_extended_error(bad)


def test_parse_hpgl_error():
    responder, _ = _responder()
    assert responder.parse_hpgl_error("0") == (0, "No error")
    with pytest.raises(ResponderError):
        responder.parse_hpgl_error("9")


def test_parse_int_and_float():
    responder, _ = _responder()
    assert responder.parse_int(" 42 ", "status") == 42
    assert responder.parse_float("38.1", "velocity") == 38.1
    with pytest.raises(ResponderError):
        responder.parse_int("nope", "status")


def test_parse_options():
    responder, _ = _responder()
    assert responder.parse_options("0,1,0,0,1,0,0,0") == [0, 1, 0, 0, 1, 0, 0, 0]
    with pytest.raises(ResponderError):
        responder.parse_options("0,1,0")


def test_drain_discards_pending():
    responder, port = _responder(["24"])
    responder.drain()
    assert port.inWaiting() == 0
