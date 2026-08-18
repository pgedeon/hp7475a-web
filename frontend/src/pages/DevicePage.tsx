import { useState } from "react";
import { api, apiErrorMessage } from "../api/client";
import type { ConnectResult, DeviceError, DeviceStatus, PortInfo } from "../api/types";
import { useApp } from "../state/app";

/** Decode HP 7475A status byte (manual): bit0 pen-down, bit4 ready, bit5 error. */
export function decodeStatusBits(status: number): { penDown: boolean; ready: boolean; error: boolean } {
  return { penDown: (status & 1) !== 0, ready: (status & 16) !== 0, error: (status & 32) !== 0 };
}

const STEPS = ["Pick port", "Serial settings", "Connect + identify", "Paper check", "Done"];

export default function DevicePage() {
  const { device, refreshDevice, toast } = useApp();
  const [ports, setPorts] = useState<PortInfo[] | null>(null);
  const [portsError, setPortsError] = useState<string | null>(null);
  const [selectedPort, setSelectedPort] = useState<PortInfo | null>(null);
  const [baud, setBaud] = useState(9600);
  const [bytesize, setBytesize] = useState(8);
  const [parity, setParity] = useState("N");
  const [stopbits, setStopbits] = useState(1);
  const [busy, setBusy] = useState(false);
  const [connectInfo, setConnectInfo] = useState<ConnectResult | null>(null);
  const [identity, setIdentity] = useState<string | null>(null);
  const [step, setStep] = useState(0);
  const [devError, setDevError] = useState<DeviceError | null>(null);

  const loadPorts = async () => {
    setPortsError(null);
    try {
      const res = await api.listPorts();
      setPorts(res.ports);
    } catch (e) {
      setPortsError(apiErrorMessage(e));
    }
  };

  const connect = async () => {
    if (!selectedPort) return;
    setBusy(true);
    try {
      const res = await api.connect({
        port: selectedPort.by_id_path || selectedPort.device,
        baudrate: baud, bytesize, parity, stopbits,
      });
      setConnectInfo(res);
      await refreshDevice();
      setStep(2);
    } catch (e) {
      toast("error", `Connect failed: ${apiErrorMessage(e)}`);
    } finally {
      setBusy(false);
    }
  };

  const identify = async () => {
    setBusy(true);
    try {
      const res = await api.identify();
      const id = String(res.identity ?? JSON.stringify(res));
      setIdentity(id);
      setStep(3);
      if (!id.includes("7475A")) toast("info", `Unexpected identity: ${id} — verify cable/settings`);
      else toast("ok", "Identified: 7475A ✓");
    } catch (e) {
      toast("error", `Identify failed: ${apiErrorMessage(e)}`);
    } finally {
      setBusy(false);
    }
  };

  const queryError = async () => {
    try { setDevError(await api.deviceError()); }
    catch (e) { toast("error", apiErrorMessage(e)); }
  };

  const disconnect = async () => {
    try {
      await api.disconnect();
      setConnectInfo(null); setIdentity(null); setStep(0);
      await refreshDevice();
      toast("ok", "Disconnected");
    } catch (e) { toast("error", apiErrorMessage(e)); }
  };

  const connected = device?.connected ?? false;

  return (
    <div className="page device-page">
      <section className="panel">
        <h2>Device {connected ? "· connected" : "· disconnected"}</h2>

        {device == null && <p className="muted" data-testid="device-empty">No device status yet…</p>}
        {connected && device?.port && (
          <div className="banner ok" data-testid="device-connected">
            Connected to <code>{device.port}</code>
            {device.settings ? ` · ${device.settings.baudrate} ${device.settings.bytesize}${device.settings.parity}${device.settings.stopbits}` : ""}
          </div>
        )}

        {!connected && (<>
          <ol className="wizard-steps" aria-label="connection wizard steps">
            {STEPS.map((s, i) => <li key={s} className={i === step ? "current" : i < step ? "done" : ""}>{s}</li>)}
          </ol>

          {step === 0 && (<>
            <div className="row spread">
              <h3>Step 1 — Pick serial port</h3>
              <button onClick={() => void loadPorts()} data-testid="refresh-ports">Refresh ports</button>
            </div>
            {portsError && <div className="banner err">Port discovery failed: {portsError} <button onClick={() => void loadPorts()}>Retry</button></div>}
            {!ports && !portsError && <p className="muted" data-testid="ports-empty">Click “Refresh ports” to scan.</p>}
            {ports && ports.length === 0 && <p className="muted empty" data-testid="ports-empty">No serial ports found. Plug in the FTDI adapter; check <code>dialout</code> group membership.</p>}
            <ul className="port-list">
              {(ports ?? []).map((p) => (
                <li key={p.device} className={selectedPort?.device === p.device ? "selected" : ""}>
                  <label>
                    <input type="radio" name="port" checked={selectedPort?.device === p.device}
                      onChange={() => setSelectedPort(p)} />
                    <span>
                      <b>{p.device}</b>
                      {p.ftdi && <span className="badge ok ftdi" data-testid="ftdi-badge">FTDI</span>}
                      {p.description && <span className="muted small"> · {p.description}</span>}
                      {p.vid != null && p.pid != null && <span className="muted small"> · {p.vid.toString(16)}:{p.pid.toString(16)}</span>}
                    </span>
                  </label>
                  {p.by_id_path && <div className="muted small mono">{p.by_id_path}</div>}
                  {p.hint && <div className="port-hint small" data-testid="port-hint">{p.hint}</div>}
                  {p.writable === false && <div className="banner warn small">Not writable — add user to <code>dialout</code>: sudo usermod -aG dialout "$USER" (log out/in after)</div>}
                </li>
              ))}
            </ul>
            <button className="primary" disabled={!selectedPort} onClick={() => setStep(1)}>Next: serial settings</button>
          </>)}

          {step === 1 && (<>
            <h3>Step 2 — Serial settings (rear-panel switches must match)</h3>
            <div className="serial-form">
              <label>Baud rate
                <select value={baud} onChange={(e) => setBaud(Number(e.target.value))}>
                  {[300, 1200, 2400, 4800, 9600, 19200].map((b) => <option key={b}>{b}</option>)}
                </select>
              </label>
              <label>Data bits
                <select value={bytesize} onChange={(e) => setBytesize(Number(e.target.value))}>
                  <option>7</option><option>8</option>
                </select>
              </label>
              <label>Parity
                <select value={parity} onChange={(e) => setParity(e.target.value)}>
                  <option value="N">None</option><option value="E">Even</option><option value="O">Odd</option>
                </select>
              </label>
              <label>Stop bits
                <select value={stopbits} onChange={(e) => setStopbits(Number(e.target.value))}>
                  <option>1</option><option>2</option>
                </select>
              </label>
            </div>
            <p className="muted small">HP 7475A default preset: 9600 8N1.</p>
            <div className="row-actions">
              <button onClick={() => setStep(0)}>Back</button>
              <button className="primary" disabled={busy} onClick={() => void connect()} data-testid="connect-btn">
                {busy ? "Connecting…" : "Connect"}
              </button>
            </div>
          </>)}

          {step === 2 && (<>
            <h3>Step 3 — Identify plotter</h3>
            {connectInfo && <pre className="hpgl-preview">{JSON.stringify(connectInfo.info, null, 2)}</pre>}
            <div className="row-actions">
              <button onClick={() => setStep(1)}>Back</button>
              <button className="primary" disabled={busy} onClick={() => void identify()} data-testid="identify-btn">
                {busy ? "Querying…" : "Send identify query"}
              </button>
            </div>
          </>)}

          {step === 3 && (<>
            <h3>Step 4 — Paper check</h3>
            <p>
              Loaded-paper extents (OH) are not exposed over REST yet — verify the sheet
              matches the paper you will select for jobs (A4 detected on this device at
              validation time). Status below must show <b>ready</b>, no error.
            </p>
            <DeviceStatusView status={device} />
            <div className="row-actions">
              <button onClick={() => void queryError()}>Query error (OE/ESC.E)</button>
              <button className="primary" onClick={() => setStep(4)}>Next</button>
            </div>
          </>)}

          {step === 4 && (<>
            <h3>Step 5 — Done</h3>
            <p>Connection verified{identity ? ` — identity ${identity}` : ""}. Manual controls and plotting are now available.</p>
            <DeviceStatusView status={device} />
          </>)}
        </>)}

        {connected && (<>
          <DeviceStatusView status={device} />
          <div className="row-actions">
            <button onClick={() => void identify()} data-testid="identify-btn">Identify</button>
            <button onClick={() => void queryError()}>Query error</button>
            <button className="danger" onClick={() => void disconnect()}>Disconnect</button>
          </div>
          {devError && <pre className="hpgl-preview" data-testid="device-error">{JSON.stringify(devError, null, 2)}</pre>}
        </>)}
      </section>
    </div>
  );
}

function DeviceStatusView({ status }: { status: DeviceStatus | null }) {
  if (!status) return <p className="muted">No status.</p>;
  const bits = status.status?.status != null ? decodeStatusBits(status.status.status) : null;
  return (
    <div className="device-status" data-testid="device-status">
      <span className={status.connected ? "badge ok" : "badge err"}>
        {status.connected ? "connected" : "disconnected"}
      </span>
      {bits && (<>
        <span className={bits.penDown ? "badge warn" : "badge info"}>pen {bits.penDown ? "down" : "up"}</span>
        <span className={bits.ready ? "badge ok" : "badge warn"}>{bits.ready ? "ready" : "not ready"}</span>
        <span className={bits.error ? "badge err" : "badge ok"}>{bits.error ? "error" : "no error"}</span>
        <span className="muted small mono">status byte: {status.status?.status}</span>
      </>)}
      {status.status?.error && <span className="err small">{status.status.error}</span>}
    </div>
  );
}
