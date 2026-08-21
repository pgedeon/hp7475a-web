/** VectorizePage: upload → options → background job (start/poll/stage/cancel)
 *  → preview → download / Send-to-Plot (goal a7f70dae job flow). */
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, act, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { AppProvider } from "../state/app";
import VectorizePage from "../pages/VectorizePage";
import { ApiError } from "../api/client";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      deviceStatus: vi.fn(),
      getPapers: vi.fn(),
      vectorizeStart: vi.fn(),
      vectorizeStatus: vi.fn(),
      vectorizeCancel: vi.fn(),
      vectorizeSvg: vi.fn(),
      uploadSvg: vi.fn(),
    },
  };
});
import { api } from "../api/client";
import type { VectorizeJobStatus } from "../api/types";
vi.mock("../lib/inkColors", () => ({
  detectInkColors: vi.fn(async () => 1),
}));
import { detectInkColors } from "../lib/inkColors";
const mockDetect = vi.mocked(detectInkColors);

const mockApi = vi.mocked(api);

const RESULT = {
  svg_id: "vec1", filename: "cat.svg", path: "vectorize/vec1/output.svg",
  duration_s: 23.4,
};
const SVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><path d="M0 0L100 100"/></svg>`;

const RUNNING = (stage: string, elapsed = 5): VectorizeJobStatus =>
  ({ status: "running", stage, elapsed_s: elapsed, result: null, error: null });
const DONE: VectorizeJobStatus = { status: "done", stage: null, elapsed_s: 23.4, result: RESULT, error: null };
const FAILED: VectorizeJobStatus = {
  status: "error", stage: null, elapsed_s: 61,
  result: null, error: { message: "SLD CLI failed", stderr_tail: "Traceback … CUDA error" },
};

function ui(node: ReactNode, onSentToPlot?: (m: unknown) => void) {
  return <AppProvider>{onSentToPlot
    ? <VectorizePage onSentToPlot={onSentToPlot} />
    : node}</AppProvider>;
}

async function selectImage() {
  fireEvent.change(await screen.findByLabelText("Image file"), {
    target: { files: [new File(["png"], "cat.png", { type: "image/png" })] },
  });
  await screen.findByText("cat.png");
  // Auto color detection runs async after upload; flush it.
  await act(async () => { await Promise.resolve(); });
}

/** Flush microtasks + one poll interval (POLL_MS = 2000, fake timers). */
async function tick(ms = 2000) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
}

beforeEach(() => {
  // shouldAdvanceTime: RTL waitFor + component sleeps coexist (auto-advance)
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.clearAllMocks();
  mockDetect.mockClear();
  mockDetect.mockImplementation(async () => 1);
  mockApi.deviceStatus.mockResolvedValue({
    connected: false, port: null, settings: null, status: null,
  });
  mockApi.getPapers.mockResolvedValue({});
});

afterEach(() => {
  vi.useRealTimers();
});

describe("VectorizePage", () => {
  it("renders empty state", async () => {
    render(ui(<VectorizePage />));
    expect(await screen.findByTestId("result-empty")).toBeInTheDocument();
    expect(screen.getByTestId("run-btn")).toBeDisabled();
  });

  it("default options send colors=1, auto threshold omitted", async () => {
    mockApi.vectorizeStart.mockResolvedValue({ job_id: "j1" });
    mockApi.vectorizeStatus.mockResolvedValue(DONE);
    mockApi.vectorizeSvg.mockResolvedValue(SVG);
    render(ui(<VectorizePage />));
    await selectImage();
    expect(screen.getByTestId("thresh-input")).toBeDisabled();
    fireEvent.click(screen.getByTestId("run-btn"));
    await tick(0);
    expect(mockApi.vectorizeStart).toHaveBeenCalledWith(
      expect.any(File), { thresh: null, multipleLines: false, colors: 1 });
    await tick(); // poll 1 → done
    await screen.findByTestId("result");
  });

  it("manual threshold + multiple lines forwarded (2 → 0.99)", async () => {
    mockApi.vectorizeStart.mockResolvedValue({ job_id: "j1" });
    mockApi.vectorizeStatus.mockResolvedValue(DONE);
    mockApi.vectorizeSvg.mockResolvedValue(SVG);
    render(ui(<VectorizePage />));
    await selectImage();
    fireEvent.click(screen.getByTestId("thresh-manual"));
    expect(screen.getByTestId("thresh-input")).toBeEnabled();
    fireEvent.change(screen.getByTestId("thresh-input"), { target: { value: "2" } });
    fireEvent.click(screen.getByTestId("multiple-lines"));
    fireEvent.click(screen.getByTestId("run-btn"));
    await tick(0);
    expect(mockApi.vectorizeStart).toHaveBeenCalledWith(
      expect.any(File), { thresh: 0.99, multipleLines: true, colors: 1 });
    await tick();
    await screen.findByTestId("result");
  });

  it("colors ≥ 2 enables multicolor mode and forwards colors", async () => {
    mockApi.vectorizeStart.mockResolvedValue({ job_id: "j1" });
    mockApi.vectorizeStatus.mockResolvedValue(DONE);
    mockApi.vectorizeSvg.mockResolvedValue(SVG);
    render(ui(<VectorizePage />));
    await selectImage();
    fireEvent.change(screen.getByTestId("colors-input"), { target: { value: "3" } });
    expect(screen.getByTestId("multicolor-hint")).toBeInTheDocument();
    expect(screen.getByTestId("thresh-input")).toBeDisabled();
    expect(screen.queryByTestId("multiple-lines")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("run-btn"));
    await tick(0);
    expect(mockApi.vectorizeStart).toHaveBeenCalledWith(
      expect.any(File), { thresh: null, multipleLines: false, colors: 3 });
    await tick();
    await screen.findByTestId("result");
  });

  it("colors input clamps to 1..8", async () => {
    render(ui(<VectorizePage />));
    await selectImage();
    const input = screen.getByTestId("colors-input");
    fireEvent.change(input, { target: { value: "99" } });
    expect((input as HTMLInputElement).value).toBe("8");
    fireEvent.change(input, { target: { value: "0" } });
    expect((input as HTMLInputElement).value).toBe("1");
  });

  it("auto-detects ink colors on upload and populates Colors", async () => {
    mockDetect.mockImplementation(async () => 4);
    render(ui(<VectorizePage />));
    await selectImage();
    await waitFor(() =>
      expect(screen.getByTestId("colors-input")).toHaveValue(4));
    expect(screen.getByTestId("detected-colors")).toHaveTextContent("4");
  });

  it("detection failure leaves colors untouched", async () => {
    mockDetect.mockImplementation(async () => {
      throw new Error("canvas unavailable");
    });
    render(ui(<VectorizePage />));
    await selectImage();
    await waitFor(() => expect(mockDetect).toHaveBeenCalled());
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByTestId("colors-input")).toHaveValue(1);
    expect(screen.queryByTestId("detected-colors")).not.toBeInTheDocument();
  });

  it("busy state polls, shows stage + elapsed, disables Run", async () => {
    mockApi.vectorizeStart.mockResolvedValue({ job_id: "j1" });
    mockApi.vectorizeStatus
      .mockResolvedValueOnce(RUNNING("loading image"))
      .mockResolvedValue(DONE);
    mockApi.vectorizeSvg.mockResolvedValue(SVG);
    render(ui(<VectorizePage />));
    await selectImage();
    fireEvent.click(screen.getByTestId("run-btn"));
    await tick(0);
    expect(screen.getByTestId("busy")).toBeInTheDocument();
    expect(screen.getByTestId("cancel-btn")).toBeInTheDocument();
    expect(screen.getByTestId("run-btn")).toBeDisabled();
    await tick(); // poll 1 → running w/ stage
    expect(screen.getByTestId("stage").textContent).toBe("loading image");
    expect(screen.getByTestId("elapsed").textContent).toBe("5 s");
    expect(mockApi.vectorizeStatus).toHaveBeenCalledWith("j1");
    await tick(); // poll 2 → done
    await waitFor(() => expect(screen.getByTestId("result")).toBeInTheDocument());
    expect(screen.queryByTestId("busy")).not.toBeInTheDocument();
    expect(screen.queryByTestId("cancel-btn")).not.toBeInTheDocument();
  });

  it("success shows preview + duration", async () => {
    mockApi.vectorizeStart.mockResolvedValue({ job_id: "j1" });
    mockApi.vectorizeStatus.mockResolvedValue(DONE);
    mockApi.vectorizeSvg.mockResolvedValue(SVG);
    render(ui(<VectorizePage />));
    await selectImage();
    fireEvent.click(screen.getByTestId("run-btn"));
    await tick(0);
    await tick();
    const res = await screen.findByTestId("result");
    expect(res.textContent).toContain("cat.svg");
    expect(res.textContent).toContain("23.4 s");
    expect(screen.getByTestId("vectorize-preview").querySelector("path")).not.toBeNull();
  });

  it("job error surfaces message + stderr tail", async () => {
    mockApi.vectorizeStart.mockResolvedValue({ job_id: "j1" });
    mockApi.vectorizeStatus.mockResolvedValue(FAILED);
    render(ui(<VectorizePage />));
    await selectImage();
    fireEvent.click(screen.getByTestId("run-btn"));
    await tick(0);
    await tick();
    const banner = await screen.findByTestId("error-banner");
    expect(banner.textContent).toContain("SLD CLI failed");
    expect(screen.getByTestId("stderr-tail").textContent).toContain("CUDA error");
  });

  it("HTTP failure on start surfaces via error mapping", async () => {
    mockApi.vectorizeStart.mockRejectedValue(new ApiError(502, {
      message: "boom", stderr_tail: "tail",
    }));
    render(ui(<VectorizePage />));
    await selectImage();
    fireEvent.click(screen.getByTestId("run-btn"));
    await tick(0);
    const banner = await screen.findByTestId("error-banner");
    expect(banner.textContent).toContain("boom");
  });

  it("Cancel calls DELETE and leaves the busy state", async () => {
    mockApi.vectorizeStart.mockResolvedValue({ job_id: "j1" });
    mockApi.vectorizeStatus.mockResolvedValue(RUNNING("ordering strokes"));
    mockApi.vectorizeCancel.mockResolvedValue({ status: "cancelling" });
    render(ui(<VectorizePage />));
    await selectImage();
    fireEvent.click(screen.getByTestId("run-btn"));
    await tick(0);
    await tick();
    expect(screen.getByTestId("busy")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("cancel-btn"));
    await tick(0);
    expect(mockApi.vectorizeCancel).toHaveBeenCalledWith("j1");
    await waitFor(() =>
      expect(screen.queryByTestId("busy")).not.toBeInTheDocument());
    expect(screen.getByTestId("run-btn")).toBeEnabled();
  });

  it("Send to Plot uploads the SVG through the normal flow + navigates", async () => {
    mockApi.vectorizeStart.mockResolvedValue({ job_id: "j1" });
    mockApi.vectorizeStatus.mockResolvedValue(DONE);
    mockApi.vectorizeSvg.mockResolvedValue(SVG);
    mockApi.uploadSvg.mockResolvedValue({
      id: "f9", name: "cat.svg", size: SVG.length, sanitize: { ok: true },
    });
    const onSent = vi.fn();
    render(ui(undefined, onSent));
    await selectImage();
    fireEvent.click(screen.getByTestId("run-btn"));
    await tick(0);
    await tick();
    await screen.findByTestId("result");
    fireEvent.click(screen.getByTestId("send-to-plot"));
    await waitFor(() => expect(mockApi.uploadSvg).toHaveBeenCalledTimes(1));
    const f = mockApi.uploadSvg.mock.calls[0][0] as File;
    expect(f.name).toBe("cat.svg");
    expect(f.type).toBe("image/svg+xml");
    expect(mockApi.uploadSvg.mock.calls[0][1]).toBe(false);
    expect(onSent).toHaveBeenCalledWith(expect.objectContaining({ id: "f9", name: "cat.svg" }));
  });

  it("Download SVG creates a blob URL from the result", async () => {
    const createObjectURL = vi.fn(() => "blob:mock");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL, revokeObjectURL }));
    mockApi.vectorizeStart.mockResolvedValue({ job_id: "j1" });
    mockApi.vectorizeStatus.mockResolvedValue(DONE);
    mockApi.vectorizeSvg.mockResolvedValue(SVG);
    render(ui(<VectorizePage />));
    await selectImage();
    fireEvent.click(screen.getByTestId("run-btn"));
    await tick(0);
    await tick();
    await screen.findByTestId("result");
    fireEvent.click(screen.getByTestId("download-svg"));
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:mock");
    vi.unstubAllGlobals();
  });
});
