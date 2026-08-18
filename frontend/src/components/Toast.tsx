import { useApp } from "../state/app";

/** Global toast stack (bottom-right). Errors surface here from every page. */
export default function Toasts() {
  const { toasts, dismissToast } = useApp();
  if (toasts.length === 0) return null;
  return (
    <div className="toasts" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={`toast ${t.kind}`} role="status">
          <span>{t.text}</span>
          <button className="toast-x" aria-label="dismiss" onClick={() => dismissToast(t.id)}>×</button>
        </div>
      ))}
    </div>
  );
}
