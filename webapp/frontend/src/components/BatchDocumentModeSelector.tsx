// Phase 1H — premium batch document-mode picker.
// Shown in the new-batch dialog (and the batch settings drawer once that
// lands). The selected mode flows to `batch_metadata.json` and from
// there into `run_context.document_mode` for vendor processors.

import {
  DOCUMENT_MODE_DESCRIPTIONS,
  DOCUMENT_MODE_LABELS,
  DOCUMENT_MODES,
  type DocumentMode,
} from "../types";

type Props = {
  value: DocumentMode;
  onChange: (next: DocumentMode) => void;
  disabled?: boolean;
  compact?: boolean;
};

export function BatchDocumentModeSelector({
  value,
  onChange,
  disabled,
  compact,
}: Props) {
  return (
    <div className={`mode-selector ${compact ? "compact" : ""}`}>
      <div className="mode-selector-question">
        What type of documents are you uploading?
      </div>
      <div className="mode-selector-grid">
        {DOCUMENT_MODES.map((m) => {
          const active = value === m;
          return (
            <button
              key={m}
              type="button"
              className={`mode-card ${active ? "active" : ""}`}
              onClick={() => onChange(m)}
              disabled={disabled}
              aria-pressed={active}
            >
              <div className="mode-card-icon">{iconFor(m)}</div>
              <div className="mode-card-body">
                <div className="mode-card-title">{DOCUMENT_MODE_LABELS[m]}</div>
                <div className="mode-card-desc">
                  {DOCUMENT_MODE_DESCRIPTIONS[m]}
                </div>
              </div>
              {active && <div className="mode-card-check">✓</div>}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function iconFor(mode: DocumentMode): string {
  switch (mode) {
    case "auto_detect":
      return "✨";
    case "digital_pdf":
      return "📄";
    case "scanned_pdf":
      return "🖨";
    case "mixed_pdf":
      return "🗂";
    case "csv_excel":
      return "📊";
  }
}
