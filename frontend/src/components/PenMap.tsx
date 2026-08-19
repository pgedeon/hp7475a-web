import { PEN_COLORS, type RawLayer } from "../api/types";

/** Pens are hardware slots 1..6 — 0 is a legal HP-GL "no pen" internally but
 *  the UI only ever assigns 1..6; "don't plot" = omit from pen_map. */
export function isValidPen(n: number): boolean {
  return Number.isInteger(n) && n >= 1 && n <= 6;
}

/** Normalize analyzer layers (string | object) to {name, color}. */
export function normalizeLayers(layers: RawLayer[] | undefined): { name: string; color?: string }[] {
  return (layers ?? []).map((l) =>
    typeof l === "string" ? { name: l } : { name: String(l.name ?? "layer"), color: l.color }
  );
}

/**
 * Layer → pen mapping table with color swatches. Invalid pen values in
 * onChange (0, 7, ...) are rejected — assignment stays unchanged.
 */
export default function PenMap({
  layers, penMap, onChange,
}: {
  layers: { name: string; color?: string }[];
  penMap: Record<string, number>;
  onChange: (map: Record<string, number>) => void;
}) {
  if (layers.length === 0) {
    return <p className="muted">No layers detected in this file.</p>;
  }
  const set = (layer: string, pen: number) => {
    if (!isValidPen(pen)) return; // reject 0 / 7 / fractional inputs
    onChange({ ...penMap, [layer]: pen });
  };
  return (
    <table className="penmap" aria-label="layer to pen mapping">
      <thead>
        <tr><th>Layer / color</th><th>Swatch</th><th>Pen</th></tr>
      </thead>
      <tbody>
        {layers.map((l) => {
          const pen = penMap[l.name];
          return (
            <tr key={l.name}>
              <td>{l.name}</td>
              <td>
                <span className="swatch" aria-hidden="true"
                  style={{ background: l.color ?? "#888" }} />
                <span className="muted">{l.color ?? "—"}</span>
              </td>
              <td>
                <select aria-label={`pen for layer ${l.name}`}
                  value={pen ?? ""}
                  onChange={(e) => {
                    const v = e.target.value;
                    if (v === "") {
                      const next = { ...penMap };
                      delete next[l.name];
                      onChange(next);
                    } else set(l.name, Number(v));
                  }}>
                  <option value="">— do not plot —</option>
                  {[1, 2, 3, 4, 5, 6].map((p) => (
                    <option key={p} value={p}>
                      Pen {p}
                    </option>
                  ))}
                </select>
                {pen != null && (
                  <span className="pen-dot" title={`Pen ${pen}`}
                    style={{ background: PEN_COLORS[pen] }} />
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
