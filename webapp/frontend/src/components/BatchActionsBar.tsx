// Phase 1J — compact, professional action bar.
//
// Replaces the older full-width stacked-button BatchActionsPanel.
// Primary action: Process. Secondary: Export. Other actions live behind
// a "More ⋯" dropdown so the sidebar stays uncluttered. The bar
// disables itself appropriately based on batch state.

import { useEffect, useRef, useState } from "react";

type Props = {
  hasFiles: boolean;
  isProcessing: boolean;
  hasPreview: boolean;
  isExporting: boolean;
  hasExport: boolean;
  editedCellCount: number;
  onProcess: () => void;
  onPreview: () => void;
  onExport: () => void;
  onDownload: () => void;
  onResetEdits: () => void;
  onClear?: () => void;
};

export function BatchActionsBar(props: Props) {
  const {
    hasFiles,
    isProcessing,
    hasPreview,
    isExporting,
    hasExport,
    editedCellCount,
    onProcess,
    onPreview,
    onExport,
    onDownload,
    onResetEdits,
    onClear,
  } = props;

  const [moreOpen, setMoreOpen] = useState(false);
  const moreRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!moreOpen) return;
    const close = (e: MouseEvent) => {
      if (!moreRef.current?.contains(e.target as Node)) setMoreOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [moreOpen]);

  const hasEdits = editedCellCount > 0;
  // Export is visually prominent only once there's something to export.
  const exportEmphasis = hasPreview;

  return (
    <div className="actions-bar" role="toolbar" aria-label="Batch actions">
      <button
        type="button"
        className="btn btn-primary btn-compact"
        disabled={!hasFiles || isProcessing}
        onClick={onProcess}
        title={
          hasEdits
            ? "Re-processing will discard your unsaved preview edits."
            : "Run vendor processors over every uploaded file."
        }
      >
        {isProcessing ? (
          <>
            <span className="spinner" aria-hidden /> Processing…
          </>
        ) : (
          <>▶ Process</>
        )}
      </button>

      <button
        type="button"
        className={`btn btn-compact ${exportEmphasis ? "btn-accent" : ""}`}
        disabled={!hasPreview || isExporting}
        onClick={onExport}
        title="Build the ResMan workbook from the current preview and download."
      >
        {isExporting
          ? "Exporting…"
          : hasEdits
            ? `↓ Export (${editedCellCount} edits)`
            : "↓ Export"}
      </button>

      <div className="actions-more" ref={moreRef}>
        <button
          type="button"
          className="btn btn-compact btn-ghost"
          onClick={() => setMoreOpen((v) => !v)}
          aria-expanded={moreOpen}
          aria-haspopup="menu"
          title="More actions"
        >
          More ⋯
        </button>
        {moreOpen && (
          <div className="actions-more-menu" role="menu">
            <button
              type="button"
              role="menuitem"
              className="actions-more-item"
              disabled={!hasPreview}
              onClick={() => {
                setMoreOpen(false);
                onPreview();
              }}
            >
              Refresh preview
            </button>
            <button
              type="button"
              role="menuitem"
              className="actions-more-item"
              disabled={!hasEdits}
              onClick={() => {
                setMoreOpen(false);
                onResetEdits();
              }}
            >
              Reset {hasEdits ? `${editedCellCount} edit${editedCellCount === 1 ? "" : "s"}` : "edits"}
            </button>
            <button
              type="button"
              role="menuitem"
              className="actions-more-item"
              disabled={!hasExport}
              onClick={() => {
                setMoreOpen(false);
                onDownload();
              }}
            >
              Re-download last export
            </button>
            {onClear && (
              <button
                type="button"
                role="menuitem"
                className="actions-more-item danger"
                onClick={() => {
                  setMoreOpen(false);
                  onClear();
                }}
              >
                Delete batch
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
