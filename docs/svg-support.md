# SVG Support Matrix

What the pipeline accepts, converts, and reports. Goal: **no silent
surprises** — anything we can't plot is surfaced in the analysis, never
quietly dropped or approximated.

## Plotting model

The HP 7475A draws **stroked vectors only** (no fills, no raster, no
gradients). The pipeline therefore treats an SVG as a set of stroked paths
organized into layers → pens.

Analysis reports two tiers so the UI can act on them differently:
- **`unsupported`** — blockers: content that will NOT be plotted as-is
  (text, rasters, filters, gradients, markers, clip-paths, masks). Plotting
  proceeds only after conversion/removal.
- **`warnings`** — non-blocking quirks the pipeline absorbs. Currently just
  fills: vpype extracts each filled shape's *outline*, so a fill-only file
  still plots (as outlines) instead of dead-ending as "unsupported".

| SVG feature | Status | Detail |
|---|---|---|
| `<path>` (M/L/C/A/H/V/Z) | ✅ plotted | via vpype (Béziers flattened with chord tolerance) |
| `<line>`, `<rect>`, `<circle>`, `<ellipse>`, `<polyline>`, `<polygon>` | ✅ plotted | converted to paths |
| Stroke color | ✅ → layer/pen mapping | distinct colors = distinct layers by default |
| `stroke-width` | ⚠️ informational | pen width is physical; analysis reports it |
| `stroke-dasharray` | ⚠️ not plotted | dashes render as solid lines (vpype ignores the pattern); not separately reported |
| Fill (any non-`none`) | ⚠️ warning (non-blocking) | **outline-only**: the shape's boundary is plotted, filled interior ignored — surfaces in `warnings`, never blocks. (Hatching = roadmap, not v1) |
| `<text>` | ⚠️ convertible at upload | fonts are environment-dependent; enable **Convert text to paths (Inkscape)** at upload for server-side conversion (see below), or pre-convert in Inkscape (Path → Object to Path) |
| `<image>` / raster | ❌ rejected in report | no raster capability |
| Gradients / filters / masks / clip-paths | ❌ not plotted | reported; geometry may partially convert but is flagged |
| Markers | ❌ not plotted | reported |
| `<use>` (internal refs) | ✅ resolved | external refs are stripped by the sanitizer |
| Nested groups | ✅ | flattened into their layer |
| `transform` | ✅ | applied (vpype handles the common cases) |
| Inkscape layers (`inkscape:label`) | ✅ preferred layer source | fallback: top-level `<g id>` |
| CSS classes / `<style>` blocks | ⚠️ partial | presentation attributes on elements are honored; complex CSS cascade may not be — convert styles to attributes (Inkscape "Save as plain SVG") for exactness |
| ViewBox / width+height | ✅ required one of | `no-viewbox` + no size → analysis error |

## Sanitizer (fail-closed)

Applied to every upload **before** storage: rejects oversized files, strips
`<script>`, `on*` event attributes, `javascript:`/`data:text/html` URLs,
`<foreignObject>`, external `<use>`/hrefs, DTD/DOCTYPE (XXE surface), and
comments/PIs. Unparseable XML → rejected (422), never "best-effort" parsed.

## Text → paths conversion (server-side, phase 3 F6)

The upload form's **Convert text to paths (Inkscape)** checkbox (default
off, stateless) runs headless Inkscape on the server when the sanitized
upload contains `<text>`:

1. upload is sanitized FIRST (fail-closed, as always);
2. `inkscape --export-type=svg --export-text-to-path --export-plain-svg`
   runs in a temp dir (30 s timeout, scrubbed environment, no network);
3. the output is **re-sanitized** before storage — a converted file that
   fails the sanitizer keeps the original instead (never stored);
4. analysis runs on the CONVERTED file: text warnings disappear, and the
   glyph outlines arrive as stroked `<path>` elements (colors mode reads
   `style="stroke:…"` too).

Limitations (honest reporting, not silent approximation):

- **Fills stay outline-only.** Text-to-path conversion does not convert
  fills to hatching; filled glyph interiors are still ignored — stroke
  your text for plotting.
- The bounding box may shift a fraction of a mm (glyph metrics vs. path
  outline); the analysis re-runs on the converted geometry, so what you
  see is what the plotter gets.
- Inkscape missing / erroring / timing out → fail-soft: the original
  sanitized file is stored and a warning surfaces in the upload response
  (`conversion.warning`) and the UI. Uploads are never blocked.
- Rasters, filters, gradients etc. are NOT converted — only text.

## Paper fit

Analysis computes the geometry bounding box (mm) and reports `est_paper_fit`
for each paper (A4/A3/A/B) with a 5 mm margin. The layout step then fits the
design onto the chosen paper; if it can't, Prepare fails with an actionable
message instead of plotting a crop.

## Recommended authoring flow

1. Design in Inkscape; use one layer per pen; set stroke colors per layer.
2. Convert text to paths — either here (Path → Object to Path) or at upload
   time via the **Convert text to paths (Inkscape)** checkbox (server-side).
3. Save as **plain SVG**.
4. Upload; check the analysis panel (layers/colors/unsupported list empty —
   each warning now carries an action hint).
5. Map layers → pens, preview, confirm, plot.
