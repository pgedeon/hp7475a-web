/** Typed fetch wrapper — every REST call in the app goes through here.
 *  Base path `/api` (Vite dev proxy / same-origin production).
 *  Errors: HTTP problems raise ApiError{status, detail}; network failure
 *  raises ApiError{status:0} so pages can show "backend down + retry". */

import type {
  Analysis, AppSettings, ConnectBody, ConnectResult, DeviceError, DeviceStatus,
  Job, JobCreateBody, PaperInfo, PortInfo, SanitizeReport, UploadSvgResult,
} from "./types";

const BASE: string = import.meta.env.VITE_API_BASE ?? "/api";

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown) {
    super(typeof detail === "string" && detail ? detail : `HTTP ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

function detailOf(body: unknown, status: number): unknown {
  if (body && typeof body === "object" && "detail" in body) {
    const d = (body as { detail: unknown }).detail;
    return d ?? `HTTP ${status}`;
  }
  return `HTTP ${status}: ${typeof body === "string" ? body.slice(0, 300) : "request failed"}`;
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(BASE + path, init);
  } catch {
    throw new ApiError(0, "Backend unreachable — is the server running on :8750?");
  }
  if (!res.ok) {
    let body: unknown = null;
    try { body = await res.json(); } catch { /* non-JSON error body */ }
    throw new ApiError(res.status, detailOf(body, res.status));
  }
  return (await res.json()) as T;
}

function post(path: string, body?: unknown): Promise<Record<string, unknown>> {
  return req<Record<string, unknown>>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
}

/** Extract a human message from a FastAPI 422 detail (string or object). */
export function apiErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (typeof err.detail === "string") return err.detail;
    try { return JSON.stringify(err.detail); } catch { return err.message; }
  }
  if (err instanceof Error) return err.message;
  return String(err);
}

export const api = {
  health: () => req<{ status: string }>("/health"),

  listPorts: () => req<{ ports: PortInfo[]; selected: string | null }>("/serial/ports"),

  connect: (body: ConnectBody) => req<ConnectResult>("/device/connect", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }),

  disconnect: () => post("/device/disconnect"),
  identify: () => post("/device/identify"),
  deviceStatus: () => req<DeviceStatus>("/device/status"),
  deviceError: () => req<DeviceError>("/device/error"),

  selectPen: (pen: number) => post(`/device/pen/${pen}`),
  penUp: () => post("/device/pen-up"),
  penDown: () => post("/device/pen-down"),
  move: (x: number, y: number, units: "mm" | "plotter") =>
    post("/device/move", { x, y, units }),
  park: () => post("/device/park"),

  uploadSvg: async (file: File): Promise<UploadSvgResult> => {
    const fd = new FormData();
    fd.append("file", file, file.name);
    return req<UploadSvgResult>("/files/svg", { method: "POST", body: fd });
  },

  uploadHpgl: async (file: File): Promise<{ id: string; name: string; size: number; validation: SanitizeReport }> => {
    const fd = new FormData();
    fd.append("file", file, file.name);
    return req("/files/hpgl", { method: "POST", body: fd });
  },

  analysis: (fileId: string) => req<Analysis>(`/files/${fileId}/analysis`),

  createJob: (body: JobCreateBody) => req<Job>("/jobs", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }),

  listJobs: () => req<{ jobs: Job[]; active_job_id: string | null }>("/jobs"),
  getJob: (id: string) => req<Job>(`/jobs/${id}`),
  deleteJob: (id: string) => req<{ deleted: string }>(`/jobs/${id}`, { method: "DELETE" }),

  prepareJob: (id: string) => post(`/jobs/${id}/prepare`),
  startJob: (id: string) => post(`/jobs/${id}/start`),
  pauseJob: (id: string) => post(`/jobs/${id}/pause`),
  resumeJob: (id: string) => post(`/jobs/${id}/resume`),
  cancelJob: (id: string) => post(`/jobs/${id}/cancel`),

  /** Post-processing preview SVG (image/svg+xml). 404 until prepared —
   *  callers catch ApiError(404) and render a "not ready" placeholder. */
  jobPreview: async (id: string): Promise<string> => {
    let res: Response;
    try {
      res = await fetch(`${BASE}/jobs/${id}/preview`);
    } catch {
      throw new ApiError(0, "Backend unreachable — is the server running on :8750?");
    }
    if (!res.ok) {
      let body: unknown = null;
      try { body = await res.json(); } catch { /* empty */ }
      throw new ApiError(res.status, detailOf(body, res.status));
    }
    return res.text();
  },

  getSettings: () => req<AppSettings>("/settings"),
  putSettings: (custom: Record<string, unknown>) => req<{ saved: boolean }>("/settings", {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ custom }),
  }),
  getPapers: () => req<Record<string, PaperInfo>>("/papers"),
};
