/** useStatusSocket: message delivery, log ring, reconnect after close. */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useStatusSocket } from "../api/ws";
import { FakeWebSocket } from "./fakews";

beforeEach(() => {
  FakeWebSocket.reset();
  vi.stubGlobal("WebSocket", FakeWebSocket);
  vi.useRealTimers();
});

describe("useStatusSocket", () => {
  it("delivers parsed messages and appends to log", () => {
    const onMessage = vi.fn();
    const { result } = renderHook(() => useStatusSocket(onMessage));
    act(() => {
      FakeWebSocket.emit({ type: "job", job_id: "j1", status: "SENDING", bytes_sent: 1, bytes_total: 2 });
    });
    expect(result.current.state).toBe("connecting"); // no open yet
    expect(result.current.last).toMatchObject({ type: "job", job_id: "j1" });
    expect(result.current.log).toHaveLength(1);
    expect(onMessage).toHaveBeenCalledWith(expect.objectContaining({ job_id: "j1" }));
  });

  it("caps the log at 50 entries", () => {
    const { result } = renderHook(() => useStatusSocket());
    act(() => {
      for (let i = 0; i < 60; i++) FakeWebSocket.emit({ type: "device", event: `e${i}` });
    });
    expect(result.current.log).toHaveLength(50);
    expect(result.current.log[49]).toMatchObject({ event: "e59" });
  });

  it("reconnects with backoff after close (backend restart)", () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useStatusSocket());
    const first = FakeWebSocket.instances[0];
    expect(first).toBeDefined();
    act(() => { first.onopen?.(); });
    expect(result.current.state).toBe("open");
    act(() => { first.onclose?.(); });
    expect(result.current.state).toBe("closed");
    expect(FakeWebSocket.instances).toHaveLength(1);
    act(() => { vi.advanceTimersByTime(1100); }); // first backoff ≈1s
    expect(FakeWebSocket.instances).toHaveLength(2); // reconnected
    act(() => { FakeWebSocket.instances[1].onopen?.(); });
    expect(result.current.state).toBe("open");
  });

  it("ignores malformed frames", () => {
    const { result } = renderHook(() => useStatusSocket());
    act(() => {
      for (const ws of FakeWebSocket.instances) ws.onmessage?.({ data: "{not json" });
    });
    expect(result.current.last).toBeNull();
  });
});
