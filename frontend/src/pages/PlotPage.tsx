import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import { api, apiErrorMessage, ApiError } from "../api/client";
import type { Analysis, Job, PenMapMode, UploadSvgResult } from "../api/types";
import { PAPER_NAMES, isJobEvent } from "../api/types";
import { useApp } from "../state/app";
import PenMap, { normalizeLayers } from "../components/PenMap";
import PagePreview from "../components/PagePreview";
import StatusBadge from "../components/StatusBadge";
import Progress from "../components/Progress";
import Modal from "../components/Modal";

interface OptState { linemerge: boolean; linesimplify: boolean; sort: boolean; reloop: boolean }
const DEFAULT_OPTS: OptState = { linemerge: true, linesimplify: true, sort: true, reloop: true };

const ACTIVE_STATES = new Set(["QUEUED", "PREPARING", "READY", "SENDING", "PLOTTING", "COMPLETING", "PAUSED"]);

/** Per-file mapping selections: active mode + separate maps keyed for each
 *  mode (layer names vs color hexes) so toggling never loses selections. */
interface FileMapState {
  mode: PenMapMode;
  maps: Record<PenMapMode, Record<string, number>>;
}

/** Auto-assign pens 1–6 to the first six entries, cycling on overflow. */
function seedMap(names: string[]): Record<string, number> {
  const m: Record<string, number> = {};
  names.slice(0, 6).forEach((n, i) => { m[n] = (i % 6) + 1; });
  return m;
}

/** VS velocity in cm/s — HP 7475A range 1–38 (manual, 0.38 steps). */
const VELOCITIES: { label: string; value: number | null }[] = [
  { label: "Default (plotter setting)", value: null },
  { label: "Slow · 10 cm/s", value: 10 },
  { label: "Medium · 20 cm/s", value: 20 },
  { label: "Fast · 38 cm/s", value: 38 },
];

/** Generic renderer for the sanitizer report dict (shape pinned loosely). */
function SanitizeReportView({ report }: { report: Record<string, unknown> }) {
  const entries = Object.entries(report);
  if (entries.length === 0) return <p className="ok">Sanitizer: clean, nothing removed.</p>;
  return (
    <div className="sanitize-report" data-testid="sanitize-report">
      <h4>Sanitize report</h4>
      <ul>
        {entries.map(([k, v]) => (
          <li key={k}><b>{k}:</b>{" "}
            {Array.isArray(v)
              ? v.length ? v.map(String).join(", ") : "none"
              : typeof v === "object" && v !== null ? JSON.stringify(v) : String(v)}
          </li>
        ))}
      </ul>
    </div>
  );
}

export default function PlotPage() {
  const { papers, papersError, retryPapers, toast, ws } = useApp();

  const [file, setFile] = useState<UploadSvgResult | null>(null);
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [analysisError, setAnalysisError] = useState<string | null>(null);
  const [mapState, setMapState] = useState<Record<string, FileMapState>>({});
  const [paper, setPaper] = useState<string>("a4");
  const [opts, setOpts] = useState<OptState>(DEFAULT_OPTS);
  const [velSelect, setVelSelect] = useState<string>("");
  const [customVel, setCustomVel] = useState(20);
  const [uploading, setUploading] = useState(false);
  const [preparing, setPreparing] = useState(false);
  const [job, setJob] = useState<Job | null>(null);
  const [previewSvg, setPreviewSvg] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const layers = useMemo(() => normalizeLayers(analysis?.layers), [analysis]);
  const strokeColors = useMemo(() => analysis?.stroke_colors ?? [], [analysis]);
  // Wanted mode (per file) vs effective mode — files without stroke_colors
  // can't use color mapping, so fall back to layers defensively.
  const wantedMode: PenMapMode = (file && mapState[file.id]?.mode) ?? "layers";
  const mode: PenMapMode = wantedMode === "colors" && strokeColors.length === 0 ? "layers" : wantedMode;
  const colorModeUnavailable = wantedMode === "colors" && mode === "layers";
  const penMap = (file && mapState[file.id]?.maps[mode]) ?? {};
  const mappingRows = mode === "colors"
    ? strokeColors.map((c) => ({ name: c, color: c }))
    : layers;

  const updateMap = (next: Record<string, number>) => {
    if (!file) return;
    setMapState((s) => {
      const cur = s[file.id] ?? { mode, maps: { layers: {}, colors: {} } };
      return { ...s, [file.id]: { ...cur, maps: { ...cur.maps, [mode]: next } } };
    });
  };

  const setMappingMode = (m: PenMapMode) => {
    if (!file) return;
    setMapState((s) => {
      const cur = s[file.id] ?? { mode: m, maps: { layers: {}, colors: {} } };
      return { ...s, [file.id]: { ...cur, mode: m } };
    });
  };

  // ---- upload ------------------------------------------------------------
  const upload = useCallback(async (f: File) => {
    setUploading(true);
    try {
      const meta = await api.uploadSvg(f);
      setFile(meta); setAnalysis(null); setAnalysisError(null);
      setJob(null); setPreviewSvg(null); setPreviewError(null); setConfirmed(false);
      try {
        const a = await api.analysis(meta.id);
        setAnalysis(a);
        // Default: layer mapping only when Inkscape layers exist AND >1;
        // otherwise colors are the natural grouping (goal 3e598c6e).
        const ls = normalizeLayers(a.layers);
        const m: PenMapMode = ls.length > 1 ? "layers" : "colors";
        setMapState((s) => ({ ...s, [meta.id]: { mode: m, maps: {
          layers: seedMap(ls.map((l) => l.name)),
          colors: seedMap(a.stroke_colors ?? []),
        } } }));
      } catch (e) {
        setAnalysisError(apiErrorMessage(e));
      }
    } catch (e) {
      toast("error", `Upload failed: ${apiErrorMessage(e)}`);
    } finally {
      setUploading(false);
    }
  }, [toast]);

  const onDrop = (e: DragEvent) => {
    e.preventDefault(); setDragOver(false);
    const f = e.dataTransfer.files?.[0];
    if (f) void upload(f);
  };

  // ---- create + prepare ----------------------------------------------------
  const prepare = async () => {
    if (!file) return;
    setPreparing(true);
    try {
      const options: Record<string, unknown> = { ...opts };
      const vel = velSelect === "" || velSelect === "custom" ? null : Number(velSelect);
      if (vel != null) options.velocity = vel;
      const j = await api.createJob({
        file_id: file.id, name: file.name, paper,
        pen_map: penMap, pen_map_mode: mode, options,
      });
      setJob(j); setPreviewSvg(null); setPreviewError(null);
      await api.prepareJob(j.id);
    } catch (e) {
      toast("error", apiErrorMessage(e));
      setJob(null);
    } finally {
      setPreparing(false);
    }
  };

  // Poll job while active; fetch preview once not-QUEUED/PREPARING.
  useEffect(() => {
    if (!job || !ACTIVE_STATES.has(job.status)) return;
    const id = job.id;
    pollRef.current = setInterval(async () => {
      try {
        const j = await api.getJob(id);
        setJob((prev) => (prev && prev.id === id ? j : prev));
      } catch { /* transient — WS will catch up */ }
    }, 2500);
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [job]);

  // WS job events patch the local job directly.
  useEffect(() => {
    const m = ws.last;
    if (!m || !isJobEvent(m) || !job || m.job_id !== job.id) return;
    // Return the SAME ref when nothing changed — otherwise this effect
    // re-fires on its own setJob and loops forever.
    setJob((prev) => {
      if (!prev || prev.id !== m.job_id) return prev;
      const next = {
        ...prev,
        status: m.status,
        bytes_sent: m.bytes_sent ?? prev.bytes_sent,
        bytes_total: m.bytes_total ?? prev.bytes_total,
        error: m.error ?? prev.error,
      };
      return next.status === prev.status && next.bytes_sent === prev.bytes_sent
        && next.bytes_total === prev.bytes_total && next.error === prev.error
        ? prev : next;
    });
  }, [ws.last, job]);

  // Fetch preview when job leaves PREPARING.
  useEffect(() => {
    if (!job || job.status === "QUEUED" || job.status === "PREPARING" || previewSvg) return;
    let alive = true;
    (async () => {
      try {
        const svg = await api.jobPreview(job.id);
        if (alive) { setPreviewSvg(svg); setPreviewError(null); }
      } catch (e) {
        if (alive) setPreviewError(e instanceof ApiError && e.status === 404
          ? "Preview not generated yet (prepare the job first)."
          : apiErrorMessage(e));
      }
    })();
    return () => { alive = false; };
  }, [job, previewSvg]);

  // ---- controls --------------------------------------------------------
  const jobCmd = async (cmd: (id: string) => Promise<unknown>) => {
    if (!job) return;
    try { await cmd(job.id); } catch (e) { toast("error", apiErrorMessage(e)); }
  };

  const startPlot = async () => {
    setConfirmOpen(false);
    await jobCmd(api.startJob);
  };

  const canPause = job && (job.status === "SENDING" || job.status === "PLOTTING");
  const canResume = job && job.status === "PAUSED";
  const canCancel = job && ACTIVE_STATES.has(job.status);
  const pensUsed = [...new Set(Object.values(penMap))].sort();

  return (
    <div className="page plot-page">
      <section className="panel">
        <h2>1 · Upload SVG</h2>
        {papersError && <div className="banner err">
          Paper list failed: {papersError} <button onClick={retryPapers}>Retry</button>
        </div>}
        <div
          className={`dropzone${dragOver ? " over" : ""}`}
          data-testid="dropzone"
          onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          onDrop={onDrop}
        >
          <p>{uploading ? "Uploading…" : "Drag & drop an SVG here"}</p>
          <p className="muted small">or</p>
          <input ref={fileInput} type="file" accept=".svg,image/svg+xml" aria-label="SVG file"
            onChange={(e) => { const f = e.target.files?.[0]; if (f) void upload(f); }} />
        </div>
        {file && (
          <div className="file-meta">
            <p><b>{file.name}</b> · {(file.size / 1024).toFixed(1)} KB · id {file.id}</p>
            <SanitizeReportView report={file.sanitize as Record<string, unknown>} />
            {analysisError && <div className="banner err">Analysis failed: {analysisError} <button onClick={() => file && void api.analysis(file.id).then(setAnalysis).catch((e) => setAnalysisError(apiErrorMessage(e)))}>Retry</button></div>}
            {analysis && (
              <div className="analysis" data-testid="analysis">
                <h4>Analysis</h4>
                <ul>
                  <li>Layers: {layers.map((l) => l.name).join(", ") || "none detected"}</li>
                  <li>Stroke colors: {analysis.stroke_colors?.join(", ") || "—"}</li>
                  <li>BBox (mm): {fmtBbox(analysis.bbox_mm)}</li>
                </ul>
                {analysis.unsupported && analysis.unsupported.length > 0 && (
                  <div className="banner warn" role="alert">
                    Unsupported content: {analysis.unsupported.join(", ")}.
                    These cannot be plotted as vector lines — convert text to paths, remove rasters/filters.
                  </div>
                )}
                {analysis.est_paper_fit && (
                  <table className="fit-table" aria-label="paper fit">
                    <thead><tr><th>Paper</th><th>Fit</th></tr></thead>
                    <tbody>
                      {Object.entries(analysis.est_paper_fit).map(([p, fit]) => {
                        const ok = typeof fit === "boolean" ? fit : (fit?.fits ?? false);
                        return (
                          <tr key={p} className={p === paper ? "selected" : ""}>
                            <td>{p}</td>
                            <td className={ok ? "ok" : "err"}>{ok ? "fits" : "too small"}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                )}
              </div>
            )}
          </div>
        )}
      </section>

      <section className="panel">
        <h2>2 · Configure</h2>
        {!file && <p className="muted" data-testid="configure-empty">Upload a file to configure pens, optimization and paper.</p>}
        {file && (<>
          <h3>Pen mapping ({mode === "colors" ? "stroke color → pen 1–6" : "layer → pen 1–6"})</h3>
          <div className="mode-toggle" role="group" aria-label="pen mapping mode" data-testid="pen-mode">
            <button aria-pressed={mode === "layers"} data-testid="mode-layers"
              onClick={() => setMappingMode("layers")}>By Layer</button>
            <button aria-pressed={mode === "colors"} data-testid="mode-colors"
              onClick={() => setMappingMode("colors")}>By Color</button>
          </div>
          {colorModeUnavailable && (
            <p className="muted small" role="note" data-testid="color-mode-unavailable">
              Analysis reported no stroke colors for this file — layer mapping shown.
            </p>
          )}
          <PenMap mode={mode} layers={mappingRows} penMap={penMap} onChange={updateMap} />

          <h3>Optimization</h3>
          <div className="opt-grid" role="group" aria-label="optimization options">
            {(Object.keys(DEFAULT_OPTS) as (keyof OptState)[]).map((k) => (
              <label key={k}>
                <input type="checkbox" checked={opts[k]} data-testid={`opt-${k}`}
                  onChange={(e) => setOpts({ ...opts, [k]: e.target.checked })} />
                {k}
              </label>
            ))}
          </div>

          <h3>Pen velocity (VS)</h3>
          <select aria-label="pen velocity" value={velSelect}
            onChange={(e) => setVelSelect(e.target.value)}>
            {VELOCITIES.map((v) => (
              <option key={v.label} value={v.value ?? ""}>{v.label}</option>
            ))}
            <option value="custom">Custom…</option>
          </select>
          {velSelect === "custom" && (
            <div className="custom-vel">
              <label>
                Custom velocity (cm/s, 1–38):
                <input type="number" min={1} max={38} step={0.38} value={customVel}
                  aria-label="custom velocity"
                  onChange={(e) => {
                    const v = Math.min(38, Math.max(1, Number(e.target.value) || 1));
                    setCustomVel(v);
                  }} />
              </label>
              <button className="ghost" onClick={() => setVelSelect(String(customVel))}>Apply</button>
            </div>
          )}

          <h3>Paper</h3>
          <div className="paper-select" role="radiogroup" aria-label="paper size">
            {PAPER_NAMES.map((p) => (
              <label key={p} className={p === paper ? "selected" : ""}>
                <input type="radio" name="paper" value={p} checked={p === paper}
                  onChange={() => setPaper(p)} />
                {p.toUpperCase()}
                {papers[p] && <span className="muted small"> · {papers[p].size_mm[0].toFixed(0)}×{papers[p].size_mm[1].toFixed(0)} mm</span>}
              </label>
            ))}
          </div>
          {papers[paper] && (
            <p className="muted small" data-testid="dip-hint">
              ⚠ DIP-mode hint: {papers[paper].dip_mode} — {papers[paper].info}
            </p>
          )}

          <div className="row-actions">
            <button className="primary" disabled={!file || preparing || pensUsed.length === 0}
              onClick={() => void prepare()} data-testid="prepare-btn">
              {preparing ? "Preparing…" : "Create job + prepare"}
            </button>
            <button className="danger big" disabled={!job || !(job.status === "READY" || job.status === "COMPLETED" || job.status === "CANCELLED")}
              onClick={() => { setConfirmed(false); setConfirmOpen(true); }} data-testid="plot-btn">
              PLOT
            </button>
          </div>
        </>)}
      </section>

      <section className="panel preview-panel">
        <h2>3 · Preview & plot</h2>
        <PagePreview svg={previewSvg} error={previewError}
          paper={papers[paper] ?? null} paperName={paper} />
        {job && (
          <div className="job-live" data-testid="job-live">
            <div className="row">
              <StatusBadge status={job.status} />
              <b>{job.name}</b>
              {job.error && <span className="err small">{job.error}</span>}
            </div>
            <Progress value={job.bytes_sent} total={job.bytes_total} />
            <div className="row-actions">
              <button disabled={!canPause} onClick={() => void jobCmd(api.pauseJob)}>Pause</button>
              <button disabled={!canResume} onClick={() => void jobCmd(api.resumeJob)}>Resume</button>
              <button className="danger" disabled={!canCancel} onClick={() => void jobCmd(api.cancelJob)}>Cancel</button>
            </div>
            <p className="muted small">Buffered hardware movement may continue briefly after pause/cancel.</p>
          </div>
        )}
      </section>

      {confirmOpen && job && (
        <Modal title="Start plot — the plotter WILL move" onClose={() => setConfirmOpen(false)}
          footer={<>
            <button onClick={() => setConfirmOpen(false)}>Cancel</button>
            <button className="danger" disabled={!confirmed} data-testid="confirm-start"
              onClick={() => void startPlot()}>Start Plot</button>
          </>}>
          <table className="confirm-table">
            <tbody>
              <tr><td>File</td><td>{job.name}</td></tr>
              <tr><td>Paper</td><td>{paper.toUpperCase()}{papers[paper] ? ` (${papers[paper].dip_mode} DIP)` : ""}</td></tr>
              <tr><td>Pens</td><td>{pensUsed.length ? pensUsed.join(", ") : "none"}</td></tr>
              <tr><td>HP-GL size</td><td>{(job.hpgl?.length ?? job.bytes_total) > 0 ? `${((job.hpgl?.length ?? job.bytes_total) / 1024).toFixed(1)} KB` : "—"}</td></tr>
            </tbody>
          </table>
          {analysis?.unsupported && analysis.unsupported.length > 0 && (
            <div className="banner warn">Warnings: source contains unsupported content ({analysis.unsupported.join(", ")}) — it will not be plotted.</div>
          )}
          {pensUsed.length === 0 && <div className="banner warn">No pens mapped — nothing would plot.</div>}
          <label className="confirm-check">
            <input type="checkbox" checked={confirmed} data-testid="confirm-check"
              onChange={(e) => setConfirmed(e.target.checked)} />
            I checked paper + pens. The plotter WILL move — start now.
          </label>
        </Modal>
      )}
    </div>
  );
}

function fmtBbox(bbox: Analysis["bbox_mm"]): string {
  if (!bbox) return "—";
  if (Array.isArray(bbox)) return bbox.map((n) => Number(n).toFixed(1)).join(", ");
  const b = bbox as Record<string, number | undefined>;
  const vals = [b.min_x, b.min_y, b.max_x, b.max_y].map((n) => (n ?? 0).toFixed(1));
  return `min(${vals[0]}, ${vals[1]}) max(${vals[2]}, ${vals[3]})`;
}
