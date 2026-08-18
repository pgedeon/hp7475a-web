/** SettingsPage: read-only stream view + custom JSON edit/save. */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { AppProvider } from "../state/app";
import Toasts from "../components/Toast";
import SettingsPage from "../pages/SettingsPage";
import { FakeWebSocket } from "./fakews";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      deviceStatus: vi.fn(),
      getPapers: vi.fn(),
      getSettings: vi.fn(),
      putSettings: vi.fn(),
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

describe("SettingsPage", () => {
  it("renders stream settings read-only", async () => {
    mockApi.getSettings.mockResolvedValue({
      host: "127.0.0.1", port: 8750,
      stream: { safety_margin: 128, default_chunk: 256, query_timeout_s: 2, max_retries: 3, completion_timeout_s: 30 },
      job_history_keep: 50, custom: { plotter: { default_paper: "a4" } },
    });
    render(<AppProvider><SettingsPage /><Toasts /></AppProvider>);
    const dl = await screen.findByTestId("stream-settings");
    expect(dl).toHaveTextContent("safety_margin");
    expect(dl).toHaveTextContent("128");
    expect((screen.getByLabelText("custom settings JSON") as HTMLTextAreaElement).value).toContain("default_paper");
  });

  it("rejects invalid JSON before PUT", async () => {
    mockApi.getSettings.mockResolvedValue({ custom: {} });
    render(<AppProvider><SettingsPage /><Toasts /></AppProvider>);
    const ta = await screen.findByLabelText("custom settings JSON");
    fireEvent.change(ta, { target: { value: "{oops" } });
    expect(screen.getByTestId("save-settings")).toBeDisabled();
    expect(screen.getByText(/Invalid JSON/)).toBeInTheDocument();
    expect(mockApi.putSettings).not.toHaveBeenCalled();
  });

  it("saves valid custom settings", async () => {
    mockApi.getSettings.mockResolvedValue({ custom: { a: 1 } });
    mockApi.putSettings.mockResolvedValue({ saved: true });
    render(<AppProvider><SettingsPage /><Toasts /></AppProvider>);
    const ta = await screen.findByLabelText("custom settings JSON");
    fireEvent.change(ta, { target: { value: '{"b": 2}' } });
    await act(async () => { fireEvent.click(screen.getByTestId("save-settings")); });
    expect(mockApi.putSettings).toHaveBeenCalledWith({ b: 2 });
    expect(await screen.findByText("Settings saved")).toBeInTheDocument();
  });
});
