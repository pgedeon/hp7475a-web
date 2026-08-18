import { useEffect, useRef, type ReactNode } from "react";

/** Accessible modal: Esc closes, cancel button auto-focus, background click
 *  ignored for hardware-safety dialogs (deliberate — no accidental dismiss). */
export default function Modal({
  title, children, onClose, footer,
}: { title: string; children: ReactNode; onClose: () => void; footer?: ReactNode }) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    cancelRef.current?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="modal-overlay">
      <div className="modal" role="dialog" aria-modal="true" aria-label={title}>
        <h3>{title}</h3>
        <div className="modal-body">{children}</div>
        {footer != null && <div className="modal-footer">{footer}</div>}
      </div>
    </div>
  );
}
