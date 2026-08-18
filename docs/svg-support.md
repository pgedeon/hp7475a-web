# SVG Support Matrix

What the pipeline accepts, converts, and reports. Goal: **no silent
surprises** — anything we can't plot is surfaced in the analysis, never
quietly dropped or approximated.

## Plotting model

The HP 7475A draws **stroked vectors only** (no fills, no raster, no
gradients). The pipeline therefore treats an SVG as a set of stroked paths
organized into layers → pens.

| SVG feature | Status | Detail |
|---|---|---|
| `<path>` (M/L/C/A/H/V/Z) | ✅ plotted | via vpype (Béziers flattened with chord tolerance) |
| `<line>`, `<rect>`, `<circle>`, `<ellipse>`, `<polyline>`, `<polygon>` | ✅ plotted | converted to paths |
| Stroke color | ✅ → layer/pen mapping | distinct colors = distinct layers by default |
| `stroke-width` | ⚠️ informational | pen width is physical; analysis reports it |
| `stroke-dasharray` | ⚠️ not plotted | reported in `unsupported` |
| Fill (any non-`none`) | ❌ not plotted | **outline-only option** draws the boundary; fill area itself ignored + reported. (Hatching = roadmap, not v1) |
| `<text>` | ❌ not plotted | fonts are environment-dependent; convert text→paths in Inkscape first (documented in UI) |
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

## Paper fit

Analysis computes the geometry bounding box (mm) and reports `est_paper_fit`
for each paper (A4/A3/A/B) with a 5 mm margin. The layout step then fits the
design onto the chosen paper; if it can't, Prepare fails with an actionable
message instead of plotting a crop.

## Recommended authoring flow

1. Design in Inkscape; use one layer per pen; set stroke colors per layer.
2. Convert text to paths (Path → Object to Path).
3. Save as **plain SVG**.
4. Upload; check the analysis panel (layers/colors/unsupported list empty).
5. Map layers → pens, preview, confirm, plot.
