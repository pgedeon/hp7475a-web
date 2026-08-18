/* Temporary bisect scratch — delete after debugging. */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { AppProvider } from "../state/app";
import PlotPage from "../pages/PlotPage";
import { FakeWebSocket } from "./fakews";
import type { Job } from "../api/types";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      deviceStatus: vi.fn(),
      getPapers: vi.fn(),
      uploadSvg: vi.fn(),
      analysis: vi.fn(),
      createJob: vi.fn(),
      prepareJob: vi.fn(),
      getJob: vi.fn(),
      jobPreview: vi.fn(),
      startJob: vi.fn(),
    },
  };
});
import { api } from "../api/client";
const mockApi = vi.mocked(api);

const JOB: Job = {
  id: "job-1", name: "drawing.svg", status: "QUEUED", file_id: "f1", paper: "a4",
  pen_map: { cut: 1 }, options: {}, hpgl: "", bytes_total: 0, bytes_sent: 0,
  error: null, stats: {}, created_at: 1, updated_at: 1,
};

beforeEach(() => {
  vi.clearAllMocks();
  FakeWebSocket.reset();
  vi.stubGlobal("WebSocket", FakeWebSocket);
  mockApi.deviceStatus.mockResolvedValue({ connected: true, port: "/dev/ttyUSB0", settings: null, status: { status: 16 } });
  mockApi.getPapers.mockResolvedValue({
    a4: { size_mm: [297, 210], x_range: [0, 11040], y_range: [0, 7721], dip_mode: "metric", info: "x" },
  });
});

describe("bisect", () => {
  it("C: B + WS emit", async () => {
    console.log("C start");
    mockApi.uploadSvg.mockResolvedValue({ id: "f1", name: "drawing.svg", size: 100, sanitize: {} });
    mockApi.analysis.mockResolvedValue({ layers: ["cut"], stroke_colors: ["#f00"], unsupported: [] });
    mockApi.createJob.mockResolvedValue({ ...JOB });
    mockApi.prepareJob.mockResolvedValue({ accepted: true });
    render(<AppProvider><PlotPage /></AppProvider>);
    console.log("C rendered");
    const input = await screen.findByLabelText("SVG file");
    console.log("C input found");
    await act(async () => {
      fireEvent.change(input, { target: { files: [new File(["<svg/>"], "drawing.svg")] } });
    });
    console.log("C uploaded");
    for (let i = 0; i < 15; i++) {
      const el = document.querySelector('[data-testid="analysis"]');
      console.log("poll", i, "found:", !!el, "bodyLen:", document.body.innerHTML.length);
      if (el) break;
      await new Promise((r) => setTimeout(r, 100));
    }
    expect(screen.getByTestId("analysis")).toBeInTheDocument();
    console.log("C analysis");
    await act(async () => {
      fireEvent.click(screen.getByTestId("prepare-btn"));
    });
    console.log("C prepare-act resolved");
    console.log("C emit");
    act(() => {
      FakeWebSocket.emit({ type: "job", job_id: "job-1", status: "READY", bytes_sent: 0, bytes_total: 2048 });
    });
    console.log("C emitted");
    expect(await screen.findByText("READY")).toBeInTheDocument();
    console.log("C done");
  });
});
