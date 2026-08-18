import { useCallback, useEffect, useState } from "react";
import { api, apiErrorMessage } from "../api/client";
import type { AppSettings } from "../api/types";
import { useApp } from "../state/app";

/** Settings: stream/backend view is read-only; the editable `custom` blob is
 *  the only persisted section (PUT /api/settings stores `custom` wholesale). */
export default function SettingsPage() {
  const { toast } = useApp();
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [draftError, setDraftError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const s = await api.getSettings();
      setSettings(s);
      setDraft(JSON.stringify(s.custom ?? {}, null, 2));
      setError(null);
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const save = async () => {
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(draft) as Record<string, unknown>;
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw new Error("not an object");
    } catch (e) {
      setDraftError(`Invalid JSON: ${e instanceof Error ? e.message : String(e)}`);
      return;
    }
    setDraftError(null);
    setSaving(true);
    try {
      await api.putSettings(parsed);
      toast("ok", "Settings saved");
      await load();
    } catch (e) {
      toast("error", `Save failed: ${apiErrorMessage(e)}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="page settings-page">
      <section className="panel">
        <h2>Settings</h2>
        {error && <div className="banner err">Failed to load: {error} <button onClick={() => void load()}>Retry</button></div>}
        {!settings && !error && <p className="muted">Loading…</p>}
        {settings && (<>
          <h3>Stream / backend (read-only)</h3>
          <dl className="job-meta" data-testid="stream-settings">
            {settings.host != null && <><dt>Host</dt><dd>{settings.host}</dd></>}
            {settings.port != null && <><dt>Port</dt><dd>{settings.port}</dd></>}
            {settings.stream && Object.entries(settings.stream).map(([k, v]) => (
              <FragmentRow key={k} k={k} v={v} />
            ))}
            {settings.job_history_keep != null && <><dt>Job history keep</dt><dd>{settings.job_history_keep}</dd></>}
          </dl>
          <h3>Custom settings (JSON)</h3>
          <textarea className="settings-json" rows={10} aria-label="custom settings JSON"
            value={draft} spellCheck={false}
            onChange={(e) => {
              setDraft(e.target.value);
              try {
                const p = JSON.parse(e.target.value) as unknown;
                setDraftError(p && typeof p === "object" && !Array.isArray(p) ? null : "Top level must be a JSON object");
              } catch (err) {
                setDraftError(`Invalid JSON: ${err instanceof Error ? err.message : String(err)}`);
              }
            }} />
          {draftError && <div className="banner err small">{draftError}</div>}
          <div className="row-actions">
            <button className="primary" disabled={saving || draftError != null} onClick={() => void save()} data-testid="save-settings">
              {saving ? "Saving…" : "Save custom settings"}
            </button>
            <button onClick={() => setDraft(JSON.stringify(settings.custom ?? {}, null, 2))}>Reset draft</button>
          </div>
        </>)}
      </section>
    </div>
  );
}

function FragmentRow({ k, v }: { k: string; v: unknown }) {
  return <><dt>{k}</dt><dd>{String(v)}</dd></>;
}
