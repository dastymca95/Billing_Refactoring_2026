// Compact sidebar utility bar.
//
// Phase 1W moves Process into each batch row. This bar now keeps only
// cross-cutting controls: Stop for the currently running batch and a More
// menu for preview/export/delete utilities.

import { useEffect, useRef, useState } from "react";

type Props = {
  hasFiles: boolean;
  isProcessing: boolean;
  hasPreview: boolean;
  isExporting: boolean;
  hasExport: boolean;
  editedCellCount: number;
  batchName?: string;
  isCancelling?: boolean;
  onProcess: () => void;
  onCancel?: () => void;
  onPreview: () => void;
  onExport: () => void;
  onDownload: () => void;
  onResetEdits: () => void;
  onClear?: () => void;
};

export function BatchActionsBar(props: Props) {
  const {
    isProcessing,
    hasPreview,
    hasExport,
    editedCellCount,
    isCancelling,
    onCancel,
    onPreview,
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

  const confirmAndClear = () => {
    if (!onClear) return;
    setMoreOpen(false);
    onClear();
  };

  const confirmAndCancel = () => {
    if (!onCancel) return;
    onCancel();
  };

  return (
    <div className="actions-bar actions-bar-utility" role="toolbar" aria-label="Batch utilities">
      {isProcessing && onCancel && (
        <button
          type="button"
          className="btn btn-compact btn-danger"
          disabled={isCancelling}
          onClick={confirmAndCancel}
          title={
            isCancelling
              ? "Cancellation already requested"
              : "Stop this batch at the next safe checkpoint"
          }
        >
          <StopIcon /> {isCancelling ? "Stopping" : "Stop"}
        </button>
      )}

      <div className="actions-more" ref={moreRef}>
        <button
          type="button"
          className="btn btn-compact btn-ghost"
          onClick={() => setMoreOpen((v) => !v)}
          aria-expanded={moreOpen}
          aria-haspopup="menu"
          title="More actions"
          data-testid="batch-utility-more"
        >
          <DotsIcon />
        </button>
        {moreOpen && (
          <div className="actions-more-menu" role="menu">
            <div className="actions-more-section-title">Preview</div>
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
              <RefreshIcon /> Refresh preview
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
              <UndoIcon />
              {hasEdits
                ? `Reset ${editedCellCount} edit${editedCellCount === 1 ? "" : "s"}`
                : "Reset edits"}
            </button>

            <div className="actions-more-section-title">Export</div>
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
              <DownloadIcon /> Re-download last export
            </button>

            {onClear && (
              <>
                <div className="actions-more-sep" role="separator" />
                <button
                  type="button"
                  role="menuitem"
                  className="actions-more-item danger"
                  onClick={confirmAndClear}
                  data-testid="delete-batch-menu-item"
                >
                  <TrashIcon /> Delete active batch
                </button>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function StopIcon() {
  return (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <rect x="6" y="6" width="12" height="12" rx="1.5" />
    </svg>
  );
}

function DotsIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <circle cx="5" cy="12" r="1.6" />
      <circle cx="12" cy="12" r="1.6" />
      <circle cx="19" cy="12" r="1.6" />
    </svg>
  );
}

function RefreshIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="23 4 23 10 17 10" />
      <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
    </svg>
  );
}

function UndoIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="3 7 8 12 3 17" />
      <path d="M21 17v-2a4 4 0 0 0-4-4H8" />
    </svg>
  );
}

function DownloadIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <polyline points="7 10 12 15 17 10" />
      <line x1="12" y1="15" x2="12" y2="3" />
    </svg>
  );
}

function TrashIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="3 6 5 6 21 6" />
      <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
      <path d="M10 11v6" />
      <path d="M14 11v6" />
    </svg>
  );
}
