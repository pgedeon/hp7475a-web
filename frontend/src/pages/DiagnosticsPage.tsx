import { useEffect, useState } from "react";
import { api, apiErrorMessage } from "../api/client";
import type { DeviceError } from "../api/types";
import { isJobEvent } from "../api/types";
import { useApp } from "../state/app";
import { decodeStatusBits } from "./DevicePage";

/**
 * Diagnostics: ESC.B buffer monitor (from device status, if the backend
 * includes it — dedicated buffer endpoint not implemented yet), OE/ESC.E
 * error query, and a raw log of recent WS events.
 */
export default function DiagnosticsPage() {
  const { device, deviceError, refreshDevice, ws, toast } = useApp();
  const [devError, setDevError] = useState<DeviceError | null>(null);
  const [autoPoll, setAutoPoll] = useState(true);

  useEffect(() => {
    if (!autoPoll) return;
    const t = setInterval(() => void refreshDevice(), 2000);
    return () => clearInterval(t);
  }, [autoPoll, refreshDevice]);

  const queryError = async () => {
    try { setDevError(await api.deviceError()); }
    catch (e) { toast("error", apiErrorMessage(e)); }
  };

  const s = device?.status ?? null;
  const bits = s?.status != null ? decodeStatusBits(s.status) : null;
  const buffer = pickBuffer(s);

  return (
    <div className="page diag-page">
      <section className="panel">
        <h2>Diagnostics</h2>
        {deviceError && <div className="banner err">Status poll failed: {deviceError} <button onClick={() => void refreshDevice()}>Retry</button></div>}
        <h3>Device status (OS byte)</h3>
        {!device?.connected && <p className="muted" data-testid="diag-empty">Device not connected.</p>}
        {device?.connected && bits && (<>
          <div className="device-status" data-testid="diag-status">
            <span className={bits.penDown ? "badge warn" : "badge info"}>pen {bits.penDown ? "down" : "up"}</span>
            <span className={bits.ready ? "badge ok" : "badge warn"}>{bits.ready ? "ready" : "not ready"}</span>
            <span className={bits.error ? "badge err" : "badge ok"}>{bits.error ? "error" : "no error"}</span>
            <span className="mono">raw: {String(s?.status)}</span>
          </div>
          <label className="small">
            <input type="checkbox" checked={autoPoll} onChange={(e) => setAutoPoll(e.target.checked)} /> poll every 2 s
          </label>
        </>)}

        <h3>ESC.B buffer monitor</h3>
        {buffer != null
          ? <p className="mono" data-testid="escb">buffer free: {buffer}</p>
          : <p className="muted small">Buffer level not exposed by /api/device/status yet — backend gap.</p>}

        <h3>OE / ESC.E error</h3>
        <button onClick={() => void queryError()} disabled={!device?.connected}>Query error</button>
        {devError && (
          <pre className="hpgl-preview" data-testid="oee">
            {JSON.stringify(devError, null, 2)}
          </pre>
        )}
        {!devError && <p className="muted small">No error queried yet.</p>}
      </section>

      <section className="panel">
        <h3>Raw WS events (last {ws.log.length})</h3>
        <p className="muted small">
          Socket: <span data-testid="ws-state">{ws.state}</span>
          {ws.lastAttempt > 0 && ` · last attempt ${new Date(ws.lastAttempt).toLocaleTimeString()}`}
        </p>
        {ws.log.length === 0 && <p className="muted empty" data-testid="ws-empty">No events yet.</p>}
        <ul className="ws-log" data-testid="ws-log">
          {[...ws.log].reverse().map((m, i) => (
            <li key={i} className="mono small">
            {isJobEvent(m) ? `job ${m.job_id.slice(0, 8)} ${m.status} ${m.bytes_sent ?? 0}/${m.bytes_total ?? 0}${m.error ? ` err=${m.error}` : ""}`
                : JSON.stringify(m)}
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}

/** Defensive: the status dict may carry a buffer field under any of these keys. */
function pickBuffer(s: Record<string, unknown> | null | undefined): string | null {
  if (!s) return null;
  for (const k of ["buffer", "buffer_free", "esc_b", "escb", "buffer_available"]) {
    if (s[k] !== undefined) return String(s[k]);
  }
  return null;
}
