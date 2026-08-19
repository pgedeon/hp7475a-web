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
      getPapers: vi.fn(),
      uploadSvg: vi.fn(),
      fileRaw: vi.fn(),
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

describe("PlotPage phase 2 — velocity, copies, estimate, pen badge, travel", () => {
  async function prepFlow() {
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
    await screen.findByTestId("analysis");
  }

  it("velocity slider defaults to 38.1 and sends the chosen value", async () => {
    await prepFlow();
    const slider = screen.getByTestId("vel-slider") as HTMLInputElement;
    expect(slider.value).toBe("38.1");
    expect(screen.getByTestId("vel-value").textContent).toContain("default");
    fireEvent.change(slider, { target: { value: "20.14" } });
    expect(screen.getByTestId("vel-value").textContent).toContain("20.14");
    await act(async () => { fireEvent.click(screen.getByTestId("prepare-btn")); });
    expect(mockApi.createJob).toHaveBeenCalledWith(expect.objectContaining({
      options: expect.objectContaining({ velocity_cm_s: 20.14 }),
    }));
  });


  it("copies inputs send a tiling grid only when > 1×1", async () => {
    await prepFlow();
    // default: no copies key in options
    await act(async () => { fireEvent.click(screen.getByTestId("prepare-btn")); });
    const first = mockApi.createJob.mock.calls[0][0] as { options: Record<string, unknown> };
    expect(first.options.copies).toBeUndefined();

    fireEvent.change(screen.getByTestId("copies-rows"), { target: { value: "2" } });
    fireEvent.change(screen.getByTestId("copies-cols"), { target: { value: "3" } });
    fireEvent.change(screen.getByTestId("copies-spacing"), { target: { value: "8" } });
    expect(screen.getByText("2×3 = 6 copies")).toBeInTheDocument();
    await act(async () => { fireEvent.click(screen.getByTestId("prepare-btn")); });
    const second = mockApi.createJob.mock.calls[1][0] as { options: Record<string, unknown> };
    expect(second.options.copies).toEqual({ rows: 2, cols: 3, spacing_mm: 8 });
  });

  it("shows the plot-time estimate + pen badge on the live job card", async () => {
    mockApi.uploadSvg.mockResolvedValue({ id: "f1", name: "d.svg", size: 1, sanitize: {} });
    mockApi.analysis.mockResolvedValue({ layers: ["L"], stroke_colors: [], unsupported: [] });
    mockApi.createJob.mockResolvedValue({
      ...JOB, status: "READY", bytes_total: 4096, bytes_sent: 4096,
      estimate: { drawn_mm: 900, travel_mm: 100, velocity_cm_s: 38.1, est_seconds: 300 },
    });
    mockApi.prepareJob.mockResolvedValue({ accepted: true });
    render(ui(<PlotPage />));
    await act(async () => {
      fireEvent.change(await screen.findByLabelText("SVG file"), {
        target: { files: [new File(["x"], "d.svg")] },
      });
    });
    await act(async () => { fireEvent.click(screen.getByTestId("prepare-btn")); });
    act(() => {
      FakeWebSocket.emit({ type: "job", job_id: "job-1", status: "SENDING", bytes_total: 4096, bytes_sent: 0 });
    });
    expect((await screen.findByTestId("estimate")).textContent ?? "").toMatch(/5\.0 min/);

    act(() => {
      FakeWebSocket.emit({ type: "job", event: "progress", job_id: "job-1",
        acked_bytes: 2048, total_bytes: 4096, pen_down: true });
    });
    expect((await screen.findByTestId("pen-badge")).textContent ?? "").toContain("pen down");
  });

  it("progress events update bytes without a full job frame", async () => {
    mockApi.uploadSvg.mockResolvedValue({ id: "f1", name: "d.svg", size: 1, sanitize: {} });
    mockApi.analysis.mockResolvedValue({ layers: ["L"], stroke_colors: [], unsupported: [] });
    mockApi.createJob.mockResolvedValue({ ...JOB, status: "SENDING", bytes_total: 4096 });
    mockApi.prepareJob.mockResolvedValue({ accepted: true });
    render(ui(<PlotPage />));
    await act(async () => {
      fireEvent.change(await screen.findByLabelText("SVG file"), {
        target: { files: [new File(["x"], "d.svg")] },
      });
    });
    await act(async () => { fireEvent.click(screen.getByTestId("prepare-btn")); });
    act(() => {
      FakeWebSocket.emit({ type: "job", event: "progress", job_id: "job-1",
        acked_bytes: 1024, total_bytes: 4096, pen_down: null });
    });
    expect((await screen.findByRole("progressbar")).textContent ?? "").toContain("25.0%");
  });

  it("travel toggle flips preview visibility class (no re-fetch)", async () => {
    mockApi.uploadSvg.mockResolvedValue({ id: "f1", name: "d.svg", size: 1, sanitize: {} });
    mockApi.analysis.mockResolvedValue({ layers: ["L"], stroke_colors: [], unsupported: [] });
    mockApi.createJob.mockResolvedValue({ ...JOB, status: "READY", bytes_total: 10, bytes_sent: 10 });
    mockApi.prepareJob.mockResolvedValue({ accepted: true });
    mockApi.jobPreview.mockResolvedValue(
      `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 297 210">` +
      `<g class="travel-group"><polyline class="travel" points="0,0 10,10"/></g>` +
      `<path d="M0 0L100 100" stroke="#f00"/></svg>`
    );
    render(ui(<PlotPage />));
    await act(async () => {
      fireEvent.change(await screen.findByLabelText("SVG file"), {
        target: { files: [new File(["x"], "d.svg")] },
      });
    });
    await act(async () => { fireEvent.click(screen.getByTestId("prepare-btn")); });
    act(() => {
      FakeWebSocket.emit({ type: "job", job_id: "job-1", status: "READY", bytes_total: 10, bytes_sent: 10 });
    });
    const toggle = await screen.findByTestId("travel-toggle") as HTMLInputElement;
    const wrap = screen.getByTestId("preview-state");
    expect(wrap.className).not.toContain("show-travel");
    expect(toggle.checked).toBe(false);
    const fetchesBefore = mockApi.jobPreview.mock.calls.length;
    fireEvent.click(toggle);
    expect(wrap.className).toContain("show-travel");
    expect(mockApi.jobPreview.mock.calls.length).toBe(fetchesBefore); // CSS-only
  });
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
  // default: no artwork svg fetched (older tests exercise other branches)
  mockApi.fileRaw.mockResolvedValue(null);
  vi.stubGlobal("WebSocket", FakeWebSocket);
  mockApi.deviceStatus.mockResolvedValue({ connected: true, port: "/dev/ttyUSB0", settings: null, status: { status: 16 } });
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
    mockApi.analysis.mockResolvedValue({ layers: ["cut"], stroke_colors: ["#f00"], unsupported: [] });
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
      expect.objectContaining({ file_id: "f1", paper: "a4", pen_map: { cut: 1 } })
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
    expect(screen.getByText("Start plot — the plotter WILL move")).toBeInTheDocument();
    expect(screen.getByTestId("confirm-start")).toBeDisabled();
    fireEvent.click(screen.getByTestId("confirm-check"));
    expect(screen.getByTestId("confirm-start")).toBeEnabled();
    await act(async () => {
      fireEvent.click(screen.getByTestId("confirm-start"));
    });
    expect(mockApi.startJob).toHaveBeenCalledWith("job-1");
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

  it("sends rotate_90 + margin_mm in job options and flips the orientation label", async () => {
    mockApi.uploadSvg.mockResolvedValue({ id: "f1", name: "portrait.svg", size: 100, sanitize: {} });
    mockApi.analysis.mockResolvedValue({
      layers: ["L"], stroke_colors: [], unsupported: [],
      bbox_mm: { min_x: 0, min_y: 0, max_x: 180, max_y: 260 }, // portrait artwork
      est_paper_fit: { a4: true }, fit_rotate90: { a4: true },
    });
    mockApi.createJob.mockResolvedValue({ ...JOB });
    mockApi.prepareJob.mockResolvedValue({ accepted: true });

    render(ui(<PlotPage />));
    await act(async () => {
      fireEvent.change(await screen.findByLabelText("SVG file"), {
        target: { files: [new File(["<svg/>"], "portrait.svg")] },
      });
    });
    await screen.findByTestId("analysis");

    // 180x260 on A4 (297x210 in carriage frame): fits only rotated → hint
    expect(screen.getByTestId("rotation-hint").textContent)
      .toContain("fits A4 only when rotated");
    expect(screen.getByTestId("orientation-label").textContent).toContain("portrait");

    fireEvent.click(screen.getByTestId("opt-rotate90"));
    expect(screen.getByTestId("orientation-label").textContent).toContain("landscape → portrait");
    fireEvent.change(screen.getByTestId("margin-input"), { target: { value: "15" } });

    await act(async () => { fireEvent.click(screen.getByTestId("prepare-btn")); });
    expect(mockApi.createJob).toHaveBeenCalledWith(expect.objectContaining({
      options: expect.objectContaining({ rotate_90: true, margin_mm: 15 }),
    }));
  });
});

describe("PlotPage phase 3 — instant artwork preview, convert option, hints", () => {
  const ARTWORK = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50"><path d="M0 0L10 10" stroke="#f00"/></svg>';
  const PLACEMENT = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 297 210"><path d="M0 0L100 100"/></svg>';

  it("shows artwork preview right after upload, before any job (F5)", async () => {
    mockApi.uploadSvg.mockResolvedValue({ id: "f1", name: "d.svg", size: 1, sanitize: {},
      text_converted: false, conversion: { attempted: false, converted: false, warning: null } });
    mockApi.analysis.mockResolvedValue({ layers: ["L"], stroke_colors: [], unsupported: [] });
    mockApi.fileRaw.mockResolvedValue(ARTWORK);
    render(ui(<PlotPage />));
    await act(async () => {
      fireEvent.change(await screen.findByLabelText("SVG file"), {
        target: { files: [new File(["x"], "d.svg")] },
      });
    });
    await screen.findByTestId("analysis");
    expect(await screen.findByTestId("artwork-preview")).toBeInTheDocument();
    expect(screen.getByTestId("artwork-label").textContent)
      .toContain("Artwork preview — configure & create job for on-paper placement");
    // no placement preview until a job is prepared
    expect(screen.queryByTestId("preview-state")).toBeNull();
  });

  it("placement preview replaces artwork when the job is READY (F5)", async () => {
    mockApi.uploadSvg.mockResolvedValue({ id: "f1", name: "d.svg", size: 1, sanitize: {} });
    mockApi.analysis.mockResolvedValue({ layers: ["L"], stroke_colors: [], unsupported: [] });
    mockApi.fileRaw.mockResolvedValue(ARTWORK);
    mockApi.createJob.mockResolvedValue({ ...JOB });
    mockApi.prepareJob.mockResolvedValue({ accepted: true });
    mockApi.jobPreview.mockResolvedValue(PLACEMENT);
    render(ui(<PlotPage />));
    await act(async () => {
      fireEvent.change(await screen.findByLabelText("SVG file"), {
        target: { files: [new File(["x"], "d.svg")] },
      });
    });
    await screen.findByTestId("artwork-preview");
    await act(async () => { fireEvent.click(screen.getByTestId("prepare-btn")); });
    act(() => {
      FakeWebSocket.emit({ type: "job", job_id: "job-1", status: "READY", bytes_sent: 0, bytes_total: 2048 });
    });
    await waitFor(() => expect(mockApi.jobPreview).toHaveBeenCalledWith("job-1"));
    expect(await screen.findByTestId("preview-state")).toBeInTheDocument();
    expect(screen.queryByTestId("artwork-preview")).toBeNull(); // swapped out
  });

  it("convert checkbox is sent with the upload (default off)", async () => {
    mockApi.uploadSvg.mockResolvedValue({ id: "f1", name: "d.svg", size: 1, sanitize: {} });
    mockApi.analysis.mockResolvedValue({ layers: ["L"], stroke_colors: [], unsupported: [] });
    mockApi.fileRaw.mockResolvedValue(ARTWORK);
    render(ui(<PlotPage />));
    const check = await screen.findByTestId("convert-text") as HTMLInputElement;
    expect(check.checked).toBe(false);
    await act(async () => {
      fireEvent.change(screen.getByLabelText("SVG file"), {
        target: { files: [new File(["x"], "d.svg")] },
      });
    });
    expect(mockApi.uploadSvg).toHaveBeenCalledWith(expect.anything(), false);
    fireEvent.click(check);
    expect(check.checked).toBe(true);
    await act(async () => {
      fireEvent.change(screen.getByLabelText("SVG file"), {
        target: { files: [new File(["y"], "d2.svg")] },
      });
    });
    expect(mockApi.uploadSvg).toHaveBeenLastCalledWith(expect.anything(), true);
  });

  it("renders hints for unsupported content and the shortcut button flips convert (F7)", async () => {
    const warn = "text elements (convert to paths before plotting): 1";
    mockApi.uploadSvg.mockResolvedValue({ id: "f1", name: "d.svg", size: 1, sanitize: {} });
    mockApi.analysis.mockResolvedValue({
      layers: ["L"], stroke_colors: [], unsupported: [warn],
      hints: { [warn]: "enable 'Convert text to paths' when uploading (server-side Inkscape)" },
    });
    mockApi.fileRaw.mockResolvedValue(ARTWORK);
    render(ui(<PlotPage />));
    await act(async () => {
      fireEvent.change(await screen.findByLabelText("SVG file"), {
        target: { files: [new File(["x"], "d.svg")] },
      });
    });
    await screen.findByTestId("analysis");
    expect(screen.getByRole("alert")).toHaveTextContent(/Unsupported content: text elements/);
    const hints = screen.getByTestId("unsupported-hints");
    expect(hints.textContent).toContain("enable 'Convert text to paths'");
    const btn = screen.getByTestId("hint-convert-btn");
    fireEvent.click(btn);
    expect((screen.getByTestId("convert-text") as HTMLInputElement).checked).toBe(true);
    expect(screen.queryByTestId("hint-convert-btn")).toBeNull(); // hint button gone once enabled
  });

  it("shows conversion warning + converted note from the upload response (F6)", async () => {
    mockApi.uploadSvg.mockResolvedValue({ id: "f1", name: "d.svg", size: 1, sanitize: {},
      text_converted: false,
      conversion: { attempted: true, converted: false, warning: "text-to-path conversion unavailable (Inkscape not installed)" } });
    mockApi.analysis.mockResolvedValue({ layers: ["L"], stroke_colors: [], unsupported: [] });
    mockApi.fileRaw.mockResolvedValue(ARTWORK);
    render(ui(<PlotPage />));
    await act(async () => {
      fireEvent.change(await screen.findByLabelText("SVG file"), {
        target: { files: [new File(["x"], "d.svg")] },
      });
    });
    await screen.findByTestId("analysis");
    expect(screen.getByTestId("conversion-warning").textContent)
      .toContain("Inkscape not installed");
  });
});
