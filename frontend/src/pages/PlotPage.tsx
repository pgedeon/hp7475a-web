import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import { api, apiErrorMessage, ApiError } from "../api/client";
import type { Analysis, Job, PenMapMode, UploadSvgResult } from "../api/types";
import { PAPER_NAMES, isJobEvent } from "../api/types";
import { useApp } from "../state/app";
import PenMap, { normalizeLayers } from "../components/PenMap";
import PagePreview from "../components/PagePreview";
import ArtworkPreview from "../components/ArtworkPreview";
import StatusBadge from "../components/StatusBadge";
import Progress from "../components/Progress";
import Modal from "../components/Modal";

interface OptState { linemerge: boolean; linesimplify: boolean; sort: boolean; reloop: boolean }
const DEFAULT_OPTS: OptState = { linemerge: true, linesimplify: true, sort: true, reloop: true };

interface CopiesState { rows: number; cols: number; spacing_mm: number }
const DEFAULT_COPIES: CopiesState = { rows: 1, cols: 1, spacing_mm: 5 };

const ACTIVE_STATES = new Set(["QUEUED", "PREPARING", "READY", "SENDING", "PLOTTING", "COMPLETING", "PAUSED"]);

/** VS velocity bounds (cm/s) — HP 7475A, 0.38 steps (brief F2). */
const VEL_MIN = 10, VEL_MAX = 38.1, VEL_STEP = 0.38;

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
  const [mapMode, setMapMode] = useState<PenMapMode>("layers");
  const [penMaps, setPenMaps] = useState<Record<PenMapMode, Record<string, number>>>({ layers: {}, colors: {} });
  const [paper, setPaper] = useState<string>("a4");
  const [opts, setOpts] = useState<OptState>(DEFAULT_OPTS);
  const [rotate, setRotate] = useState(false);
  const [margin, setMargin] = useState(10);
  const [velocity, setVelocity] = useState(VEL_MAX);
  const [copies, setCopies] = useState<CopiesState>(DEFAULT_COPIES);
  const [showTravel, setShowTravel] = useState(false);
  const [convertText, setConvertText] = useState(false);
  const [artworkSvg, setArtworkSvg] = useState<string | null>(null);
  const [penDown, setPenDown] = useState<boolean | null>(null);
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
  // Effective mapping mode (goal 3e598c6e): wanted mode vs defensive fallback —
  // files without stroke colors can't map by color, so layers are shown.
  const mode: PenMapMode =
    mapMode === "colors" && strokeColors.length === 0 ? "layers" : mapMode;
  const colorModeUnavailable = mapMode === "colors" && mode === "layers";
  const penMap = penMaps[mode];
  const setPenMap = (next: Record<string, number>) =>
    setPenMaps((s) => ({ ...s, [mode]: next }));
  const mappingRows = mode === "colors"
    ? strokeColors.map((c) => ({ name: c, color: c }))
    : layers;

  // Artwork w/h (mm) from analysis bbox — drives the orientation label
  // and the "fits only when rotated" tip (goal 47da763c).
  const artworkDims = useMemo((): [number, number] | null => {
    const b = analysis?.bbox_mm;
    if (!b) return null;
    const n = Array.isArray(b) ? b : [b.min_x, b.min_y, b.max_x, b.max_y];
    const v = n.map(Number);
    if (v.some((x) => !Number.isFinite(x))) return null;
    return [Math.abs(v[2] - v[0]), Math.abs(v[3] - v[1])];
  }, [analysis]);

  const rotationHint = useMemo(() => {
    const p = papers[paper];
    if (!artworkDims || !p) return null;
    const [w, h] = artworkDims;
    const fitsNormal = w <= p.size_mm[0] && h <= p.size_mm[1];
    const fitsRotated = analysis?.fit_rotate90?.[paper] ?? (w <= p.size_mm[1] && h <= p.size_mm[0]);
    if (!fitsNormal && fitsRotated) {
      return `Tip: fits ${paper.toUpperCase()} only when rotated — enable Rotate 90°.`;
    }
    return null;
  }, [artworkDims, analysis, paper, papers]);

  // ---- upload ------------------------------------------------------------
  const upload = useCallback(async (f: File) => {
    setUploading(true);
    setArtworkSvg(null);
    try {
      const meta = await api.uploadSvg(f, convertText);
      setFile(meta); setAnalysis(null); setAnalysisError(null);
      setJob(null); setPreviewSvg(null); setPreviewError(null); setConfirmed(false);
      // F5: instant artwork preview from the STORED (sanitized) file.
      api.fileRaw(meta.id).then(setArtworkSvg).catch(() => setArtworkSvg(null));
      try {
        const a = await api.analysis(meta.id);
        setAnalysis(a);
        // Default: layer mapping only when Inkscape layers exist AND >1;
        // otherwise colors are the natural grouping (goal 3e598c6e).
        const ls = normalizeLayers(a.layers);
        const m: PenMapMode = ls.length > 1 ? "layers" : "colors";
        const seed = (names: string[]): Record<string, number> => {
          const map: Record<string, number> = {};
          names.slice(0, 6).forEach((n, i) => { map[n] = (i % 6) + 1; });
          return map;
        };
        setMapMode(m);
        setPenMaps({ layers: seed(ls.map((l) => l.name)), colors: seed(a.stroke_colors ?? []) });
      } catch (e) {
        setAnalysisError(apiErrorMessage(e));
      }
    } catch (e) {
      toast("error", `Upload failed: ${apiErrorMessage(e)}`);
    } finally {
      setUploading(false);
    }
  }, [toast, convertText]);

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
      options.rotate_90 = rotate;
      options.margin_mm = margin;
      options.velocity_cm_s = velocity;
      if (copies.rows > 1 || copies.cols > 1) options.copies = copies;
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

  // WS job events patch the local job directly. Returns the SAME reference
  // when the patch is a no-op so the effect converges instead of looping
  // (job is a dep; a fresh object every run would re-trigger forever).
  useEffect(() => {
    const m = ws.last;
    if (!m || !isJobEvent(m) || !job || m.job_id !== job.id) return;
    if (m.event === "progress") {
      if (typeof m.pen_down === "boolean") setPenDown(m.pen_down);
      setJob((prev) => (prev && (m.acked_bytes ?? prev.bytes_sent) !== prev.bytes_sent
        ? {
            ...prev,
            bytes_sent: m.acked_bytes ?? prev.bytes_sent,
            bytes_total: m.total_bytes ?? prev.bytes_total,
          }
        : prev));
      return;
    }
    if (m.event === "resume") {
      setJob((prev) => (prev
        ? {
            ...prev,
            bytes_sent: m.acked_bytes ?? prev.bytes_sent,
            bytes_total: m.total_bytes ?? prev.bytes_total,
          }
        : prev));
      return;
    }
    setJob((prev) => {
      if (!prev) return prev;
      const next = {
        status: m.status,
        bytes_sent: m.bytes_sent ?? prev.bytes_sent,
        bytes_total: m.bytes_total ?? prev.bytes_total,
        error: m.error ?? prev.error,
      };
      if (next.status === prev.status && next.bytes_sent === prev.bytes_sent
        && next.bytes_total === prev.bytes_total && next.error === prev.error) return prev;
      return { ...prev, ...next };
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
        <label className="convert-text-check">
          <input type="checkbox" checked={convertText} data-testid="convert-text"
            onChange={(e) => setConvertText(e.target.checked)} />
          Convert text to paths (Inkscape)
          <span className="muted small"> — applies to the next upload; server-side, re-sanitized</span>
        </label>
        {file && (
          <div className="file-meta">
            <p><b>{file.name}</b> · {(file.size / 1024).toFixed(1)} KB · id {file.id}</p>
            <SanitizeReportView report={file.sanitize as Record<string, unknown>} />
            {file.text_converted && (
              <p className="ok small" data-testid="converted-note">
                Text converted to paths (Inkscape) — geometry is stored stroke-only.
              </p>
            )}
            {file.conversion?.warning && (
              <div className="banner warn" data-testid="conversion-warning" role="status">
                {file.conversion.warning} — original file kept.
              </div>
            )}
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
                    <p>Unsupported content: {analysis.unsupported.join(", ")}.
                    These cannot be plotted as vector lines — convert text to paths, remove rasters/filters.</p>
                    <ul className="hint-list" data-testid="unsupported-hints">
                      {analysis.unsupported
                        .filter((w) => analysis.hints?.[w])
                        .map((w) => (
                          <li key={w}>
                            <b>{w}</b> — {analysis.hints?.[w]}
                            {w.includes("text elements") && !convertText && (
                              <button type="button" className="link-btn" data-testid="hint-convert-btn"
                                onClick={() => setConvertText(true)}>
                                Enable text conversion
                              </button>
                            )}
                          </li>
                        ))}
                    </ul>
                  </div>
                )}
                {analysis.warnings && analysis.warnings.length > 0 && (
                  <div className="banner info" role="status">
                    <p>Warnings (non-blocking): {analysis.warnings.join(", ")}.</p>
                    <ul className="hint-list" data-testid="warnings-hints">
                      {analysis.warnings
                        .filter((w) => analysis.hints?.[w])
                        .map((w) => (
                          <li key={w}><b>{w}</b> — {analysis.hints?.[w]}</li>
                        ))}
                    </ul>
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
            <button type="button" aria-pressed={mode === "layers"} data-testid="mode-layers"
              onClick={() => setMapMode("layers")}>By Layer</button>
            <button type="button" aria-pressed={mode === "colors"} data-testid="mode-colors"
              onClick={() => setMapMode("colors")}>By Color</button>
          </div>
          {colorModeUnavailable && (
            <p className="muted small" role="note" data-testid="color-mode-unavailable">
              Analysis reported no stroke colors for this file — layer mapping shown.
            </p>
          )}
          <PenMap mode={mode} layers={mappingRows} penMap={penMap} onChange={setPenMap} />

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
          <div className="vel-slider">
            <input type="range" min={VEL_MIN} max={VEL_MAX} step={VEL_STEP}
              value={velocity} data-testid="vel-slider" aria-label="pen velocity cm/s"
              onChange={(e) => setVelocity(Number(e.target.value))} />
            <span className={velocity >= VEL_MAX ? "muted" : ""} data-testid="vel-value">
              {velocity.toFixed(2)} cm/s{velocity >= VEL_MAX ? " (default)" : ""}
            </span>
          </div>

          <h3>Copies (tile on page)</h3>
          <div className="copies-grid" role="group" aria-label="copies tiling">
            <label>Rows
              <input type="number" min={1} max={20} step={1} value={copies.rows}
                aria-label="copies rows" data-testid="copies-rows"
                onChange={(e) => setCopies({ ...copies, rows: clampInt(e.target.value, 1, 20, 1) })} />
            </label>
            <label>Cols
              <input type="number" min={1} max={20} step={1} value={copies.cols}
                aria-label="copies cols" data-testid="copies-cols"
                onChange={(e) => setCopies({ ...copies, cols: clampInt(e.target.value, 1, 20, 1) })} />
            </label>
            <label>Spacing (mm)
              <input type="number" min={0} max={100} step={1} value={copies.spacing_mm}
                aria-label="copies spacing mm" data-testid="copies-spacing"
                onChange={(e) => setCopies({ ...copies, spacing_mm: clampInt(e.target.value, 0, 100, 0) })} />
            </label>
            {(copies.rows > 1 || copies.cols > 1) && (
              <span className="muted small">
                {copies.rows}×{copies.cols} = {copies.rows * copies.cols} copies
              </span>
            )}
          </div>

          <h3>Orientation &amp; margin</h3>
          <div className="opt-grid" role="group" aria-label="orientation and margin">
            <label>
              <input type="checkbox" checked={rotate} data-testid="opt-rotate90"
                onChange={(e) => setRotate(e.target.checked)} />
              Rotate 90°
            </label>
            {artworkDims && (
              <span className="muted small" data-testid="orientation-label">
                artwork {rotate ? "landscape → portrait" : artworkDims[0] >= artworkDims[1] ? "landscape" : "portrait"}
              </span>
            )}
            <label>
              Margin (mm)
              <input type="number" min={5} max={25} step={0.5} value={margin}
                aria-label="margin mm" data-testid="margin-input"
                onChange={(e) => {
                  const v = Math.min(25, Math.max(5, Number(e.target.value) || 10));
                  setMargin(v);
                }} />
            </label>
          </div>
          {rotationHint && (
            <p className="banner warn" role="status" data-testid="rotation-hint">{rotationHint}</p>
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
        {/* F5: artwork preview until the annotated placement preview (or its
            error) exists — the swap happens exactly when prepare resolves. */}
        {previewError ? (
          <PagePreview svg={null} error={previewError}
            paper={papers[paper] ?? null} paperName={paper}
            showTravel={showTravel} onToggleTravel={setShowTravel} />
        ) : previewSvg ? (
          <PagePreview svg={previewSvg} error={null}
            paper={papers[paper] ?? null} paperName={paper}
            showTravel={showTravel} onToggleTravel={setShowTravel} />
        ) : artworkSvg !== null ? (
          <ArtworkPreview svg={artworkSvg}
            note={job ? "preparing placement preview…" : null} />
        ) : (
          <PagePreview svg={null} error={null}
            paper={papers[paper] ?? null} paperName={paper}
            showTravel={showTravel} onToggleTravel={setShowTravel} />
        )}
        {job && (
          <div className="job-live" data-testid="job-live">
            <div className="row">
              <StatusBadge status={job.status} />
              <b>{job.name}</b>
              {["SENDING", "PLOTTING", "COMPLETING"].includes(String(job.status)) && (
                <span className={`pen-badge${penDown ? " down" : ""}`} data-testid="pen-badge"
                  title={penDown === null ? "pen state unknown (buffer-based progress)" : penDown ? "pen down" : "pen up"}>
                  {penDown === null ? "?" : penDown ? "▼ pen down" : "▲ pen up"}
                </span>
              )}
              {job.error && <span className="err small">{job.error}</span>}
            </div>
            <Progress value={job.bytes_sent} total={job.bytes_total} />
            {job.estimate && (
              <p className="muted small" data-testid="estimate">
                ≈ {fmtDuration(job.estimate.est_seconds)} — drawn {Math.round(job.estimate.drawn_mm)} mm
                + travel {Math.round(job.estimate.travel_mm)} mm @ {job.estimate.velocity_cm_s} cm/s
              </p>
            )}
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
          {analysis?.warnings && analysis.warnings.length > 0 && (
            <div className="banner info">Notes: non-blocking quirks in the source ({analysis.warnings.join(", ")}) — plot proceeds, outlines only where filled.</div>
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

function clampInt(raw: string, min: number, max: number, fallback: number): number {
  const n = Math.round(Number(raw));
  return Number.isFinite(n) ? Math.min(max, Math.max(min, n)) : fallback;
}

function fmtDuration(s: number): string {
  if (s < 90) return `${Math.round(s)} s`;
  if (s < 5400) return `${(s / 60).toFixed(1)} min`;
  return `${(s / 3600).toFixed(1)} h`;
}
