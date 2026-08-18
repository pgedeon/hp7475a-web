/** PlotPage flow: upload → sanitize report → analysis → pen map → prepare →
 *  WS READY → preview → confirm modal gating. */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, act, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { AppProvider } from "../state/app";
import PlotPage from "../pages/PlotPage";
import Toasts from "../components/Toast";
import { FakeWebSocket } from "./fakews";
import type { Job } from "../api/types";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      deviceStatus: vi.fn(),
      hardClip: vi.fn(),
      getPapers: vi.fn(),
      uploadSvg: vi.fn(),
      analysis: vi.fn(),
      createJob: vi.fn(),
      prepareJob: vi.fn(),
      getJob: vi.fn(),
      jobPreview: vi.fn(),
      startJob: vi.fn(),
      listPorts: vi.fn(),
    },
  };
});
import { api } from "../api/client";

const mockApi = vi.mocked(api);

function ui(node: ReactNode) {
  return <AppProvider>{node}<Toasts /></AppProvider>;
}

const JOB: Job = {
  id: "job-1", name: "drawing.svg", status: "QUEUED", file_id: "f1", paper: "a4",
  pen_map: { cut: 1, engrave: 2 }, options: {}, hpgl: "", bytes_total: 0,
  bytes_sent: 0, error: null, stats: {}, created_at: 1, updated_at: 1,
};

beforeEach(() => {
  vi.clearAllMocks();
  FakeWebSocket.reset();
  vi.stubGlobal("WebSocket", FakeWebSocket);
  mockApi.deviceStatus.mockResolvedValue({ connected: true, port: "/dev/ttyUSB0", settings: null, status: { status: 16 } });
  mockApi.hardClip.mockResolvedValue({ limits: [0, 0, 11040, 7721], paper: "a4" });
  mockApi.getPapers.mockResolvedValue({
    a4: { size_mm: [297, 210], x_range: [0, 11040], y_range: [0, 7721], dip_mode: "metric", info: "Plotter must be configured in Metric mode (rear DIP)." },
    a3: { size_mm: [420, 297], x_range: [0, 16158], y_range: [0, 11040], dip_mode: "metric", info: "" },
    a: { size_mm: [279.4, 215.9], x_range: [0, 10365], y_range: [0, 7962], dip_mode: "imperial", info: "" },
    b: { size_mm: [431.8, 279.4], x_range: [0, 16640], y_range: [0, 10365], dip_mode: "imperial", info: "" },
  });
});

describe("PlotPage", () => {
  it("uploads SVG and renders sanitize report + analysis", async () => {
    mockApi.uploadSvg.mockResolvedValue({
      id: "f1", name: "drawing.svg", size: 4096,
      sanitize: { removed: ["script"], warnings: [], ok: true },
    });
    mockApi.analysis.mockResolvedValue({
      layers: ["cut", "engrave"], stroke_colors: ["#ff0000", "#00ff00"],
      unsupported: ["text"], est_paper_fit: { a4: true, a3: true },
    });

    render(ui(<PlotPage />));
    const input = await screen.findByLabelText("SVG file");
    const file = new File(["<svg/>"], "drawing.svg", { type: "image/svg+xml" });
    await act(async () => {
      fireEvent.change(input, { target: { files: [file] } });
    });

    expect(mockApi.uploadSvg).toHaveBeenCalledTimes(1);
    expect(await screen.findByTestId("sanitize-report")).toHaveTextContent("script");
    expect(screen.getByTestId("analysis")).toHaveTextContent("cut");
    expect(screen.getByRole("alert")).toHaveTextContent(/Unsupported content: text/);
    // auto pen map assigned
    expect(screen.getByLabelText("pen for layer cut")).toHaveValue("1");
    expect(screen.getByLabelText("pen for layer engrave")).toHaveValue("2");
    // DIP hint from paper table
    expect(screen.getByTestId("dip-hint")).toHaveTextContent("metric");
  });

  it("shows toast error when upload fails (422)", async () => {
    mockApi.uploadSvg.mockRejectedValue(
      Object.assign(new Error("x"), { status: 422, detail: "SVG rejected: contains script" })
    );
    render(ui(<PlotPage />));
    fireEvent.change(await screen.findByLabelText("SVG file"), {
      target: { files: [new File(["x"], "bad.svg")] },
    });
    expect(await screen.findByText(/Upload failed/)).toBeInTheDocument();
    expect(screen.queryByTestId("sanitize-report")).toBeNull();
  });

  it("prepare → WS READY → preview → confirm modal blocks start until checked", async () => {
    mockApi.uploadSvg.mockResolvedValue({ id: "f1", name: "drawing.svg", size: 100, sanitize: {} });
    mockApi.analysis.mockResolvedValue({ layers: ["cut", "engrave"], stroke_colors: ["#f00"], unsupported: [] });
    mockApi.createJob.mockResolvedValue({ ...JOB });
    mockApi.prepareJob.mockResolvedValue({ accepted: true });
    mockApi.jobPreview.mockResolvedValue(
      `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 297 210"><path d="M0 0L100 100" stroke="#f00"/></svg>`
    );

    render(ui(<PlotPage />));
    await act(async () => {
      fireEvent.change(await screen.findByLabelText("SVG file"), {
        target: { files: [new File(["<svg/>"], "drawing.svg")] },
      });
    });
    await screen.findByTestId("analysis");

    await act(async () => {
      fireEvent.click(screen.getByTestId("prepare-btn"));
    });
    expect(mockApi.createJob).toHaveBeenCalledWith(
      expect.objectContaining({
        file_id: "f1", paper: "a4", pen_map: { cut: 1, engrave: 2 }, pen_map_mode: "layers",
      })
    );
    expect(mockApi.prepareJob).toHaveBeenCalledWith("job-1");

    // Simulate backend WS: job became READY
    act(() => {
      FakeWebSocket.emit({ type: "job", job_id: "job-1", status: "READY", bytes_sent: 0, bytes_total: 2048 });
    });
    expect(await screen.findByText("READY")).toBeInTheDocument();
    await waitFor(() => expect(mockApi.jobPreview).toHaveBeenCalledWith("job-1"));

    // Open confirmation modal — Start Plot must be gated behind the checkbox
    fireEvent.click(screen.getByTestId("plot-btn"));
    expect(screen.getByRole("heading", { name: /the plotter WILL move/i })).toBeInTheDocument();
    expect(screen.getByTestId("confirm-start")).toBeDisabled();
    fireEvent.click(screen.getByTestId("confirm-check"));
    expect(screen.getByTestId("confirm-start")).toBeEnabled();
    await act(async () => {
      fireEvent.click(screen.getByTestId("confirm-start"));
    });
    expect(mockApi.startJob).toHaveBeenCalledWith("job-1");
  });

  it("defaults to By Color when ≤1 layer; toggle switches mapping source", async () => {
    mockApi.uploadSvg.mockResolvedValue({ id: "f2", name: "flat.svg", size: 10, sanitize: {} });
    mockApi.analysis.mockResolvedValue({ layers: ["only"], stroke_colors: ["#ff0000", "#0000ff"], unsupported: [] });

    render(ui(<PlotPage />));
    await act(async () => {
      fireEvent.change(await screen.findByLabelText("SVG file"), {
        target: { files: [new File(["x"], "flat.svg")] },
      });
    });

    // 1 layer → colors are the default grouping
    expect(screen.getByTestId("mode-colors")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText("pen for color #ff0000")).toBeInTheDocument();
    expect(screen.queryByLabelText("pen for layer only")).toBeNull();

    // toggle to layers — same file, different rows
    fireEvent.click(screen.getByTestId("mode-layers"));
    expect(screen.getByTestId("mode-layers")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText("pen for layer only")).toBeInTheDocument();
    expect(screen.queryByLabelText("pen for color #ff0000")).toBeNull();

    // toggle back — color selections were kept (separate maps per mode)
    fireEvent.click(screen.getByTestId("mode-colors"));
    expect(screen.getByLabelText("pen for color #ff0000")).toHaveValue("1");
  });

  it("color rows render a swatch with the stroke hex as background", async () => {
    mockApi.uploadSvg.mockResolvedValue({ id: "f3", name: "c.svg", size: 10, sanitize: {} });
    mockApi.analysis.mockResolvedValue({ layers: [], stroke_colors: ["#ff0000", "#0000ff"], unsupported: [] });

    render(ui(<PlotPage />));
    await act(async () => {
      fireEvent.change(await screen.findByLabelText("SVG file"), {
        target: { files: [new File(["x"], "c.svg")] },
      });
    });
    const row = screen.getByLabelText("pen for color #0000ff").closest("tr");
    expect(row).not.toBeNull();
    expect(row!.querySelector(".swatch")).toHaveStyle({ background: "#0000ff" });
    expect(screen.getAllByText("#0000ff").length).toBeGreaterThan(0);
  });

  it("job create payload carries pen_map_mode colors + color-keyed pen_map", async () => {
    mockApi.uploadSvg.mockResolvedValue({ id: "f4", name: "colors.svg", size: 10, sanitize: {} });
    mockApi.analysis.mockResolvedValue({ layers: [], stroke_colors: ["#ff0000", "#0000ff"], unsupported: [] });
    mockApi.createJob.mockResolvedValue({ ...JOB, pen_map: { "#ff0000": 1, "#0000ff": 2 } });
    mockApi.prepareJob.mockResolvedValue({ accepted: true });

    render(ui(<PlotPage />));
    await act(async () => {
      fireEvent.change(await screen.findByLabelText("SVG file"), {
        target: { files: [new File(["x"], "colors.svg")] },
      });
    });
    await act(async () => { fireEvent.click(screen.getByTestId("prepare-btn")); });
    expect(mockApi.createJob).toHaveBeenCalledWith(expect.objectContaining({
      pen_map_mode: "colors",
      pen_map: { "#ff0000": 1, "#0000ff": 2 },
    }));
  });

  it("falls back to layer mode with a notice when stroke_colors are absent", async () => {
    mockApi.uploadSvg.mockResolvedValue({ id: "f5", name: "layers-only.svg", size: 10, sanitize: {} });
    // ≤1 layer defaults to colors, but no stroke_colors reported → fall back
    mockApi.analysis.mockResolvedValue({ layers: ["solo"], stroke_colors: [], unsupported: [] });

    render(ui(<PlotPage />));
    await act(async () => {
      fireEvent.change(await screen.findByLabelText("SVG file"), {
        target: { files: [new File(["x"], "layers-only.svg")] },
      });
    });
    expect(screen.getByTestId("color-mode-unavailable")).toHaveTextContent(/no stroke colors/i);
    expect(screen.getByTestId("mode-layers")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText("pen for layer solo")).toBeInTheDocument();
  });

  it("WS progress patches the live job row", async () => {
    mockApi.uploadSvg.mockResolvedValue({ id: "f1", name: "d.svg", size: 1, sanitize: {} });
    mockApi.analysis.mockResolvedValue({ layers: ["L"], stroke_colors: [], unsupported: [] });
    mockApi.createJob.mockResolvedValue({ ...JOB });
    mockApi.prepareJob.mockResolvedValue({ accepted: true });

    render(ui(<PlotPage />));
    await act(async () => {
      fireEvent.change(await screen.findByLabelText("SVG file"), {
        target: { files: [new File(["x"], "d.svg")] },
      });
    });
    await act(async () => { fireEvent.click(screen.getByTestId("prepare-btn")); });
    act(() => {
      FakeWebSocket.emit({ type: "job", job_id: "job-1", status: "SENDING", bytes_sent: 1024, bytes_total: 4096 });
    });
    expect(await screen.findByText("SENDING")).toBeInTheDocument();
    expect(screen.getByRole("progressbar").textContent).toContain("25.0%");
  });

  /** Upload helper for the paper/scale tests: de-nested findBy (awaiting a
   *  query inside act() triggers React's act-environment console.error). */
  async function uploadDrawing() {
    const input = await screen.findByLabelText("SVG file");
    await act(async () => {
      fireEvent.change(input, {
        target: { files: [new File(["<svg/>"], "drawing.svg")] },
      });
    });
    await screen.findByTestId("analysis");
  }

  function mockPrepareFlow() {
    mockApi.uploadSvg.mockResolvedValue({ id: "f1", name: "drawing.svg", size: 100, sanitize: {} });
    mockApi.analysis.mockResolvedValue({ layers: ["cut"], stroke_colors: [], unsupported: [] });
    mockApi.createJob.mockResolvedValue({ ...JOB });
    mockApi.prepareJob.mockResolvedValue({ accepted: true });
  }

  it("defaults paper to the hard-clip detected size and shows the hint", async () => {
    mockApi.hardClip.mockResolvedValue({ limits: [0, 0, 16158, 11040], paper: "a3" });
    mockPrepareFlow();
    render(ui(<PlotPage />));
    await uploadDrawing();
    expect(screen.getByTestId("plotter-paper-hint")).toHaveTextContent("Plotter: A3");
    expect(screen.getByRole("radio", { name: /A3/ })).toBeChecked();
  });

  it("warns when picked paper is larger than the plotter's", async () => {
    mockPrepareFlow();
    render(ui(<PlotPage />));
    await uploadDrawing();
    await screen.findByTestId("plotter-paper-hint"); // default mock: a4
    fireEvent.click(screen.getByRole("radio", { name: /A3/ }));
    expect(screen.getByTestId("paper-mismatch-warning"))
      .toHaveTextContent("Plotter is configured for A4 — plotting A3 will be rejected/clamped");
  });

  it("selecting A3 puts paper a3 in createJob payload", async () => {
    mockPrepareFlow();
    render(ui(<PlotPage />));
    await uploadDrawing();
    fireEvent.click(screen.getByRole("radio", { name: /A3/ }));
    await act(async () => { fireEvent.click(screen.getByTestId("prepare-btn")); });
    expect(mockApi.createJob).toHaveBeenCalledWith(expect.objectContaining({ paper: "a3" }));
  });

  it("scale slider value is sent as a 0-1 fraction in createJob payload", async () => {
    mockPrepareFlow();
    render(ui(<PlotPage />));
    await uploadDrawing();
    fireEvent.change(screen.getByTestId("scale-slider"), { target: { value: "50" } });
    expect(screen.getByTestId("scale-readout")).toHaveTextContent("50%");
    await act(async () => { fireEvent.click(screen.getByTestId("prepare-btn")); });
    expect(mockApi.createJob).toHaveBeenCalledWith(expect.objectContaining({
      paper: "a4", scale: 0.5, pen_map: { cut: 1 }, pen_map_mode: "layers",
    }));
  });

  it("re-prepares (debounced 400ms) on scale change, keeping pen map", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      mockPrepareFlow();
      render(ui(<PlotPage />));
      await uploadDrawing();
      await act(async () => { fireEvent.click(screen.getByTestId("prepare-btn")); });
      expect(mockApi.createJob).toHaveBeenCalledTimes(1);

      fireEvent.change(screen.getByTestId("scale-slider"), { target: { value: "50" } });
      // still inside the 400ms debounce window
      expect(mockApi.createJob).toHaveBeenCalledTimes(1);
      await act(async () => { vi.advanceTimersByTime(400); });
      await act(async () => {});
      expect(mockApi.createJob).toHaveBeenCalledTimes(2);
      expect(mockApi.createJob).toHaveBeenLastCalledWith(expect.objectContaining({
        file_id: "f1", paper: "a4", scale: 0.5, pen_map: { cut: 1 }, pen_map_mode: "layers",
      }));
      expect(mockApi.prepareJob).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });
});
