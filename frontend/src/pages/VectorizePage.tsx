import { useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import { api, apiErrorMessage, ApiError } from "../api/client";
import { detectInkColors } from "../lib/inkColors";
import type { UploadSvgResult, VectorizeResult } from "../api/types";
import { useApp } from "../state/app";
import { sanitizePreviewSvg, extractViewBox, innerOf } from "../components/PagePreview";

/** Threshold bounds — backend validates 0.01..0.99 (goal 950c719c). */
const THRESH_MIN = 0.01, THRESH_MAX = 0.99;
/** Color-layer bounds — backend validates 1..8 (goal a7f70dae). */
const COLORS_MIN = 1, COLORS_MAX = 8;
/** Status poll interval while a vectorize job runs. */
const POLL_MS = 2000;

/** Human message from a vectorize error carrying {message, stderr_tail}. */
function vectorizeErrorMessage(err: unknown): string {
  if (err instanceof ApiError && err.detail && typeof err.detail === "object") {
    const m = (err.detail as { message?: unknown }).message;
    if (typeof m === "string" && m) return m;
  }
  return apiErrorMessage(err);
}

function stderrTailOf(err: unknown): string | null {
  if (err instanceof ApiError && err.detail && typeof err.detail === "object") {
    const t = (err.detail as { stderr_tail?: unknown }).stderr_tail;
    if (typeof t === "string" && t) return t;
  }
  return null;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Raster drawing → SVG via the server-side SLD pipeline (goal 950c719c,
 *  background jobs goal a7f70dae). POST starts a job; we poll status every
 *  POLL_MS until done/error and can cancel mid-run. colors ≥ 2 runs the
 *  multi-color layered pipeline (per-color stroke groups → by-color pen map). */
export default function VectorizePage({
  onSentToPlot,
}: {
  onSentToPlot?: (file: UploadSvgResult) => void;
} = {}) {
  const { toast } = useApp();

  const [file, setFile] = useState<File | null>(null);
  const [autoThresh, setAutoThresh] = useState(true);
  const [thresh, setThresh] = useState(0.5);
  const [multipleLines, setMultipleLines] = useState(false);
  const [colors, setColors] = useState(1);
  const [detected, setDetected] = useState<number | null>(null);
  const fileSeq = useRef(0);
  const [running, setRunning] = useState(false);
  const [stage, setStage] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [result, setResult] = useState<VectorizeResult | null>(null);
  const [svgText, setSvgText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [stderrTail, setStderrTail] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const jobIdRef = useRef<string | null>(null);
  const cancelRef = useRef(false);

  useEffect(() => () => { cancelRef.current = true; }, []);

  const safeSvg = useMemo(() => (svgText ? sanitizePreviewSvg(svgText) : null), [svgText]);
  const vb = useMemo(() => (svgText ? extractViewBox(svgText) : null), [svgText]);
  const multicolor = colors > 1;

  const selectFile = (f: File) => {
    setFile(f);
    setResult(null);
    setSvgText(null);
    setError(null);
    setStderrTail(null);
    // Auto-detect dominant ink colors and populate the Colors input.
    const seq = ++fileSeq.current;
    setDetected(null);
    detectInkColors(f)
      .then((n) => {
        if (fileSeq.current !== seq) return; // file changed mid-detect
        setColors(n);
        setDetected(n);
      })
      .catch(() => {
        if (fileSeq.current === seq) setDetected(null); // detection optional
      });
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const f = e.dataTransfer.files?.[0];
    if (f) selectFile(f);
  };

  const onThreshChange = (raw: string) => {
    const n = Number(raw);
    setThresh(Number.isFinite(n) ? Math.min(THRESH_MAX, Math.max(THRESH_MIN, n)) : THRESH_MIN);
  };

  const resetBusy = () => {
    setRunning(false);
    setStage(null);
    jobIdRef.current = null;
  };

  const run = async () => {
    if (!file || running) return;
    setRunning(true);
    setResult(null);
    setSvgText(null);
    setError(null);
    setStderrTail(null);
    setElapsed(0);
    setStage("starting job");
    cancelRef.current = false;
    try {
      const { job_id } = await api.vectorizeStart(file, {
        thresh: autoThresh || multicolor ? null : thresh,
        multipleLines: multipleLines && !multicolor,
        colors,
      });
      jobIdRef.current = job_id;

      for (;;) {
        if (cancelRef.current) return; // cancelled locally
        await sleep(POLL_MS);
        if (cancelRef.current) return;
        const st = await api.vectorizeStatus(job_id);
        setStage(st.stage ?? (st.status === "queued" ? "queued" : null));
        setElapsed(st.elapsed_s);
        if (st.status === "done" && st.result) {
          setResult(st.result);
          setSvgText(await api.vectorizeSvg(st.result.svg_id));
          break;
        }
        if (st.status === "error") {
          setError(st.error?.message ?? "vectorization failed");
          setStderrTail(st.error?.stderr_tail || null);
          break;
        }
      }
    } catch (e) {
      setError(vectorizeErrorMessage(e));
      setStderrTail(stderrTailOf(e));
    } finally {
      resetBusy();
    }
  };

  const cancel = async () => {
    const id = jobIdRef.current;
    cancelRef.current = true;
    if (id) {
      try { await api.vectorizeCancel(id); } catch { /* best effort */ }
    }
    resetBusy();
  };

  const download = () => {
    if (!svgText || !result) return;
    const url = URL.createObjectURL(new Blob([svgText], { type: "image/svg+xml" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = result.filename;
    a.click();
    URL.revokeObjectURL(url);
  };

  /** Send to Plot: upload the vectorized SVG through the EXISTING file flow
   *  (same endpoint as PlotPage upload), then hand the stored-file metadata
   *  to the Plot tab — no second storage path, no hacks. */
  const sendToPlot = async () => {
    if (!result || !svgText || sending) return;
    setSending(true);
    try {
      const name = result.filename.endsWith(".svg") ? result.filename : `${result.filename}.svg`;
      const f = new File([svgText], name, { type: "image/svg+xml" });
      const meta = await api.uploadSvg(f, false);
      onSentToPlot?.(meta);
    } catch (e) {
      toast("error", `Upload failed: ${apiErrorMessage(e)}`);
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="page plot-page">
      <section className="panel">
        <h2>1 · Upload image</h2>
        <p className="muted small">
          Raster line drawing (PNG/JPG/…) → SVG via SLD-Vectorization, running
          on the server as a background job. Simple drawings take ~30 s;
          complex ones can take several minutes.
        </p>
        <div
          className={`dropzone${dragOver ? " over" : ""}`}
          data-testid="dropzone"
          onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          onDrop={onDrop}
        >
          <p>{file ? file.name : "Drag & drop an image here"}</p>
          <p className="muted small">or</p>
          <input ref={fileInput} type="file" accept="image/*" aria-label="Image file"
            data-testid="image-file"
            onChange={(e) => { const f = e.target.files?.[0]; if (f) selectFile(f); }} />
        </div>

        <h3>Options</h3>
        <div className="opt-grid" role="group" aria-label="vectorize options">
          <label>
            Colors
            <input type="number" min={COLORS_MIN} max={COLORS_MAX} step={1}
              value={colors} data-testid="colors-input" aria-label="color layers"
              onChange={(e) => {
                const n = Number(e.target.value);
                setColors(Number.isFinite(n) ? Math.min(COLORS_MAX, Math.max(COLORS_MIN, Math.round(n))) : 1);
              }} />
          </label>          <label>
            Threshold
            <input type="number" min={THRESH_MIN} max={THRESH_MAX} step={0.01}
              value={thresh} disabled={autoThresh || multicolor} data-testid="thresh-input"
              aria-label="threshold"
              onChange={(e) => onThreshChange(e.target.value)} />
          </label>
        </div>
        {!multicolor && (
          <>
            <div className="mode-toggle" role="group" aria-label="threshold mode">
              <button type="button" aria-pressed={autoThresh} data-testid="thresh-auto"
                onClick={() => setAutoThresh(true)}>Auto threshold</button>
              <button type="button" aria-pressed={!autoThresh} data-testid="thresh-manual"
                onClick={() => setAutoThresh(false)}>Manual</button>
            </div>
            <label>
              <input type="checkbox" checked={multipleLines} data-testid="multiple-lines"
                onChange={(e) => setMultipleLines(e.target.checked)} />
              Multiple lines (multi-stroke input)
            </label>
            <p className="muted small">
              Threshold 0.01–0.99 = dark-pixel cutoff; Auto (omit) is recommended.
            </p>
          </>
        )}
        {multicolor && (
          <p className="muted small" data-testid="multicolor-hint">
            Multi-color: the image is quantized to ≤ {colors} ink colors and each
            color layer is vectorized separately (threshold / multiple-lines not
            applicable). Runtime scales with the number of colors.
            {detected != null && (
              <> Auto-detected <span data-testid="detected-colors">{detected}</span> ink
                color{detected === 1 ? "" : "s"} in the image.</>
            )}
          </p>
        )}

        <div className="row-actions">
          <button className="primary" disabled={!file || running}
            onClick={() => void run()} data-testid="run-btn">
            {running ? `Vectorizing… ${Math.floor(elapsed)} s` : "Vectorize"}
          </button>
          {running && (
            <button onClick={() => void cancel()} data-testid="cancel-btn">Cancel</button>
          )}
        </div>
        {running && (
          <div className="banner info" role="status" data-testid="busy">
            Vectorization running — <span data-testid="elapsed">{Math.floor(elapsed)} s</span> elapsed.
            {stage && <> Current step: <span data-testid="stage">{stage}</span>.</>}
            {" "}Keep this tab open.
          </div>
        )}
        {error && (
          <div className="banner err" role="alert" data-testid="error-banner">
            Vectorize failed: {error}
            {stderrTail && <pre className="stderr" data-testid="stderr-tail">{stderrTail}</pre>}
          </div>
        )}
      </section>

      <section className="panel">
        <h2>2 · Result</h2>
        {!result && !running && (
          <p className="muted" data-testid="result-empty">
            No result yet — upload an image and run vectorization.
          </p>
        )}
        {result && (
          <div className="file-meta" data-testid="result">
            <p><b>{result.filename}</b> · vectorized in {result.duration_s.toFixed(1)} s</p>
            {safeSvg && vb ? (
              <div className="preview-wrap" data-testid="vectorize-preview">
                <svg className="page-preview" viewBox={`${vb.minX} ${vb.minY} ${vb.w} ${vb.h}`}
                  role="img" aria-label="vectorized preview">
                  <g dangerouslySetInnerHTML={{ __html: safeSvg.includes("<svg") ? innerOf(safeSvg) : safeSvg }} />
                </svg>
                <p className="small muted">
                  {multicolor
                    ? "Vectorized color layers — map each stroke color to a pen on the Plot tab."
                    : "Vectorized single-line drawing — check the strokes before plotting."}
                </p>
              </div>
            ) : (
              <div className="preview-empty" data-testid="vectorize-preview">
                <p className="muted">Preview unavailable — download the SVG instead.</p>
              </div>
            )}
            <div className="row-actions">
              <button onClick={download} data-testid="download-svg">Download SVG</button>
              <button className="primary" disabled={sending}
                onClick={() => void sendToPlot()} data-testid="send-to-plot">
                {sending ? "Uploading…" : "Send to Plot"}
              </button>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
