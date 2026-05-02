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
};

export function BatchActionsPanel({
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
}: Props) {
  const hasEdits = editedCellCount > 0;
  return (
    <div className="card">
      <div className="card-header">Actions</div>
      <div className="card-body actions">
        <button
          className="primary"
          disabled={!hasFiles || isProcessing}
          onClick={onProcess}
          title={
            hasEdits
              ? "Re-processing will discard your unsaved preview edits."
              : ""
          }
        >
          {isProcessing ? "Processing…" : "Process Batch"}
        </button>
        <button disabled={!hasPreview} onClick={onPreview}>
          Refresh Preview
        </button>
        <button
          disabled={!hasEdits}
          onClick={onResetEdits}
          title={hasEdits ? `Reset ${editedCellCount} edited cell(s)` : ""}
        >
          Reset Edits
        </button>
        <button
          className="primary"
          disabled={!hasPreview || isExporting}
          onClick={onExport}
          title="Build the ResMan workbook from the current preview and download it."
        >
          {isExporting
            ? "Exporting…"
            : hasEdits
              ? `Export & Download (${editedCellCount} edits)`
              : "Export & Download"}
        </button>
        <button
          disabled={!hasExport}
          onClick={onDownload}
          title="Download the most recent export again (no re-build)."
        >
          Re-download last export
        </button>
      </div>
    </div>
  );
}
