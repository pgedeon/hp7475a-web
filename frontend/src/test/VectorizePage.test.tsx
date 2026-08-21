/** VectorizePage: upload → options → run (busy/elapsed) → preview →
 *  download / Send-to-Plot (uploads through the normal file flow). */
import { beforeEach, describe, expect, it, vi } from "vitest";
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
      vectorize: vi.fn(),
      vectorizeSvg: vi.fn(),
      uploadSvg: vi.fn(),
    },
  };
});
import { api } from "../api/client";
const mockApi = vi.mocked(api);

const RESULT = {
  svg_id: "vec1", filename: "cat.svg", path: "vectorize/vec1/output.svg",
  duration_s: 23.4,
};
const SVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><path d="M0 0L100 100"/></svg>`;

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
}

beforeEach(() => {
  vi.clearAllMocks();
  mockApi.deviceStatus.mockResolvedValue({
    connected: false, port: null, settings: null, status: null,
  });
  mockApi.getPapers.mockResolvedValue({});
});

describe("VectorizePage", () => {
  it("renders empty state", async () => {
    render(ui(<VectorizePage />));
    expect(await screen.findByTestId("result-empty")).toBeInTheDocument();
    expect(screen.getByTestId("run-btn")).toBeDisabled();
  });

  it("auto threshold (default) omits thresh, sends multiple_lines=false", async () => {
    mockApi.vectorize.mockResolvedValue(RESULT);
    mockApi.vectorizeSvg.mockResolvedValue(SVG);
    render(ui(<VectorizePage />));
    await selectImage();
    expect(screen.getByTestId("thresh-input")).toBeDisabled();
    await act(async () => { fireEvent.click(screen.getByTestId("run-btn")); });
    expect(mockApi.vectorize).toHaveBeenCalledWith(
      expect.any(File), { thresh: null, multipleLines: false });
    await screen.findByTestId("result");
  });

  it("manual threshold sends the clamped number (2 → 0.99)", async () => {
    mockApi.vectorize.mockResolvedValue(RESULT);
    mockApi.vectorizeSvg.mockResolvedValue(SVG);
    render(ui(<VectorizePage />));
    await selectImage();
    fireEvent.click(screen.getByTestId("thresh-manual"));
    expect(screen.getByTestId("thresh-input")).toBeEnabled();
    fireEvent.change(screen.getByTestId("thresh-input"), { target: { value: "2" } });
    fireEvent.click(screen.getByTestId("multiple-lines"));
    await act(async () => { fireEvent.click(screen.getByTestId("run-btn")); });
    expect(mockApi.vectorize).toHaveBeenCalledWith(
      expect.any(File), { thresh: 0.99, multipleLines: true });
  });

  it("busy state shows elapsed timer and disables Run until done", async () => {
    let resolveRun: (v: typeof RESULT) => void = () => {};
    mockApi.vectorize.mockReturnValue(new Promise((res) => { resolveRun = res; }));
    mockApi.vectorizeSvg.mockResolvedValue(SVG);
    render(ui(<VectorizePage />));
    await selectImage();
    await act(async () => { fireEvent.click(screen.getByTestId("run-btn")); });
    expect(screen.getByTestId("busy")).toBeInTheDocument();
    expect(screen.getByTestId("elapsed").textContent).toContain("0 s");
    expect(screen.getByTestId("run-btn")).toBeDisabled();
    await act(async () => { resolveRun(RESULT); });
    await waitFor(() => expect(screen.getByTestId("result")).toBeInTheDocument());
    expect(screen.queryByTestId("busy")).not.toBeInTheDocument();
  });

  it("success shows preview + duration", async () => {
    mockApi.vectorize.mockResolvedValue(RESULT);
    mockApi.vectorizeSvg.mockResolvedValue(SVG);
    render(ui(<VectorizePage />));
    await selectImage();
    await act(async () => { fireEvent.click(screen.getByTestId("run-btn")); });
    const res = await screen.findByTestId("result");
    expect(res.textContent).toContain("cat.svg");
    expect(res.textContent).toContain("23.4 s");
    expect(screen.getByTestId("vectorize-preview").querySelector("path")).not.toBeNull();
  });

  it("502 {message, stderr_tail} surfaces both", async () => {
    mockApi.vectorize.mockRejectedValue(new ApiError(502, {
      message: "SLD CLI failed", stderr_tail: "Traceback … CUDA error",
    }));
    render(ui(<VectorizePage />));
    await selectImage();
    await act(async () => { fireEvent.click(screen.getByTestId("run-btn")); });
    const banner = await screen.findByTestId("error-banner");
    expect(banner.textContent).toContain("SLD CLI failed");
    expect(screen.getByTestId("stderr-tail").textContent).toContain("CUDA error");
  });

  it("Send to Plot uploads the SVG through the normal flow + navigates", async () => {
    mockApi.vectorize.mockResolvedValue(RESULT);
    mockApi.vectorizeSvg.mockResolvedValue(SVG);
    mockApi.uploadSvg.mockResolvedValue({
      id: "f9", name: "cat.svg", size: SVG.length, sanitize: { ok: true },
    });
    const onSent = vi.fn();
    render(ui(undefined, onSent));
    await selectImage();
    await act(async () => { fireEvent.click(screen.getByTestId("run-btn")); });
    await screen.findByTestId("result");
    await act(async () => { fireEvent.click(screen.getByTestId("send-to-plot")); });
    expect(mockApi.uploadSvg).toHaveBeenCalledTimes(1);
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
    mockApi.vectorize.mockResolvedValue(RESULT);
    mockApi.vectorizeSvg.mockResolvedValue(SVG);
    render(ui(<VectorizePage />));
    await selectImage();
    await act(async () => { fireEvent.click(screen.getByTestId("run-btn")); });
    await screen.findByTestId("result");
    fireEvent.click(screen.getByTestId("download-svg"));
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:mock");
    vi.unstubAllGlobals();
  });
});
