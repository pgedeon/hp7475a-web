"""Transport tests: chunker boundaries (property test), partial writes,
query retries, SOFTWARE_CHECK flow control incl. overflow abort, diagnostic
mode (BUILD_SPEC §10)."""

from __future__ import annotations

import random

import pytest

from app.services.serial import protocol
from app.services.serial.transport import (
    DeviceDisconnected,
    FlowControl,
    PlotterBufferOverflow,
    SerialTransport,
    TransportMalformed,
    TransportError,
    TransportSettings,
    TransportTimeout,
    split_chunks,
)

ESC_B = protocol.ESC_OUTPUT_BUFFER_SPACE.encode()
ESC_E = protocol.ESC_OUTPUT_EXTENDED_ERROR.encode()

INSTRUCTIONS = [
    "SP1;", "PU100,200;", "PA12,34;", "PA1,2,3,4;", "PD5,6;",
    "VS2.5;", "IN;", "PR-3,-4;", "PU;", "PD0,0;",
]


def test_chunker_rejoins_and_respects_boundaries():
    stream = b"PA1,2;PD3,4;PU;SP2;"
    chunks = split_chunks(stream, 5)
    assert b"".join(chunks) == stream
    # PA1,2;/PD3,4; oversized wholes (6B > 5); PU;SP2; = 7B > 5 → split
    assert chunks == [b"PA1,2;", b"PD3,4;", b"PU;", b"SP2;"]


def test_chunker_property_random_streams():
    """Chunker never splits mid-instruction for random instruction streams."""
    rng = random.Random(20260818)
    for _ in range(300):
        stream = "".join(rng.choices(INSTRUCTIONS, k=rng.randint(1, 40))).encode()
        max_len = rng.randint(1, 40)
        chunks = split_chunks(stream, max_len)
        assert b"".join(chunks) == stream
        for i, chunk in enumerate(chunks[:-1]):
            # every non-final chunk ends an instruction; it is either
            # room-sized or one whole (oversized) instruction
            assert chunk.endswith(b";")
            assert len(chunk) <= max_len or chunk.count(b";") == 1


def test_chunker_unterminated_tail_stays_whole():
    assert split_chunks(b"PA1,2", 3) == [b"PA1,2"]


def test_chunker_edges():
    assert split_chunks(b"", 10) == []
    with pytest.raises(ValueError):
        split_chunks(b"PA1;", 0)


# -- write / query semantics over MockSerial ---------------------------------


def _transport(mock_serial_cls, **settings) -> tuple[SerialTransport, object]:
    transport = SerialTransport()
    transport.open("/dev/mock", TransportSettings(**settings) if settings else None)
    return transport, transport._port


def test_write_handles_partial_writes(mock_serial_cls):
    transport, port = _transport(mock_serial_cls)
    port.write_limit = 3  # OS accepts 3 bytes per write call
    data = b"PA100,100;"
    transport.write(data)
    assert port.tx == data


def test_write_failure_raises_disconnected(mock_serial_cls):
    transport, port = _transport(mock_serial_cls)
    port.fail_writes = True
    with pytest.raises(DeviceDisconnected):
        transport.write(b"PU;")


def test_query_retries_after_timeout(mock_serial_cls):
    transport, port = _transport(mock_serial_cls, read_timeout=0.1, query_retries=2)
    port.reply_script[b"OI;"] = [None, "7475A"]  # 1st attempt silent, 2nd replies
    assert transport.query(b"OI;") == "7475A"
    assert len(port.writes) == 2  # proves a retry happened


def test_query_timeout_after_bounded_retries(mock_serial_cls):
    transport, port = _transport(mock_serial_cls, read_timeout=0.1, query_retries=1)
    with pytest.raises(TransportTimeout):
        transport.query(b"OI;")


def test_query_malformed_reply_raises_after_retries(mock_serial_cls):
    transport, port = _transport(mock_serial_cls, read_timeout=0.1, query_retries=1)
    port.reply_script[b"OA;"] = ["garbage", "garbage"]
    with pytest.raises(TransportMalformed):
        transport.query(b"OA;", parse=transport._responder.parse_position)


def test_query_read_failure_raises_disconnected(mock_serial_cls):
    transport, port = _transport(mock_serial_cls)
    port.fail_reads = True
    with pytest.raises(DeviceDisconnected):
        transport.query(b"OI;")


def test_send_chunked_requires_open_port():
    with pytest.raises(TransportError):
        SerialTransport().send_chunked(b"PU;")


def test_send_chunked_rejects_non_ascii(mock_serial_cls):
    transport, _ = _transport(mock_serial_cls)
    with pytest.raises(ValueError):
        transport.send_chunked(b"PA\xff1;")


# -- SOFTWARE_CHECK -----------------------------------------------------------


def test_software_check_chunk_plan_respects_free_minus_margin(mock_serial_cls):
    """Chunk sizes = min(free-32, chunk_size); only boundary-safe chunks."""
    transport, port = _transport(mock_serial_cls, chunk_size=256, safety_margin=32)
    data = b"P1;" * 200  # 600 bytes, every 3B an instruction
    # free replies consumed one per ESC.B write; final ESC.E -> 0
    port.reply_script[ESC_B] = ["1024", "64", "1024", "1024"]
    port.reply_script[ESC_E] = ["0"]
    transport.send_chunked(data)
    hpgl_writes = [w for w in port.writes if w not in (ESC_B, ESC_E)]
    assert [len(w) for w in hpgl_writes] == [255, 30, 255, 60]  # 3B instructions
    assert b"".join(hpgl_writes) == data


def test_software_check_aborts_when_free_zero_and_error_16(mock_serial_cls):
    transport, port = _transport(mock_serial_cls)
    port.reply_script[ESC_B] = ["0"]
    port.reply_script[ESC_E] = ["16"]
    with pytest.raises(PlotterBufferOverflow, match="overflow"):
        transport.send_chunked(b"PA1,2;")
    assert all(w in (ESC_B, ESC_E) for w in port.writes)  # no HP-GL sent


def test_software_check_final_error_sweep_catches_overflow(mock_serial_cls):
    """Overflow reported only at the end (after stream completed) still aborts."""
    transport, port = _transport(mock_serial_cls, chunk_size=256)
    port.reply_script[ESC_B] = ["1024"]
    port.reply_script[ESC_E] = ["16"]
    with pytest.raises(PlotterBufferOverflow):
        transport.send_chunked(b"PA1,2;" * 10)


def test_software_check_oversized_instruction_refused(mock_serial_cls):
    transport, port = _transport(mock_serial_cls)
    port.reply_script[ESC_B] = ["1024"]
    port.reply_script[ESC_E] = ["0"]
    huge = ("PA" + "1," * 998 + "1;").encode()  # 2001B single instruction
    with pytest.raises(PlotterBufferOverflow, match="single instruction"):
        transport.send_chunked(huge)


def test_software_check_stall_times_out(mock_serial_cls):
    """Buffer reports nearly-full forever → bounded stall → TransportTimeout."""
    transport, port = _transport(
        mock_serial_cls, chunk_size=256, poll_delay=0.01, stall_timeout=0.1
    )
    port.reply_script[ESC_B] = ["16"] * 100  # free=16 < margin 32 forever
    port.reply_script[ESC_E] = ["0"] * 100
    with pytest.raises(TransportTimeout):
        transport.send_chunked(b"PA1,2;PA3,4;" * 5)


# -- other strategies ----------------------------------------------------------


def test_diagnostic_mode_fixed_chunks_with_delay(mock_serial_cls):
    transport, port = _transport(
        mock_serial_cls,
        flow_control=FlowControl.DIAGNOSTIC,
        diagnostic_chunk=64, diagnostic_delay=0,
    )
    data = b"PA1,2;" * 30  # 180B
    transport.send_chunked(data)
    assert port.tx == data
    assert all(len(w) <= 64 for w in port.writes)
    assert not any(w.startswith(b"\x1b") for w in port.writes)  # no queries


def test_xonxoff_mode_chunked_without_queries(mock_serial_cls):
    transport, port = _transport(
        mock_serial_cls, flow_control=FlowControl.XON_XOFF, chunk_size=32
    )
    data = b"PD1,1;PD2,2;" * 20
    transport.send_chunked(data)
    assert port.tx == data
    assert all(len(w) <= 32 for w in port.writes)
    assert port.kwargs.get("xonxoff") is True
    assert port.kwargs.get("rtscts") is False  # HARDWARE_DTR caveat: never rtscts


def test_hardware_dtr_mode_never_enables_rtscts(mock_serial_cls):
    transport, port = _transport(mock_serial_cls, flow_control=FlowControl.HARDWARE_DTR)
    data = b"PU;PA1,2;"
    transport.send_chunked(data)
    assert port.tx == data
    assert port.kwargs.get("rtscts") is False
