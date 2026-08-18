import { useState } from "react";
import { api, apiErrorMessage } from "../api/client";
import type { PaperInfo } from "../api/types";
import { PAPER_NAMES } from "../api/types";
import { useApp } from "../state/app";

/** Clamp mm coordinates to paper bounds (A4 default — no OH/position REST
 *  endpoint yet; backend gap, see report). Returns clamped values + flags. */
export function clampToPaper(
  x: number, y: number, paper: PaperInfo | null
): { x: number; y: number; clampedX: boolean; clampedY: boolean } {
  const [w, h] = paper?.size_mm ?? [297, 210];
  const cx = Math.min(w, Math.max(0, x));
  const cy = Math.min(h, Math.max(0, y));
  return { x: cx, y: cy, clampedX: cx !== x, clampedY: cy !== y };
}

const STEPS = [1, 10, 50];

export default function ManualPage() {
  const { device, papers, refreshDevice, toast } = useApp();
  const connected = device?.connected ?? false;
  const [paperName, setPaperName] = useState<string>("a4");
  const paper = papers[paperName] ?? null;
  const [pos, setPos] = useState({ x: 0, y: 0 }); // assumed abs pos (no OA endpoint)
  const [step, setStep] = useState(1);
  const [gotoX, setGotoX] = useState("0");
  const [gotoY, setGotoY] = useState("0");
  const [clampWarn, setClampWarn] = useState<string | null>(null);

  /** Track assumed position locally; warn when target leaves paper bounds. */
  const moveAbs = async (x: number, y: number) => {
    const c = clampToPaper(x, y, paper);
    if (c.clampedX || c.clampedY) {
      setClampWarn(`Clamped to ${paperName.toUpperCase()} bounds: (${c.x.toFixed(1)}, ${c.y.toFixed(1)}) mm`);
    } else setClampWarn(null);
    setPos({ x: c.x, y: c.y });
    try {
      await api.move(c.x, c.y, "mm");
      await refreshDevice();
    } catch (e) {
      toast("error", apiErrorMessage(e));
    }
  };

  const jog = (dx: number, dy: number) => void moveAbs(pos.x + dx, pos.y + dy);

  const cmd = async (fn: () => Promise<unknown>, label: string) => {
    try { await fn(); await refreshDevice(); toast("ok", `${label} OK`); }
    catch (e) { toast("error", `${label} failed: ${apiErrorMessage(e)}`); }
  };

  return (
    <div className="page manual-page">
      <section className="panel">
        <h2>Manual control</h2>
        {!connected && (
          <div className="banner warn" data-testid="manual-disabled">
            Not connected — connect on the Device page first. Controls disabled.
          </div>
        )}
        <div className="manual-grid">
          <div>
            <h3>Jog (mm, clamped to paper)</h3>
            <div className="paper-mini">
              <label>Clamp paper:{" "}
                <select value={paperName} onChange={(e) => setPaperName(e.target.value)} aria-label="clamp paper">
                  {PAPER_NAMES.map((p) => <option key={p}>{p}</option>)}
                </select>
              </label>
              {paper && <span className="muted small"> {paper.size_mm[0].toFixed(0)}×{paper.size_mm[1].toFixed(0)} mm</span>}
            </div>
            <div className="jog-pad" role="group" aria-label="jog pad">
              <div />
              <button disabled={!connected} onClick={() => jog(0, step)} aria-label={`jog up ${step} mm`}>↑</button>
              <div />
              <button disabled={!connected} onClick={() => jog(-step, 0)} aria-label={`jog left ${step} mm`}>←</button>
              <span className="jog-center" title="assumed position">{pos.x.toFixed(0)},{pos.y.toFixed(0)}</span>
              <button disabled={!connected} onClick={() => jog(step, 0)} aria-label={`jog right ${step} mm`}>→</button>
              <div />
              <button disabled={!connected} onClick={() => jog(0, -step)} aria-label={`jog down ${step} mm`}>↓</button>
              <div />
            </div>
            <div className="step-row" role="group" aria-label="jog step">
              {STEPS.map((s) => (
                <label key={s}>
                  <input type="radio" name="step" checked={step === s} onChange={() => setStep(s)} /> {s} mm
                </label>
              ))}
            </div>
            {clampWarn && <div className="banner warn small" data-testid="clamp-warn" role="alert">{clampWarn}</div>}
            <p className="muted small">Assumed position (no position query endpoint yet — resets to 0,0).</p>
          </div>

          <div>
            <h3>Move to X/Y (mm)</h3>
            <div className="goto-row">
              <label>X <input type="number" value={gotoX} aria-label="target x mm"
                onChange={(e) => setGotoX(e.target.value)} /></label>
              <label>Y <input type="number" value={gotoY} aria-label="target y mm"
                onChange={(e) => setGotoY(e.target.value)} /></label>
              <button className="primary" disabled={!connected}
                onClick={() => void moveAbs(Number(gotoX) || 0, Number(gotoY) || 0)}>Move</button>
            </div>

            <h3>Pen</h3>
            <div className="pen-buttons">
              {[1, 2, 3, 4, 5, 6].map((p) => (
                <button key={p} disabled={!connected}
                  onClick={() => void cmd(() => api.selectPen(p), `Select pen ${p}`)}>Pen {p}</button>
              ))}
            </div>
            <div className="pen-buttons">
              <button disabled={!connected} onClick={() => void cmd(api.penUp, "Pen up")}>Pen up</button>
              <button disabled={!connected} onClick={() => void cmd(api.penDown, "Pen down")}>Pen down</button>
              <button disabled={!connected} onClick={() => void cmd(api.park, "Park")}>Park pen</button>
            </div>
          </div>
        </div>
      </section>
    </div>
  );
}
