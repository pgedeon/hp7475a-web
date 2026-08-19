/** DevicePage wizard: port hints (FTDI, by-id, writable/dialout), step flow. */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { AppProvider } from "../state/app";
import Toasts from "../components/Toast";
import DevicePage from "../pages/DevicePage";
import { FakeWebSocket } from "./fakews";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      deviceStatus: vi.fn(),
      getPapers: vi.fn(),
      listPorts: vi.fn(),
      connect: vi.fn(),
      identify: vi.fn(),
      deviceError: vi.fn(),
      disconnect: vi.fn(),
    },
  };
});
import { api } from "../api/client";
const mockApi = vi.mocked(api);

beforeEach(() => {
  vi.clearAllMocks();
  FakeWebSocket.reset();
  vi.stubGlobal("WebSocket", FakeWebSocket);
  mockApi.deviceStatus.mockResolvedValue({ connected: false, port: null, settings: null, status: null });
  mockApi.getPapers.mockResolvedValue({});
});

describe("DevicePage wizard", () => {
  it("port scan shows FTDI badge, by-id path and hints", async () => {
    mockApi.listPorts.mockResolvedValue({
      ports: [
        {
          device: "/dev/ttyUSB0",
          by_id_path: "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A10OCJBA-if00-port0",
          description: "FT232R USB UART",
          vid: 1027, pid: 24577, ftdi: true, writable: true,
          hint: "FTDI adapter — classic HP 7475A companion",
        },
        {
          device: "/dev/ttyS0", description: "16550A UART", ftdi: false,
          writable: false, hint: "legacy UART — add user to dialout group",
        },
      ],
      selected: null,
    });
    render(<AppProvider><DevicePage /><Toasts /></AppProvider>);
    await act(async () => { fireEvent.click(await screen.findByTestId("refresh-ports")); });

    expect(await screen.findByText("/dev/ttyUSB0")).toBeInTheDocument();
    expect(screen.getByTestId("ftdi-badge")).toHaveTextContent("FTDI");
    expect(screen.getByText(/usb-FTDI_FT232R_USB_UART_A10OCJBA/)).toBeInTheDocument();
    const hints = screen.getAllByTestId("port-hint");
    expect(hints[0]).toHaveTextContent(/classic HP 7475A companion/);
    expect(screen.getByText(/usermod -aG dialout/)).toBeInTheDocument(); // dialout hint
  });

  it("empty port list → empty state with dialout guidance", async () => {
    mockApi.listPorts.mockResolvedValue({ ports: [], selected: null });
    render(<AppProvider><DevicePage /><Toasts /></AppProvider>);
    await act(async () => { fireEvent.click(await screen.findByTestId("refresh-ports")); });
    expect(await screen.findByTestId("ports-empty")).toHaveTextContent(/No serial ports found/);
  });

  it("step 1→2: settings default 9600 8N1, connect shows connected banner", async () => {
    mockApi.deviceStatus
      .mockResolvedValueOnce({ connected: false, port: null, settings: null, status: null })
      .mockResolvedValue({ connected: true, port: "/dev/ttyUSB0", settings: { baudrate: 9600, bytesize: 8, parity: "N", stopbits: 1 }, status: { status: 16 } });
    mockApi.listPorts.mockResolvedValue({
      ports: [{ device: "/dev/ttyUSB0", ftdi: true, writable: true }], selected: null,
    });
    mockApi.connect.mockResolvedValue({
      connected: true, port: "/dev/ttyUSB0",
      info: { identity: "7475A", buffer: 1024 },
    });
    mockApi.identify.mockResolvedValue({ identity: "7475A" });
    render(<AppProvider><DevicePage /><Toasts /></AppProvider>);
    await act(async () => { fireEvent.click(await screen.findByTestId("refresh-ports")); });
    fireEvent.click(screen.getByLabelText(/\/dev\/ttyUSB0/)); // radio
    fireEvent.click(screen.getByText("Next: serial settings"));
    expect(screen.getByText("Step 2 — Serial settings (rear-panel switches must match)")).toBeInTheDocument();

    await act(async () => { fireEvent.click(screen.getByTestId("connect-btn")); });
    expect(mockApi.connect).toHaveBeenCalledWith({
      port: "/dev/ttyUSB0", baudrate: 9600, bytesize: 8, parity: "N", stopbits: 1,
    });

    // Post-connect the wizard is replaced by the connected banner (identify
    // step only renders while the device reports disconnected).
    expect(await screen.findByTestId("device-connected")).toHaveTextContent(/ttyUSB0/);
  });
});
