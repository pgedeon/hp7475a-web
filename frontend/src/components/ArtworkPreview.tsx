import { useMemo } from "react";
import { sanitizePreviewSvg, extractViewBox, innerOf } from "./PagePreview";

/** Uploaded-artwork preview (goal 47da763c phase 3 F5): renders the SANITIZED
 *  stored SVG exactly as uploaded — no paper frame, no placement math.
 *  Swapped out by the annotated placement preview once a job is READY.
 *  Labeled explicitly so it is never mistaken for on-paper placement. */
export default function ArtworkPreview({
  svg, note,
}: {
  svg: string | null;
  note?: string | null;
}) {
  const safe = useMemo(() => (svg ? sanitizePreviewSvg(svg) : null), [svg]);
  const vb = useMemo(() => (svg ? extractViewBox(svg) : null), [svg]);

  if (!safe || !vb) {
    return (
      <div className="preview-empty" data-testid="artwork-preview">
        <p className="muted">Artwork preview unavailable.</p>
        <p className="muted small">The file is stored — you can still create a job.</p>
      </div>
    );
  }
  return (
    <div className="artwork-preview" data-testid="artwork-preview">
      <svg className="page-preview" viewBox={`${vb.minX} ${vb.minY} ${vb.w} ${vb.h}`}
        role="img" aria-label="uploaded artwork preview (not to paper placement)">
        <g dangerouslySetInnerHTML={{ __html: safe.includes("<svg") ? innerOf(safe) : safe }} />
      </svg>
      <p className="small muted" data-testid="artwork-label">
        Artwork preview — configure &amp; create job for on-paper placement
        {note ? ` · ${note}` : ""}
      </p>
    </div>
  );
}
