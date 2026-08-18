"""Fake plotter behaviors over a real pty (BUILD_SPEC §35): replies in
parse order, buffer occupancy with delayed execution, overflow accounting,
fault injection."""

from __future__ import annotations

import time

import pytest
import serial

from app.services.serial import protocol
from app.services.serial.responder import Responder, ResponderError

ESC_B = protocol.ESC_OUTPUT_BUFFER_SPACE.encode()
ESC_E = protocol.ESC_OUTPUT_EXTENDED_ERROR.encode()


@pytest.fixture
def link(fake_plotter):
    """Open pyserial + Responder on the fake pty."""
    port = serial.Serial(fake_plotter.port_path, timeout=0.1, write_timeout=1.0)
    responder = Responder(port, timeout=2.0)
    yield port, responder
    port.close()


def _drain_ok(fake_plotter, timeout=5.0):
    assert fake_plotter.wait_idle(timeout=timeout), "fake plotter never drained"


def test_identification_reply(link):
    port, responder = link
    port.write(protocol.HPGL_OUTPUT_IDENTIFICATION.encode())
    assert responder.read_line() == protocol.IDENTIFICATION


def test_output_replies_in_parse_order(link, fake_plotter):
    """OI queued behind slow motion answers only after it executes."""
    port, responder = link
    fake_plotter.set_exec_delay(0.05)
    port.write(b"PA100,100;" * 5 + protocol.HPGL_OUTPUT_IDENTIFICATION.encode())
    with pytest.raises(ResponderError):
        responder.read_line(timeout=0.05)  # not answered yet
    assert responder.read_line(timeout=5.0) == protocol.IDENTIFICATION


def test_position_state_updates(link, fake_plotter):
    port, responder = link
    port.write(b"PD100,200;PR10,-20;PU500,600;")
    _drain_ok(fake_plotter)
    assert (fake_plotter.x, fake_plotter.y) == (500, 600)
    assert fake_plotter.pen_down is False
    port.write(protocol.HPGL_OUTPUT_ACTUAL_POSITION.encode())
    assert responder.read_line() == "500,600,0"


def test_hard_clip_clamping_inside_fake(link, fake_plotter):
    port, responder = link
    port.write(b"PA99999,-5;")
    _drain_ok(fake_plotter)
    assert (fake_plotter.x, fake_plotter.y) == (11040, 0)


def test_pen_and_velocity_state(link, fake_plotter):
    port, _ = link
    port.write(b"SP3;VS12.5;")
    _drain_ok(fake_plotter)
    assert fake_plotter.pen == 3
    assert fake_plotter.velocity == 12.5


def test_in_resets_state_without_motion(fake_plotter):
    fake_plotter.pen_down = True
    fake_plotter.hpgl_error = 3
    fake_plotter.p1p2 = (0, 0, 1, 1)
    fake_plotter._execute("IN;")
    assert fake_plotter.pen_down is False
    assert fake_plotter.hpgl_error == 0
    assert fake_plotter.p1p2 == fake_plotter.hard_clip
    assert (fake_plotter.x, fake_plotter.y) == (0, 0)  # IN never moves


def test_status_byte_powerup_then_ready(link):
    port, responder = link
    port.write(protocol.HPGL_OUTPUT_STATUS.encode())
    assert responder.read_line() == str(protocol.STATUS_POWER_UP)
    port.write(protocol.HPGL_OUTPUT_STATUS.encode())
    assert responder.read_line() == "16"  # initialized bit cleared by first OS


def test_options_p1p2_hard_clip_replies(link):
    port, responder = link
    port.write(protocol.HPGL_OUTPUT_OPTIONS.encode())
    assert responder.read_line() == "0,1,0,0,1,0,0,0"
    port.write(protocol.HPGL_OUTPUT_P1_P2.encode())
    assert responder.read_line() == "0,0,11040,7721"


def test_esc_b_reflects_occupancy_with_exec_delay(link, fake_plotter):
    port, responder = link
    fake_plotter.set_exec_delay(0.02)
    port.write(b"PA50,50;" * 20)  # 160B queued, draining slowly
    assert fake_plotter.wait_received(160)
    port.write(ESC_B)
    free = int(responder.read_line(timeout=5.0))
    assert free < protocol.INPUT_BUFFER_BYTES  # buffer visibly occupied
    _drain_ok(fake_plotter)
    port.write(ESC_B)
    assert int(responder.read_line()) == protocol.INPUT_BUFFER_BYTES


def test_buffer_overflow_sets_error_16(fake_plotter):
    fake_plotter.set_exec_delay(0.05)
    blast = b"PA1,1;" * 300  # 1800B > 1024B buffer
    import serial as _serial
    port = _serial.Serial(fake_plotter.port_path, timeout=0.1, write_timeout=5.0)
    try:
        port.write(blast)
        deadline = time.monotonic() + 5.0
        while not any(e[3] > 0 for e in fake_plotter.occupancy_log) \
                and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        port.close()
    assert any(e[3] > 0 for e in fake_plotter.occupancy_log)  # bytes dropped
    assert fake_plotter.rs232_error == 16


def test_occupancy_never_exceeds_buffer(fake_plotter):
    for occ_before, raw, occ_after, dropped in fake_plotter.occupancy_log:
        assert occ_after <= fake_plotter._buffer_size




def test_fault_timeout_actually_silent(link, fake_plotter):
    port, responder = link
    fake_plotter.fault_timeout()
    port.write(protocol.HPGL_OUTPUT_IDENTIFICATION.encode())
    with pytest.raises(ResponderError):
        responder.read_line(timeout=0.3)


def test_fault_malformed_garbage(link, fake_plotter):
    port, responder = link
    fake_plotter.fault_malformed()
    port.write(protocol.HPGL_OUTPUT_IDENTIFICATION.encode())
    assert responder.read_line() == "##garbage##"


def test_fault_disconnect_breaks_io(fake_plotter):
    port = serial.Serial(fake_plotter.port_path, timeout=0.1, write_timeout=1.0)
    fake_plotter.fault_disconnect()
    with pytest.raises(serial.SerialException):
        for _ in range(50):
            port.write(b"OI;")
            port.read(64)
            time.sleep(0.02)
    port.close()
