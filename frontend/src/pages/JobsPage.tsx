import { useCallback, useEffect, useState } from "react";
import { api, apiErrorMessage } from "../api/client";
import type { Job } from "../api/types";
import { isJobEvent } from "../api/types";
import { useApp } from "../state/app";
import StatusBadge from "../components/StatusBadge";
import Progress from "../components/Progress";

/** Merge a WS job event into a jobs list row (pure — unit-tested). */
export function applyJobEvent(jobs: Job[], ev: {
  job_id?: string; event?: string; status?: string; bytes_sent?: number;
  acked_bytes?: number; total_bytes?: number;
  bytes_total?: number; error?: string | null; pen_down?: boolean | null;
}): Job[] {
  if (!ev.job_id) return jobs;
  let hit = false;
  const next = jobs.map((j) => {
    if (j.id !== ev.job_id) return j;
    hit = true;
    return {
      ...j,
      status: ev.status ?? j.status,
      bytes_sent: ev.acked_bytes ?? ev.bytes_sent ?? j.bytes_sent,
      bytes_total: ev.total_bytes ?? ev.bytes_total ?? j.bytes_total,
      error: ev.error ?? j.error,
    };
  });
  if (!hit) return next; // unknown id — list refresh will pick it up
  return next;
}

const HPGL_PREVIEW_BYTES = 5000;

function fmtTime(epoch: number): string {
  return epoch > 0 ? new Date(epoch * 1000).toLocaleTimeString() : "—";
}

function fmtEstimate(s: number): string {
  return s < 90 ? `≈ ${Math.round(s)} s` : `≈ ${(s / 60).toFixed(1)} min`;
}

export default function JobsPage() {
  const { toast, ws } = useApp();
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Job | null>(null);
  const [penDown, setPenDown] = useState<Record<string, boolean | null>>({});

  const refresh = useCallback(async () => {
    try {
      const res = await api.listJobs();
      setJobs(res.jobs);
      setActiveId(res.active_job_id);
      setError(null);
      setSelected((prev) => (prev ? res.jobs.find((j) => j.id === prev.id) ?? prev : prev));
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  // Live WS progress patches the row (and the open drawer).
  useEffect(() => {
    const m = ws.last;
    if (!m || !isJobEvent(m)) return;
    if (m.event === "progress" && m.job_id && m.pen_down != null) {
      setPenDown((prev) => ({ ...prev, [m.job_id]: m.pen_down! }));
    }
    setJobs((prev) => (prev ? applyJobEvent(prev, m) : prev));
    setSelected((prev) => (prev && prev.id === m.job_id ? applyJobEvent([prev], m)[0] : prev));
  }, [ws.last]);

  const replot = async (j: Job) => {
    try {
      const copy = await api.createJob({
        file_id: j.file_id ?? "", name: `Replot — ${j.name}`,
        paper: j.paper, pen_map: j.pen_map, options: j.options,
      });
      toast("ok", `Replot job created (${copy.id.slice(0, 8)}…)`);
      await refresh();
    } catch (e) {
      toast("error", `Replot failed: ${apiErrorMessage(e)}`);
    }
  };

  const del = async (j: Job) => {
    if (!window.confirm(`Delete job ${j.name}?`)) return;
    try {
      await api.deleteJob(j.id);
      if (selected?.id === j.id) setSelected(null);
      await refresh();
    } catch (e) {
      toast("error", `Delete failed: ${apiErrorMessage(e)}`);
    }
  };

  const downloadHpgl = (j: Job) => {
    const blob = new Blob([j.hpgl], { type: "application/octet-stream" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${j.name.replace(/[^\w.-]+/g, "_")}.hpgl`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  return (
    <div className="page jobs-page">
      <section className="panel">
        <div className="row spread">
          <h2>Jobs</h2>
          <button onClick={() => void refresh()}>Refresh</button>
        </div>
        {error && <div className="banner err">Failed to load jobs: {error} <button onClick={() => void refresh()}>Retry</button></div>}
        {jobs && jobs.length === 0 && (
          <p className="muted empty" data-testid="jobs-empty">No jobs yet — create one on the Plot page.</p>
        )}
        {jobs && jobs.length > 0 && (
          <table className="jobs-table" aria-label="job history">
            <thead>
              <tr><th>Name</th><th>Status</th><th>Bytes</th><th>Estimate</th><th>Paper</th><th>Created</th><th>Updated</th><th /></tr>
            </thead>
            <tbody>
              {jobs.map((j) => (
                <tr key={j.id} className={j.id === activeId ? "active" : ""}>
                  <td>{j.name}</td>
                  <td>
                    <StatusBadge status={j.status} />
                    {["SENDING", "PLOTTING", "COMPLETING"].includes(String(j.status)) && (
                      <span className={`pen-badge${penDown[j.id] ? " down" : ""}`}
                        data-testid={`pen-badge-${j.id.slice(0, 8)}`}
                        title={penDown[j.id] == null ? "pen state unknown" : penDown[j.id] ? "pen down" : "pen up"}>
                        {penDown[j.id] ? "▼" : "▲"}
                      </span>
                    )}
                  </td>
                  <td className="bytes-cell"><Progress value={j.bytes_sent} total={j.bytes_total} compact /></td>
                  <td className="muted small">{j.estimate ? fmtEstimate(j.estimate.est_seconds) : "—"}</td>
                  <td>{j.paper}</td>
                  <td>{fmtTime(j.created_at)}</td>
                  <td>{fmtTime(j.updated_at)}</td>
                  <td className="row-actions">
                    <button onClick={() => setSelected(j)}>Details</button>
                    <button onClick={() => void replot(j)}>Replot</button>
                    <button className="danger" onClick={() => void del(j)}>Delete</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {selected && (
        <aside className="drawer" data-testid="job-drawer" aria-label="job details">
          <div className="row spread">
            <h3>{selected.name}</h3>
            <button onClick={() => setSelected(null)}>Close</button>
          </div>
          <StatusBadge status={selected.status} />
          <Progress value={selected.bytes_sent} total={selected.bytes_total} />
          {selected.estimate && (
            <p className="muted small" data-testid="drawer-estimate">
              Plot estimate {fmtEstimate(selected.estimate.est_seconds)} — drawn{" "}
              {Math.round(selected.estimate.drawn_mm)} mm + travel {Math.round(selected.estimate.travel_mm)} mm @ {selected.estimate.velocity_cm_s} cm/s
            </p>
          )}
          <dl className="job-meta">
            <dt>ID</dt><dd>{selected.id}</dd>
            <dt>Paper</dt><dd>{selected.paper}</dd>
            <dt>Pen map</dt><dd>{Object.entries(selected.pen_map).map(([l, p]) => `${l}→${p}`).join(", ") || "—"}</dd>
            <dt>Options</dt><dd>{JSON.stringify(selected.options)}</dd>
            <dt>Error</dt><dd>{selected.error ?? "—"}</dd>
          </dl>
          <h4>HP-GL inspector</h4>
          {selected.hpgl ? (<>
            <pre className="hpgl-preview" data-testid="hpgl-preview">
              {selected.hpgl.slice(0, HPGL_PREVIEW_BYTES)}
              {selected.hpgl.length > HPGL_PREVIEW_BYTES && `\n… (${selected.hpgl.length.toLocaleString()} bytes total)`}
            </pre>
            <div className="row-actions">
              <button onClick={() => void navigator.clipboard.writeText(selected.hpgl)}>Copy HP-GL</button>
              <button onClick={() => downloadHpgl(selected)}>Download .hpgl</button>
            </div>
          </>) : <p className="muted small">No HP-GL attached yet (prepare the job).</p>}
          <h4>Stats</h4>
          <pre className="hpgl-preview">{JSON.stringify(selected.stats, null, 2)}</pre>
        </aside>
      )}
    </div>
  );
}
