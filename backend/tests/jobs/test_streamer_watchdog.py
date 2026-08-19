"""Streamer ESC.E overflow watchdog regression (2026-08-19 incident).

A silent input-buffer overflow mid-PD corrupts the plot (horizontal
garbage lines). The watchdog must abort the job loudly on error 16,
both when the buffer stalls and at end-of-stream.
"""
import threading

import pytest

from app.jobs.streamer import ChunkedStreamer, StreamerFatal


class StubTransport:
    """Minimal TransportLike: reports free space, optional overflow."""

    def __init__(self, free=1024, error_code=0):
        self.free = free
        self.error_code = error_code
        self.written = bytearray()

    def query(self, data, timeout, retries):
        if b".B" in data:
            return str(self.free)
        if b".E" in data:
            return "garbage-proof: caller parses via extended_error()"
        return "0"

    def write(self, data):
        self.written.extend(data)
        return len(data)

    def extended_error(self):
        return (self.error_code, "input buffer overflow" if self.error_code == 16 else "ok")


PAYLOAD = "IN;DF;SP1;" + "PU10,10;PD" + ",".join(
    [f"{100 + i},{200 + i}" for i in range(60)]
) + ";PU5000,5000;SP0;"


def _run(t):
    s = ChunkedStreamer(t, safety_margin=8, default_chunk=1024,
                        query_timeout_s=0.05, max_retries=0,
                        zero_free_poll_s=0.01, zero_free_max_wait_s=0.5)
    return s.stream(PAYLOAD, pause_event=threading.Event(),
                    cancel_event=threading.Event())


def test_clean_stream_passes():
    t = StubTransport(free=1024, error_code=0)
    sent = _run(t)
    assert sent == len(PAYLOAD)
    assert bytes(t.written) == PAYLOAD.encode()


def test_overflow_on_stall_aborts():
    t = StubTransport(free=4, error_code=16)  # never room + overflow latched
    with pytest.raises(StreamerFatal, match="overflow"):
        _run(t)


def test_overflow_at_end_aborts():
    # buffer drains fine but .E latched 16 from an earlier hiccup
    t = StubTransport(free=1024, error_code=16)
    with pytest.raises(StreamerFatal, match="overflow"):
        _run(t)


def test_boundary_only_never_splits_mid_instruction():
    """2026-08-19 root-cause fix: a chunk boundary inside an instruction
    corrupted plots on real hardware (OE 2, misassembled coordinates).
    Every transport write must end at a ';' boundary regardless of the
    window size."""
    class RecTransport(StubTransport):
        def __init__(self):
            super().__init__()
            self.calls = []
        def write(self, data):
            self.calls.append(bytes(data))
            return super().write(data)

    short = "IN;SP1;" + "PU10,10;PD20,30;PD30,10;" * 20 + "PU0,0;SP0;"
    rt = RecTransport()
    s = ChunkedStreamer(rt, safety_margin=8, default_chunk=32,
                        query_timeout_s=0.05, max_retries=0,
                        zero_free_poll_s=0.01, zero_free_max_wait_s=0.5)
    sent = s.stream(short, pause_event=threading.Event(),
                    cancel_event=threading.Event())
    assert sent == len(short)
    assert bytes(rt.written) == short.encode()
    partials = [c for c in rt.calls if not c.endswith(b";")]
    assert not partials, f"mid-instruction write(s): {[c[-20:] for c in partials]}"
    assert len(rt.calls) > 3, "expected several boundary chunks, got one big write"


def test_oversized_instruction_refused_loudly():
    """An instruction that can never fit the safe window must abort with a
    clear message instead of hanging or silently splitting."""
    huge = "PD" + ",".join(f"{100 + i},{200 + i}" for i in range(400)) + ";"
    payload = "IN;SP1;PU0,0;" + huge + "SP0;"
    t = StubTransport(free=1024, error_code=0)
    s = ChunkedStreamer(t, safety_margin=8, default_chunk=1024,
                        query_timeout_s=0.05, max_retries=0,
                        zero_free_poll_s=0.01, zero_free_max_wait_s=0.5)
    with pytest.raises(StreamerFatal, match="exceeds"):
        s.stream(payload, pause_event=threading.Event(),
                 cancel_event=threading.Event())
