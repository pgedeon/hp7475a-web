import { useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import { api, apiErrorMessage, ApiError } from "../api/client";
import type { UploadSvgResult, VectorizeResult } from "../api/types";
import { useApp } from "../state/app";
import { sanitizePreviewSvg, extractViewBox, innerOf } from "../components/PagePreview";

/** Threshold bounds — backend validates 0.01..0.99 (goal 950c719c). */
const THRESH_MIN = 0.01, THRESH_MAX = 0.99;

/** Human message from a vectorize 502 {message, stderr_tail} detail. */
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

/** Raster single-line drawing → SVG via the server-side SLD CLI (goal
 *  950c719c). Synchronous backend call (23 s – 3 min) — the busy state with
 *  elapsed timer makes the wait explicit; result offers download and
 *  "Send to Plot" (uploads the SVG through the normal file flow and hands
 *  it to the Plot tab via onSentToPlot). */
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
  const [running, setRunning] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [result, setResult] = useState<VectorizeResult | null>(null);
  const [svgText, setSvgText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [stderrTail, setStderrTail] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const startedAt = useRef(0);

  useEffect(() => () => { if (timerRef.current) clearInterval(timerRef.current); }, []);

  const safeSvg = useMemo(() => (svgText ? sanitizePreviewSvg(svgText) : null), [svgText]);
  const vb = useMemo(() => (svgText ? extractViewBox(svgText) : null), [svgText]);

  const selectFile = (f: File) => {
    setFile(f);
    setResult(null);
    setSvgText(null);
    setError(null);
    setStderrTail(null);
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

  const run = async () => {
    if (!file || running) return;
    setRunning(true);
    setResult(null);
    setSvgText(null);
    setError(null);
    setStderrTail(null);
    startedAt.current = Date.now();
    setElapsed(0);
    timerRef.current = setInterval(
      () => setElapsed((Date.now() - startedAt.current) / 1000), 1000);
    try {
      const r = await api.vectorize(file, {
        thresh: autoThresh ? null : thresh,
        multipleLines,
      });
      setResult(r);
      setSvgText(await api.vectorizeSvg(r.svg_id));
    } catch (e) {
      setError(vectorizeErrorMessage(e));
      setStderrTail(stderrTailOf(e));
    } finally {
      if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }
      setRunning(false);
    }
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
          Raster single-line drawing (PNG/JPG/…) → SVG via SLD-Vectorization,
          running on the server. One vectorization takes 23 s – 3 min.
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
        <div className="mode-toggle" role="group" aria-label="threshold mode">
          <button type="button" aria-pressed={autoThresh} data-testid="thresh-auto"
            onClick={() => setAutoThresh(true)}>Auto threshold</button>
          <button type="button" aria-pressed={!autoThresh} data-testid="thresh-manual"
            onClick={() => setAutoThresh(false)}>Manual</button>
        </div>
        <div className="opt-grid" role="group" aria-label="vectorize options">
          <label>
            Threshold
            <input type="number" min={THRESH_MIN} max={THRESH_MAX} step={0.01}
              value={thresh} disabled={autoThresh} data-testid="thresh-input"
              aria-label="threshold"
              onChange={(e) => onThreshChange(e.target.value)} />
          </label>
          <label>
            <input type="checkbox" checked={multipleLines} data-testid="multiple-lines"
              onChange={(e) => setMultipleLines(e.target.checked)} />
            Multiple lines (multi-stroke input)
          </label>
        </div>
        <p className="muted small">
          Threshold 0.01–0.99 = dark-pixel cutoff; Auto (omit) is recommended.
        </p>

        <div className="row-actions">
          <button className="primary" disabled={!file || running}
            onClick={() => void run()} data-testid="run-btn">
            {running ? `Vectorizing… ${Math.floor(elapsed)} s` : "Vectorize"}
          </button>
        </div>
        {running && (
          <div className="banner info" role="status" data-testid="busy">
            Vectorization running — <span data-testid="elapsed">{Math.floor(elapsed)} s</span> elapsed.
            This can take 23 s – 3 min; keep this tab open.
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
                <p className="small muted">Vectorized single-line drawing — check the strokes before plotting.</p>
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
