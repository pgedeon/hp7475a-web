/** DiagnosticsPage: OS bits, ESC.B buffer field, OE query, WS log render. */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { AppProvider } from "../state/app";
import DiagnosticsPage from "../pages/DiagnosticsPage";
import { FakeWebSocket } from "./fakews";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      deviceStatus: vi.fn(),
      getPapers: vi.fn(),
      deviceError: vi.fn(),
    },
  };
});
import { api } from "../api/client";
const mockApi = vi.mocked(api);

beforeEach(() => {
  vi.clearAllMocks();
  FakeWebSocket.reset();
  vi.stubGlobal("WebSocket", FakeWebSocket);
  mockApi.getPapers.mockResolvedValue({});
});

describe("DiagnosticsPage", () => {
  it("disconnected → empty state, query disabled", async () => {
    mockApi.deviceStatus.mockResolvedValue({ connected: false, port: null, settings: null, status: null });
    render(<AppProvider><DiagnosticsPage /></AppProvider>);
    expect(await screen.findByTestId("diag-empty")).toHaveTextContent(/not connected/i);
    expect(screen.getByText("Query error")).toBeDisabled();
    expect(screen.getByTestId("ws-empty")).toHaveTextContent(/No events yet/);
  });

  it("connected → status bits, buffer field, OE query result", async () => {
    mockApi.deviceStatus.mockResolvedValue({
      connected: true, port: "/dev/ttyUSB0", settings: null,
      status: { status: 17, buffer_free: 1024 }, // pen down + ready
    });
    mockApi.deviceError.mockResolvedValue({ error: 0, message: "no error" });
    render(<AppProvider><DiagnosticsPage /></AppProvider>);
    const st = await screen.findByTestId("diag-status");
    expect(st).toHaveTextContent("pen down");
    expect(st).toHaveTextContent("ready");
    expect(screen.getByTestId("escb")).toHaveTextContent("1024");
    await act(async () => { fireEvent.click(screen.getByText("Query error")); });
    expect(mockApi.deviceError).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("oee")).toHaveTextContent("no error");
  });

  it("backend down → banner with retry; WS events render in log", async () => {
    mockApi.deviceStatus.mockRejectedValue(Object.assign(new Error("HTTP 0"), { status: 0 }));
    render(<AppProvider><DiagnosticsPage /></AppProvider>);
    expect(await screen.findByText(/Status poll failed/)).toBeInTheDocument();
    expect(screen.getByText("Retry")).toBeInTheDocument();

    act(() => {
      FakeWebSocket.emit({ type: "job", job_id: "j123456789", status: "SENDING", bytes_sent: 5, bytes_total: 10 });
      FakeWebSocket.emit({ type: "device", event: "status" });
    });
    const log = screen.getByTestId("ws-log");
    expect(log).toHaveTextContent("job j1234567 SENDING 5/10");
    expect(log).toHaveTextContent("device");
  });
});
