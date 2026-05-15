// File-explorer style batch sidebar.
//
// Phase 1W finishes the migration from a flat active-batch file list to
// operator-friendly batch folders:
//   * each batch row can expand, switch, process, delete, and accept drops
//   * expanded rows load files independently with finite loading/error states
//   * file rows have clear open/delete actions
//   * uploads can target any batch row, not only the active batch

import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

import { api, getFriendlyErrorMessage } from "../api";
import { KebabMenu } from "./KebabMenu";
import {
  DOCUMENT_MODE_DESCRIPTIONS,
  DOCUMENT_MODE_LABELS,
  DOCUMENT_MODES,
  type BatchListEntry,
  type BatchProgress,
  type DocumentMode,
  type FileEntry,
  type UploadFileProgress,
} from "../types";

type Props = {
  batchList: BatchListEntry[];
  activeBatchId: string | null;
  onSwitchBatch: (batchId: string) => Promise<boolean | void> | boolean | void;
  onCreateBatch: (params: {
    batchName?: string;
    documentMode: DocumentMode;
  }) => Promise<string | void> | string | void;
  createRequestToken?: number;
  onRenameBatch: (batchId: string, newName: string) => Promise<void>;
  onDeleteBatch: (batchId: string) => void;
  onRefreshBatchList: () => void;

  files: FileEntry[];
  selectedFile: string | null;
  activeDocumentPage?: {
    batchId: string;
    filename: string;
    pageNumber: number;
  } | null;
  onSelectFile: (batchId: string, filename: string) => Promise<void> | void;
  onSelectPage: (
    batchId: string,
    filename: string,
    pageNumber: number,
  ) => Promise<void> | void;
  onDeleteFile: (
    batchId: string,
    filename: string,
  ) => Promise<FileEntry[] | void> | FileEntry[] | void;
  onUploadFiles: (files: File[]) => void;
  onUploadFilesToBatch?: (
    batchId: string,
    files: File[],
  ) => Promise<void> | void;
  uploadItems?: UploadFileProgress[];

  onProcessBatch: (batchId: string) => Promise<void> | void;
  // Phase 2M — process a single file in the batch (skips the queue,
  // runs synchronously on the server, refreshes the preview).
  onProcessFile?: (
    batchId: string,
    filename: string,
    mode?: "replace" | "merge",
  ) => Promise<void> | void;
  processingBatchId?: string | null;
  isProcessing: boolean;
  isSwitchingBatch?: boolean;
  // Phase 2D — cross-batch queue snapshot (running + queued ids).
  queueStatus?: { running: string | null; queued: string[] };
  // Phase 2I.14 — live progress snapshot for the running batch.
  // BatchRow uses it to paint a percent-fill on the folder; FileChild
  // uses it to mark which PDF is being read right now and how far the
  // run has advanced through the file list.
  progress?: BatchProgress | null;
};

const ACCEPT_TYPES = ".csv,.xlsx,.xls,.pdf,.png,.jpg,.jpeg,.gif,.bmp,.webp,.docx,.doc,.txt";
const SCREENSHOT_EXTENSIONS = new Set(["png", "jpg", "jpeg", "webp", "gif", "bmp"]);
const FILE_LOAD_TIMEOUT_MS = 12_000;
const BATCH_NAME_MAX = 80;

export function BatchExplorer({
  batchList,
  activeBatchId,
  onSwitchBatch,
  onCreateBatch,
  createRequestToken,
  onRenameBatch,
  onDeleteBatch,
  onRefreshBatchList,
  files,
  selectedFile,
  activeDocumentPage,
  onSelectFile,
  onSelectPage,
  onDeleteFile,
  onUploadFiles,
  onUploadFilesToBatch,
  uploadItems = [],
  onProcessBatch,
  onProcessFile,
  processingBatchId,
  isProcessing,
  isSwitchingBatch,
  queueStatus,
  progress,
}: Props) {
  const [openIds, setOpenIds] = useState<Set<string>>(() => new Set());
  const listRef = useRef<HTMLDivElement | null>(null);

  const restoreListScroll = useCallback((scrollTop: number) => {
    const list = listRef.current;
    if (!list) return;
    window.requestAnimationFrame(() => {
      list.scrollTop = scrollTop;
      window.requestAnimationFrame(() => {
        list.scrollTop = scrollTop;
      });
    });
  }, []);

  // Phase 2I.14 — auto-expand the batch that just started running, so
  // the operator sees the per-file progress without having to click.
  // Only expands; never collapses an already-open folder.
  const runningId = queueStatus?.running ?? null;
  useEffect(() => {
    if (!runningId) return;
    setOpenIds((prev) => {
      if (prev.has(runningId)) return prev;
      const next = new Set(prev);
      next.add(runningId);
      return next;
    });
  }, [runningId]);

  // Phase 2D — derive per-batch queue state for the chip on each row.
  const batchState = useCallback(
    (id: string): "idle" | "queued" | "running" | "completed" | "failed" => {
      if (queueStatus?.running === id) return "running";
      if (queueStatus?.queued.includes(id)) return "queued";
      // Completed / failed are derived from the BatchListEntry.status
      // when the row renders; we return idle here and let the row
      // decide based on the batch's own status field. This keeps the
      // queue logic local and predictable.
      return "idle";
    },
    [queueStatus],
  );
  const [filesByBatchId, setFilesByBatchId] = useState<Record<string, FileEntry[]>>({});
  const [loadingBatches, setLoadingBatches] = useState<Set<string>>(new Set());
  const [fileLoadErrors, setFileLoadErrors] = useState<Record<string, string>>({});
  const [dragOverBatchId, setDragOverBatchId] = useState<string | null>(null);
  const [openFileKeys, setOpenFileKeys] = useState<Set<string>>(new Set());

  const uploadToBatch = useCallback(
    async (targetBatchId: string, dropped: File[]) => {
      if (dropped.length === 0) return;
      if (onUploadFilesToBatch) {
        await onUploadFilesToBatch(targetBatchId, dropped);
      } else if (targetBatchId === activeBatchId) {
        onUploadFiles(dropped);
      } else {
        await Promise.resolve(onSwitchBatch(targetBatchId));
      }
    },
    [activeBatchId, onSwitchBatch, onUploadFiles, onUploadFilesToBatch],
  );

  useEffect(() => {
    if (!activeBatchId) return;
    setDragOverBatchId(null);
  }, [activeBatchId]);

  useEffect(() => {
    if (!activeDocumentPage) return;
    const key = fileKey(activeDocumentPage.batchId, activeDocumentPage.filename);
    setOpenFileKeys((prev) => {
      if (prev.has(key)) return prev;
      const next = new Set(prev);
      next.add(key);
      return next;
    });
  }, [activeDocumentPage]);

  useEffect(() => {
    const clearDragState = () => setDragOverBatchId(null);
    window.addEventListener("dragend", clearDragState);
    window.addEventListener("drop", clearDragState);
    return () => {
      window.removeEventListener("dragend", clearDragState);
      window.removeEventListener("drop", clearDragState);
    };
  }, []);

  useEffect(() => {
    if (!activeBatchId) return;
    setFilesByBatchId((prev) => ({ ...prev, [activeBatchId]: files }));
    setFileLoadErrors((prev) => {
      if (!(activeBatchId in prev)) return prev;
      const next = { ...prev };
      delete next[activeBatchId];
      return next;
    });
  }, [activeBatchId, files]);

  const loadBatchFiles = useCallback(
    async (targetBatchId: string, force = false) => {
      if (targetBatchId === activeBatchId) return;
      if (!force) {
        if (filesByBatchId[targetBatchId] !== undefined) return;
        if (loadingBatches.has(targetBatchId)) return;
      }

      setLoadingBatches((prev) => {
        const next = new Set(prev);
        next.add(targetBatchId);
        return next;
      });
      setFileLoadErrors((prev) => {
        if (!(targetBatchId in prev)) return prev;
        const next = { ...prev };
        delete next[targetBatchId];
        return next;
      });

      let timeoutId: number | undefined;
      try {
        const timeout = new Promise<never>((_, reject) => {
          timeoutId = window.setTimeout(
            () => reject(new Error("Timed out loading files.")),
            FILE_LOAD_TIMEOUT_MS,
          );
        });
        const res = await Promise.race([api.listFiles(targetBatchId), timeout]);
        setFilesByBatchId((prev) => ({ ...prev, [targetBatchId]: res.files }));
      } catch (e) {
        setFilesByBatchId((prev) => ({ ...prev, [targetBatchId]: [] }));
        setFileLoadErrors((prev) => ({
          ...prev,
          [targetBatchId]: getFriendlyErrorMessage(e, "Load files"),
        }));
        // eslint-disable-next-line no-console
        console.warn("batch file load failed:", e);
      } finally {
        if (timeoutId !== undefined) window.clearTimeout(timeoutId);
        setLoadingBatches((prev) => {
          const next = new Set(prev);
          next.delete(targetBatchId);
          return next;
        });
      }
    },
    [activeBatchId, filesByBatchId, loadingBatches],
  );

  useEffect(() => {
    openIds.forEach((id) => {
      if (id === activeBatchId) return;
      if (filesByBatchId[id] !== undefined) return;
      if (loadingBatches.has(id)) return;
      if (fileLoadErrors[id]) return;
      void loadBatchFiles(id);
    });
  }, [
    activeBatchId,
    fileLoadErrors,
    filesByBatchId,
    loadBatchFiles,
    loadingBatches,
    openIds,
  ]);

  const toggleOpen = (batchId: string) => {
    const scrollTop = listRef.current?.scrollTop ?? 0;
    setOpenIds((prev) => {
      const next = new Set(prev);
      if (next.has(batchId)) next.delete(batchId);
      else next.add(batchId);
      return next;
    });
    restoreListScroll(scrollTop);
  };

  const toggleFileOpen = (batchId: string, filename: string) => {
    const key = fileKey(batchId, filename);
    setOpenFileKeys((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const hasFileDrag = (e: React.DragEvent) =>
    Array.from(e.dataTransfer?.types || []).includes("Files");

  const handleBatchDragEnter = (batchId: string, e: React.DragEvent) => {
    if (!hasFileDrag(e)) return;
    e.preventDefault();
    e.stopPropagation();
    setDragOverBatchId(batchId);
  };

  const handleBatchDragOver = (batchId: string, e: React.DragEvent) => {
    if (!hasFileDrag(e)) return;
    e.preventDefault();
    e.stopPropagation();
    if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
    setDragOverBatchId(batchId);
  };

  const handleBatchDragLeave = (batchId: string, e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    const related = e.relatedTarget as Node | null;
    if (related && e.currentTarget.contains(related)) return;
    if (dragOverBatchId === batchId) setDragOverBatchId(null);
  };

  const handleBatchDrop = async (batchId: string, e: React.DragEvent) => {
    if (!hasFileDrag(e)) return;
    e.preventDefault();
    e.stopPropagation();
    setDragOverBatchId(null);
    const dropped = Array.from(e.dataTransfer?.files || []);
    if (dropped.length === 0) return;
    setOpenIds((prev) => {
      const next = new Set(prev);
      next.add(batchId);
      return next;
    });
    try {
      await uploadToBatch(batchId, dropped);
      setFilesByBatchId((prev) => {
        const next = { ...prev };
        delete next[batchId];
        return next;
      });
      void loadBatchFiles(batchId, true);
    } finally {
      setDragOverBatchId(null);
    }
  };

  return (
    <div className="batch-explorer" data-testid="batch-explorer">
      <div className="batch-explorer-list" ref={listRef}>
        <CreateBatchRow
          requestToken={createRequestToken}
          onCreate={onCreateBatch}
          onUploadCreatedFiles={(batchId, pasted) => uploadToBatch(batchId, pasted)}
        />
        {batchList.length === 0 && (
          <div className="batch-explorer-empty">
            No batches yet. Create a batch to start collecting bills.
          </div>
        )}
        {batchList.map((b) => {
          const isActive = b.batch_id === activeBatchId;
          const isOpen = openIds.has(b.batch_id);
          const isThisProcessing = processingBatchId === b.batch_id;
          const filesForBatch = isActive ? files : filesByBatchId[b.batch_id] ?? [];
          const uploadsForBatch = uploadItems.filter(
            (item) => item.batchId === b.batch_id,
          );
          return (
            <BatchRow
              key={b.batch_id}
              batch={b}
              isActive={isActive}
              isOpen={isOpen}
              isDragOver={dragOverBatchId === b.batch_id}
              isProcessing={isThisProcessing}
              queueState={batchState(b.batch_id)}
              progress={isThisProcessing ? progress ?? null : null}
              processDisabled={(b.files_count ?? 0) === 0 || (isProcessing && !isThisProcessing)}
              onSwitch={() => {
                if (!isActive) void onSwitchBatch(b.batch_id);
              }}
              onToggle={() => toggleOpen(b.batch_id)}
              onRename={(newName) => onRenameBatch(b.batch_id, newName)}
              onDelete={() => onDeleteBatch(b.batch_id)}
              onProcess={() => void onProcessBatch(b.batch_id)}
              onRefreshList={onRefreshBatchList}
              onDragEnter={(e) => handleBatchDragEnter(b.batch_id, e)}
              onDragOver={(e) => handleBatchDragOver(b.batch_id, e)}
              onDragLeave={(e) => handleBatchDragLeave(b.batch_id, e)}
              onDrop={(e) => void handleBatchDrop(b.batch_id, e)}
            >
              <BatchChildren
                batchId={b.batch_id}
                files={filesForBatch}
                uploadItems={uploadsForBatch}
                isLoading={
                  !isActive &&
                  loadingBatches.has(b.batch_id) &&
                  filesByBatchId[b.batch_id] === undefined
                }
                errorMessage={fileLoadErrors[b.batch_id]}
                selectedFile={isActive ? selectedFile : null}
                activeDocumentPage={activeDocumentPage}
                openFileKeys={openFileKeys}
                onToggleFileOpen={(filename) => toggleFileOpen(b.batch_id, filename)}
                onRetry={() => void loadBatchFiles(b.batch_id, true)}
                onSelectFile={(filename) => void onSelectFile(b.batch_id, filename)}
                onSelectPage={(filename, pageNumber) =>
                  void onSelectPage(b.batch_id, filename, pageNumber)
                }
                onDeleteFile={async (filename) => {
                  const updated = await onDeleteFile(b.batch_id, filename);
                  if (updated) {
                    setFilesByBatchId((prev) => ({
                      ...prev,
                      [b.batch_id]: updated,
                    }));
                  }
                }}
                onProcessFile={
                  onProcessFile
                    ? (filename, mode) => void onProcessFile(b.batch_id, filename, mode)
                    : undefined
                }
                onUploadFiles={(dropped) => void uploadToBatch(b.batch_id, dropped)}
                isProcessing={isProcessing}
                isSwitchingBatch={isActive ? isSwitchingBatch : false}
                expectedFileCount={b.files_count ?? 0}
                progress={isThisProcessing ? progress ?? null : null}
              />
            </BatchRow>
          );
        })}
      </div>
    </div>
  );
}

function CreateBatchRow({
  requestToken,
  onCreate,
  onUploadCreatedFiles,
}: {
  requestToken?: number;
  onCreate: (params: {
    batchName?: string;
    documentMode: DocumentMode;
  }) => Promise<string | void> | string | void;
  onUploadCreatedFiles?: (batchId: string, files: File[]) => Promise<void> | void;
}) {
  const [isOpen, setIsOpen] = useState(false);
  const [draftName, setDraftName] = useState("");
  const [mode, setMode] = useState<DocumentMode>("auto_detect");
  const [error, setError] = useState<string | null>(null);
  const [isCreating, setIsCreating] = useState(false);
  const [queuedScreenshots, setQueuedScreenshots] = useState<File[]>([]);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!requestToken) return;
    setIsOpen(true);
  }, [requestToken]);

  useEffect(() => {
    if (!isOpen) return;
    const id = window.setTimeout(() => inputRef.current?.focus(), 120);
    return () => window.clearTimeout(id);
  }, [isOpen]);

  useEffect(() => {
    if (!isOpen) return;
    const onDoc = (e: MouseEvent) => {
      const node = wrapRef.current;
      if (!node) return;
      if (e.target instanceof Node && !node.contains(e.target)) {
        setIsOpen(false);
        setError(null);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [isOpen]);

  const reset = () => {
    setDraftName("");
    setMode("auto_detect");
    setError(null);
    setQueuedScreenshots([]);
  };

  const close = () => {
    setIsOpen(false);
    reset();
  };

  const submit = async () => {
    const name = draftName.trim();
    if (name.length > BATCH_NAME_MAX) {
      setError(`Batch name is too long (max ${BATCH_NAME_MAX} characters).`);
      return;
    }
    setError(null);
    setIsCreating(true);
    try {
      const created = await onCreate({ batchName: name || undefined, documentMode: mode });
      const createdBatchId = typeof created === "string" ? created : undefined;
      const filesToUpload = queuedScreenshots.slice();
      close();
      if (createdBatchId && filesToUpload.length > 0 && onUploadCreatedFiles) {
        window.setTimeout(() => {
          void Promise.resolve(onUploadCreatedFiles(createdBatchId, filesToUpload)).catch((error) => {
            // The upload path owns user-facing toasts and inline row errors.
            // Keep this panel responsive; do not hold "Creating..." open
            // while screenshots are being uploaded.
            // eslint-disable-next-line no-console
            console.warn("queued screenshot upload failed:", error);
          });
        }, 0);
      }
    } catch (e) {
      setError(getFriendlyErrorMessage(e, "Create batch"));
    } finally {
      setIsCreating(false);
    }
  };

  const queueScreenshotFiles = (files: File[]) => {
    const images = files.filter(isScreenshotFile);
    if (images.length === 0) return;
    setQueuedScreenshots((prev) => [...prev, ...images.map(normalizeScreenshotFile)]);
    setMode("screenshot_image");
    setError(null);
  };

  const handlePaste = (e: React.ClipboardEvent) => {
    const files = filesFromClipboard(e.clipboardData);
    if (files.length === 0) return;
    e.preventDefault();
    e.stopPropagation();
    queueScreenshotFiles(files);
  };

  return (
    <div
      className={`create-batch-row ${isOpen ? "open" : ""}`}
      data-testid="explorer-add-batch"
      ref={wrapRef}
    >
      <button
        type="button"
        className="create-batch-trigger"
        onClick={() => setIsOpen((v) => !v)}
        aria-expanded={isOpen}
        aria-controls="inline-new-batch-panel"
        title="Create a new batch"
      >
        <span className="create-batch-plus" aria-hidden>
          <PlusIcon />
        </span>
        <span className="create-batch-title">New batch</span>
      </button>
      <div
        className={`create-batch-popover ${isOpen ? "open" : ""}`}
        id="inline-new-batch-panel"
        data-testid="inline-new-batch-panel"
        aria-hidden={!isOpen}
        role="menu"
      >
        <div className="create-batch-panel" onPaste={handlePaste}>
          <input
            ref={inputRef}
            type="text"
            className="create-batch-input"
            data-testid="inline-new-batch-name-input"
            placeholder="Optional batch name"
            value={draftName}
            maxLength={BATCH_NAME_MAX + 5}
            onChange={(e) => {
              setDraftName(e.target.value);
              if (error) setError(null);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                void submit();
              } else if (e.key === "Escape") {
                e.preventDefault();
                close();
              }
            }}
          />
          <div className="create-batch-mode-list" role="listbox" aria-label="Document type">
            {DOCUMENT_MODES.map((m) => (
              <button
                key={m}
                type="button"
                className={`create-batch-mode ${mode === m ? "active" : ""}`}
                onClick={() => setMode(m)}
                role="option"
                aria-selected={mode === m}
              >
                <span className="create-batch-mode-icon" aria-hidden>
                  {modeIcon(m)}
                </span>
                <span className="create-batch-mode-copy">
                  <span className="create-batch-mode-title">
                    {DOCUMENT_MODE_LABELS[m]}
                  </span>
                  <span className="create-batch-mode-description">
                    {shortModeDescription(m)}
                  </span>
                </span>
                {mode === m && <CheckMarkIcon />}
              </button>
            ))}
          </div>
          <div
            className={`create-batch-screenshot-drop ${queuedScreenshots.length ? "has-files" : ""}`}
            tabIndex={0}
            onPaste={handlePaste}
            onDragOver={(e) => {
              e.preventDefault();
              if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
            }}
            onDrop={(e) => {
              e.preventDefault();
              queueScreenshotFiles(Array.from(e.dataTransfer.files || []));
            }}
            data-dropzone="true"
            data-testid="inline-new-batch-screenshot-paste"
          >
            <span>{queuedScreenshots.length ? `${queuedScreenshots.length} screenshot${queuedScreenshots.length === 1 ? "" : "s"} ready` : "Paste or drop screenshots"}</span>
            <small>{queuedScreenshots.length ? "They upload after Create." : "Ctrl+V from snipping tool or phone photo."}</small>
          </div>
          {error && (
            <div className="create-batch-error" data-testid="inline-new-batch-error">
              {error}
            </div>
          )}
          <div className="create-batch-actions">
            <button
              type="button"
              className="create-batch-cancel"
              onClick={close}
              disabled={isCreating}
            >
              Cancel
            </button>
            <button
              type="button"
              className="create-batch-submit"
              onClick={() => void submit()}
              disabled={isCreating}
              data-testid="inline-create-batch-submit"
            >
              {isCreating ? "Creating..." : "Create"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function BatchRow({
  batch,
  isActive,
  isOpen,
  isDragOver,
  isProcessing,
  queueState,
  progress,
  processDisabled,
  onSwitch,
  onToggle,
  onRename,
  onDelete,
  onProcess,
  onRefreshList,
  onDragEnter,
  onDragOver,
  onDragLeave,
  onDrop,
  children,
}: {
  batch: BatchListEntry;
  isActive: boolean;
  isOpen: boolean;
  isDragOver: boolean;
  isProcessing: boolean;
  queueState?: "idle" | "queued" | "running" | "completed" | "failed";
  progress?: BatchProgress | null;
  processDisabled: boolean;
  onSwitch: () => void;
  onToggle: () => void;
  onRename: (newName: string) => Promise<void>;
  onDelete: () => void;
  onProcess: () => void;
  onRefreshList: () => void;
  onDragEnter: (e: React.DragEvent) => void;
  onDragOver: (e: React.DragEvent) => void;
  onDragLeave: (e: React.DragEvent) => void;
  onDrop: (e: React.DragEvent) => void;
  children?: ReactNode;
}) {
  const friendly = (batch.batch_name || "").trim() || "Untitled batch";
  const [isRenaming, setIsRenaming] = useState(false);
  const [draft, setDraft] = useState(friendly);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (isRenaming && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [isRenaming]);

  const startRename = () => {
    setDraft(friendly);
    setIsRenaming(true);
  };

  const commitRename = async () => {
    const trimmed = draft.trim();
    if (!trimmed || trimmed === friendly) {
      setIsRenaming(false);
      return;
    }
    try {
      await onRename(trimmed);
    } catch {
      /* parent toasts the error */
    } finally {
      setIsRenaming(false);
      onRefreshList();
    }
  };

  return (
    <div
      className={`batch-row ${isActive ? "active" : ""} ${isOpen ? "open" : ""} ${
        isDragOver ? "drag-over" : ""
      }`}
      data-testid="explorer-batch-drop-target"
      data-batch-id={batch.batch_id}
      onDragEnter={onDragEnter}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
    >
      <div className="batch-row-header">
        <button
          type="button"
          className="batch-row-chevron-btn"
          onClick={(e) => {
            e.stopPropagation();
            onToggle();
          }}
          aria-label={isOpen ? "Collapse batch" : "Expand batch"}
          aria-expanded={isOpen}
          data-testid="explorer-batch-toggle"
        >
          <span className={`batch-row-chevron ${isOpen ? "open" : ""}`} aria-hidden>
            <ChevronRight />
          </span>
        </button>
        <button
          type="button"
          className="batch-row-main"
          onClick={onSwitch}
          title={isActive ? `Active batch - ${friendly}` : `Switch to ${friendly}`}
          data-testid="explorer-batch-row"
          data-batch-id={batch.batch_id}
        >
          <FolderIcon />
          <span className="batch-row-text">
            {isRenaming ? (
              <input
                ref={inputRef}
                type="text"
                className="batch-row-rename"
                value={draft}
                maxLength={80}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    void commitRename();
                  } else if (e.key === "Escape") {
                    e.preventDefault();
                    setIsRenaming(false);
                  }
                }}
                onBlur={() => void commitRename()}
                onClick={(e) => e.stopPropagation()}
                data-testid="explorer-batch-rename-input"
              />
            ) : (
              <span
                className="batch-row-name"
                onDoubleClick={(e) => {
                  e.stopPropagation();
                  startRename();
                }}
                title="Double-click to rename"
              >
                {friendly}
              </span>
            )}
            <span className="batch-row-meta">{batchMeta(batch)}</span>
            {/* Phase 2D — queue state chip. Shows running / queued
                states from the global queue; final states (completed /
                failed) come from the BatchListEntry's own status. */}
            {(() => {
              const live = queueState;
              const persisted = (batch.status || "").toLowerCase();
              let chip: { label: string; cls: string } | null = null;
              if (live === "running") chip = { label: "Running", cls: "is-running" };
              else if (live === "queued") chip = { label: "Queued", cls: "is-queued" };
              else if (persisted === "completed") chip = { label: "Done", cls: "is-completed" };
              else if (persisted === "failed") chip = { label: "Failed", cls: "is-failed" };
              if (!chip) return null;
              return (
                <span className={`batch-queue-chip ${chip.cls}`} aria-label={`Status: ${chip.label}`}>
                  {chip.label}
                </span>
              );
            })()}
          </span>
        </button>
        <button
          type="button"
          className="batch-row-process"
          disabled={processDisabled}
          title={
            (batch.files_count ?? 0) === 0
              ? "Add files before processing this batch"
              : "Process batch"
          }
          aria-label={`Process batch "${friendly}"`}
          onClick={(e) => {
            e.stopPropagation();
            onProcess();
          }}
          data-testid="explorer-batch-process"
        >
          {isProcessing ? (
            <>
              <span className="spinner tiny" aria-hidden />
              <span className="batch-row-process-label">Running</span>
            </>
          ) : (
            <>
              <PlayIcon />
              <span className="batch-row-process-label">Process</span>
            </>
          )}
        </button>
        {/* Phase 2M — destructive + power actions live inside a kebab
            (3-dots) menu instead of an always-visible trash icon, so the
            row stays calm. The Process button is kept as a primary
            action because it's frequent and non-destructive. */}
        <span
          className="batch-row-kebab-wrap"
          onClick={(e) => e.stopPropagation()}
        >
          <KebabMenu
            ariaLabel={`More actions for "${friendly}"`}
            testId="explorer-batch-menu"
            className="batch-row-kebab"
            items={[
              {
                label: "Delete batch",
                tone: "danger",
                icon: <TrashIcon />,
                onClick: onDelete,
              },
            ]}
          />
        </span>
      </div>
      {/* Phase 2I.14 — live progress strip on the folder. Shows a thin
          animated fill that tracks `percent` plus a one-line status
          ("Reading invoice 7 of 8 - 84%"). Hidden when the batch is
          idle so the sidebar stays calm. */}
      {isProcessing && progress && (
        <BatchProgressStrip progress={progress} />
      )}
      <div
        className={`batch-row-collapse ${isOpen && children ? "open" : ""}`}
        aria-hidden={!isOpen || !children}
      >
        <div className="batch-row-collapse-inner">{children}</div>
      </div>
    </div>
  );
}

function BatchProgressStrip({ progress }: { progress: BatchProgress }) {
  const pct = clamp01(progress.percent ?? 0);
  const filesTotal = numberOrNull(progress.files_total);
  const filesDone = numberOrNull(progress.files_done) ?? 0;
  const status = progress.status ?? "processing";
  const isCancelling = status === "cancelling";
  const isCancelled = status === "cancelled";
  const isFailed = status === "failed";
  const subtitle = (() => {
    if (isCancelling) return "Stopping…";
    if (isCancelled) return "Cancelled.";
    if (isFailed) return progress.error_message || "Failed.";
    const file = progress.current_file?.trim();
    const step = progress.current_step?.trim();
    if (filesTotal != null && filesTotal > 0) {
      const ordinal = Math.min(filesDone + 1, filesTotal);
      const head = `Reading file ${ordinal} of ${filesTotal}`;
      return file ? `${head} · ${file}` : step ? `${head} · ${step}` : head;
    }
    return step || file || "Working…";
  })();
  return (
    <div
      className={`batch-row-progress ${
        isCancelling ? "is-cancelling" : ""
      } ${isFailed ? "is-failed" : ""} ${isCancelled ? "is-cancelled" : ""}`}
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(pct)}
      aria-label="Batch processing progress"
      data-testid="batch-row-progress"
    >
      <div className="batch-row-progress-track">
        <div
          className="batch-row-progress-fill"
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="batch-row-progress-meta">
        <span className="batch-row-progress-text" title={subtitle}>
          {subtitle}
        </span>
        <span className="batch-row-progress-percent">{Math.round(pct)}%</span>
      </div>
    </div>
  );
}

function clamp01(v: unknown): number {
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return 0;
  return Math.min(100, Math.max(0, n));
}
function numberOrNull(v: unknown): number | null {
  if (typeof v !== "number" || !Number.isFinite(v)) return null;
  return v;
}

function BatchChildren({
  batchId,
  files,
  uploadItems = [],
  isLoading,
  errorMessage,
  selectedFile,
  activeDocumentPage,
  openFileKeys,
  onToggleFileOpen,
  onRetry,
  onSelectFile,
  onSelectPage,
  onDeleteFile,
  onProcessFile,
  onUploadFiles,
  isProcessing,
  isSwitchingBatch,
  expectedFileCount,
  progress,
}: {
  batchId: string;
  files: FileEntry[];
  uploadItems?: UploadFileProgress[];
  isLoading: boolean;
  errorMessage?: string;
  selectedFile: string | null;
  activeDocumentPage?: {
    batchId: string;
    filename: string;
    pageNumber: number;
  } | null;
  openFileKeys: Set<string>;
  onToggleFileOpen: (filename: string) => void;
  onRetry: () => void;
  onSelectFile: (filename: string) => void;
  onSelectPage: (filename: string, pageNumber: number) => void;
  onDeleteFile: (filename: string) => void;
  onProcessFile?: (filename: string, mode?: "replace" | "merge") => void;
  onUploadFiles: (files: File[]) => void;
  isProcessing: boolean;
  isSwitchingBatch?: boolean;
  expectedFileCount: number;
  progress?: BatchProgress | null;
}) {
  const actualFileNames = new Set(files.map((file) => file.filename));
  const visibleUploads = uploadItems.filter(
    (item) => item.status !== "done" || !actualFileNames.has(item.filename),
  );
  const hasVisibleRows = files.length > 0 || visibleUploads.length > 0;

  if (isLoading && visibleUploads.length === 0) {
    const n = Math.max(1, Math.min(6, expectedFileCount || 2));
    return (
      <div className="batch-row-children">
        <ul className="batch-row-files file-list-skeleton" aria-hidden>
          {Array.from({ length: n }, (_, i) => (
            <li key={i} className="file-row file-row-skeleton">
              <div className="skeleton-line skeleton-line-name" />
              <div className="skeleton-line skeleton-line-badge" />
            </li>
          ))}
        </ul>
      </div>
    );
  }

  return (
    <div className="batch-row-children">
      {errorMessage && (
        <div className="batch-row-load-error" data-testid="batch-files-error">
          <span>Could not load files.</span>
          <button type="button" onClick={onRetry}>
            Retry
          </button>
        </div>
      )}
      {!errorMessage && !hasVisibleRows && (
        <div className="batch-row-empty" data-testid="batch-files-empty">
          No files in this batch.
        </div>
      )}
      {!errorMessage && hasVisibleRows && (
        <ul className="batch-row-files">
          {(() => {
            const currentFile = progress?.current_file?.trim() || "";
            const currentIndex = currentFile
              ? files.findIndex((f) => f.filename === currentFile)
              : -1;
            const filesDone = numberOrNull(progress?.files_done) ?? 0;
            const isRunning =
              !!progress &&
              (progress.status === "processing" ||
                progress.status === "cancelling");
            return files.map((f, idx) => {
              let phase: "idle" | "done" | "active" | "pending" = "idle";
              if (isRunning) {
                if (currentIndex >= 0) {
                  if (idx === currentIndex) phase = "active";
                  else if (idx < currentIndex) phase = "done";
                  else phase = "pending";
                } else if (idx < filesDone) {
                  phase = "done";
                } else {
                  phase = "pending";
                }
              }
              const filePct =
                phase === "active" ? clamp01(progress?.percent ?? 0) : null;
              return (
                <FileChild
                  key={f.filename}
                  batchId={batchId}
                  file={f}
                  isSelected={selectedFile === f.filename}
                  activePage={
                    activeDocumentPage?.batchId === batchId &&
                    activeDocumentPage.filename === f.filename
                      ? activeDocumentPage.pageNumber
                      : null
                  }
                  isPageListOpen={openFileKeys.has(fileKey(batchId, f.filename))}
                  onTogglePages={() => onToggleFileOpen(f.filename)}
                  onSelect={() => onSelectFile(f.filename)}
                  onSelectPage={(pageNumber) =>
                    onSelectPage(f.filename, pageNumber)
                  }
                  onDelete={() => onDeleteFile(f.filename)}
                  onProcess={
                    onProcessFile
                      ? (mode) => onProcessFile(f.filename, mode)
                      : undefined
                  }
                  processingPhase={phase}
                  filePercent={filePct}
                />
              );
            });
          })()}
          {visibleUploads.map((item) => (
            <UploadFileChild key={item.id} item={item} />
          ))}
        </ul>
      )}
      <AddFilesAffordance
        onUploadFiles={onUploadFiles}
        disabled={isProcessing || isSwitchingBatch === true}
      />
    </div>
  );
}

function UploadFileChild({ item }: { item: UploadFileProgress }) {
  const ext = (item.extension || extensionFromName(item.filename)).replace(/^\./, "").toLowerCase();
  const pct = clamp01(item.percent);
  const isFailed = item.status === "failed";
  const isUploading = item.status === "uploading";
  const isDone = item.status === "done";
  const phase = isFailed ? "failed" : isUploading ? "active" : isDone ? "done" : "pending";
  const statusLabel = isFailed
    ? "Upload failed"
    : isUploading
        ? `Uploading ${Math.round(pct)}%`
        : isDone
          ? "Uploaded"
          : "Waiting to upload";
  return (
    <li
      className={`file-tree-node upload-tree-node phase-${phase}`}
      data-testid="explorer-upload-node"
      data-filename={item.filename}
    >
      <div className={`file-row upload-file-row phase-${phase}`}>
        <span className="file-row-page-spacer" aria-hidden />
        <div
          className="file-row-main upload-file-main"
          aria-label={`${statusLabel}: ${item.filename}`}
        >
          <FileTypeIcon ext={ext} />
          <span className="file-row-name upload-file-name" title={item.filename}>
            {item.filename}
          </span>
          <span
            className={`upload-inline-progress ${isFailed ? "upload-failed" : ""}`}
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(pct)}
          >
            <span
              className="upload-inline-progress-fill"
              style={{ width: `${isFailed ? 100 : isDone ? 100 : pct}%` }}
            />
          </span>
          <span
            className={`upload-stop-indicator ${isFailed ? "upload-failed" : ""}`}
            aria-hidden
          >
            <span />
          </span>
        </div>
      </div>
      {isFailed && item.error && (
        <div className="upload-row-error" title={item.error}>
          {item.error}
        </div>
      )}
    </li>
  );
}

function FileChild({
  batchId,
  file,
  isSelected,
  activePage,
  isPageListOpen,
  onTogglePages,
  onSelect,
  onSelectPage,
  onDelete,
  onProcess,
  processingPhase = "idle",
  filePercent = null,
}: {
  batchId: string;
  file: FileEntry;
  isSelected: boolean;
  activePage: number | null;
  isPageListOpen: boolean;
  onTogglePages: () => void;
  onSelect: () => void;
  onSelectPage: (pageNumber: number) => void;
  onDelete: () => void;
  onProcess?: (mode?: "replace" | "merge") => void;
  processingPhase?: "idle" | "done" | "active" | "pending";
  filePercent?: number | null;
}) {
  const ext = (file.extension || "").replace(/^\./, "").toLowerCase();
  const vendor = vendorLabel(file);
  const support = fileSupportLabel(file);
  const pageCount = ext === "pdf" ? Math.max(1, Number(file.page_count || 1)) : 0;
  const showPages = pageCount > 0;
  return (
    <li
      className={`file-tree-node ${isSelected ? "selected" : ""} ${
        isPageListOpen ? "pages-open" : ""
      } phase-${processingPhase}`}
      data-batch-id={batchId}
      data-filename={file.filename}
      data-testid="explorer-file-node"
    >
      <div
        className={`file-row ${isSelected ? "selected" : ""} phase-${processingPhase}`}
      >
        {showPages ? (
          <button
            type="button"
            className="file-row-page-toggle"
            onClick={(e) => {
              e.stopPropagation();
              onTogglePages();
            }}
            aria-label={isPageListOpen ? "Collapse pages" : "Expand pages"}
            aria-expanded={isPageListOpen}
            data-testid="explorer-file-page-toggle"
          >
            <span className={`batch-row-chevron ${isPageListOpen ? "open" : ""}`} aria-hidden>
              <ChevronRight />
            </span>
          </button>
        ) : (
          <span className="file-row-page-spacer" aria-hidden />
        )}
        <button
          type="button"
          className="file-row-main"
          onClick={onSelect}
          aria-label={`Open ${file.filename}`}
          data-testid="explorer-file-row"
        >
          <FileTypeIcon ext={ext} />
          <span className="file-row-name" title={file.filename}>
            {file.filename}
          </span>
          {processingPhase === "active" && (
            <span
              className="file-row-status file-row-status-active"
              aria-label="Processing"
              title="Processing now"
            >
              <span className="spinner tiny" aria-hidden />
            </span>
          )}
          {processingPhase === "done" && (
            <span
              className="file-row-status file-row-status-done"
              aria-label="Processed"
              title="Processed"
            >
              <CheckMarkIcon />
            </span>
          )}
          {processingPhase === "pending" && (
            <span
              className="file-row-status file-row-status-pending"
              aria-label="Waiting"
              title="Waiting in queue"
            >
              <span className="file-row-pending-dot" aria-hidden />
            </span>
          )}
          <span className="file-row-size">{formatSize(file.size_bytes)}</span>
          {support && (
            <span className={support.className} title={support.title}>
              {support.text}
            </span>
          )}
          {vendor && (
            <span className={vendor.className} title={file.vendor_detection_reason}>
              {vendor.text}
            </span>
          )}
        </button>
        {processingPhase === "active" && filePercent != null && (
          <div
            className="file-row-progress"
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(filePercent)}
            aria-label={`Processing ${file.filename}`}
          >
            <div
              className="file-row-progress-fill"
              style={{ width: `${filePercent}%` }}
            />
          </div>
        )}
        {/* Phase 2M — actions live inside a kebab (3-dots) menu so the
            row stays minimalist. Single-file processing was added here
            because, until now, the only way to re-run a vendor on
            one bill was to delete the rest of the batch. */}
        <span
          className="file-row-kebab-wrap"
          onClick={(e) => e.stopPropagation()}
        >
          <KebabMenu
            ariaLabel={`More actions for "${file.filename}"`}
            testId="explorer-file-menu"
            className="file-row-kebab"
            items={[
              {
                label: "New template from file",
                hint: "Show only this document.",
                icon: <PlayIcon />,
                onClick: () => {
                  if (onProcess) onProcess("replace");
                },
                hidden: !onProcess,
                disabled: processingPhase === "active",
              },
              {
                label: "Add to current template",
                hint: "Append or refresh this file's rows.",
                icon: <PlusIcon />,
                onClick: () => {
                  if (onProcess) onProcess("merge");
                },
                hidden: !onProcess,
                disabled: processingPhase === "active",
              },
              {
                label: "Delete file",
                tone: "danger",
                icon: <TrashIcon />,
                testId: "explorer-file-delete",
                onClick: onDelete,
              },
            ]}
          />
        </span>
      </div>
      {showPages && isPageListOpen && (
        <ul className="file-page-list" aria-label={`Pages in ${file.filename}`}>
          {Array.from({ length: pageCount }, (_, i) => i + 1).map((pageNumber) => (
            <li key={pageNumber}>
              <button
                type="button"
                className={`file-page-row ${
                  activePage === pageNumber ? "active" : ""
                }`}
                onClick={() => onSelectPage(pageNumber)}
                data-testid="explorer-file-page"
                data-page-number={pageNumber}
                aria-current={activePage === pageNumber ? "page" : undefined}
              >
                <span className="file-page-dot" aria-hidden />
                <span>Page {pageNumber}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

function AddFilesAffordance({
  onUploadFiles,
  disabled,
}: {
  onUploadFiles: (files: File[]) => void;
  disabled: boolean;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [isDragOver, setIsDragOver] = useState(false);
  const dragDepth = useRef(0);

  const open = () => {
    if (disabled) return;
    inputRef.current?.click();
  };

  const onDragEnter = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (!Array.from(e.dataTransfer?.types || []).includes("Files")) return;
    dragDepth.current += 1;
    setIsDragOver(true);
  };
  const onDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
  };
  const onDragLeave = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) setIsDragOver(false);
  };
  const onDrop = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragDepth.current = 0;
    setIsDragOver(false);
    const dropped = Array.from(e.dataTransfer?.files || []);
    if (dropped.length > 0) onUploadFiles(dropped);
  };

  return (
    <div
      className={`add-files-affordance ${isDragOver ? "is-drag-over" : ""}`}
      data-dropzone="true"
      onDragEnter={onDragEnter}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
      onClick={open}
      role="button"
      tabIndex={disabled ? -1 : 0}
      title="Add files to this batch"
      aria-label="Add files to this batch"
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          open();
        }
      }}
      data-testid="explorer-add-files"
    >
      <PlusIcon />
      <span>Add files</span>
      <input
        ref={inputRef}
        type="file"
        accept={ACCEPT_TYPES}
        multiple
        style={{ display: "none" }}
        onChange={(e) => {
          const list = Array.from(e.target.files || []);
          if (list.length > 0) onUploadFiles(list);
          e.target.value = "";
        }}
      />
    </div>
  );
}

function batchMeta(batch: BatchListEntry): string {
  const files = batch.files_count ?? 0;
  const invoices = batch.invoices_count ?? 0;
  const status = batch.status ? prettyStatus(batch.status) : "idle";
  return `${files} file${files === 1 ? "" : "s"} - ${invoices} inv - ${status}`;
}

function fileKey(batchId: string, filename: string): string {
  return `${batchId}::${filename}`;
}

function prettyStatus(status: string): string {
  if (status.toLowerCase().trim() === "cancelled") return "Ready";
  const s = status.replace(/_/g, " ").trim();
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : "Idle";
}

function vendorLabel(f: FileEntry): { className: string; text: string } | null {
  if (!f.vendor_key) return null;
  if (f.vendor_key === "unknown")
    return { className: "badge gray", text: "needs review" };
  if (!f.supported_in_phase_1)
    return { className: "badge yellow", text: prettyVendor(f.vendor_key) };
  return { className: "badge green", text: prettyVendor(f.vendor_key) };
}

function fileSupportLabel(f: FileEntry): { className: string; text: string; title: string } | null {
  const text = (f.file_support_label || "").trim();
  const sourceType = (f.source_type || "").trim();
  if (!text && !sourceType) return null;
  const status = (f.file_support_status || "supported").toLowerCase();
  const label = text || sourceType.replace(/_/g, " ");
  return {
    className: status === "supported" ? "badge gray" : "badge yellow",
    text: label,
    title: f.file_support_reason || "Universal ingestion file type",
  };
}

function prettyVendor(key: string): string {
  if (key === "richmond_utilities") return "Richmond";
  if (key === "hopkinsville_water_environment_authority") return "Hopkinsville";
  return key.replace(/_/g, " ");
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function ChevronRight() {
  return (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="9 18 15 12 9 6" />
    </svg>
  );
}

function FolderIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
    </svg>
  );
}

function FileTypeIcon({ ext }: { ext: string }) {
  const label = ext ? ext.toUpperCase().slice(0, 4) : "FILE";
  return (
    <span className={`file-type-icon ext-${ext || "default"}`} aria-hidden="true">
      <span className="file-type-icon-label">{label}</span>
    </span>
  );
}

function PlusIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <line x1="12" y1="5" x2="12" y2="19" />
      <line x1="5" y1="12" x2="19" y2="12" />
    </svg>
  );
}

function PlayIcon() {
  return (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <polygon points="6 4 20 12 6 20 6 4" />
    </svg>
  );
}

function TrashIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="3 6 5 6 21 6" />
      <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
      <path d="M10 11v6" />
      <path d="M14 11v6" />
      <path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" />
    </svg>
  );
}

function CheckMarkIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="20 6 9 17 4 12" />
    </svg>
  );
}

function modeIcon(mode: DocumentMode): string {
  switch (mode) {
    case "digital_pdf":
      return "PDF";
    case "scanned_pdf":
      return "OCR";
    case "screenshot_image":
      return "IMG";
    case "mixed_pdf":
      return "Mix";
    case "csv_excel":
      return "CSV";
    case "auto_detect":
    default:
      return "Auto";
  }
}

function shortModeDescription(mode: DocumentMode): string {
  switch (mode) {
    case "auto_detect":
      return "Safe default";
    case "digital_pdf":
      return "Text-based bills";
    case "scanned_pdf":
      return "Scanned bills";
    case "screenshot_image":
      return "Paste screenshots";
    case "mixed_pdf":
      return "Mixed PDFs";
    case "csv_excel":
      return "CSV or Excel";
    default:
      return DOCUMENT_MODE_DESCRIPTIONS[mode];
  }
}

function filesFromClipboard(data: DataTransfer): File[] {
  const out: File[] = [];
  for (const item of Array.from(data.items || [])) {
    if (item.kind !== "file" || !item.type.startsWith("image/")) continue;
    const file = item.getAsFile();
    if (file) out.push(file);
  }
  return out;
}

function isScreenshotFile(file: File): boolean {
  return file.type.startsWith("image/") || SCREENSHOT_EXTENSIONS.has(extensionFromName(file.name));
}

function normalizeScreenshotFile(file: File): File {
  const ext = extensionForImageType(file.type) || extensionFromName(file.name) || "png";
  const stamp = new Date()
    .toISOString()
    .replace(/[-:]/g, "")
    .replace(/\..+$/, "")
    .replace("T", "_");
  const name =
    file.name && !/^image\./i.test(file.name)
      ? file.name
      : `screenshot_${stamp}.${ext}`;
  return new File([file], name, { type: file.type || `image/${ext}` });
}

function extensionForImageType(type: string): string {
  switch (type.toLowerCase()) {
    case "image/jpeg":
      return "jpg";
    case "image/png":
      return "png";
    case "image/webp":
      return "webp";
    case "image/gif":
      return "gif";
    case "image/bmp":
      return "bmp";
    default:
      return "";
  }
}

function extensionFromName(name: string): string {
  const m = /\.([A-Za-z0-9]+)$/.exec(name || "");
  return m ? m[1].toLowerCase() : "";
}
