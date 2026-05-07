// Phase 1K — compact toast component.
//
// Replaces the giant green / red banners that sat above the template
// pane in earlier phases. Toasts dock to the bottom-right of the
// viewport, auto-dismiss after a configurable timeout (default 4 s),
// and stack so a quick succession of events doesn't drop messages.
//
// Keyed by id so the same toast id replaces (rather than duplicates)
// when the same event fires twice.

import { useEffect } from "react";

export type ToastTone = "info" | "success" | "warning" | "error";

export type Toast = {
  id: string;
  message: string;
  tone?: ToastTone;
  /** Milliseconds before auto-dismiss. 0 = sticky until manually closed. */
  ttl?: number;
};

type Props = {
  toasts: Toast[];
  onDismiss: (id: string) => void;
};

export function Toasts({ toasts, onDismiss }: Props) {
  return (
    <div className="toast-stack" aria-live="polite" aria-atomic="false">
      {toasts.map((t) => (
        <ToastItem key={t.id} toast={t} onDismiss={onDismiss} />
      ))}
    </div>
  );
}

function ToastItem({
  toast,
  onDismiss,
}: {
  toast: Toast;
  onDismiss: (id: string) => void;
}) {
  const ttl = toast.ttl ?? 4000;
  useEffect(() => {
    if (ttl <= 0) return;
    const handle = window.setTimeout(() => onDismiss(toast.id), ttl);
    return () => window.clearTimeout(handle);
  }, [toast.id, ttl, onDismiss]);

  return (
    <div className={`toast tone-${toast.tone ?? "info"}`} role="status">
      <span className="toast-msg">{toast.message}</span>
      <button
        type="button"
        className="toast-close"
        onClick={() => onDismiss(toast.id)}
        aria-label="Dismiss"
        title="Dismiss"
      >
        ✕
      </button>
    </div>
  );
}
