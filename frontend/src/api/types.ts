/** Shared API types. Backend shapes pinned by backend/app/api/routes.py;
 *  fields the backend may not send yet are optional — render defensively. */

export interface PortInfo {
  device: string;
  by_id_path?: string | null;
  description?: string;
  vid?: number | null;
  pid?: number | null;
  ftdi?: boolean;
  writable?: boolean;
  hint?: string;
  [k: string]: unknown;
}

export interface SerialSettings {
  baudrate: number;
  bytesize: number;
  parity: string;
  stopbits: number;
  [k: string]: unknown;
}

export interface ConnectBody extends SerialSettings {
  port: string;
}

export interface ConnectResult {
  connected: boolean;
  port: string;
  info: Record<string, unknown>;
}

export interface DeviceStatusBits {
  status?: number;
  error?: string;
  [k: string]: unknown;
}

export interface DeviceStatus {
  connected: boolean;
  port: string | null;
  settings: SerialSettings | null;
  status?: DeviceStatusBits | null;
}

export interface DeviceError {
  hpgl?: { code: number; meaning: string } | null;
  extended?: { code: number; meaning: string } | null;
  [k: string]: unknown;
}

/** Analysis layers may arrive as plain strings (names) or richer objects. */
export type RawLayer = string | { name?: string; color?: string; [k: string]: unknown };

export interface Analysis {
  bbox_mm?: { min_x?: number; min_y?: number; max_x?: number; max_y?: number } | number[] | null;
  stroke_colors?: string[];
  layers?: RawLayer[];
  /** e.g. ["text", "raster", ...] — spec §15 unsupported-content flags */
  unsupported?: string[];
  /** non-blocking quirks (phase 3 F7): fills → outline-only rendering */
  warnings?: string[];
  /** paper name → fits (bool or {fits:bool}); shape not yet pinned by backend */
  est_paper_fit?: Record<string, boolean | { fits?: boolean } | undefined>;
  /** paper name → fits only when rotated 90° (goal 47da763c) */
  fit_rotate90?: Record<string, boolean>;
  /** warning string → action hint (phase 3 F7); keys mirror unsupported[] */
  hints?: Record<string, string>;
  [k: string]: unknown;
}

/** POST /api/vectorize result (goal 950c719c). */
export interface VectorizeResult {
  svg_id: string;
  filename: string;
  /** data-relative path of the produced SVG (backend storage) */
  path: string;
  duration_s: number;
}

export interface ConversionInfo {
  attempted: boolean;
  converted: boolean;
  warning: string | null;
}

export interface UploadSvgResult {
  id: string;
  name: string;
  size: number;
  sanitize: SanitizeReport;
  /** phase 3 F6: stored file went through Inkscape text→paths */
  text_converted?: boolean;
  conversion?: ConversionInfo;
}

/** Sanitizer report — exact keys still being pinned by the pipeline lane. */
export interface SanitizeReport {
  removed?: string[];
  warnings?: string[];
  [k: string]: unknown;
}

export type JobStatus =
  | "QUEUED" | "PREPARING" | "READY" | "SENDING" | "PLOTTING"
  | "PAUSED" | "COMPLETING" | "COMPLETED" | "CANCELLED" | "FAILED" | "DISCONNECTED";

export interface Job {
  id: string;
  name: string;
  status: JobStatus | string;
  file_id: string | null;
  paper: string;
  pen_map: Record<string, number>;
  options: Record<string, unknown>;
  hpgl: string;
  bytes_total: number;
  bytes_sent: number;
  error: string | null;
  stats: Record<string, unknown>;
  created_at: number;
  updated_at: number;
  /** phase-2 F2: lifted from stats.pipeline.estimate by the API layer */
  estimate?: JobEstimate;
}

export interface JobEstimate {
  drawn_mm: number;
  travel_mm: number;
  velocity_cm_s: number;
  est_seconds: number;
}

/** How pen_map keys are interpreted: Inkscape layer names or stroke-color hexes. */
export type PenMapMode = "layers" | "colors";

export interface JobCreateBody {
  file_id: string;
  name?: string;
  paper: string;
  /** "layers" (default) | "colors" — selects pen_map key semantics. */
  pen_map_mode?: PenMapMode;
  pen_map: Record<string, number>;
  options: Record<string, unknown>;
}

export interface PaperInfo {
  size_mm: [number, number];
  x_range: [number, number];
  y_range: [number, number];
  dip_mode: string;
  info?: string;
  /** usable plot area (w, h) mm in carriage orientation; null = full clip */
  safe_area_mm?: [number, number] | null;
  /** how the sheet is loaded: A4 landscape, A3 portrait (caption word) */
  loads_orientation?: string;
}

export interface StreamSettings {
  safety_margin: number;
  default_chunk: number;
  query_timeout_s: number;
  max_retries: number;
  completion_timeout_s: number;
}

export interface AppSettings {
  host?: string;
  port?: number;
  stream?: StreamSettings;
  job_history_keep?: number;
  custom?: Record<string, unknown>;
}

export interface JobEvent {
  type: "job";
  job_id: string;
  status: JobStatus | string;
  /** phase-2 F3: "progress" | "resume" | undefined (legacy state frames) */
  event?: string;
  acked_bytes?: number;
  total_bytes?: number;
  pen_down?: boolean | null;
  bytes_sent?: number;
  bytes_total?: number;
  error?: string | null;
}

export type WsMessage =
  | JobEvent
  | { type: "device"; event: string; [k: string]: unknown }
  | { type: string; [k: string]: unknown };

/** Narrow a WS frame to the job-progress variant. */
export function isJobEvent(m: WsMessage): m is JobEvent {
  return m.type === "job" && typeof (m as JobEvent).job_id === "string";
}

/** Default display palette for pen slots (mapping aid only, spec §20). */
export const PEN_COLORS: Record<number, string> = {
  1: "#e8e8e8", // black ink on dark UI
  2: "#ff5252", // red
  3: "#4d9fff", // blue
  4: "#4dd97a", // green
  5: "#ffa938", // orange
  6: "#c07bff", // purple
};

export const PAPER_NAMES = ["a4", "a3", "a", "b"] as const;
export type PaperName = (typeof PAPER_NAMES)[number];
