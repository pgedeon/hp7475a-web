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
  it("A: upload only", async () => {
    mockApi.uploadSvg.mockResolvedValue({ id: "f1", name: "drawing.svg", size: 100, sanitize: {} });
    mockApi.analysis.mockResolvedValue({ layers: ["cut"], stroke_colors: ["#f00"], unsupported: [] });
    render(<AppProvider><PlotPage /></AppProvider>);
    const input = await screen.findByLabelText("SVG file");
    await act(async () => {
      fireEvent.change(input, { target: { files: [new File(["<svg/>"], "drawing.svg")] } });
    });
    expect(await screen.findByTestId("analysis")).toBeInTheDocument();
  });

  it("B: upload + prepare click", async () => {
    console.log("B start");
    mockApi.uploadSvg.mockResolvedValue({ id: "f1", name: "drawing.svg", size: 100, sanitize: {} });
    mockApi.analysis.mockResolvedValue({ layers: ["cut"], stroke_colors: ["#f00"], unsupported: [] });
    mockApi.createJob.mockResolvedValue({ ...JOB });
    mockApi.prepareJob.mockResolvedValue({ accepted: true });
    render(<AppProvider><PlotPage /></AppProvider>);
    const input = await screen.findByLabelText("SVG file");
    console.log("B upload");
    await act(async () => {
      fireEvent.change(input, { target: { files: [new File(["<svg/>"], "drawing.svg")] } });
    });
    await screen.findByTestId("analysis");
    console.log("B click prepare");
    await act(async () => {
      fireEvent.click(screen.getByTestId("prepare-btn"));
    });
    console.log("B prepare done");
    expect(mockApi.prepareJob).toHaveBeenCalledWith("job-1");
  });

  it("C: B + WS emit", async () => {
    console.log("C start");
    mockApi.uploadSvg.mockResolvedValue({ id: "f1", name: "drawing.svg", size: 100, sanitize: {} });
    mockApi.analysis.mockResolvedValue({ layers: ["cut"], stroke_colors: ["#f00"], unsupported: [] });
    mockApi.createJob.mockResolvedValue({ ...JOB });
    mockApi.prepareJob.mockResolvedValue({ accepted: true });
    mockApi.jobPreview.mockResolvedValue(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 297 210"><path d="M0 0L1 1"/></svg>`);
    render(<AppProvider><PlotPage /></AppProvider>);
    const input = await screen.findByLabelText("SVG file");
    await act(async () => {
      fireEvent.change(input, { target: { files: [new File(["<svg/>"], "drawing.svg")] } });
    });
    await screen.findByTestId("analysis");
    await act(async () => {
      fireEvent.click(screen.getByTestId("prepare-btn"));
    });
    console.log("C emit");
    act(() => {
      FakeWebSocket.emit({ type: "job", job_id: "job-1", status: "READY", bytes_sent: 0, bytes_total: 2048 });
    });
    console.log("C emitted");
    expect(await screen.findByText("READY")).toBeInTheDocument();
    console.log("C done");
  });
});
