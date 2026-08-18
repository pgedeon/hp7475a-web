"""Driver flows over the pty fake (BUILD_SPEC §9, §10-§12, §17-§18):
connect without pen motion, queries, pen controls, clamped moves, velocity
quantization, park, initialize, completion sentinel, fault classification."""

from __future__ import annotations

import pytest

from app.services.serial import protocol
from app.services.serial.driver import (
    DeviceIdentificationError,
    HP7475ADevice,
    MoveResult,
)
from app.services.serial.transport import (
    DeviceDisconnected,
    TransportMalformed,
    TransportTimeout,
)

def _drain(fake_plotter, timeout=5.0):
    assert fake_plotter.wait_idle(timeout=timeout), "fake plotter never drained"


# -- connect -------------------------------------------------------------------


def test_connect_identifies_without_pen_motion(fake_plotter, fast_settings):
    dev = HP7475ADevice(fake_plotter.port_path, settings=fast_settings)
    assert dev.connect() == "7475A"
    try:
        assert dev.connected
        # only OI crossed the wire — no motion/pen instructions at all
        assert fake_plotter.commands == [protocol.HPGL_OUTPUT_IDENTIFICATION]
        assert (fake_plotter.x, fake_plotter.y) == (0, 0)
        assert fake_plotter.pen_down is False
    finally:
        dev.close()
    assert not dev.connected


def test_connect_rejects_wrong_identity(fake_plotter, fast_settings):
    class WrongID(HP7475ADevice):
        def identify(self) -> str:
            return "7550A"

    dev = WrongID(fake_plotter.port_path, settings=fast_settings)
    with pytest.raises(DeviceIdentificationError):
        dev.connect()
    assert not dev.connected  # port closed again


def test_context_manager_closes(fake_plotter, fast_settings):
    with HP7475ADevice(fake_plotter.port_path, settings=fast_settings) as dev:
        dev.connect()
        assert dev.connected
    assert not dev.connected


# -- queries --------------------------------------------------------------------


def test_status_and_position_and_errors(driver, fake_plotter):
    report = driver.status()
    assert report.status_byte == protocol.STATUS_POWER_UP
    assert driver.status().status_byte == 16  # initialized bit cleared by OS
    assert driver.position() == (0.0, 0.0, False)
    assert driver.errors() == (0, protocol.HPGL_ERRORS[0])


def test_select_pen_validates_range(driver):
    with pytest.raises(ValueError):
        driver.select_pen(0)
    with pytest.raises(ValueError):
        driver.select_pen(7)


def test_select_pen_changes_fake_pen(driver, fake_plotter):
    driver.select_pen(5)
    _drain(fake_plotter)
    assert fake_plotter.pen == 5


def test_pen_up_down(driver, fake_plotter):
    driver.pen_down()
    _drain(fake_plotter)
    assert fake_plotter.pen_down
    driver.pen_up()
    _drain(fake_plotter)
    assert not fake_plotter.pen_down


# -- motion ----------------------------------------------------------------------


def test_move_abs_within_bounds(driver, fake_plotter):
    result = driver.move_abs(500, 700)
    assert result == MoveResult(500, 700, clamped=False)
    _drain(fake_plotter)
    assert (fake_plotter.x, fake_plotter.y) == (500, 700)


def test_move_abs_clamps_to_hard_clip(driver, fake_plotter):
    assert driver.move_abs(-100, 5000) == MoveResult(0, 5000, clamped=True)
    assert driver.move_abs(99999, -5) == MoveResult(11040, 0, clamped=True)
    _drain(fake_plotter)
    assert (fake_plotter.x, fake_plotter.y) == (11040, 0)


def test_move_abs_clamps_to_configured_paper(fake_plotter, fast_settings):
    with HP7475ADevice(fake_plotter.port_path, settings=fast_settings,
                       paper="a3") as dev:
        dev.connect()
        result = dev.move_abs(15000, 12000)
        assert result == MoveResult(15000, 11040, clamped=True)


# -- velocity ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "requested,expected",
    [(1.0, 1.14), (0.38, 0.38), (2.0, 1.9), (38.1, 38.0), (0.5, 0.38)],
)
def test_set_velocity_quantizes(driver, fake_plotter, requested, expected):
    assert driver.set_velocity(requested) == expected
    _drain(fake_plotter)
    assert fake_plotter.velocity == expected


@pytest.mark.parametrize("bad", [0.1, 0.0, 38.2, 100.0, -1.0])
def test_set_velocity_rejects_out_of_range(driver, bad):
    with pytest.raises(ValueError):
        driver.set_velocity(bad)


# -- park / initialize ---------------------------------------------------------------


def test_park_moves_to_corner_and_stores_pen(driver, fake_plotter):
    driver.select_pen(2)
    driver.move_abs(1000, 1000)
    _drain(fake_plotter)
    driver.park()
    _drain(fake_plotter)
    assert fake_plotter.pen == 0
    assert (fake_plotter.x, fake_plotter.y) == (0, 0)
    assert not fake_plotter.pen_down


def test_initialize_clears_error_state(driver, fake_plotter):
    fake_plotter.hpgl_error = 3
    fake_plotter.pen_down = True
    driver.initialize_device()
    _drain(fake_plotter)
    assert fake_plotter.hpgl_error == 0
    assert not fake_plotter.pen_down
    assert (fake_plotter.x, fake_plotter.y) == (0, 0)  # IN never moves


# -- completion (hardware-notes §5) ----------------------------------------------------


def test_await_completion_sentinel_after_buffered_plot(driver, fake_plotter):
    fake_plotter.set_exec_delay(0.01)
    stream = b"PD100,100;PA200,200;" * 20
    driver._transport.send_chunked(stream)
    driver._transport.write(protocol.HPGL_OUTPUT_ACTUAL_POSITION.encode())
    driver.await_completion("200,200,1", timeout=10.0)
    assert (fake_plotter.x, fake_plotter.y) == (200, 200)


def test_await_completion_with_status_polling(driver, fake_plotter):
    fake_plotter.set_exec_delay(0.01)
    stream = b"PU100,100;" * 10
    driver._transport.send_chunked(stream)
    driver._transport.write(protocol.HPGL_OUTPUT_ACTUAL_POSITION.encode())
    statuses = []
    driver.await_completion("100,100,0", timeout=10.0,
                            on_status=statuses.append, poll_interval=0.05)
    assert statuses  # at least one StatusReport surfaced


def test_await_completion_timeout_classifies_failed(driver, fake_plotter):
    fake_plotter.fault_timeout()
    with pytest.raises(TransportTimeout):
        driver.await_completion("999,999,0", timeout=0.5)


# -- fault classification (BUILD_SPEC §10/§29) ------------------------------------------


def test_query_timeout_classifies_failed(driver, fake_plotter):
    fake_plotter.fault_timeout()
    with pytest.raises(TransportTimeout):
        driver.status()


def test_malformed_reply_classifies_failed(driver, fake_plotter):
    fake_plotter.fault_malformed()
    with pytest.raises(TransportMalformed):
        driver.position()


def test_disconnect_classifies_disconnected(driver, fake_plotter):
    fake_plotter.fault_disconnect()
    with pytest.raises(DeviceDisconnected):
        driver.identify()


# -- SOFTWARE_CHECK accounting over pty (acceptance criterion) ----------------------------


def test_software_check_never_sends_more_than_free_minus_margin(fake_plotter):
    from app.services.serial.transport import SerialTransport, TransportSettings

    fake_plotter.set_exec_delay(0.01)
    settings = TransportSettings(read_timeout=2.0, chunk_size=256,
                                 safety_margin=32, stall_timeout=15.0)
    transport = SerialTransport()
    transport.open(fake_plotter.port_path, settings)
    try:
        stream = b"PA100,200;PD300,400;" * 80  # 1280B of 16B instructions
        transport.send_chunked(stream)
    finally:
        transport.close()
    cap = protocol.INPUT_BUFFER_BYTES
    margin = settings.safety_margin
    for occ_before, raw, occ_after, dropped in fake_plotter.occupancy_log:
        if protocol.ESC in raw.decode("ascii", "replace"):
            continue  # query bytes (ESC .B / ESC .E), not HP-GL chunks
        assert occ_after <= cap - margin, (occ_before, raw, occ_after)
    assert fake_plotter.rs232_error == 0  # no overflow ever happened
