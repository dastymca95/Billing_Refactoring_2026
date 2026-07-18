// Phase 2K — Cell Explain / Correct / Learn UI.
//
// Three pieces in one file because they share state intimately:
//
//   * <CellContextMenu> — the right-click menu that pops up over a
//     template cell. Renders four actions and routes them through
//     callbacks the parent provides.
//   * <CellExplainModal> — the explanation popup. Fetches
//     `/cells/.../explain` and renders the synthesised text.
//   * <RemapScopeChooser> — the small modal asking "what field is
//     this region?" and "what scope?" once the user has drawn a box.

import { useEffect, useRef, useState } from "react";
import type { CellExplain, TraceItem } from "../types";

// ---------------------------------------------------------------------------
// Context menu
// ---------------------------------------------------------------------------

type ContextMenuProps = {
  x: number;
  y: number;
  onExplain: () => void;
  onShowTrace: () => void;
  onEditValue: () => void;
  onRemapSource: () => void;
  onDeleteRows?: () => void;
  onDeleteColumns?: () => void;
  deleteRowsLabel?: string;
  deleteColumnsLabel?: string;
  onClose: () => void;
};

export function CellContextMenu({
  x,
  y,
  onExplain,
  onShowTrace,
  onEditValue,
  onRemapSource,
  onDeleteRows,
  onDeleteColumns,
  deleteRowsLabel,
  deleteColumnsLabel,
  onClose,
}: ContextMenuProps) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (ref.current && e.target instanceof Node && !ref.current.contains(e.target)) {
        onClose();
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose]);
  // Clamp to viewport so the menu never opens off-screen.
  const W = 240;
  const actionCount = 4 + (onDeleteRows ? 1 : 0) + (onDeleteColumns ? 1 : 0);
  const H = actionCount * 38 + 12;
  const cx = Math.min(x, window.innerWidth - W - 8);
  const cy = Math.min(y, window.innerHeight - H - 8);
  return (
    <div
      ref={ref}
      className="cell-context-menu"
      role="menu"
      style={{ left: cx, top: cy }}
      data-testid="cell-context-menu"
    >
      <button type="button" role="menuitem" onClick={() => { onExplain(); onClose(); }}>
        <ExplainIcon />
        <span>Why is this value here?</span>
      </button>
      <button type="button" role="menuitem" onClick={() => { onShowTrace(); onClose(); }}>
        <TraceIcon />
        <span>Show source trace</span>
      </button>
      <button type="button" role="menuitem" onClick={() => { onEditValue(); onClose(); }}>
        <EditIcon />
        <span>Edit value</span>
      </button>
      <button type="button" role="menuitem" onClick={() => { onRemapSource(); onClose(); }}>
        <RemapIcon />
        <span>Remap source region</span>
      </button>
      {(onDeleteRows || onDeleteColumns) && <div className="cell-context-menu-separator" />}
      {onDeleteRows && (
        <button
          type="button"
          role="menuitem"
          className="danger"
          onClick={() => { onDeleteRows(); onClose(); }}
        >
          <DeleteIcon />
          <span>{deleteRowsLabel || "Delete row"}</span>
        </button>
      )}
      {onDeleteColumns && (
        <button
          type="button"
          role="menuitem"
          className="danger"
          onClick={() => { onDeleteColumns(); onClose(); }}
        >
          <DeleteIcon />
          <span>{deleteColumnsLabel || "Delete column"}</span>
        </button>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Explain modal
// ---------------------------------------------------------------------------

type ExplainModalProps = {
  explain: CellExplain;
  onClose: () => void;
  onTeach?: (newValue: unknown, scope: "cell" | "vendor") => Promise<void> | void;
  onRemap?: () => void;
};

export function CellExplainModal({
  explain,
  onClose,
  onTeach,
  onRemap,
}: ExplainModalProps) {
  const [draft, setDraft] = useState<string>(
    explain.current_value == null ? "" : String(explain.current_value),
  );
  const [scope, setScope] = useState<"cell" | "vendor">("cell");
  const [busy, setBusy] = useState(false);
  const onTeachRef = useRef(onTeach);
  useEffect(() => {
    onTeachRef.current = onTeach;
  }, [onTeach]);

  const handleSave = async () => {
    if (busy) return;
    setBusy(true);
    try {
      await onTeachRef.current?.(draft, scope);
      onClose();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="cell-explain-backdrop" role="dialog" aria-modal="true">
      <div
        className="cell-explain-modal"
        data-testid="cell-explain-modal"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="cell-explain-header">
          <div>
            <div className="cell-explain-eyebrow">CELL · {explain.column}</div>
            <h2>Why is this value here?</h2>
          </div>
          <button type="button" className="cell-explain-close" onClick={onClose}>
            ×
          </button>
        </header>
        <section className="cell-explain-body">
          <div className="cell-explain-current">
            <span className="cell-explain-current-label">Current value</span>
            <code>
              {explain.current_value == null || explain.current_value === ""
                ? "(blank)"
                : String(explain.current_value)}
            </code>
            <KindBadge kind={explain.cell_kind} />
          </div>
          <p className="cell-explain-summary">{explain.summary}</p>
          {explain.missing_components?.length > 0 && (
            <div className="cell-explain-missing">
              <strong>Missing components</strong>
              <ul>
                {explain.missing_components.map((m) => (
                  <li key={m}>{m}</li>
                ))}
              </ul>
            </div>
          )}
          {explain.traces?.length > 0 && (
            <div className="cell-explain-sources">
              <strong>Source regions</strong>
              <ul>
                {explain.traces.map((t) => (
                  <SourceRow key={t.trace_id} item={t} />
                ))}
              </ul>
            </div>
          )}
          <div className="cell-explain-correct">
            <label>
              <span>New value</span>
              <input
                type="text"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                placeholder={
                  explain.current_value == null
                    ? "Enter a value…"
                    : String(explain.current_value)
                }
                data-testid="cell-explain-value-input"
              />
            </label>
            <fieldset className="cell-explain-scope">
              <legend>Apply to</legend>
              <label>
                <input
                  type="radio"
                  name="scope"
                  checked={scope === "cell"}
                  onChange={() => setScope("cell")}
                />
                <span>Just this cell (one-off)</span>
              </label>
              <label>
                <input
                  type="radio"
                  name="scope"
                  checked={scope === "vendor"}
                  onChange={() => setScope("vendor")}
                />
                <span>
                  Future bills from <strong>{explain.vendor_key || "this vendor"}</strong>{" "}
                  (learn)
                </span>
              </label>
            </fieldset>
          </div>
        </section>
        <footer className="cell-explain-footer">
          {onRemap && (
            <button
              type="button"
              className="cell-explain-remap"
              onClick={() => { onRemap(); onClose(); }}
            >
              Remap source region…
            </button>
          )}
          <span style={{ flex: 1 }} />
          <button type="button" className="cell-explain-cancel" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="cell-explain-save"
            onClick={handleSave}
            disabled={busy}
            data-testid="cell-explain-save"
          >
            {busy
              ? "Saving…"
              : scope === "vendor"
              ? "Save & teach"
              : "Save change"}
          </button>
        </footer>
      </div>
    </div>
  );
}

function SourceRow({ item }: { item: TraceItem }) {
  const conf = Math.round((item.confidence || 0) * 100);
  return (
    <li>
      <span className="cell-explain-source-label">{item.field_label}</span>
      {item.detected_text && (
        <span className="cell-explain-source-text">
          “{truncate(item.detected_text, 80)}”
        </span>
      )}
      <span className="cell-explain-source-meta">
        {item.rule_id} · {conf}%
      </span>
    </li>
  );
}

function KindBadge({ kind }: { kind: string }) {
  const map: Record<string, { label: string; cls: string }> = {
    extracted: { label: "Extracted", cls: "kind-extracted" },
    derived: { label: "Derived", cls: "kind-derived" },
    fallback: { label: "Fallback used", cls: "kind-fallback" },
    user_edited: { label: "Learned", cls: "kind-edited" },
  };
  const m = map[kind] || { label: kind || "—", cls: "" };
  return <span className={`cell-explain-kind ${m.cls}`}>{m.label}</span>;
}

function truncate(s: string, n: number) {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

// ---------------------------------------------------------------------------
// Remap scope chooser modal
// ---------------------------------------------------------------------------

const FIELD_OPTIONS = [
  { value: "service_address", label: "Service address" },
  { value: "invoice_number", label: "Invoice / Account number" },
  { value: "due_date", label: "Due date" },
  { value: "service_period", label: "Service period" },
  { value: "total_amount", label: "Total amount" },
  { value: "line_item_description", label: "Line-item description" },
  { value: "custom", label: "Custom field…" },
] as const;

type RemapChooserProps = {
  vendorKey: string;
  onConfirm: (params: {
    field_key: string;
    scope: "cell" | "vendor";
    note: string;
  }) => Promise<void> | void;
  onCancel: () => void;
};

export function RemapScopeChooser({ vendorKey, onConfirm, onCancel }: RemapChooserProps) {
  const [field, setField] = useState<string>("service_address");
  const [customField, setCustomField] = useState<string>("");
  const [scope, setScope] = useState<"cell" | "vendor">("vendor");
  const [note, setNote] = useState<string>("");
  const [busy, setBusy] = useState(false);

  const handleConfirm = async () => {
    if (busy) return;
    const fk = field === "custom" ? customField.trim() : field;
    if (!fk) return;
    setBusy(true);
    try {
      await onConfirm({ field_key: fk, scope, note });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="cell-explain-backdrop" role="dialog" aria-modal="true">
      <div
        className="cell-explain-modal cell-remap-modal"
        data-testid="cell-remap-modal"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="cell-explain-header">
          <div>
            <div className="cell-explain-eyebrow">REGION REMAP</div>
            <h2>What field is this region?</h2>
          </div>
          <button type="button" className="cell-explain-close" onClick={onCancel}>
            ×
          </button>
        </header>
        <section className="cell-explain-body">
          <label className="cell-remap-row">
            <span>Field</span>
            <select value={field} onChange={(e) => setField(e.target.value)}>
              {FIELD_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </label>
          {field === "custom" && (
            <label className="cell-remap-row">
              <span>Custom field key</span>
              <input
                type="text"
                value={customField}
                onChange={(e) => setCustomField(e.target.value)}
                placeholder="e.g. payment_stub_address"
              />
            </label>
          )}
          <fieldset className="cell-explain-scope">
            <legend>Apply to</legend>
            <label>
              <input
                type="radio"
                checked={scope === "cell"}
                onChange={() => setScope("cell")}
              />
              <span>Only this cell</span>
            </label>
            <label>
              <input
                type="radio"
                checked={scope === "vendor"}
                onChange={() => setScope("vendor")}
              />
              <span>
                Future bills from <strong>{vendorKey || "this vendor"}</strong>
              </span>
            </label>
          </fieldset>
          <label className="cell-remap-row">
            <span>Note (optional)</span>
            <textarea
              rows={2}
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="Why this region is the right source"
            />
          </label>
        </section>
        <footer className="cell-explain-footer">
          <span style={{ flex: 1 }} />
          <button type="button" className="cell-explain-cancel" onClick={onCancel}>
            Cancel
          </button>
          <button
            type="button"
            className="cell-explain-save"
            onClick={handleConfirm}
            disabled={busy || (field === "custom" && !customField.trim())}
            data-testid="cell-remap-confirm"
          >
            {busy ? "Saving…" : "Save & teach"}
          </button>
        </footer>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Icons
// ---------------------------------------------------------------------------

function ExplainIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="9" />
      <path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.7.4-1 1-1 1.7" />
      <line x1="12" y1="17" x2="12.01" y2="17" />
    </svg>
  );
}
function TraceIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3" y="3" width="14" height="3" rx="1" />
      <rect x="3" y="10" width="10" height="3" rx="1" />
      <circle cx="20" cy="14" r="3" />
      <line x1="22" y1="16" x2="24" y2="18" />
    </svg>
  );
}
function EditIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.121 2.121 0 1 1 3 3L7 19l-4 1 1-4 12.5-12.5z" />
    </svg>
  );
}
function RemapIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <path d="M9 9l6 6" />
      <path d="M15 9l-6 6" />
    </svg>
  );
}
function DeleteIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3 6h18" />
      <path d="M8 6V4h8v2" />
      <path d="M19 6l-1 14H6L5 6" />
      <path d="M10 11v5" />
      <path d="M14 11v5" />
    </svg>
  );
}
