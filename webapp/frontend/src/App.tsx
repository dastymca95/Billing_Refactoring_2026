import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "./api";
import { AiFallbackStatusBadge } from "./components/AiFallbackStatusBadge";
import { BatchActionsPanel } from "./components/BatchActionsPanel";
import { BatchDocumentModeSelector } from "./components/BatchDocumentModeSelector";
import { DocumentPreviewPanel } from "./components/DocumentPreviewPanel";
import { DropZone } from "./components/DropZone";
import { FileList } from "./components/FileList";
import { ManualReviewPanel } from "./components/ManualReviewPanel";
import { ProcessingTimeline } from "./components/ProcessingTimeline";
import { ProgressBar } from "./components/ProgressBar";
import {
  ResManTemplatePreview,
  type CellEdits,
} from "./components/ResManTemplatePreview";
import type {
  BatchListEntry,
  BatchProgress,
  DocumentMode,
  FileEntry,
  ManualReviewItem,
  PreviewResponse,
} from "./types";

// localStorage key used to remember the active batch across page refreshes.
const ACTIVE_BATCH_LS_KEY = "billing_refactoring_active_batch_id";

// How often the frontend polls /progress while processing.
const PROGRESS_POLL_MS = 750;

// Maximum total time we'll wait for a background processing run before
// showing a "still working" message. The poll never auto-aborts.
const MAX_PROCESSING_WAIT_MS = 15 * 60 * 1000;

export default function App() {
  const [batchId, setBatchId] = useState<string | null>(null);
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [selected, setSelected] = useState<string | null>(null);

  const [isProcessing, setIsProcessing] = useState(false);
  const [isExporting, setIsExporting] = useState(false);
  const [hasExport, setHasExport] = useState(false);

  const [preview, setPreview] = useState<PreviewResponse | null>(null);
  const [review, setReview] = useState<ManualReviewItem[]>([]);
  const [edits, setEdits] = useState<CellEdits>({});
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  // Collapsible-panel state.
  const [docPreviewCollapsed, setDocPreviewCollapsed] = useState(false);
  const [manualReviewCollapsed, setManualReviewCollapsed] = useState(false);

  // Phase 1F: per-batch progress + polling.
  const [progress, setProgress] = useState<BatchProgress | null>(null);
  const pollingTimerRef = useRef<number | null>(null);

  // Phase 1G: batch management
  const [batchName, setBatchName] = useState<string>("");
  const [batchList, setBatchList] = useState<BatchListEntry[]>([]);
  const [showBatchPicker, setShowBatchPicker] = useState<boolean>(false);

  // Phase 1H: batch creation dialog state.
  const [showCreateBatchDialog, setShowCreateBatchDialog] = useState(false);
  const [createBatchName, setCreateBatchName] = useState("");
  const [createBatchMode, setCreateBatchMode] =
    useState<DocumentMode>("auto_detect");

  const refreshBatchList = useCallback(async () => {
    try {
      const r = await api.listBatches();
      setBatchList(r.batches);
    } catch {
      /* non-fatal */
    }
  }, []);

  const editedCellCount = useMemo(
    () => Object.values(edits).reduce((s, m) => s + Object.keys(m).length, 0),
    [edits],
  );

  const ensureBatch = useCallback(async () => {
    if (batchId) return batchId;
    const res = await api.createBatch(batchName.trim() || undefined);
    setBatchId(res.batch_id);
    setBatchName(res.batch_name);
    try {
      localStorage.setItem(ACTIVE_BATCH_LS_KEY, res.batch_id);
    } catch {
      /* localStorage may be disabled; non-fatal */
    }
    void refreshBatchList();
    return res.batch_id;
  }, [batchId, batchName, refreshBatchList]);

  // Switch to an existing batch (loads files / preview / manual review).
  const handleSwitchBatch = useCallback(
    async (newId: string) => {
      if (newId === batchId) {
        setShowBatchPicker(false);
        return;
      }
      try {
        const status = await api.getBatch(newId);
        setBatchId(status.batch_id);
        setBatchName(status.batch_name || status.batch_id);
        setFiles(status.files);
        setSelected(status.files[0]?.filename ?? null);
        setHasExport(status.export_available);
        setEdits({});
        setError(null);
        if (status.preview_available) {
          const prev = await api.preview(status.batch_id);
          const rev = await api.manualReview(status.batch_id);
          setPreview(prev);
          setReview(rev.items);
          setInfo(`Switched to "${status.batch_name || status.batch_id}".`);
        } else {
          setPreview(null);
          setReview([]);
          setInfo(
            `Switched to "${status.batch_name || status.batch_id}". Click Process Batch to populate the preview.`,
          );
        }
        try {
          localStorage.setItem(ACTIVE_BATCH_LS_KEY, status.batch_id);
        } catch {
          /* non-fatal */
        }
        setShowBatchPicker(false);
      } catch (e) {
        setError(`Could not switch batch: ${e}`);
      }
    },
    [batchId],
  );

  // Rename the active batch.
  const handleRenameBatch = useCallback(async () => {
    if (!batchId) return;
    const next = window.prompt("Rename this batch:", batchName);
    if (next == null) return;
    const trimmed = next.trim();
    if (!trimmed) return;
    try {
      await api.renameBatch(batchId, trimmed);
      setBatchName(trimmed);
      void refreshBatchList();
      setInfo(`Renamed batch to "${trimmed}".`);
    } catch (e) {
      setError(`Rename failed: ${e}`);
    }
  }, [batchId, batchName, refreshBatchList]);

  // Phase 1H — open the new-batch dialog. The dialog gathers name +
  // document_mode before creating; the legacy `window.prompt` flow is
  // gone but the keyboard shortcut Enter still creates with defaults.
  const handleCreateNewBatch = useCallback(() => {
    setCreateBatchName("");
    setCreateBatchMode("auto_detect");
    setShowCreateBatchDialog(true);
    setShowBatchPicker(false);
  }, []);

  const handleSubmitCreateBatch = useCallback(async () => {
    const name = createBatchName.trim();
    try {
      const r = await api.createBatch(name || undefined, {
        documentMode: createBatchMode,
      });
      // Drop any prior batch state and switch to the new one.
      setBatchId(r.batch_id);
      setBatchName(r.batch_name);
      setFiles([]);
      setSelected(null);
      setPreview(null);
      setReview([]);
      setEdits({});
      setHasExport(false);
      setError(null);
      setInfo(
        `Created batch "${r.batch_name}" · mode=${createBatchMode}.`,
      );
      try {
        localStorage.setItem(ACTIVE_BATCH_LS_KEY, r.batch_id);
      } catch {
        /* non-fatal */
      }
      setShowCreateBatchDialog(false);
      void refreshBatchList();
    } catch (e) {
      setError(`Could not create batch: ${e}`);
    }
  }, [createBatchMode, createBatchName, refreshBatchList]);

  const refreshFiles = useCallback(
    async (bid: string) => {
      const res = await api.listFiles(bid);
      setFiles(res.files);
      if (res.files.length > 0 && !selected) {
        setSelected(res.files[0].filename);
      }
    },
    [selected],
  );

  const handleFiles = useCallback(
    async (newFiles: File[]) => {
      try {
        setError(null);
        const bid = await ensureBatch();
        for (const f of newFiles) {
          await api.uploadFile(bid, f);
        }
        await refreshFiles(bid);
        setPreview(null);
        setReview([]);
        setEdits({});
        setHasExport(false);
      } catch (e) {
        setError(String(e));
      }
    },
    [ensureBatch, refreshFiles],
  );

  // ---- Phase 1F: progress polling ----
  // While `isProcessing` is true, poll `/api/batches/<id>/progress` every
  // PROGRESS_POLL_MS milliseconds and surface the snapshot to the
  // ProgressBar component. The poll auto-stops when the snapshot reports
  // status="completed" or "failed".
  const stopPolling = useCallback(() => {
    if (pollingTimerRef.current !== null) {
      window.clearInterval(pollingTimerRef.current);
      pollingTimerRef.current = null;
    }
  }, []);

  const startPolling = useCallback((bid: string) => {
    stopPolling();
    const tick = async () => {
      try {
        const snap = await api.getBatchProgress(bid);
        setProgress(snap);
        if (snap.status === "completed" || snap.status === "failed") {
          stopPolling();
        }
      } catch {
        // network blip — keep polling, the next tick will retry.
      }
    };
    void tick();
    pollingTimerRef.current = window.setInterval(tick, PROGRESS_POLL_MS);
  }, [stopPolling]);

  // Stop polling on unmount.
  useEffect(() => stopPolling, [stopPolling]);

  // Phase 1G: process now runs as a background task. The frontend kicks
  // it off (POST returns 202 quickly), starts polling /progress, and
  // waits for status=completed|failed before pulling preview/manual-review.
  const waitForProcessingDone = useCallback(
    async (bid: string): Promise<BatchProgress | null> => {
      const start = Date.now();
      while (Date.now() - start < MAX_PROCESSING_WAIT_MS) {
        try {
          const snap = await api.getBatchProgress(bid);
          setProgress(snap);
          if (snap.status === "completed" || snap.status === "failed") {
            return snap;
          }
        } catch {
          /* network blip — keep polling */
        }
        await new Promise((res) => setTimeout(res, PROGRESS_POLL_MS));
      }
      return null;
    },
    [],
  );

  const handleProcess = useCallback(async () => {
    if (!batchId) return;
    if (editedCellCount > 0) {
      const ok = window.confirm(
        `Re-processing will discard ${editedCellCount} unsaved preview edit(s). Continue?`,
      );
      if (!ok) return;
    }
    setIsProcessing(true);
    setError(null);
    setInfo(null);
    setProgress({
      batch_id: batchId,
      status: "processing",
      percent: 0,
      current_step: "Starting…",
    });
    // Also drive a polling timer so the bar feels live even between
    // explicit waitForProcessingDone iterations.
    startPolling(batchId);
    try {
      // Kick off background processing (returns 202 / accepted immediately).
      await api.process(batchId);
      // Poll progress until done.
      const final = await waitForProcessingDone(batchId);
      if (final && final.status === "failed") {
        setError(`Processing failed: ${final.error_message || "see backend logs"}`);
        return;
      }
      // Pull the cached preview + manual review.
      const prev = await api.preview(batchId);
      const rev = await api.manualReview(batchId);
      setPreview(prev);
      setReview(rev.items);
      setEdits({});
      setHasExport(false);
      const s = prev.summary || {};
      setInfo(
        `Processed ${s.files_supported ?? "?"}/${s.files_total ?? "?"} files · ` +
          `${s.invoices_total ?? prev.invoice_count} invoices · ${s.manual_review_total ?? rev.items.length} flagged`,
      );
      void refreshBatchList();
    } catch (e) {
      setError(String(e));
      setProgress((prev) =>
        prev
          ? { ...prev, status: "failed", error_message: String(e), percent: 100 }
          : null,
      );
    } finally {
      setIsProcessing(false);
      // Allow one more tick so the bar reaches 100% before disappearing.
      window.setTimeout(stopPolling, PROGRESS_POLL_MS + 50);
    }
  }, [batchId, editedCellCount, refreshBatchList, startPolling, stopPolling, waitForProcessingDone]);

  const handleRefreshPreview = useCallback(async () => {
    if (!batchId) return;
    if (editedCellCount > 0) {
      const ok = window.confirm(
        `Refreshing the preview will discard ${editedCellCount} unsaved preview edit(s). Continue?`,
      );
      if (!ok) return;
    }
    try {
      const prev = await api.preview(batchId);
      const rev = await api.manualReview(batchId);
      setPreview(prev);
      setReview(rev.items);
      setEdits({});
    } catch (e) {
      setError(String(e));
    }
  }, [batchId, editedCellCount]);

  const handleCellEdit = useCallback(
    (rowIndex: number, columnKey: string, newValue: unknown) => {
      setEdits((prev) => {
        const next = { ...prev };
        const rowEdits = { ...(next[rowIndex] ?? {}) };
        const original = (preview?.rows[rowIndex] as any)?.[columnKey];
        if (original === newValue || (original == null && newValue === "")) {
          delete rowEdits[columnKey];
        } else {
          rowEdits[columnKey] = newValue;
        }
        if (Object.keys(rowEdits).length === 0) {
          delete next[rowIndex];
        } else {
          next[rowIndex] = rowEdits;
        }
        return next;
      });
    },
    [preview],
  );

  const handleResetEdits = useCallback(() => {
    if (editedCellCount === 0) return;
    setEdits({});
  }, [editedCellCount]);

  // Trigger a real browser download for the latest export. Uses an
  // anchor click instead of `window.location.href` so the user stays on
  // the page (and the download counts as a user-initiated download).
  const triggerDownload = useCallback(
    (filename?: string) => {
      if (!batchId) return;
      const url = filename
        ? `${api.downloadUrl(batchId)}?filename=${encodeURIComponent(filename)}`
        : api.downloadUrl(batchId);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename ?? "";
      a.style.display = "none";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
    },
    [batchId],
  );

  const handleExport = useCallback(async () => {
    if (!batchId) return;
    setIsExporting(true);
    setError(null);
    try {
      let editedRows: Record<string, unknown>[] | undefined;
      const fullColumns = preview?.columns ?? [];
      if (editedCellCount > 0 && preview && fullColumns.length > 0) {
        editedRows = preview.rows.map((row, i) => {
          const overrides = edits[i] ?? {};
          const merged: Record<string, unknown> = {};
          for (const col of fullColumns) {
            merged[col] =
              col in overrides ? overrides[col] : (row as any)[col];
          }
          return merged;
        });
      }
      const res = await api.exportBatch(batchId, editedRows);
      const exported = res.exported ?? [];
      setHasExport(exported.length > 0);
      const editedLabel = res.export_used_edited_rows
        ? ` (with ${res.edited_rows_count ?? 0} edited rows, ${editedCellCount} cells)`
        : "";
      setInfo(
        `Exported ${exported.length} file(s)${editedLabel}. Download starting…`,
      );
      // Phase 1E: Export now downloads in one click — no separate
      // "Download Excel" step. The Download button still works (re-issue
      // the same download) but the primary flow is exported -> downloaded.
      if (exported.length > 0) {
        const filename = exported[exported.length - 1]?.filename;
        // Small delay so the success banner paints before the download
        // dialog steals focus on some browsers.
        setTimeout(() => triggerDownload(filename), 50);
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setIsExporting(false);
    }
  }, [batchId, edits, editedCellCount, preview, triggerDownload]);

  const handleDownload = useCallback(() => {
    if (!batchId) return;
    triggerDownload();
  }, [batchId, triggerDownload]);

  const handleClear = useCallback(async () => {
    if (batchId) {
      const ok = window.confirm(
        `Delete batch "${batchName || batchId}"? This removes all uploaded files, preview data, and exports for this batch on the server.`,
      );
      if (!ok) return;
      try {
        await api.deleteBatch(batchId);
      } catch {
        /* ignore */
      }
    }
    try {
      localStorage.removeItem(ACTIVE_BATCH_LS_KEY);
    } catch {
      /* non-fatal */
    }
    setBatchId(null);
    setBatchName("");
    setFiles([]);
    setSelected(null);
    setPreview(null);
    setReview([]);
    setEdits({});
    setHasExport(false);
    setError(null);
    setInfo(null);
    setProgress(null);
    void refreshBatchList();
  }, [batchId, batchName, refreshBatchList]);

  useEffect(() => {
    api.health().catch((e) => setError("Backend is not reachable: " + String(e)));
    void refreshBatchList();
  }, [refreshBatchList]);

  // ---- Phase 1E: rehydrate active batch from localStorage on first load ----
  // If the user refreshes the page mid-flow, restore:
  //   - the batch_id (and confirm it still exists)
  //   - the uploaded file list
  //   - the preview rows + manual-review items (if processing already ran)
  //   - whether an export already happened
  // If the cached batch_id no longer exists on disk, drop it from
  // localStorage and start clean.
  useEffect(() => {
    let cached: string | null = null;
    try {
      cached = localStorage.getItem(ACTIVE_BATCH_LS_KEY);
    } catch {
      cached = null;
    }
    if (!cached) return;
    let cancelled = false;
    (async () => {
      try {
        const status = await api.getBatch(cached!);
        if (cancelled) return;
        setBatchId(status.batch_id);
        setBatchName(status.batch_name || status.batch_id);
        setFiles(status.files);
        if (status.files.length > 0) {
          setSelected(status.files[0].filename);
        }
        setHasExport(status.export_available);
        if (status.preview_available) {
          try {
            const prev = await api.preview(status.batch_id);
            const rev = await api.manualReview(status.batch_id);
            if (cancelled) return;
            setPreview(prev);
            setReview(rev.items);
          } catch (e) {
            // Preview cache might be stale; surface but don't kill the rehydration.
            if (!cancelled) setError("Could not restore preview: " + String(e));
          }
        }
        const summaryParts: string[] = [
          `Restored "${status.batch_name || status.batch_id}"`,
        ];
        if (status.files_total) summaryParts.push(`${status.files_total} file(s)`);
        if (status.preview_available) summaryParts.push("preview available");
        if (status.export_available) summaryParts.push("export available");
        setInfo(summaryParts.join(" · ") + ".");
      } catch (e) {
        // 404 → stale localStorage entry; clear it so the next refresh
        // starts fresh.
        try {
          localStorage.removeItem(ACTIVE_BATCH_LS_KEY);
        } catch {
          /* non-fatal */
        }
        if (!cancelled) {
          // Don't surface 404s as errors — that's just "no prior batch".
          if (!String(e).includes("404")) {
            setError("Could not restore previous batch: " + String(e));
          }
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // ---- Global drag/drop guard --------------------------------------------
  // Without this, dropping a PDF anywhere outside the drop zone causes the
  // browser to navigate away from the app (Chrome opens the dropped PDF in
  // the current tab). We swallow drag/drop events at the window level when
  // the target is NOT inside an element marked data-dropzone="true".
  useEffect(() => {
    const isInsideDropzone = (e: DragEvent): boolean => {
      const t = e.target as HTMLElement | null;
      if (!t) return false;
      return !!t.closest('[data-dropzone="true"]');
    };

    const handler = (e: DragEvent) => {
      // Only swallow file drags. Internal text/HTML drags don't carry files.
      const hasFiles = Array.from(e.dataTransfer?.types ?? []).includes("Files");
      if (!hasFiles) return;
      if (isInsideDropzone(e)) return;
      e.preventDefault();
      // Tell Chrome we don't accept the drop here, so it doesn't try to
      // navigate. "none" prevents the cursor from showing the copy/move icon.
      if (e.dataTransfer) e.dataTransfer.dropEffect = "none";
    };

    window.addEventListener("dragenter", handler);
    window.addEventListener("dragover", handler);
    window.addEventListener("drop", handler);
    return () => {
      window.removeEventListener("dragenter", handler);
      window.removeEventListener("dragover", handler);
      window.removeEventListener("drop", handler);
    };
  }, []);

  return (
    <div className="app">
      <div className="topbar">
        <div className="title">Billing Refactoring 2026 · Web Console</div>
        <div className="topbar-actions">
          <AiFallbackStatusBadge />
          <div className="batch-controls">
            <button
              className="batch-picker-button"
              onClick={() => {
                void refreshBatchList();
                setShowBatchPicker((v) => !v);
              }}
              title={batchId ? `Active batch · ${batchName || batchId}` : "Pick or create a batch"}
            >
              <span className="batch-picker-label">
                {batchName || (batchId ?? "No batch")}
              </span>
              <span className="batch-picker-caret">▾</span>
            </button>
            {batchId && (
              <button
                onClick={handleRenameBatch}
                className="icon-button"
                title="Rename this batch"
              >
                Rename
              </button>
            )}
            <button onClick={handleCreateNewBatch} className="icon-button">
              + New batch
            </button>
            {showBatchPicker && (
              <div className="batch-picker-dropdown">
                <div className="batch-picker-header">Recent batches</div>
                {batchList.length === 0 && (
                  <div className="batch-picker-empty">No batches yet.</div>
                )}
                {batchList.slice(0, 12).map((b) => (
                  <button
                    key={b.batch_id}
                    className={`batch-picker-item ${b.batch_id === batchId ? "active" : ""}`}
                    onClick={() => void handleSwitchBatch(b.batch_id)}
                    title={b.batch_id}
                  >
                    <span className="batch-picker-item-name">{b.batch_name}</span>
                    <span className="batch-picker-item-meta">
                      {b.files_count} files · {b.invoices_count} inv
                      {b.export_available ? " · ✓" : ""}
                    </span>
                  </button>
                ))}
              </div>
            )}
          </div>
          <button
            onClick={handleClear}
            className="danger"
            disabled={!batchId && files.length === 0}
          >
            Delete Batch
          </button>
        </div>
      </div>

      <div className={`workspace ${docPreviewCollapsed ? "doc-collapsed" : ""}`}>
        {/* Left compact sidebar — upload + files + actions */}
        <aside className="sidebar">
          <section className="sidebar-section" data-dropzone="true">
            <h3 className="section-title">Upload</h3>
            <DropZone onFiles={handleFiles} disabled={isProcessing} compact />
          </section>

          <section className="sidebar-section sidebar-section-files">
            <h3 className="section-title">
              Files
              {files.length > 0 ? (
                <span className="muted"> · {files.length}</span>
              ) : null}
            </h3>
            <div className="card sidebar-files-card">
              <div className="card-body tight">
                <FileList
                  files={files}
                  selected={selected}
                  onSelect={setSelected}
                />
              </div>
            </div>
          </section>

          <section className="sidebar-section">
            <h3 className="section-title">Actions</h3>
            <BatchActionsPanel
              hasFiles={files.length > 0}
              isProcessing={isProcessing}
              hasPreview={preview !== null}
              isExporting={isExporting}
              hasExport={hasExport}
              editedCellCount={editedCellCount}
              onProcess={handleProcess}
              onPreview={handleRefreshPreview}
              onExport={handleExport}
              onDownload={handleDownload}
              onResetEdits={handleResetEdits}
            />
            <ProgressBar progress={progress} isProcessing={isProcessing} />
            <ProcessingTimeline progress={progress} />
          </section>
        </aside>

        {/* Middle column — document preview, collapsible */}
        <section className="document-column">
          {!docPreviewCollapsed ? (
            <DocumentPreviewPanel
              batchId={batchId}
              filename={selected}
              onToggleCollapsed={() => setDocPreviewCollapsed(true)}
            />
          ) : (
            <button
              className="collapsed-rail-button"
              onClick={() => setDocPreviewCollapsed(false)}
              title="Show document preview"
            >
              ◀ Doc preview
            </button>
          )}
        </section>

        {/* Right column — template (primary workspace) + manual review drawer */}
        <main className="template-column">
          {error && <div className="error-banner">{error}</div>}
          {info && !error && <div className="success-banner">{info}</div>}

          <div className="template-pane">
            <ResManTemplatePreview
              preview={preview}
              edits={edits}
              onCellEdit={handleCellEdit}
            />
          </div>

          <div
            className={`manual-review-drawer ${manualReviewCollapsed ? "collapsed" : ""}`}
          >
            <ManualReviewPanel
              items={review}
              collapsed={manualReviewCollapsed}
              onToggleCollapsed={() =>
                setManualReviewCollapsed(!manualReviewCollapsed)
              }
            />
          </div>
        </main>
      </div>

      {/* Phase 1H — premium new-batch dialog (modal) */}
      {showCreateBatchDialog && (
        <div
          className="modal-backdrop"
          onClick={() => setShowCreateBatchDialog(false)}
          role="presentation"
        >
          <div
            className="modal-card"
            onClick={(e) => e.stopPropagation()}
            role="dialog"
            aria-modal="true"
            aria-labelledby="new-batch-title"
          >
            <div className="modal-header">
              <span id="new-batch-title">New batch</span>
              <button
                className="icon-button"
                onClick={() => setShowCreateBatchDialog(false)}
                title="Close"
              >
                ✕
              </button>
            </div>
            <div className="modal-body">
              <label className="modal-field">
                <span className="modal-field-label">Batch name (optional)</span>
                <input
                  type="text"
                  className="modal-input"
                  placeholder="e.g. May 2026 Hopkinsville"
                  value={createBatchName}
                  onChange={(e) => setCreateBatchName(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") void handleSubmitCreateBatch();
                    if (e.key === "Escape") setShowCreateBatchDialog(false);
                  }}
                  autoFocus
                />
              </label>
              <BatchDocumentModeSelector
                value={createBatchMode}
                onChange={setCreateBatchMode}
              />
            </div>
            <div className="modal-footer">
              <button
                className="icon-button"
                onClick={() => setShowCreateBatchDialog(false)}
              >
                Cancel
              </button>
              <button
                className="primary"
                onClick={() => void handleSubmitCreateBatch()}
              >
                Create batch
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
