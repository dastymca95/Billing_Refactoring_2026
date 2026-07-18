import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type ChangeEvent,
  type ClipboardEvent as ReactClipboardEvent,
  type ComponentProps,
  type DragEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

import { api, getFriendlyErrorMessage, isApiError } from "../../api";
import { DocumentPreviewPanel } from "../../components/DocumentPreviewPanel";
import { HumanAdjudicationPanel } from "../../components/TemplateWorkspace";
import {
  ResManTemplatePreview,
  type CellEdits,
} from "../../components/ResManTemplatePreview";
import type {
  BatchListEntry,
  BatchProgress,
  FileEntry,
  HumanAdjudicationContext,
  HumanAdjudicationOptions,
  OperatorActivityEvent,
  PreviewResponse,
  PreviewRow,
  UploadFileProgress,
} from "../../types";
import {
  billingV2Reducer,
  initialBillingV2State,
  mergeEditedRows,
  rowDocumentTarget,
  type BillingV2ActivePage,
  type BillingV2Filter,
  type BillingV2ViewMode,
} from "./billingV2State";

const ACTIVE_BATCH_LS_KEY = "billing_v2_active_batch_id";
const PROCESS_POLL_MS = 650;
const UPLOAD_CONCURRENCY = 3;
const DETACHED_VIEWER_FEATURES =
  "popup=yes,width=1180,height=900,resizable=yes,scrollbars=no";

type InvoiceGroup = {
  id: string;
  label: string;
  rowIndexes: number[];
};

type GroupSummary = {
  key: string;
  label: string;
  rows: number;
  amount: number;
};
type DocumentPreviewPanelProps = ComponentProps<typeof DocumentPreviewPanel>;

export function BillingV2() {
  const [state, dispatch] = useReducer(
    billingV2Reducer,
    initialBillingV2State,
  );
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const loadSeqRef = useRef(0);
  const preparedPreviewRef = useRef<string>("");
  const [detachedRoot, setDetachedRoot] = useState<HTMLElement | null>(null);
  const detachedWindowRef = useRef<Window | null>(null);
  const [adjudicationOpen, setAdjudicationOpen] = useState(false);
  const [adjudicationSaving, setAdjudicationSaving] = useState(false);
  const [adjudicationContext, setAdjudicationContext] =
    useState<HumanAdjudicationContext | null>(null);
  const [adjudicationContextError, setAdjudicationContextError] = useState("");
  const [activityRefreshKey, setActivityRefreshKey] = useState(0);

  useEffect(() => {
    if (!state.activeBatchId || !state.preview || Object.keys(state.edits).length === 0) return;
    const timer = window.setTimeout(() => {
      const rows = mergeEditedRows(state.preview!.rows, state.edits);
      void api.accountingReadiness(state.activeBatchId!, rows).then((accounting_readiness) => {
        dispatch({ type: "setAccountingReadiness", accountingReadiness: accounting_readiness });
      }).catch((error) => dispatch({ type: "setError", error: getFriendlyErrorMessage(error, "Validate readiness") }));
    }, 250);
    return () => window.clearTimeout(timer);
  }, [state.activeBatchId, state.edits]);

  useEffect(() => {
    if (!adjudicationOpen) return;
    let cancelled = false;
    setAdjudicationContextError("");
    void api.humanAdjudicationContext().then((context) => {
      if (!cancelled) setAdjudicationContext(context);
    }).catch((error) => {
      if (!cancelled) {
        setAdjudicationContext(null);
        setAdjudicationContextError(
          getFriendlyErrorMessage(error, "Authorize human adjudication"),
        );
      }
    });
    return () => {
      cancelled = true;
    };
  }, [adjudicationOpen]);

  const refreshBatchList = useCallback(async () => {
    dispatch({ type: "batchListLoading", loading: true });
    try {
      const res = await api.listBatches();
      dispatch({ type: "batchListLoaded", batches: res.batches });
      return res.batches;
    } catch (error) {
      dispatch({
        type: "setError",
        error: getFriendlyErrorMessage(error, "Load batches"),
      });
      dispatch({ type: "batchListLoading", loading: false });
      return [];
    }
  }, []);

  const refreshBatch = useCallback(async (batchId: string) => {
    const seq = ++loadSeqRef.current;
    dispatch({ type: "batchLoading", loading: true });
    try {
      const [status, progress] = await Promise.all([
        api.getBatch(batchId),
        api.getBatchProgress(batchId).catch(() => null),
      ]);
      let preview: PreviewResponse | null = null;
      if (status.preview_available) {
        try {
          preview = await api.preview(batchId);
        } catch (error) {
          if (!isPreviewMissing(error)) throw error;
        }
      }
      if (seq !== loadSeqRef.current) return null;
      dispatch({
        type: "batchLoaded",
        batchId,
        batchName: status.batch_name || batchNameFromList(batchId, []),
        files: status.files,
        preview,
        progress,
        hasExport: status.export_available,
      });
      dispatch({
        type: "setProcessing",
        processing:
          progress?.status === "processing" || progress?.status === "cancelling",
      });
      return preview;
    } catch (error) {
      if (seq === loadSeqRef.current) {
        dispatch({
          type: "setError",
          error: getFriendlyErrorMessage(error, "Load batch"),
        });
        dispatch({ type: "batchLoading", loading: false });
      }
      return null;
    }
  }, []);

  const prepareLinks = useCallback(
    async (batchId: string, opts: { refreshPreview?: boolean } = {}) => {
      dispatch({ type: "setPreparingLinks", preparing: true });
      try {
        const summary = await api.prepareBillingV2Links(batchId);
        dispatch({ type: "linksPrepared", summary });
        if (opts.refreshPreview && summary.prepared && summary.changed) {
          const preview = await api.preview(batchId);
          dispatch({ type: "setPreview", preview });
          return preview;
        }
      } catch (error) {
        dispatch({
          type: "setError",
          error: getFriendlyErrorMessage(error, "Prepare document links"),
        });
      } finally {
        dispatch({ type: "setPreparingLinks", preparing: false });
      }
      return null;
    },
    [],
  );

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const [audit, batches] = await Promise.all([
          api.billingV2Audit(),
          refreshBatchList(),
        ]);
        if (cancelled) return;
        dispatch({ type: "auditLoaded", audit });
        const stored = safeLocalStorageGet(ACTIVE_BATCH_LS_KEY);
        const initial =
          (stored && batches.find((batch) => batch.batch_id === stored)) ||
          batches[0] ||
          null;
        if (initial) {
          dispatch({
            type: "setActiveBatch",
            batchId: initial.batch_id,
            batchName: initial.batch_name,
          });
          await refreshBatch(initial.batch_id);
        }
      } catch (error) {
        if (!cancelled) {
          dispatch({
            type: "setError",
            error: getFriendlyErrorMessage(error, "Initialize Billing V2"),
          });
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [refreshBatch, refreshBatchList]);

  useEffect(() => {
    if (state.activeBatchId) {
      safeLocalStorageSet(ACTIVE_BATCH_LS_KEY, state.activeBatchId);
    }
  }, [state.activeBatchId]);

  useEffect(() => {
    if (!state.activeBatchId || !state.preview?.row_count) return;
    const key = `${state.activeBatchId}:${state.preview.row_count}`;
    if (preparedPreviewRef.current === key || state.preparingLinks) return;
    preparedPreviewRef.current = key;
    void prepareLinks(state.activeBatchId, { refreshPreview: true });
  }, [
    prepareLinks,
    state.activeBatchId,
    state.preparingLinks,
    state.preview?.row_count,
  ]);

  useEffect(() => {
    if (!state.activeBatchId || !state.processing) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const progress = await api.getBatchProgress(state.activeBatchId!);
        if (cancelled) return;
        dispatch({ type: "setProgress", progress });
        if (isTerminalProgress(progress)) {
          dispatch({ type: "setProcessing", processing: false });
          dispatch({ type: "setCancelling", cancelling: false });
          await refreshBatchList();
          const preview = await refreshBatch(progress.batch_id);
          if (progress.status === "completed" && preview?.row_count) {
            await prepareLinks(progress.batch_id, { refreshPreview: true });
          }
        }
      } catch (error) {
        if (!cancelled) {
          dispatch({
            type: "setError",
            error: getFriendlyErrorMessage(error, "Poll processing"),
          });
        }
      }
    };
    void tick();
    const id = window.setInterval(() => void tick(), PROCESS_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [
    prepareLinks,
    refreshBatch,
    refreshBatchList,
    state.activeBatchId,
    state.processing,
  ]);

  useEffect(() => {
    if (state.uploadItems.every((item) => item.status !== "done")) return;
    const id = window.setTimeout(
      () => dispatch({ type: "clearFinishedUploads" }),
      2800,
    );
    return () => window.clearTimeout(id);
  }, [state.uploadItems]);

  useEffect(() => {
    return () => {
      const win = detachedWindowRef.current;
      detachedWindowRef.current = null;
      setDetachedRoot(null);
      if (win && !win.closed) win.close();
    };
  }, []);

  const invoiceGroups = useMemo(
    () => buildInvoiceGroups(state.preview),
    [state.preview],
  );

  useEffect(() => {
    if (state.singleGroupIndex < invoiceGroups.length) return;
    dispatch({
      type: "setSingleGroupIndex",
      index: Math.max(0, invoiceGroups.length - 1),
    });
  }, [invoiceGroups.length, state.singleGroupIndex]);

  useEffect(() => {
    if (state.selectedRowIndex == null || state.viewMode !== "single") return;
    const nextIndex = invoiceGroups.findIndex((group) =>
      group.rowIndexes.includes(state.selectedRowIndex!),
    );
    if (nextIndex >= 0 && nextIndex !== state.singleGroupIndex) {
      dispatch({ type: "setSingleGroupIndex", index: nextIndex });
    }
  }, [
    invoiceGroups,
    state.selectedRowIndex,
    state.singleGroupIndex,
    state.viewMode,
  ]);

  const visibleRowIndexes = useMemo(
    () =>
      buildVisibleRows({
        preview: state.preview,
        search: state.search,
        filter: state.filter,
        viewMode: state.viewMode,
        activeGroup: invoiceGroups[state.singleGroupIndex] ?? null,
      }),
    [
      invoiceGroups,
      state.filter,
      state.preview,
      state.search,
      state.singleGroupIndex,
      state.viewMode,
    ],
  );

  const groupSummaries = useMemo(
    () => buildGroupSummaries(state.preview?.rows ?? [], state.groupBy),
    [state.groupBy, state.preview?.rows],
  );

  const selectedRow = useMemo(
    () =>
      state.selectedRowIndex == null
        ? null
        : state.preview?.rows[state.selectedRowIndex] ?? null,
    [state.preview?.rows, state.selectedRowIndex],
  );

  const batchOptions = useMemo(() => {
    if (
      !state.activeBatchId ||
      state.batchList.some((batch) => batch.batch_id === state.activeBatchId)
    ) {
      return state.batchList;
    }
    const optimistic: BatchListEntry = {
      batch_id: state.activeBatchId,
      batch_name: state.activeBatchName || "Untitled batch",
      created_at: "",
      status: state.processing ? "processing" : "idle",
      files_count: state.files.length,
      invoices_count: state.preview?.invoice_count ?? 0,
      rows_count: state.preview?.row_count ?? 0,
      manual_review_count: 0,
      export_available: false,
      last_export_file: null,
      supported_vendor_summary: {},
    };
    return [optimistic, ...state.batchList];
  }, [
    state.activeBatchId,
    state.activeBatchName,
    state.batchList,
    state.files.length,
    state.preview?.invoice_count,
    state.preview?.row_count,
    state.processing,
  ]);

  const activeDocumentRef = useMemo(() => {
    if (!state.selectedFilename) return null;
    return {
      filename: state.selectedFilename,
      pageNumber: state.activePage?.pageNumber ?? state.documentTarget?.pageNumber ?? 1,
    };
  }, [state.activePage?.pageNumber, state.documentTarget?.pageNumber, state.selectedFilename]);

  const uploadFiles = useCallback(
    async (incoming: File[]) => {
      const files = incoming.filter((file) => file && file.size >= 0);
      if (files.length === 0) return;
      let batchId = state.activeBatchId;
      if (!batchId) {
        const created = await api.createBatch(defaultBatchName(), {
          documentMode: "auto_detect",
          aiFallbackEnabled: true,
          aiFallbackPolicy: "only_low_confidence",
        });
        batchId = created.batch_id;
        dispatch({
          type: "setActiveBatch",
          batchId,
          batchName: created.batch_name,
        });
        void refreshBatchList();
      }

      const tasks = files.map((file, index) => async () => {
        const id = `${Date.now()}-${index}-${file.name}`;
        const extension = extensionFromName(file.name);
        const item: UploadFileProgress = {
          id,
          batchId: batchId!,
          filename: file.name || `upload-${index + 1}`,
          size_bytes: file.size,
          extension,
          percent: 0,
          status: "queued",
        };
        dispatch({ type: "upsertUploadItem", item });
        try {
          await api.uploadFile(
            batchId!,
            file,
            (progress) => {
              dispatch({
                type: "patchUploadItem",
                id,
                patch: {
                  percent: Math.max(1, Math.min(95, progress.percent)),
                  status: "uploading",
                },
              });
            },
          );
          dispatch({
            type: "patchUploadItem",
            id,
            patch: { percent: 100, status: "done" },
          });
        } catch (error) {
          dispatch({
            type: "patchUploadItem",
            id,
            patch: {
              percent: 100,
              status: "failed",
              error: getFriendlyErrorMessage(error, "Upload file"),
            },
          });
        }
      });
      await runLimited(tasks, UPLOAD_CONCURRENCY);
      await refreshBatch(batchId);
      await refreshBatchList();
      dispatch({
        type: "setNotice",
        notice: `${files.length} document${files.length === 1 ? "" : "s"} added to the batch.`,
      });
    },
    [refreshBatch, refreshBatchList, state.activeBatchId],
  );

  const createBatch = useCallback(async () => {
    try {
      const created = await api.createBatch(defaultBatchName(), {
        documentMode: "auto_detect",
        aiFallbackEnabled: true,
        aiFallbackPolicy: "only_low_confidence",
      });
      dispatch({
        type: "setActiveBatch",
        batchId: created.batch_id,
        batchName: created.batch_name,
      });
      await refreshBatchList();
      await refreshBatch(created.batch_id);
      dispatch({ type: "setNotice", notice: "New empty Billing V2 batch created." });
    } catch (error) {
      dispatch({
        type: "setError",
        error: getFriendlyErrorMessage(error, "Create batch"),
      });
    }
  }, [refreshBatch, refreshBatchList]);

  const switchBatch = useCallback(
    async (event: ChangeEvent<HTMLSelectElement>) => {
      const batchId = event.target.value || null;
      const batch = state.batchList.find((item) => item.batch_id === batchId);
      dispatch({
        type: "setActiveBatch",
        batchId,
        batchName: batch?.batch_name,
      });
      if (batchId) await refreshBatch(batchId);
    },
    [refreshBatch, state.batchList],
  );

  const processBatch = useCallback(async () => {
    if (!state.activeBatchId) return;
    dispatch({ type: "setError", error: "" });
    dispatch({ type: "setNotice", notice: "Batch processing started." });
    dispatch({ type: "setProcessing", processing: true });
    try {
      await api.process(state.activeBatchId);
    } catch (error) {
      dispatch({ type: "setProcessing", processing: false });
      dispatch({
        type: "setError",
        error: getFriendlyErrorMessage(error, "Process batch"),
      });
    }
  }, [state.activeBatchId]);

  const cancelBatch = useCallback(async () => {
    if (!state.activeBatchId) return;
    dispatch({ type: "setCancelling", cancelling: true });
    try {
      const res = await api.cancelBatch(state.activeBatchId);
      dispatch({ type: "setNotice", notice: res.message });
    } catch (error) {
      dispatch({
        type: "setError",
        error: getFriendlyErrorMessage(error, "Cancel batch"),
      });
      dispatch({ type: "setCancelling", cancelling: false });
    }
  }, [state.activeBatchId]);

  const exportBatch = useCallback(async () => {
    if (!state.activeBatchId || !state.preview) return;
    dispatch({ type: "setExporting", exporting: true });
    dispatch({ type: "setError", error: "" });
    try {
      let preview = state.preview;
      const prepared = await api.prepareBillingV2Links(state.activeBatchId);
      dispatch({ type: "linksPrepared", summary: prepared });
      if (prepared.changed) {
        preview = await api.preview(state.activeBatchId);
        dispatch({ type: "setPreview", preview });
      }
      const rows = mergeEditedRows(preview.rows, state.edits);
      const res = await api.exportBatch(state.activeBatchId, rows);
      const exported = res.exported?.[0]?.filename || "ResMan import";
      dispatch({
        type: "setNotice",
        notice: `Export created: ${exported}`,
      });
      await refreshBatch(state.activeBatchId);
      await refreshBatchList();
    } catch (error) {
      dispatch({
        type: "setError",
        error: getFriendlyErrorMessage(error, "Export batch"),
      });
    } finally {
      dispatch({ type: "setExporting", exporting: false });
    }
  }, [
    refreshBatch,
    refreshBatchList,
    state.activeBatchId,
    state.edits,
    state.preview,
  ]);

  const saveHumanAdjudication = useCallback(async (
    options: HumanAdjudicationOptions,
  ) => {
    if (!state.activeBatchId || !state.preview || Object.keys(state.edits).length === 0) {
      return;
    }
    setAdjudicationSaving(true);
    dispatch({ type: "setError", error: "" });
    try {
      const saved = await api.saveEdits(state.activeBatchId, state.edits, options);
      const refreshed = await api.preview(state.activeBatchId);
      dispatch({ type: "setPreview", preview: refreshed });
      setActivityRefreshKey((value) => value + 1);
      setAdjudicationOpen(false);
      const report = saved.adjudication;
      const optionalScopes = [
        report?.benchmark_submissions ? "benchmark submitted" : "",
        report?.learning_approvals ? "learning approved" : "",
        report?.rule_proposals ? "rule proposal created" : "",
      ].filter(Boolean);
      dispatch({
        type: "setNotice",
        notice: `Saved ${saved.applied} human correction${saved.applied === 1 ? "" : "s"}${
          optionalScopes.length ? `; ${optionalScopes.join(", ")}` : ""
        }.`,
      });
    } catch (error) {
      dispatch({
        type: "setError",
        error: getFriendlyErrorMessage(error, "Save human adjudication"),
      });
    } finally {
      setAdjudicationSaving(false);
    }
  }, [state.activeBatchId, state.edits, state.preview]);

  const selectRow = useCallback(
    (rowIndex: number | null) => {
      const row = rowIndex == null ? null : state.preview?.rows[rowIndex] ?? null;
      const target = rowDocumentTarget(state.activeBatchId, row);
      dispatch({ type: "selectRow", rowIndex, target });
    },
    [state.activeBatchId, state.preview?.rows],
  );

  const selectActiveDocumentPage = useCallback(
    (page: BillingV2ActivePage) => {
      dispatch({ type: "activePageChanged", page });
      if (!state.preview?.rows || page.batchId !== state.activeBatchId) return;
      const rowIndex = state.preview.rows.findIndex((row) =>
        rowMatchesDocumentPage(row, page.filename, page.pageNumber),
      );
      if (rowIndex >= 0 && rowIndex !== state.selectedRowIndex) {
        dispatch({ type: "selectRow", rowIndex });
      }
    },
    [state.activeBatchId, state.preview?.rows, state.selectedRowIndex],
  );

  const detachViewer = useCallback(() => {
    if (detachedWindowRef.current && !detachedWindowRef.current.closed) {
      detachedWindowRef.current.focus();
      return;
    }
    const win = window.open("", "billing-v2-document-viewer", DETACHED_VIEWER_FEATURES);
    if (!win) {
      dispatch({
        type: "setError",
        error: "Pop-up blocked. Allow pop-ups to detach the Billing V2 viewer.",
      });
      return;
    }
    const root = prepareDetachedDocumentWindow(win);
    detachedWindowRef.current = win;
    setDetachedRoot(root);
    win.addEventListener("beforeunload", () => {
      detachedWindowRef.current = null;
      setDetachedRoot(null);
    });
    win.focus();
  }, []);

  const reattachViewer = useCallback(() => {
    const win = detachedWindowRef.current;
    detachedWindowRef.current = null;
    setDetachedRoot(null);
    if (win && !win.closed) win.close();
  }, []);

  const handleDrop = useCallback(
    (event: DragEvent<HTMLDivElement>) => {
      const files = Array.from(event.dataTransfer?.files || []);
      if (files.length === 0) return;
      event.preventDefault();
      event.stopPropagation();
      void uploadFiles(files);
    },
    [uploadFiles],
  );

  const handlePaste = useCallback(
    (event: ReactClipboardEvent<HTMLDivElement>) => {
      if (isEditableTarget(event.target)) return;
      const files = imageFilesFromClipboard(event.clipboardData);
      if (files.length === 0) return;
      event.preventDefault();
      event.stopPropagation();
      void uploadFiles(files);
    },
    [uploadFiles],
  );

  const progressPercent = Math.max(0, Math.min(100, state.progress?.percent ?? 0));
  const currentGroup = invoiceGroups[state.singleGroupIndex] ?? null;
  const editedCellCount = Object.values(state.edits).reduce(
    (sum, row) => sum + Object.keys(row).length,
    0,
  );
  const rowsVisible = visibleRowIndexes ? visibleRowIndexes.size : state.preview?.row_count ?? 0;

  const viewer = renderViewer({
    batchId: state.activeBatchId,
    filename: state.selectedFilename,
    files: state.files,
    uploadItems: state.uploadItems,
    progress: state.progress,
    documentTarget: state.documentTarget,
    onActivePageChange: selectActiveDocumentPage,
    onAddDocuments: uploadFiles,
    onProcessBatch: processBatch,
    onDetach: detachViewer,
    onReattach: reattachViewer,
    isDetached: false,
    processing: state.processing,
  });

  const detachedViewer = detachedRoot
    ? createPortal(
        renderViewer({
          batchId: state.activeBatchId,
          filename: state.selectedFilename,
          files: state.files,
          uploadItems: state.uploadItems,
          progress: state.progress,
          documentTarget: state.documentTarget,
          onActivePageChange: selectActiveDocumentPage,
          onAddDocuments: uploadFiles,
          onProcessBatch: processBatch,
          onDetach: detachViewer,
          onReattach: reattachViewer,
          isDetached: true,
          processing: state.processing,
        }),
        detachedRoot,
      )
    : null;

  return (
    <section
      className="billing-v2-shell"
      data-testid="billing-v2"
      onDragOver={(event) => {
        if (event.dataTransfer?.types.includes("Files")) event.preventDefault();
      }}
      onDrop={handleDrop}
      onPaste={handlePaste}
    >
      <div className="billing-v2-toolbar" role="toolbar" aria-label="Billing V2 controls">
        <select
          className="billing-v2-batch-select"
          value={state.activeBatchId ?? ""}
          onChange={switchBatch}
          disabled={state.loadingBatches || state.loadingBatch}
          aria-label="Batch"
          data-testid="billing-v2-batch-select"
        >
          <option value="">Select batch</option>
          {batchOptions.map((batch) => (
            <option key={batch.batch_id} value={batch.batch_id}>
              {batch.batch_name}
            </option>
          ))}
        </select>
        <button type="button" className="btn btn-compact" onClick={createBatch}>
          New Batch
        </button>
        <button
          type="button"
          className="btn btn-compact"
          onClick={() => fileInputRef.current?.click()}
          disabled={!state.activeBatchId && state.loadingBatch}
        >
          Upload
        </button>
        <input
          ref={fileInputRef}
          type="file"
          multiple
          className="billing-v2-file-input"
          onChange={(event) => {
            const files = Array.from(event.currentTarget.files || []);
            event.currentTarget.value = "";
            void uploadFiles(files);
          }}
        />
        <button
          type="button"
          className="btn btn-compact btn-primary"
          onClick={processBatch}
          disabled={!state.activeBatchId || state.files.length === 0 || state.processing}
        >
          {state.processing ? "Processing" : "Process Batch"}
        </button>
        {state.processing && (
          <button
            type="button"
            className="btn btn-mini btn-danger"
            onClick={cancelBatch}
            disabled={state.cancelling}
          >
            {state.cancelling ? "Stopping" : "Stop"}
          </button>
        )}
        <button
          type="button"
          className="btn btn-compact btn-accent"
          onClick={exportBatch}
          disabled={state.preview?.accounting_readiness?.export_allowed !== true || state.exporting || state.processing}
        >
          {state.exporting ? "Exporting" : "Export"}
        </button>
        {editedCellCount > 0 && (
          <button
            type="button"
            className="btn btn-compact btn-accent"
            onClick={() => setAdjudicationOpen(true)}
            disabled={adjudicationSaving || state.processing}
            data-testid="template-save-button"
          >
            Save ({editedCellCount})
          </button>
        )}
        <BillingV2ActivityButton
          batchId={state.activeBatchId}
          refreshKey={activityRefreshKey}
        />

        <span className="billing-v2-toolbar-divider" />

        <input
          className="billing-v2-search"
          value={state.search}
          onChange={(event) =>
            dispatch({ type: "setSearch", search: event.target.value })
          }
          placeholder="Search rows"
          aria-label="Search rows"
        />
        <select
          className="billing-v2-filter"
          value={state.filter}
          onChange={(event) =>
            dispatch({
              type: "setFilter",
              filter: event.target.value as BillingV2Filter,
            })
          }
          aria-label="Filter rows"
        >
          <option value="all">All rows</option>
          <option value="needs_review">Needs review</option>
          <option value="ready">Ready</option>
          <option value="missing_required">Missing required</option>
          <option value="missing_link">Missing link</option>
          <option value="ai_generated">AI generated</option>
        </select>
        <select
          className="billing-v2-filter"
          value={state.groupBy}
          onChange={(event) =>
            dispatch({ type: "setGroupBy", groupBy: event.target.value })
          }
          aria-label="Group rows"
        >
          <option value="">No grouping</option>
          <option value="Vendor">Vendor</option>
          <option value="Property Abbreviation">Property</option>
          <option value="GL Account">GL</option>
          <option value="_source_file">Source file</option>
        </select>
        <SegmentedToggle
          value={state.viewMode}
          onChange={(viewMode) => dispatch({ type: "setViewMode", viewMode })}
        />
      </div>

      <div className="billing-v2-statusbar">
        <StatusPill tone={state.processing ? "info" : "neutral"}>
          {state.processing
            ? state.progress?.current_step || "Processing"
            : state.activeBatchName || "No batch selected"}
        </StatusPill>
        <StatusPill tone={state.linkSummary?.rows_missing_links ? "warn" : "ok"}>
          Links {state.linkSummary ? `${state.linkSummary.rows_with_links}/${state.linkSummary.rows_total}` : "pending"}
        </StatusPill>
        <StatusPill tone="neutral">
          {state.audit
            ? `${state.audit.available_count}/${state.audit.count} deterministic processors`
            : "Auditing processors"}
        </StatusPill>
        <StatusPill tone="neutral">
          {rowsVisible} row{rowsVisible === 1 ? "" : "s"}
        </StatusPill>
        {editedCellCount > 0 && (
          <button
            type="button"
            className="billing-v2-reset-edits"
            onClick={() => dispatch({ type: "resetEdits" })}
          >
            Reset {editedCellCount} edit{editedCellCount === 1 ? "" : "s"}
          </button>
        )}
        {state.processing && (
          <div className="billing-v2-progress" aria-label="Batch progress">
            <span style={{ width: `${progressPercent}%` }} />
          </div>
        )}
      </div>

      {(state.error || state.notice) && (
        <div
          className={`billing-v2-message ${state.error ? "is-error" : "is-info"}`}
          role="status"
        >
          {state.error || state.notice}
        </div>
      )}

      <div className="billing-v2-workspace">
        <main className="billing-v2-grid-panel" aria-label="ResMan template grid">
          <div className="billing-v2-panel-head">
            <div>
              <strong>ResMan Import Template</strong>
              <span>
                Full schema - {state.preview?.columns.length ?? 0} columns -{" "}
                {state.preview?.row_count ?? 0} rows
              </span>
            </div>
            {state.viewMode === "single" && currentGroup && (
              <SingleInvoiceNav
                group={currentGroup}
                index={state.singleGroupIndex}
                total={invoiceGroups.length}
                onPrevious={() =>
                  dispatch({
                    type: "setSingleGroupIndex",
                    index: Math.max(0, state.singleGroupIndex - 1),
                  })
                }
                onNext={() =>
                  dispatch({
                    type: "setSingleGroupIndex",
                    index: Math.min(invoiceGroups.length - 1, state.singleGroupIndex + 1),
                  })
                }
              />
            )}
          </div>
          {state.groupBy && groupSummaries.length > 0 && (
            <div className="billing-v2-group-strip">
              {groupSummaries.slice(0, 8).map((group) => (
                <span key={group.key} title={`${group.rows} rows`}>
                  {group.label}
                  <b>{group.amount.toLocaleString(undefined, { style: "currency", currency: "USD" })}</b>
                </span>
              ))}
            </div>
          )}
          <div className="billing-v2-grid-wrap">
            <ResManTemplatePreview
              preview={state.preview}
              edits={state.edits}
              onCellEdit={(rowIndex, column, value) =>
                dispatch({ type: "cellEdited", rowIndex, column, value })
              }
              visibleRowIndexes={visibleRowIndexes}
              selectedRowIndex={state.selectedRowIndex}
              activeDocumentRef={activeDocumentRef}
              onSelectRow={selectRow}
              selectedColumnKey={state.selectedColumnKey}
              onSelectCell={(rowIndex, column) =>
                dispatch({ type: "selectCell", rowIndex, column })
              }
            />
          </div>
        </main>

        <aside className="billing-v2-viewer-panel" aria-label="Unified document viewer">
          <div className="billing-v2-panel-head billing-v2-viewer-head">
            <div>
              <strong>Documents</strong>
              <span>
                {state.files.length} file{state.files.length === 1 ? "" : "s"} -{" "}
                {selectedRow?._meta?.source_file || state.selectedFilename || "No selection"}
              </span>
            </div>
            <button
              type="button"
              className="panel-window-btn"
              onClick={detachedRoot ? reattachViewer : detachViewer}
              title={detachedRoot ? "Attach viewer" : "Detach viewer"}
              aria-label={detachedRoot ? "Attach viewer" : "Detach viewer"}
            >
              {detachedRoot ? "In" : "Out"}
            </button>
          </div>
          <DocumentList
            files={state.files}
            selectedFilename={state.selectedFilename}
            progress={state.progress}
            preview={state.preview}
            onSelect={(filename) =>
              dispatch({ type: "selectDocument", filename, pageNumber: 1 })
            }
          />
          <div className="billing-v2-viewer-wrap">
            {detachedRoot ? (
              <div className="billing-v2-detached-placeholder">
                Viewer detached. Row and page selection remain synced.
              </div>
            ) : (
              viewer
            )}
          </div>
        </aside>
      </div>

      {state.files.length === 0 && !state.loadingBatch && (
        <div className="billing-v2-drop-hint" aria-hidden>
          Drop documents here or paste a screenshot into Billing V2.
        </div>
      )}
      {detachedViewer}
      {adjudicationOpen && state.activeBatchId && state.preview && (
        <HumanAdjudicationPanel
          batchId={state.activeBatchId}
          preview={state.preview}
          edits={state.edits}
          context={adjudicationContext}
          contextError={adjudicationContextError}
          saving={adjudicationSaving}
          onCancel={() => setAdjudicationOpen(false)}
          onConfirm={saveHumanAdjudication}
        />
      )}
    </section>
  );
}

function BillingV2ActivityButton({
  batchId,
  refreshKey,
}: {
  batchId: string | null;
  refreshKey: number;
}) {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<OperatorActivityEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [position, setPosition] = useState<{ top: number; right: number } | null>(null);
  const buttonRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (!open || !batchId) return;
    let cancelled = false;
    setLoading(true);
    void api.listBatchActivity(batchId).then((response) => {
      if (!cancelled) setItems(response.items);
    }).catch(() => {
      if (!cancelled) setItems([]);
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [batchId, open, refreshKey]);

  useEffect(() => {
    if (!open) {
      setPosition(null);
      return;
    }
    const update = () => {
      const rect = buttonRef.current?.getBoundingClientRect();
      if (rect) setPosition({ top: rect.bottom + 6, right: Math.max(8, window.innerWidth - rect.right) });
    };
    update();
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, [open]);

  return (
    <div className="billing-v2-activity">
      <button
        ref={buttonRef}
        type="button"
        className="btn btn-compact"
        disabled={!batchId}
        onClick={() => setOpen((value) => !value)}
        aria-label="Activity history"
        aria-expanded={open}
        data-testid="template-revisions-btn"
        title="Manual, AI, benchmark, learning and rule history"
      >
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" aria-hidden="true">
          <path d="M3 12a9 9 0 1 0 3-6.71" />
          <polyline points="3 4 3 9 8 9" />
          <polyline points="12 7 12 12 15.5 13.8" />
        </svg>
      </button>
      {open && position && (
        <section
          className="billing-v2-activity-popover"
          style={{ top: position.top, right: position.right }}
          aria-label="Batch activity history"
        >
          <header>
            <strong>Change history</strong>
            <button type="button" className="btn btn-mini" onClick={() => setOpen(false)}>Close</button>
          </header>
          {loading ? (
            <p>Loading activity...</p>
          ) : items.length ? (
            <div className="template-activity-list">
              {items.slice(0, 40).map((event) => (
                <article key={event.event_id} className={`template-activity-item is-${event.source}`}>
                  <div><strong>{event.summary}</strong><span>{activityKind(event)}</span></div>
                  {activityFieldSummary(event).map((line) => <small key={line}>{line}</small>)}
                  <small>{event.actor}</small>
                  <time>{new Date(event.created_at).toLocaleString()}</time>
                </article>
              ))}
            </div>
          ) : (
            <p>No manual, AI, benchmark, learning or rule events yet.</p>
          )}
        </section>
      )}
    </div>
  );
}

function activityKind(event: OperatorActivityEvent): string {
  if (event.event_type.includes("benchmark")) return "benchmark";
  if (event.event_type.includes("learning")) return "learning";
  if (event.event_type.includes("rule")) return "rule";
  return event.source;
}

function activityFieldSummary(event: OperatorActivityEvent): string[] {
  const changes = Array.isArray(event.details?.changes) ? event.details.changes : [];
  return changes.slice(0, 6).map((item) => {
    const change = item && typeof item === "object" ? item as Record<string, unknown> : {};
    const row = Number(change.row_index ?? 0) + 1;
    return `Row ${row} · ${String(change.field ?? "field")} · ${String(change.before ?? change.old_value ?? "(blank)")} → ${String(change.after ?? change.new_value ?? "(blank)")}`;
  });
}

function renderViewer({
  batchId,
  filename,
  files,
  uploadItems,
  progress,
  documentTarget,
  onActivePageChange,
  onAddDocuments,
  onProcessBatch,
  onDetach,
  onReattach,
  isDetached,
  processing,
}: {
  batchId: string | null;
  filename: string | null;
  files: FileEntry[];
  uploadItems: UploadFileProgress[];
  progress: BatchProgress | null;
  documentTarget: DocumentPreviewPanelProps["targetPage"];
  onActivePageChange: DocumentPreviewPanelProps["onActivePageChange"];
  onAddDocuments: (files: File[]) => void | Promise<void>;
  onProcessBatch: () => void | Promise<void>;
  onDetach: () => void;
  onReattach: () => void;
  isDetached: boolean;
  processing: boolean;
}) {
  return (
    <DocumentPreviewPanel
      batchId={batchId}
      filename={filename}
      targetPage={documentTarget}
      onActivePageChange={onActivePageChange}
      aiProgress={progress}
      onProcessBatch={onProcessBatch}
      isProcessing={processing}
      onPopout={isDetached ? undefined : onDetach}
    />
  );
}

function DocumentList({
  files,
  selectedFilename,
  progress,
  preview,
  onSelect,
}: {
  files: FileEntry[];
  selectedFilename: string | null;
  progress: BatchProgress | null;
  preview: PreviewResponse | null;
  onSelect: (filename: string) => void;
}) {
  if (files.length === 0) {
    return <div className="billing-v2-document-list empty">No documents uploaded.</div>;
  }
  return (
    <div className="billing-v2-document-list" data-testid="billing-v2-documents">
      {files.map((file) => {
        const status = documentStatus(file, progress, preview);
        const selected = file.filename === selectedFilename;
        return (
          <button
            type="button"
            key={file.filename}
            className={`billing-v2-document-chip ${selected ? "is-selected" : ""}`}
            onClick={() => onSelect(file.filename)}
            title={file.filename}
          >
            <span>{file.filename}</span>
            <small className={`doc-status-${status.tone}`}>{status.label}</small>
          </button>
        );
      })}
    </div>
  );
}

function SegmentedToggle({
  value,
  onChange,
}: {
  value: BillingV2ViewMode;
  onChange: (value: BillingV2ViewMode) => void;
}) {
  return (
    <div className="billing-v2-segmented" role="group" aria-label="Template view">
      <button
        type="button"
        className={value === "bulk" ? "active" : ""}
        onClick={() => onChange("bulk")}
      >
        Bulk
      </button>
      <button
        type="button"
        className={value === "single" ? "active" : ""}
        onClick={() => onChange("single")}
      >
        Single
      </button>
    </div>
  );
}

function SingleInvoiceNav({
  group,
  index,
  total,
  onPrevious,
  onNext,
}: {
  group: InvoiceGroup;
  index: number;
  total: number;
  onPrevious: () => void;
  onNext: () => void;
}) {
  return (
    <div className="billing-v2-single-nav">
      <button type="button" className="btn btn-mini" onClick={onPrevious} disabled={index <= 0}>
        Prev
      </button>
      <span title={group.label}>
        {index + 1}/{total} - {group.label}
      </span>
      <button type="button" className="btn btn-mini" onClick={onNext} disabled={index >= total - 1}>
        Next
      </button>
    </div>
  );
}

function StatusPill({
  tone,
  children,
}: {
  tone: "neutral" | "info" | "ok" | "warn";
  children: ReactNode;
}) {
  return <span className={`billing-v2-pill tone-${tone}`}>{children}</span>;
}

function buildVisibleRows({
  preview,
  search,
  filter,
  viewMode,
  activeGroup,
}: {
  preview: PreviewResponse | null;
  search: string;
  filter: BillingV2Filter;
  viewMode: BillingV2ViewMode;
  activeGroup: InvoiceGroup | null;
}): Set<number> | null {
  if (!preview) return null;
  const q = search.trim().toLowerCase();
  const activeGroupSet =
    viewMode === "single" && activeGroup ? new Set(activeGroup.rowIndexes) : null;
  const indexes = new Set<number>();
  preview.rows.forEach((row, index) => {
    if (activeGroupSet && !activeGroupSet.has(index)) return;
    if (!rowPassesFilter(row, preview.required_columns, filter)) return;
    if (q && !rowHaystack(row).includes(q)) return;
    indexes.add(index);
  });
  return indexes;
}

function rowPassesFilter(
  row: PreviewRow,
  requiredColumns: string[],
  filter: BillingV2Filter,
): boolean {
  const readinessStatus = row._meta?.readiness_status;
  switch (filter) {
    case "all":
      return true;
    case "needs_review":
      return readinessStatus !== "ready";
    case "ready":
      return readinessStatus === "ready";
    case "missing_required":
      return requiredColumns.some((col) => !hasValue(row[col]));
    case "missing_link":
      return !hasValue(row["Document Url"]);
    case "ai_generated":
      return row._meta?.ai_generated === true;
  }
}

function rowHaystack(row: PreviewRow): string {
  return [
    row["Invoice Number"],
    row["Vendor"],
    row["Property Abbreviation"],
    row["Location"],
    row["Invoice Description"],
    row["Line Item Description"],
    row._meta?.source_file,
  ]
    .filter(Boolean)
    .map((value) => String(value).toLowerCase())
    .join(" ");
}

function buildInvoiceGroups(preview: PreviewResponse | null): InvoiceGroup[] {
  if (!preview) return [];
  const byId = new Map<string, InvoiceGroup>();
  preview.rows.forEach((row, index) => {
    const id =
      row._meta?.invoice_group_id ||
      row._meta?.invoice_number ||
      String(row["Invoice Number"] || `row-${index + 1}`);
    const label =
      String(row["Invoice Number"] || row._meta?.invoice_number || id) ||
      `Invoice ${byId.size + 1}`;
    const group = byId.get(id) || { id, label, rowIndexes: [] };
    group.rowIndexes.push(index);
    byId.set(id, group);
  });
  return Array.from(byId.values());
}

function buildGroupSummaries(rows: PreviewRow[], groupBy: string): GroupSummary[] {
  if (!groupBy) return [];
  const map = new Map<string, GroupSummary>();
  rows.forEach((row) => {
    const raw =
      groupBy === "_source_file"
        ? row._meta?.source_file
        : (row as Record<string, unknown>)[groupBy];
    const key = String(raw || "Unassigned");
    const current = map.get(key) || { key, label: key, rows: 0, amount: 0 };
    current.rows += 1;
    const amount = Number(row.Amount);
    if (Number.isFinite(amount)) current.amount += amount;
    map.set(key, current);
  });
  return Array.from(map.values()).sort((a, b) => b.amount - a.amount);
}

function rowMatchesDocumentPage(
  row: PreviewRow,
  filename: string,
  pageNumber: number,
): boolean {
  const sourceFile = String(row._meta?.source_file || "");
  const sourcePage = Number(row._meta?.source_page || 1);
  return sourceFile === filename && Math.max(1, Math.floor(sourcePage)) === pageNumber;
}

function documentStatus(
  file: FileEntry,
  progress: BatchProgress | null,
  preview: PreviewResponse | null,
): { label: string; tone: "neutral" | "info" | "ok" | "warn" | "error" } {
  if (progress?.current_file === file.filename && progress.status === "processing") {
    return { label: progress.ai_stage || "Processing", tone: "info" };
  }
  const relatedRows = preview?.rows.filter(
    (row) => row._meta?.source_file === file.filename,
  );
  if (relatedRows?.length) {
    const needsReview = relatedRows.some(
      (row) => (row._meta?.manual_review_reasons ?? []).length > 0,
    );
    return needsReview
      ? { label: "Needs Review", tone: "warn" }
      : { label: "Ready", tone: "ok" };
  }
  if (file.file_support_status === "unsupported") {
    return { label: "Failed", tone: "error" };
  }
  return { label: file.file_support_label || "Uploaded", tone: "neutral" };
}

function hasValue(value: unknown): boolean {
  return value !== null && value !== undefined && String(value).trim() !== "";
}

function isPreviewMissing(error: unknown): boolean {
  if (!isApiError(error) || error.status !== 404) return false;
  const detail =
    typeof error.detail === "string" ? error.detail : error.message || "";
  return /no preview|run process|manual-review/i.test(detail);
}

function isTerminalProgress(progress: BatchProgress): boolean {
  return ["completed", "failed", "cancelled"].includes(progress.status);
}

function batchNameFromList(batchId: string, batches: BatchListEntry[]): string {
  return batches.find((batch) => batch.batch_id === batchId)?.batch_name || "Untitled batch";
}

function defaultBatchName(): string {
  return `Billing V2 ${new Date().toLocaleString()}`;
}

function extensionFromName(name: string): string {
  const ext = name.match(/\.([^.]+)$/)?.[1] || "file";
  return ext.startsWith(".") ? ext : `.${ext}`;
}

function imageFilesFromClipboard(data: DataTransfer | null): File[] {
  if (!data) return [];
  const files = Array.from(data.files || []).filter((file) =>
    file.type.startsWith("image/"),
  );
  if (files.length > 0) return files.map(normalizeClipboardImage);
  return Array.from(data.items || [])
    .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
    .map((item) => item.getAsFile())
    .filter((file): file is File => Boolean(file))
    .map(normalizeClipboardImage);
}

function normalizeClipboardImage(file: File): File {
  const ext =
    file.type === "image/jpeg"
      ? "jpg"
      : file.type === "image/webp"
        ? "webp"
        : file.type === "image/gif"
          ? "gif"
          : "png";
  const genericName = !file.name || /^(image|blob|clipboard)(\.\w+)?$/i.test(file.name);
  if (!genericName) return file;
  const stamp = new Date()
    .toISOString()
    .slice(0, 19)
    .replace("T", "-")
    .replace(/:/g, "");
  return new File([file], `screenshot-${stamp}.${ext}`, {
    type: file.type || `image/${ext}`,
    lastModified: Date.now(),
  });
}

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
}

async function runLimited(tasks: (() => Promise<void>)[], limit: number): Promise<void> {
  let next = 0;
  async function worker() {
    while (next < tasks.length) {
      const index = next;
      next += 1;
      await tasks[index]();
    }
  }
  await Promise.all(
    Array.from({ length: Math.min(limit, tasks.length) }, () => worker()),
  );
}

function prepareDetachedDocumentWindow(win: Window): HTMLElement {
  const targetDoc = win.document;
  targetDoc.open();
  targetDoc.write("<!doctype html><html><head></head><body></body></html>");
  targetDoc.close();
  targetDoc.title = "Billing V2 Viewer";
  targetDoc.body.className = "billing-v2-detached-window";

  const base = targetDoc.createElement("base");
  base.href = window.location.href.split("#")[0];
  targetDoc.head.appendChild(base);

  for (const node of Array.from(document.head.children)) {
    const tag = node.tagName.toLowerCase();
    const rel =
      tag === "link" ? (node as HTMLLinkElement).rel?.toLowerCase() : "";
    if (tag === "style" || (tag === "link" && rel.includes("stylesheet"))) {
      targetDoc.head.appendChild(node.cloneNode(true));
    }
  }

  const runtimeStyle = targetDoc.createElement("style");
  runtimeStyle.textContent = `
    html, body, #billing-v2-detached-root {
      width: 100%;
      height: 100%;
      margin: 0;
      overflow: hidden;
      background: #eef4fb;
    }
    body.billing-v2-detached-window .doc-preview-card {
      width: 100vw;
      height: 100vh;
      border: 0;
      border-radius: 0;
      box-shadow: none;
    }
    body.billing-v2-detached-window .doc-preview-body {
      min-height: 0;
      height: calc(100vh - 36px);
    }
  `;
  targetDoc.head.appendChild(runtimeStyle);

  const root = targetDoc.createElement("div");
  root.id = "billing-v2-detached-root";
  targetDoc.body.appendChild(root);
  return root;
}

function safeLocalStorageGet(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function safeLocalStorageSet(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* ignore */
  }
}
