/** ManualPage: jog disabled when disconnected, clamping display on move. */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { AppProvider } from "../state/app";
import Toasts from "../components/Toast";
import ManualPage, { clampToPaper } from "../pages/ManualPage";
import { FakeWebSocket } from "./fakews";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      deviceStatus: vi.fn(),
      getPapers: vi.fn(),
      move: vi.fn(),
      selectPen: vi.fn(),
      penUp: vi.fn(),
      penDown: vi.fn(),
      park: vi.fn(),
    },
  };
});
import { api } from "../api/client";
const mockApi = vi.mocked(api);

const A4 = { size_mm: [297, 210] as [number, number], x_range: [0, 11040] as [number, number], y_range: [0, 7721] as [number, number], dip_mode: "metric" };

beforeEach(() => {
  vi.clearAllMocks();
  FakeWebSocket.reset();
  vi.stubGlobal("WebSocket", FakeWebSocket);
  mockApi.getPapers.mockResolvedValue({ a4: A4 });
  mockApi.move.mockResolvedValue({});
});

describe("clampToPaper (pure)", () => {
  it("clamps out-of-range coordinates and flags axes", () => {
    expect(clampToPaper(999, -5, A4)).toEqual({ x: 297, y: 0, clampedX: true, clampedY: true });
    expect(clampToPaper(100, 100, A4)).toEqual({ x: 100, y: 100, clampedX: false, clampedY: false });
  });
});

describe("ManualPage", () => {
  it("disables controls and shows warning when disconnected", async () => {
    mockApi.deviceStatus.mockResolvedValue({ connected: false, port: null, settings: null, status: null });
    render(<AppProvider><ManualPage /><Toasts /></AppProvider>);
    expect(await screen.findByTestId("manual-disabled")).toHaveTextContent(/Not connected/);
    expect(screen.getByLabelText("jog up 1 mm")).toBeDisabled();
    expect(screen.getByText("Pen up")).toBeDisabled();
  });

  it("connected: move-to clamps to A4 and warns", async () => {
    mockApi.deviceStatus.mockResolvedValue({ connected: true, port: "/dev/ttyUSB0", settings: null, status: { status: 16 } });
    render(<AppProvider><ManualPage /><Toasts /></AppProvider>);
    await screen.findByTestId("manual-disabled").catch(() => null);
    await screen.findByText("Move to X/Y (mm)");

    fireEvent.change(screen.getByLabelText("target x mm"), { target: { value: "999" } });
    fireEvent.change(screen.getByLabelText("target y mm"), { target: { value: "50" } });
    await act(async () => { fireEvent.click(screen.getByText("Move", { selector: "button" })); });

    expect(mockApi.move).toHaveBeenCalledWith(297, 50, "mm");
    expect(screen.getByTestId("clamp-warn")).toHaveTextContent(/Clamped to A4/);
  });

  it("jog moves by selected step from assumed position", async () => {
    mockApi.deviceStatus.mockResolvedValue({ connected: true, port: "/dev/ttyUSB0", settings: null, status: { status: 16 } });
    render(<AppProvider><ManualPage /><Toasts /></AppProvider>);
    await screen.findByText("Move to X/Y (mm)");
    fireEvent.click(screen.getByLabelText("10 mm"));
    await act(async () => { fireEvent.click(screen.getByLabelText("jog right 10 mm")); });
    expect(mockApi.move).toHaveBeenCalledWith(10, 0, "mm");
  });
});
