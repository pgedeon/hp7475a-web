import { useState } from "react";
import { AppProvider, useApp } from "./state/app";
import Toasts from "./components/Toast";
import type { UploadSvgResult } from "./api/types";
import PlotPage from "./pages/PlotPage";
import VectorizePage from "./pages/VectorizePage";
import ManualPage from "./pages/ManualPage";
import JobsPage from "./pages/JobsPage";
import DevicePage from "./pages/DevicePage";
import DiagnosticsPage from "./pages/DiagnosticsPage";
import SettingsPage from "./pages/SettingsPage";

const TABS = ["Plot", "Vectorize", "Manual", "Jobs", "Device", "Diagnostics", "Settings"] as const;
type Tab = (typeof TABS)[number];

/** Tab shell + global status header (backend WS state, device connection). */
function Shell() {
  const [tab, setTab] = useState<Tab>("Plot");
  const [plotFile, setPlotFile] = useState<UploadSvgResult | null>(null);
  const { ws, device } = useApp();
  return (
    <div className="app">
      <header className="app-header">
        <h1>HP 7475A</h1>
        <span className={ws.state === "open" ? "badge ok" : ws.state === "connecting" ? "badge busy" : "badge err"}
          title={`WS ${ws.state}`}>
          {ws.state === "open" ? "server online" : ws.state === "connecting" ? "connecting…" : "server offline"}
        </span>
        <span className={device?.connected ? "badge ok" : "badge info"}>
          {device?.connected ? `● ${device.port}` : "○ disconnected"}
        </span>
        <nav className="tabs" aria-label="pages">
          {TABS.map((t) => (
            <button key={t} className={t === tab ? "tab active" : "tab"}
              aria-current={t === tab ? "page" : undefined}
              onClick={() => setTab(t)}>{t}</button>
          ))}
        </nav>
      </header>
      <main>
        {tab === "Plot" && <PlotPage initialFile={plotFile} />}
        {tab === "Vectorize" && <VectorizePage onSentToPlot={(f) => { setPlotFile(f); setTab("Plot"); }} />}
        {tab === "Manual" && <ManualPage />}
        {tab === "Jobs" && <JobsPage />}
        {tab === "Device" && <DevicePage />}
        {tab === "Diagnostics" && <DiagnosticsPage />}
        {tab === "Settings" && <SettingsPage />}
      </main>
      <Toasts />
    </div>
  );
}

export default function App() {
  return (
    <AppProvider>
      <Shell />
    </AppProvider>
  );
}
