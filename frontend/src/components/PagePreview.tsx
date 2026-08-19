import { useMemo } from "react";
import type { PaperInfo } from "../api/types";

/** Strip the dangerous bits from server-generated preview SVG before inline
 *  render. Defense-in-depth only — the backend already sanitized the upload;
 *  this is the "SVG rendering helper" allowed for direct manipulation. */
export function sanitizePreviewSvg(svg: string): string {
  return svg
    .replace(/<script[\s\S]*?<\/script>/gi, "")
    .replace(/ on[a-z]+\s*=\s*"[^"]*"/gi, "")
    .replace(/ on[a-z]+\s*=\s*'[^']*'/gi, "")
    .replace(/<script[\s\S]*?>/gi, "");
}

/** Extract viewBox (fallback width/height) from an SVG document string. */
export function extractViewBox(svg: string): { minX: number; minY: number; w: number; h: number } {
  const m = svg.match(/viewBox\s*=\s*"([^"]+)"/i);
  if (m) {
    const n = m[1].trim().split(/[\s,]+/).map(Number);
    if (n.length === 4 && n.every((x) => Number.isFinite(x))) {
      return { minX: n[0], minY: n[1], w: n[2], h: n[3] };
    }
  }
  const w = Number(svg.match(/\swidth\s*=\s*"?([\d.]+)/i)?.[1] ?? 0);
  const h = Number(svg.match(/\sheight\s*=\s*"?([\d.]+)/i)?.[1] ?? 0);
  return { minX: 0, minY: 0, w: w || 297, h: h || 210 };
}

/**
* plotter will actually draw) inside a paper rectangle with the hard-clip
* Post-processing preview: renders the /api/jobs/{id}/preview SVG (what the
 * plotter will actually draw) on the sheet background. Sheet outline, safe
 * plot area (red dashed), axis indicators and caption are drawn by the
 * backend annotation — the server SVG is the single source of truth.
 * Pen-up travel lines (phase 2 F1) ship inside the server SVG as a
 * <g class="travel-group">; this component only toggles visibility.
 * ponytail: zoom/pan skipped; add when canvas interactivity is needed.
 */
export default function PagePreview({
  svg, paper, error, paperName, showTravel, onToggleTravel,
}: {
  svg: string | null; paper: PaperInfo | null; paperName: string; error: string | null;
  showTravel?: boolean; onToggleTravel?: (v: boolean) => void;
}) {
  const toggleTravel = onToggleTravel ?? (() => {});
  const show = showTravel ?? false;
  const safe = useMemo(() => (svg ? sanitizePreviewSvg(svg) : null), [svg]);
  const vb = useMemo(() => (svg ? extractViewBox(svg) : null), [svg]);

  if (error) {
    return <div className="preview-empty" data-testid="preview-state">
      <p className="muted">Preview not available yet.</p>
      <p className="small err">{error}</p>
      <p className="muted small">Preparing the job generates the preview — try again after prepare finishes.</p>
    </div>;
  }
  if (!safe || !vb) {
    return <div className="preview-empty" data-testid="preview-state">
      <p className="muted">No preview yet — upload a file and prepare a job.</p>
    </div>;
  }
  const [wmm, hmm] = paper?.size_mm ?? [297, 210];
  const portrait = vb.h > vb.w;
  const paperW = portrait ? Math.min(wmm, hmm) : Math.max(wmm, hmm);
  const paperH = portrait ? Math.max(wmm, hmm) : Math.min(wmm, hmm);
  return (
    <div className={`preview-wrap${show ? " show-travel" : ""}`} data-testid="preview-state">
      <label className="travel-toggle small">
        <input type="checkbox" checked={show} data-testid="travel-toggle"
          onChange={(e) => toggleTravel(e.target.checked)} />
        Show pen travel
      </label>
      <svg className="page-preview" viewBox={`${vb.minX} ${vb.minY} ${vb.w} ${vb.h}`}
        role="img" aria-label={`preview for ${paperName}`}>
        {/* paper sheet */}
        <rect x={vb.minX} y={vb.minY} width={vb.w} height={vb.h}
          fill="#101418" stroke="#2a3a4a" strokeWidth={vb.w * 0.002} />
        {/* sheet outline + safe-area rect + axis arrows come from the
            server annotation (routes._annotate_preview) — source of truth,
            not duplicated here. */}
        {/* geometry (already pen-colored by the pipeline) */}
        <g dangerouslySetInnerHTML={{ __html: safe.includes("<svg") ? innerOf(safe) : safe }} />
      </svg>
      <p className="small muted">
        {paperName.toUpperCase()} · {paperW.toFixed(0)}×{paperH.toFixed(0)} mm — grey = sheet edge, red dashed = safe plot area
        {paper?.loads_orientation ? ` · loads ${paper.loads_orientation}` : ""}
        {paper ? ` · DIP: ${paper.dip_mode}` : ""}
      </p>
    </div>
  );
}

/** The preview SVG's inner markup (children of <svg>), for nesting. */
export function innerOf(svg: string): string {
  const open = svg.match(/<svg[^>]*>/i);
  if (!open) return svg;
  const start = svg.indexOf(open[0]) + open[0].length;
  const end = svg.toLowerCase().lastIndexOf("</svg>");
  return svg.slice(start, end);
}
