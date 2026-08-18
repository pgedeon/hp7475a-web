/** JobsPage: table render, WS progress patch, drawer HP-GL, replot, delete. */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { AppProvider } from "../state/app";
import Toasts from "../components/Toast";
import JobsPage, { applyJobEvent } from "../pages/JobsPage";
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
      listJobs: vi.fn(),
      deleteJob: vi.fn(),
      createJob: vi.fn(),
    },
  };
});
import { api } from "../api/client";
const mockApi = vi.mocked(api);

const job = (over: Partial<Job>): Job => ({
  id: "j1", name: "test.svg", status: "COMPLETED", file_id: "f1", paper: "a4",
  pen_map: { cut: 1 }, options: {}, hpgl: "IN;SP1;PA0,0;PD;PA100,100;PU;SP0;",
  bytes_total: 100, bytes_sent: 100, error: null, stats: { pipeline: {} },
  created_at: 1723990000, updated_at: 1723990100, ...over,
});

beforeEach(() => {
  vi.clearAllMocks();
  FakeWebSocket.reset();
  vi.stubGlobal("WebSocket", FakeWebSocket);
  mockApi.deviceStatus.mockResolvedValue({ connected: false, port: null, settings: null, status: null });
  mockApi.getPapers.mockResolvedValue({});
});

describe("applyJobEvent (pure)", () => {
  it("patches matching row, leaves others", () => {
    const jobs = [job({ id: "a" }), job({ id: "b" })];
    const next = applyJobEvent(jobs, { job_id: "b", status: "SENDING", bytes_sent: 10, bytes_total: 50 });
    expect(next[0].status).toBe("COMPLETED");
    expect(next[1]).toMatchObject({ id: "b", status: "SENDING", bytes_sent: 10, bytes_total: 50 });
  });
  it("ignores unknown ids", () => {
    const jobs = [job({ id: "a" })];
    expect(applyJobEvent(jobs, { job_id: "zzz", status: "FAILED" })).toHaveLength(1);
  });
});

describe("JobsPage", () => {
  it("renders empty state", async () => {
    mockApi.listJobs.mockResolvedValue({ jobs: [], active_job_id: null });
    render(<AppProvider><JobsPage /><Toasts /></AppProvider>);
    expect(await screen.findByTestId("jobs-empty")).toHaveTextContent(/No jobs yet/);
  });

  it("renders rows with badges and active highlight", async () => {
    mockApi.listJobs.mockResolvedValue({
      jobs: [job({ id: "j1" }), job({ id: "j2", name: "other.svg", status: "FAILED", error: "timeout" })],
      active_job_id: "j1",
    });
    render(<AppProvider><JobsPage /><Toasts /></AppProvider>);
    expect(await screen.findByText("test.svg")).toBeInTheDocument();
    expect(screen.getByText("COMPLETED")).toBeInTheDocument();
    expect(screen.getByText("FAILED")).toBeInTheDocument();
  });

  it("WS progress updates the job row bytes", async () => {
    mockApi.listJobs.mockResolvedValue({ jobs: [job({ status: "SENDING", bytes_sent: 0, bytes_total: 2000 })], active_job_id: "j1" });
    render(<AppProvider><JobsPage /><Toasts /></AppProvider>);
    await screen.findByText("SENDING");
    expect(screen.getByRole("progressbar").textContent).toContain("0.0 KB");
    act(() => {
      FakeWebSocket.emit({ type: "job", job_id: "j1", status: "SENDING", bytes_sent: 1000, bytes_total: 2000 });
    });
    expect(screen.getByRole("progressbar").textContent).toContain("50.0%");
  });

  it("drawer shows HP-GL inspector with truncation + stats", async () => {
    mockApi.listJobs.mockResolvedValue({ jobs: [job({ hpgl: "PU;".repeat(2000) })], active_job_id: null });
    render(<AppProvider><JobsPage /><Toasts /></AppProvider>);
    fireEvent.click(await screen.findByText("Details"));
    const pre = await screen.findByTestId("hpgl-preview");
    expect(pre.textContent).toContain("bytes total");
    expect(pre.textContent!.length).toBeLessThan(5300);
    expect(screen.getByText("Copy HP-GL")).toBeInTheDocument();
    expect(screen.getByText("Download .hpgl")).toBeInTheDocument();
  });

  it("replot creates a duplicate job", async () => {
    mockApi.listJobs.mockResolvedValue({ jobs: [job({})], active_job_id: null });
    mockApi.createJob.mockResolvedValue(job({ id: "j2", name: "Replot — test.svg" }));
    render(<AppProvider><JobsPage /><Toasts /></AppProvider>);
    fireEvent.click(await screen.findByText("Replot"));
    await screen.findByText(/Replot job created/);
    expect(mockApi.createJob).toHaveBeenCalledWith(expect.objectContaining({
      file_id: "f1", paper: "a4", pen_map: { cut: 1 },
    }));
  });

  it("delete asks for confirmation and removes on confirm", async () => {
    mockApi.listJobs
      .mockResolvedValueOnce({ jobs: [job({})], active_job_id: null })
      .mockResolvedValue({ jobs: [], active_job_id: null });
    mockApi.deleteJob.mockResolvedValue({ deleted: "j1" });
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<AppProvider><JobsPage /><Toasts /></AppProvider>);
    fireEvent.click(await screen.findByText("Delete"));
    expect(confirmSpy).toHaveBeenCalled();
    await screen.findByTestId("jobs-empty");
    expect(mockApi.deleteJob).toHaveBeenCalledWith("j1");
    confirmSpy.mockRestore();
  });

  it("backend down → error banner with retry", async () => {
    mockApi.listJobs.mockRejectedValue(Object.assign(new Error("HTTP 0"), { status: 0 }));
    render(<AppProvider><JobsPage /><Toasts /></AppProvider>);
    expect(await screen.findByText(/Failed to load jobs/)).toBeInTheDocument();
    expect(screen.getByText("Retry")).toBeInTheDocument();
  });
});
