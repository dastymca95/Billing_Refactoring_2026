import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api, getFriendlyErrorMessage, isApiError } from "./api";
import { AiFallbackStatusBadge } from "./components/AiFallbackStatusBadge";
import { BatchExplorer } from "./components/BatchExplorer";
import {
  ConfirmDialog,
  type ConfirmDialogOptions,
} from "./components/ConfirmDialog";
import { DesktopMenu } from "./components/DesktopMenu";
import { DocumentPreviewPanel } from "./components/DocumentPreviewPanel";
// DropZone + FileList: superseded by BatchExplorer in Phase 1X.
import { NavRail, type AppModule } from "./components/NavRail";
import { SettingsDialog } from "./components/SettingsDialog";
import { WindowsMenu } from "./components/WindowsMenu";
import { IssuesDrawer } from "./components/IssuesDrawer";
import { IssuesPill } from "./components/IssuesPill";
import { RenameBatchModal } from "./components/RenameBatchModal";
import { TemplateWorkspace } from "./components/TemplateWorkspace";
import {
  CellContextMenu,
  CellExplainModal,
  RemapScopeChooser,
} from "./components/CellMenu";
import { Toasts, type Toast } from "./components/Toasts";
import { WorkflowSteps } from "./components/WorkflowSteps";
import { useResizablePanel } from "./hooks/useResizablePanel";
import type { CellEdits } from "./components/ResManTemplatePreview";
import type {
  BatchListEntry,
  BatchProgress,
  DocumentMode,
  FileEntry,
  ManualReviewItem,
  PreviewResponse,
  PreviewRow,
  UploadFileProgress,
} from "./types";

// localStorage key used to remember the active batch across page refreshes.
const ACTIVE_BATCH_LS_KEY = "billing_refactoring_active_batch_id";
const NAV_COLLAPSED_LS_KEY = "billing_refactoring_nav_collapsed";
const BATCH_NAME_MAX = 80;

// How often the frontend polls /progress while processing. Phase 1O —
// dropped from 750 ms to 500 ms so the bar moves visibly with every
// per-page progress update from the backend OCR loop. Fast enough to
// feel live, slow enough that a long batch doesn't hammer the API.
const PROGRESS_POLL_MS = 500;
const UPLOAD_PARALLEL_LIMIT = 4;

// Maximum total time we'll wait for a background processing run before
// showing a "still working" message. The poll never auto-aborts.
const MAX_PROCESSING_WAIT_MS = 15 * 60 * 1000;

type DocumentNavTarget = {
  batchId: string;
  filename: string;
  pageNumber: number;
  nonce: number;
};

type ActiveDocumentPage = {
  batchId: string;
  filename: string;
  pageNumber: number;
};

type PanelKey = "batches" | "document" | "template";

// Phase 2C — derive a friendly vendor label for the breadcrumb header.
// Prefers the per-batch detection summary when one vendor dominates;
// falls back to "Mixed" if a batch has multiple vendors.
function deriveVendorLabel(status: any): string {
  const supported = status?.metadata?.supported_vendor_summary || {};
  const summary = status?.summary || {};
  const detection = summary.detection || status?.detection || {};
  const counts = new Map<string, number>();
  if (detection && typeof detection === "object") {
    for (const v of Object.values(detection)) {
      const key = typeof v === "string" ? v : (v as any)?.vendor_key;
      if (typeof key === "string" && key) counts.set(key, (counts.get(key) || 0) + 1);
    }
  }
  if (counts.size === 0 && supported && typeof supported === "object") {
    for (const k of Object.keys(supported)) counts.set(k, 1);
  }
  if (counts.size === 0) return "";
  if (counts.size === 1) return prettyVendor(Array.from(counts.keys())[0]);
  return "Mixed vendors";
}

function prettyVendor(key: string): string {
  if (key === "richmond_utilities") return "Richmond Utilities";
  if (key === "hopkinsville_water_environment_authority")
    return "Hopkinsville Water";
  if (!key) return "";
  return key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function rowDocumentRef(row: PreviewRow | null | undefined): {
  filename: string;
  pageNumber: number;
} | null {
  const meta = row?._meta;
  const filename =
    typeof meta?.source_file === "string" && meta.source_file.trim()
      ? meta.source_file.trim()
      : null;
  if (!filename) return null;
  const rawPage = meta?.source_page;
  const pageNumber =
    typeof rawPage === "number" && Number.isFinite(rawPage) && rawPage > 0
      ? Math.floor(rawPage)
      : 1;
  return { filename, pageNumber };
}

function extensionFromUploadName(name: string): string {
  const match = /\.([A-Za-z0-9]+)$/.exec(name || "");
  return match ? match[1].toLowerCase() : "";
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, Math.max(0, ms)));
}

function mergeFilesPreserveAppend(previous: FileEntry[], incoming: FileEntry[]): FileEntry[] {
  if (previous.length === 0 || incoming.length === 0) return incoming;
  const incomingByName = new Map(incoming.map((file) => [file.filename, file]));
  const ordered: FileEntry[] = [];
  const seen = new Set<string>();

  for (const oldFile of previous) {
    const nextFile = incomingByName.get(oldFile.filename);
    if (!nextFile) continue;
    ordered.push(nextFile);
    seen.add(nextFile.filename);
  }

  for (const file of incoming) {
    if (seen.has(file.filename)) continue;
    ordered.push(file);
  }

  return ordered;
}

export default function App() {
  // Top-level workspace stays focused on batches. Rules, fallback behavior,
  // and output text rules now live in Settings.
  const [activeModule, setActiveModule] = useState<AppModule>("batches");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [navCollapsed, setNavCollapsed] = useState(() => {
    try {
      return localStorage.getItem(NAV_COLLAPSED_LS_KEY) === "1";
    } catch {
      return false;
    }
  });
  const [batchId, setBatchId] = useState<string | null>(null);
  const [files, setFiles] = useState<FileEntry[]>([]);
  const filesRef = useRef<FileEntry[]>([]);
  const [uploadItems, setUploadItems] = useState<UploadFileProgress[]>([]);
  const uploadItemsRef = useRef<UploadFileProgress[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [documentTarget, setDocumentTarget] =
    useState<DocumentNavTarget | null>(null);
  const [activeDocumentPage, setActiveDocumentPage] =
    useState<ActiveDocumentPage | null>(null);
  const navNonceRef = useRef(0);

  useEffect(() => {
    uploadItemsRef.current = uploadItems;
  }, [uploadItems]);

  useEffect(() => {
    filesRef.current = files;
  }, [files]);

  useEffect(() => {
    try {
      localStorage.setItem(NAV_COLLAPSED_LS_KEY, navCollapsed ? "1" : "0");
    } catch {
      /* localStorage unavailable; keep session state only */
    }
  }, [navCollapsed]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key === ",") {
        event.preventDefault();
        setSettingsOpen(true);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const [isProcessing, setIsProcessing] = useState(false);
  const [isCancelling, setIsCancelling] = useState(false);
  const [isExporting, setIsExporting] = useState(false);
  const [isSavingEdits, setIsSavingEdits] = useState(false);
  const [hasExport, setHasExport] = useState(false);

  // Phase 1U — batch switch loading state. The previous switch flow
  // mutated `batchId` / `files` / `selected` BEFORE the preview was
  // fetched, which caused an empty-flash where the operator briefly
  // saw a half-loaded state with the new batch id but no template
  // rows ("No data yet. Click Process Batch…"). The new flow blocks
  // those mutations behind an atomic-swap once all the API calls
  // finish, and shows a translucent overlay while it's in flight.
  const [isSwitchingBatch, setIsSwitchingBatch] = useState(false);
  const [loadingBatchName, setLoadingBatchName] = useState<string | null>(null);
  // Phase 2C — focus mode (template fills the layout, sidebar+document
  // panes are hidden) and export-name display state.
  const [focusModeTemplate, setFocusModeTemplate] = useState(false);
  // Template module is the only panel with window controls and they
  // are exactly two: Detach and Reattach. While detached the popout
  // window is the live editor and the embedded template panel hides
  // so the Document panel can fill the freed horizontal space.
  const [templateDetached, setTemplateDetached] = useState(false);
  const templatePopoutRef = useRef<Window | null>(null);
  const [exportName, setExportName] = useState<string>("");
  const [vendorLabel, setVendorLabel] = useState<string>("");
  // Phase 2D — template revision history + cross-batch queue.
  const [revisions, setRevisions] = useState<import("./types").RevisionEntry[]>([]);
  const [currentRevisionId, setCurrentRevisionId] = useState<string | null>(null);
  const [queueStatus, setQueueStatus] = useState<import("./types").QueueStatus>({
    running: null,
    queued: [],
  });
  // Monotonically incremented per switch-attempt; in-flight responses
  // older than the current token are dropped so a stale fast-arriving
  // response can never overwrite a newer slow-arriving one.
  const switchTokenRef = useRef(0);

  const [preview, setPreview] = useState<PreviewResponse | null>(null);
  const [review, setReview] = useState<ManualReviewItem[]>([]);
  const [edits, setEdits] = useState<CellEdits>({});
  const [error, setError] = useState<string | null>(null);

  // Phase 1K — toast queue. Replaces the old in-page success/info
  // banner that grabbed a column of vertical space.
  const [toasts, setToasts] = useState<Toast[]>([]);

  const pushToast = useCallback((t: Omit<Toast, "id"> & { id?: string }) => {
    const id = t.id ?? `t_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`;
    setToasts((prev) => {
      // Replace any existing toast with the same id so back-to-back
      // events don't pile up.
      const filtered = prev.filter((p) => p.id !== id);
      return [...filtered, { ...t, id }];
    });
  }, []);
  const dismissToast = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const requestConfirm = useCallback((options: ConfirmDialogOptions) => {
    if (confirmResolverRef.current) {
      confirmResolverRef.current(false);
    }
    return new Promise<boolean>((resolve) => {
      confirmResolverRef.current = resolve;
      setConfirmDialog(options);
    });
  }, []);

  const resolveConfirm = useCallback((accepted: boolean) => {
    const resolve = confirmResolverRef.current;
    confirmResolverRef.current = null;
    setConfirmDialog(null);
    resolve?.(accepted);
  }, []);

  // Phase 1J — selected template row drives the inspector panel.
  // Phase 1L — inspector lives inside an Issues drawer now (no fixed
  // right column), so we no longer track an `inspectorCollapsed` state
  // on the main shell.
  const [selectedRowIndex, setSelectedRowIndex] = useState<number | null>(null);
  const [inspectorTab, setInspectorTab] = useState<"issues" | "row">("issues");
  const [issuesOpen, setIssuesOpen] = useState<boolean>(false);
  // Phase 2F — desktop window model. Minimize sends panels to the
  // bottom dock; close hides them until restored from the Windows menu.
  const [closedPanels, setClosedPanels] = useState<Set<PanelKey>>(() => new Set());
  const [minimizedPanels, setMinimizedPanels] = useState<Set<PanelKey>>(
    () => new Set(),
  );
  const [maximizedPanel, setMaximizedPanel] = useState<PanelKey | null>(null);

  const restorePanel = useCallback((panel: PanelKey) => {
    setActiveModule("batches");
    setClosedPanels((prev) => {
      const next = new Set(prev);
      next.delete(panel);
      return next;
    });
    setMinimizedPanels((prev) => {
      const next = new Set(prev);
      next.delete(panel);
      return next;
    });
    setMaximizedPanel(null);
  }, []);

  const closePanel = useCallback((panel: PanelKey) => {
    setClosedPanels((prev) => {
      const next = new Set(prev);
      next.add(panel);
      return next;
    });
    setMinimizedPanels((prev) => {
      const next = new Set(prev);
      next.delete(panel);
      return next;
    });
    setMaximizedPanel((current) => (current === panel ? null : current));
  }, []);

  const minimizePanel = useCallback((panel: PanelKey) => {
    setClosedPanels((prev) => {
      const next = new Set(prev);
      next.delete(panel);
      return next;
    });
    setMinimizedPanels((prev) => {
      const next = new Set(prev);
      next.add(panel);
      return next;
    });
    setMaximizedPanel((current) => (current === panel ? null : current));
  }, []);

  const restoreAllPanels = useCallback(() => {
    setActiveModule("batches");
    setClosedPanels(new Set());
    setMinimizedPanels(new Set());
    setMaximizedPanel(null);
  }, []);

  const minimizeAllPanels = useCallback(() => {
    setActiveModule("batches");
    setClosedPanels(new Set());
    setMinimizedPanels(new Set<PanelKey>(["batches", "document", "template"]));
    setMaximizedPanel(null);
  }, []);

  // Phase 1K — local "issue reviewed" set. Browser-session only — no
  // backend state. Reset whenever a fresh review payload lands.
  const [reviewedKeys, setReviewedKeys] = useState<Set<string>>(new Set());
  const toggleReviewed = useCallback((key: string) => {
    setReviewedKeys((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  // Phase 1F: per-batch progress + polling.
  const [progress, setProgress] = useState<BatchProgress | null>(null);
  const pollingTimerRef = useRef<number | null>(null);

  // Phase 1G: batch management
  const [batchName, setBatchName] = useState<string>("");
  const [batchList, setBatchList] = useState<BatchListEntry[]>([]);
  const [showBatchPicker, setShowBatchPicker] = useState<boolean>(false);

  // Inline batch creation lives inside BatchExplorer; this token lets
  // desktop-menu shortcuts open that row without showing a modal.
  const [createBatchRequestToken, setCreateBatchRequestToken] = useState(0);

  // Phase 1P — app-native rename modal (replaces window.prompt).
  const [showRenameDialog, setShowRenameDialog] = useState(false);
  const [confirmDialog, setConfirmDialog] =
    useState<ConfirmDialogOptions | null>(null);
  const confirmResolverRef = useRef<((accepted: boolean) => void) | null>(null);

  // Phase 1J — resizable layout. Three splitters: file sidebar width,
  // document column width, inspector panel width. Sizes persist in
  // localStorage; double-clicking a divider resets to default.
  const sidebarPanel = useResizablePanel({
    storageKey: "billing_refactoring_layout_sidebar_width",
    defaultSize: 280,
    min: 220,
    max: 460,
    direction: "horizontal",
  });
  const documentPanel = useResizablePanel({
    storageKey: "billing_refactoring_layout_document_width",
    defaultSize: 480,
    min: 320,
    max: 720,
    direction: "horizontal",
  });
  // Phase 1L — fixed inspector pane removed. Issues live in a drawer
  // overlay now; the template grid gets the freed width.

  const refreshBatchList = useCallback(async () => {
    try {
      const r = await api.listBatches();
      setBatchList(r.batches);
    } catch {
      window.setTimeout(async () => {
        try {
          const r = await api.listBatches();
          setBatchList(r.batches);
        } catch {
          /* non-fatal */
        }
      }, 500);
    }
  }, []);

  // Phase 2D — refresh revision history for the active batch.
  const refreshRevisions = useCallback(
    async (bid: string | null) => {
      if (!bid) {
        setRevisions([]);
        setCurrentRevisionId(null);
        return;
      }
      try {
        const res = await api.listRevisions(bid);
        setRevisions(res.revisions);
        setCurrentRevisionId(res.current_revision_id);
      } catch {
        // Non-fatal; keep whatever we had.
      }
    },
    [],
  );

  const handleAiMappingApplied = useCallback(async () => {
    if (!batchId) return;
    try {
      const [prev, rev] = await Promise.all([
        api.preview(batchId),
        api.manualReview(batchId),
      ]);
      setPreview(prev);
      setReview(rev.items);
      void refreshRevisions(batchId);
      pushToast({
        tone: "success",
        message: "AI mapping applied.",
        ttl: 2500,
      });
    } catch (e) {
      pushToast({
        tone: "error",
        message: getFriendlyErrorMessage(e, "Refresh AI mapping"),
      });
    }
  }, [batchId, pushToast, refreshRevisions]);

  // Phase 2D — poll the cross-batch processing queue while anything is
  // running or queued so the BatchExplorer chips stay live.
  useEffect(() => {
    let cancelled = false;
    let timer: number | null = null;
    const tick = async () => {
      try {
        const s = await api.getQueueStatus();
        if (cancelled) return;
        setQueueStatus(s);
      } catch {
        /* network blip — keep last snapshot */
      }
    };
    void tick();
    timer = window.setInterval(tick, 1500);
    return () => {
      cancelled = true;
      if (timer !== null) window.clearInterval(timer);
    };
  }, []);

  const handleActivateRevision = useCallback(
    async (revisionId: string) => {
      if (!batchId) return;
      try {
        const res = await api.activateRevision(batchId, revisionId);
        setCurrentRevisionId(res.current_revision_id);
        // Phase 2I.12 — drop local cell edits, row selection, and the
        // reviewed-keys set when switching revisions. Otherwise the
        // user's previous edits paint over the activated revision's
        // values and "switching" looks like a no-op (same display).
        setEdits({});
        setSelectedRowIndex(null);
        setReviewedKeys(new Set());
        // Re-fetch the preview/manual-review the way a switch does.
        try {
          const [prev, rev] = await Promise.all([
            api.preview(batchId),
            api.manualReview(batchId),
          ]);
          setPreview(prev);
          setReview(rev.items);
        } catch {
          /* preview re-fetch failed; the cache may have been swapped
             but the UI keeps its current state until next refresh */
        }
        pushToast({
          tone: "success",
          message: `Switched to revision v${
            revisions.length - revisions.findIndex((r) => r.revision_id === revisionId)
          }.`,
          ttl: 3000,
        });
      } catch (e) {
        pushToast({
          tone: "error",
          message: getFriendlyErrorMessage(e, "Activate revision"),
        });
      }
    },
    // pushToast/setPreview/setReview are stable refs from useState; revisions
    // is read but only for label computation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [batchId, revisions],
  );

  const handleDeleteRevision = useCallback(
    async (revisionId: string) => {
      if (!batchId) return;
      try {
        const res = await api.deleteRevision(batchId, revisionId);
        // Re-fetch the manifest so the dropdown is in sync.
        await refreshRevisions(batchId);
        setCurrentRevisionId(res.current_revision_id);
        pushToast({
          tone: "success",
          message: "Revision deleted.",
          ttl: 2500,
        });
      } catch (e) {
        pushToast({
          tone: "error",
          message: getFriendlyErrorMessage(e, "Delete revision"),
        });
      }
    },
    [batchId, refreshRevisions],
  );

  // Phase 2J/2K — Extraction Trace Overlay linkage.
  // selectedColumnKey narrows row-level traces down to a single cell.
  const [selectedColumnKey, setSelectedColumnKey] = useState<string | null>(null);
  // Cell context menu + explain modal + remap-mode state.
  const [cellMenu, setCellMenu] = useState<
    | { rowIndex: number; column: string; x: number; y: number }
    | null
  >(null);
  const [cellExplain, setCellExplain] = useState<
    import("./types").CellExplain | null
  >(null);
  // When non-null, the document viewer is in "remap" mode: clicking
  // (drawing) yields a bbox, then we open the scope chooser modal.
  const [remapTarget, setRemapTarget] = useState<
    | { rowIndex: number; column: string; vendorKey: string }
    | null
  >(null);
  const [remapDraft, setRemapDraft] = useState<
    | {
        rowIndex: number;
        column: string;
        vendorKey: string;
        page: number;
        bbox: { x: number; y: number; w: number; h: number };
      }
    | null
  >(null);
  // Forward direction: when a template row is selected, surface the
  // trace ids the row's _meta carries so the document viewer can
  // highlight the corresponding overlay regions. When a column is
  // *also* selected (cell focus), we further filter by feeds_columns.
  const selectedRowTraceIds = useMemo<string[]>(() => {
    if (selectedRowIndex == null || !preview) return [];
    const row = preview.rows[selectedRowIndex];
    const ids = (row?._meta as any)?.trace_ids;
    if (!Array.isArray(ids)) return [];
    const all = ids.filter((x) => typeof x === "string");
    // Cell-scoped narrowing: only kept when an explain payload is
    // open (we know which columns each trace feeds via the explain
    // response). Fall back to row-wide ids otherwise.
    if (selectedColumnKey && cellExplain && cellExplain.row_index === selectedRowIndex
        && cellExplain.column === selectedColumnKey) {
      return cellExplain.trace_ids?.length ? cellExplain.trace_ids : all;
    }
    return all;
  }, [selectedRowIndex, preview, selectedColumnKey, cellExplain]);

  // Reverse direction: clicking an overlay region focuses the first
  // template row that lists this trace id in its _meta.trace_ids.
  // Also navigates to the file that owns the trace's source page so
  // the row→page linkage stays consistent.
  const handleTraceClick = useCallback(
    (traceId: string) => {
      if (!preview) return;
      const idx = preview.rows.findIndex((r) => {
        const ids = (r?._meta as any)?.trace_ids;
        return Array.isArray(ids) && ids.includes(traceId);
      });
      if (idx >= 0) {
        setSelectedRowIndex(idx);
      }
    },
    [preview],
  );

  // Phase 2K — Cell Explain / Correct / Learn flow.
  const openExplainForCell = useCallback(
    async (rowIndex: number, column: string) => {
      if (!batchId) return;
      try {
        const ex = await api.explainCell(batchId, rowIndex, column);
        setCellExplain(ex);
      } catch (e) {
        pushToast({
          tone: "error",
          message: getFriendlyErrorMessage(e, "Explain cell"),
        });
      }
    },
    [batchId, pushToast],
  );

  const handleCellContextMenu = useCallback(
    (params: { rowIndex: number; column: string; x: number; y: number }) => {
      setCellMenu(params);
    },
    [],
  );

  const handleSelectCell = useCallback(
    (rowIndex: number | null, column: string | null) => {
      if (rowIndex != null) setSelectedRowIndex(rowIndex);
      setSelectedColumnKey(column);
    },
    [],
  );

  const handleCellExplainSave = useCallback(
    async (newValue: unknown, scope: "cell" | "vendor") => {
      if (!batchId || !cellExplain) return;
      try {
        await api.overrideCell(
          batchId,
          cellExplain.row_index,
          cellExplain.column,
          { new_value: newValue, scope },
        );
        if (scope === "cell") {
          // One-off: also flow through the existing edits state so the
          // value sticks in the preview without reprocessing.
          handleCellEditRef.current?.(
            cellExplain.row_index,
            cellExplain.column,
            newValue,
          );
          pushToast({
            tone: "success",
            message: "Cell updated for this batch.",
            ttl: 2500,
          });
        } else {
          pushToast({
            tone: "success",
            message:
              "Saved as learned correction. Reprocess this batch (or a new one) to apply.",
            ttl: 5000,
          });
        }
      } catch (e) {
        pushToast({
          tone: "error",
          message: getFriendlyErrorMessage(e, "Save correction"),
        });
      }
    },
    [batchId, cellExplain, pushToast],
  );

  const handleStartRemap = useCallback(
    (rowIndex: number, column: string, vendorKey: string) => {
      setRemapTarget({ rowIndex, column, vendorKey });
    },
    [],
  );

  const handleRemapDrawn = useCallback(
    (page: number, bbox: { x: number; y: number; w: number; h: number }) => {
      if (!remapTarget) return;
      setRemapDraft({ ...remapTarget, page, bbox });
    },
    [remapTarget],
  );

  const handleRemapConfirm = useCallback(
    async (params: {
      field_key: string;
      scope: "cell" | "vendor";
      note: string;
    }) => {
      if (!batchId || !remapDraft) return;
      try {
        await api.remapCellSource(
          batchId,
          remapDraft.rowIndex,
          remapDraft.column,
          {
            field_key: params.field_key,
            page: remapDraft.page,
            bbox: remapDraft.bbox,
            scope: params.scope,
            note: params.note,
          },
        );
        pushToast({
          tone: "success",
          message:
            "Region remap saved. Reprocess to apply on this and future bills.",
          ttl: 5000,
        });
      } catch (e) {
        pushToast({
          tone: "error",
          message: getFriendlyErrorMessage(e, "Save remap"),
        });
      } finally {
        setRemapDraft(null);
        setRemapTarget(null);
      }
    },
    [batchId, remapDraft, pushToast],
  );

  // Stash a stable callback ref to handleCellEdit so the explain-save
  // closure doesn't have to depend on its identity (handleCellEdit
  // is defined further down; the ref is wired in a useEffect below).
  const handleCellEditRef = useRef<
    ((rowIndex: number, columnKey: string, newValue: unknown) => void) | null
  >(null);

  const handleSaveEdits = useCallback(async () => {
    if (!batchId) return;
    // Snapshot edits at call time so a concurrent edit doesn't get
    // wiped without being persisted.
    const pending = edits;
    const count = Object.values(pending).reduce(
      (s, m) => s + Object.keys(m).length,
      0,
    );
    if (count === 0) return;
    // Convert numeric-string keys into ints — backend expects ints.
    const payload: Record<number, Record<string, unknown>> = {};
    for (const [k, v] of Object.entries(pending)) {
      const idx = Number(k);
      if (Number.isFinite(idx)) payload[idx] = v as Record<string, unknown>;
    }
    setIsSavingEdits(true);
    try {
      await api.saveEdits(batchId, payload);
      // Edits are now baked into the cache + the current snapshot.
      // Clear them locally and refresh preview so the displayed values
      // come from the cache (single source of truth) instead of a stale
      // overlay.
      setEdits({});
      try {
        const prev = await api.preview(batchId);
        setPreview(prev);
      } catch {
        /* preview refresh failed; cache was saved, UI catches up later */
      }
      // Manifest entry may have updated counts / edited_at; refresh.
      void refreshRevisions(batchId);
      pushToast({
        tone: "success",
        message: `Saved ${count} edit${count === 1 ? "" : "s"}.`,
        ttl: 2500,
      });
    } catch (e) {
      pushToast({
        tone: "error",
        message: getFriendlyErrorMessage(e, "Save edits"),
      });
    } finally {
      setIsSavingEdits(false);
    }
  }, [batchId, edits, pushToast, refreshRevisions]);

  const setDocumentPageTarget = useCallback(
    (bid: string | null, filename: string | null, pageNumber = 1) => {
      if (!bid || !filename) {
        setActiveDocumentPage(null);
        setDocumentTarget(null);
        return;
      }
      const next = {
        batchId: bid,
        filename,
        pageNumber: Math.max(1, Math.floor(pageNumber || 1)),
      };
      setActiveDocumentPage(next);
      setDocumentTarget({ ...next, nonce: ++navNonceRef.current });
    },
    [],
  );

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
  // Phase 1U — atomic batch switch. The previous batch's UI stays
  // visible while we fetch the new batch's data; only after every
  // API call finishes (or settles) do we swap the state in one tick.
  // A monotonic token guards against stale responses overwriting a
  // newer switch.
  const handleSwitchBatch = useCallback(
    async (newId: string) => {
      if (newId === batchId) {
        setShowBatchPicker(false);
        return true;
      }
      const token = ++switchTokenRef.current;
      const t0 =
        typeof performance !== "undefined" ? performance.now() : Date.now();
      const isStale = () => token !== switchTokenRef.current;

      setIsSwitchingBatch(true);
      // Pull the most recent batch list entry's name so the overlay
      // can display the destination batch's name immediately, before
      // /api/batches/<id> resolves.
      const listed = batchList.find((b) => b.batch_id === newId);
      setLoadingBatchName(listed?.batch_name?.trim() || "batch");
      setShowBatchPicker(false);

      try {
        const status = await api.getBatch(newId);
        if (isStale()) return false;
        if (import.meta.env.DEV) {
          console.debug(
            `[switch] getBatch ${(((typeof performance !== "undefined" ? performance.now() : Date.now()) - t0)).toFixed(0)}ms`,
          );
        }
        if (status.batch_name) {
          setLoadingBatchName(status.batch_name);
        }

        // Pull preview + manual-review IN PARALLEL where applicable.
        // Use Promise.allSettled so one failure doesn't sink the
        // other — the operator can still get the preview even if the
        // manual-review fetch hits a transient hiccup.
        const previewPromise = status.preview_available
          ? api.preview(newId)
          : Promise.resolve(null);
        const reviewPromise = status.preview_available
          ? api.manualReview(newId)
          : Promise.resolve(null);
        const [previewSettled, reviewSettled] = await Promise.allSettled([
          previewPromise,
          reviewPromise,
        ]);
        if (isStale()) return false;

        // Atomic state swap — every panel updates in the same React
        // commit so the operator never sees a half-loaded transition.
        setBatchId(status.batch_id);
        setBatchName(status.batch_name || "Untitled batch");
        // Phase 2C — pull the saved export name + vendor display label.
        const meta = (status as any).metadata || {};
        setExportName(typeof meta.export_name === "string" ? meta.export_name : "");
        const detectedVendor = deriveVendorLabel(status);
        setVendorLabel(detectedVendor);
        // Phase 2D — fire revisions refresh; non-blocking.
        void refreshRevisions(status.batch_id);
        setFiles(status.files);
        const nextSelected = status.files[0]?.filename ?? null;
        setSelected(nextSelected);
        setDocumentPageTarget(status.batch_id, nextSelected, 1);
        setHasExport(status.export_available);
        setEdits({});
        setSelectedRowIndex(null);
        setError(null);

        if (status.preview_available) {
          const prev =
            previewSettled.status === "fulfilled" ? previewSettled.value : null;
          const rev =
            reviewSettled.status === "fulfilled" ? reviewSettled.value : null;
          setPreview(prev as PreviewResponse | null);
          setReview(rev ? rev.items : []);
          setReviewedKeys(new Set());
          // Surface a non-fatal warning if either soft-failed.
          if (previewSettled.status === "rejected" && !isStale()) {
            // eslint-disable-next-line no-console
            console.warn("preview load failed during switch:", previewSettled.reason);
          }
          if (reviewSettled.status === "rejected" && !isStale()) {
            // eslint-disable-next-line no-console
            console.warn("manual-review load failed during switch:", reviewSettled.reason);
          }
        } else {
          setPreview(null);
          setReview([]);
          setReviewedKeys(new Set());
        }

        try {
          localStorage.setItem(ACTIVE_BATCH_LS_KEY, status.batch_id);
        } catch {
          /* non-fatal */
        }

        if (import.meta.env.DEV) {
          const ms =
            (typeof performance !== "undefined"
              ? performance.now()
              : Date.now()) - t0;
          // eslint-disable-next-line no-console
          console.debug(`[switch] total ${ms.toFixed(0)}ms`);
        }

        pushToast({
          id: "switch_batch",
          tone: "info",
          message: status.preview_available
            ? `Switched to "${status.batch_name || "Untitled batch"}".`
            : `Switched to "${status.batch_name || "Untitled batch"}". Click Process to populate the preview.`,
        });
        return true;
      } catch (e) {
        if (isStale()) return false;
        // Don't clobber the previous batch's UI on a soft failure.
        pushToast({
          tone: "error",
          message: "Could not load batch. Please try again.",
          ttl: 5000,
        });
        // eslint-disable-next-line no-console
        console.warn("switch batch failed:", e);
        return false;
      } finally {
        if (!isStale()) {
          setIsSwitchingBatch(false);
          setLoadingBatchName(null);
        }
      }
    },
    [batchId, batchList, pushToast, setDocumentPageTarget],
  );

  // Rename the active batch.
  // Phase 1P — open the app-native rename modal. The actual save runs
  // through `handleSubmitRename` so the modal can surface inline
  // validation errors before the toast layer.
  const handleRenameBatch = useCallback(() => {
    if (!batchId) return;
    setShowRenameDialog(true);
  }, [batchId]);

  const handleSubmitRename = useCallback(
    async (newName: string) => {
      if (!batchId) return;
      // Throws on backend error; the modal catches and renders inline.
      await api.updateBatch(batchId, { batchName: newName });
      setBatchName(newName);
      void refreshBatchList();
      setShowRenameDialog(false);
      pushToast({ tone: "success", message: `Renamed batch to "${newName}".` });
    },
    [batchId, refreshBatchList, pushToast],
  );

  const handleCreateNewBatch = useCallback(() => {
    setCreateBatchRequestToken((n) => n + 1);
    setShowBatchPicker(false);
  }, []);

  const handleSubmitCreateBatch = useCallback(async (params: {
    batchName?: string;
    documentMode: DocumentMode;
  }) => {
    const name = (params.batchName || "").trim();
    const documentMode = params.documentMode || "auto_detect";
    if (name.length > BATCH_NAME_MAX) {
      pushToast({
        tone: "error",
        message: `Batch name is too long (max ${BATCH_NAME_MAX} characters).`,
        ttl: 5000,
      });
      return;
    }
    try {
      const r = await api.createBatch(name || undefined, {
        documentMode,
      });
      setBatchId(r.batch_id);
      setBatchName(r.batch_name);
      setFiles([]);
      setSelected(null);
      setDocumentPageTarget(null, null);
      setPreview(null);
      setReview([]);
      setEdits({});
      setSelectedRowIndex(null);
      setHasExport(false);
      setError(null);
      pushToast({
        tone: "success",
        message: `Created batch "${r.batch_name}" · mode=${documentMode}.`,
      });
      try {
        localStorage.setItem(ACTIVE_BATCH_LS_KEY, r.batch_id);
      } catch {
        /* non-fatal */
      }
      void refreshBatchList();
      return r.batch_id;
    } catch (e) {
      pushToast({
        tone: "error",
        message: getFriendlyErrorMessage(e, "Create batch"),
        ttl: 5000,
      });
      // eslint-disable-next-line no-console
      console.warn("create batch failed:", e);
      throw e;
    }
  }, [refreshBatchList, setDocumentPageTarget, pushToast]);

  const refreshFiles = useCallback(
    async (bid: string) => {
      const res = await api.listFiles(bid);
      const orderedFiles = mergeFilesPreserveAppend(filesRef.current, res.files);
      setFiles(orderedFiles);
      if (res.files.length > 0 && !selected) {
        const nextSelected = orderedFiles[0].filename;
        setSelected(nextSelected);
        setDocumentPageTarget(bid, nextSelected, 1);
      }
    },
    [selected, setDocumentPageTarget],
  );

  const enqueueUploadItems = useCallback((targetBatchId: string, newFiles: File[]) => {
    const stamp = Date.now().toString(36);
    const items: UploadFileProgress[] = newFiles.map((file, index) => ({
      id: `${targetBatchId}:${stamp}:${index}:${file.name}:${file.size}`,
      batchId: targetBatchId,
      filename: file.name || `upload-${index + 1}`,
      size_bytes: file.size,
      extension: extensionFromUploadName(file.name),
      percent: 0,
      status: "queued",
    }));

    setUploadItems((prev) => {
      const incoming = new Set(items.map((item) => item.id));
      return [
        ...prev.filter((item) => !incoming.has(item.id)),
        ...items,
      ];
    });
    return items;
  }, []);

  const updateUploadItem = useCallback(
    (id: string, patch: Partial<UploadFileProgress>) => {
      setUploadItems((prev) =>
        prev.map((item) => (item.id === id ? { ...item, ...patch } : item)),
      );
    },
    [],
  );

  const startUploadAnimation = useCallback(
    (id: string, getTargetPercent: () => number) => {
      let frame = 0;
      const startedAt = performance.now();
      const visualMinMs = 1350;
      const tick = () => {
        setUploadItems((prev) =>
          prev.map((item) => {
            if (item.id !== id || item.status !== "uploading") return item;
            const rawTarget = Math.max(0, Math.min(100, getTargetPercent()));
            const elapsed = performance.now() - startedAt;
            const timedCeiling =
              rawTarget >= 100
                ? Math.min(100, (elapsed / visualMinMs) * 100)
                : Math.min(92, (elapsed / visualMinMs) * 92);
            const target = Math.min(rawTarget, timedCeiling);
            const delta = target - item.percent;
            if (delta <= 0.05) return item;
            const next = item.percent + Math.max(delta * 0.22, 0.35);
            return { ...item, percent: Math.min(target, next) };
          }),
        );
        frame = window.requestAnimationFrame(tick);
      };
      frame = window.requestAnimationFrame(tick);
      return () => window.cancelAnimationFrame(frame);
    },
    [],
  );

  const waitForUploadAnimation = useCallback(async (id: string, target = 98) => {
    for (let i = 0; i < 140; i += 1) {
      const current = uploadItemsRef.current.find((item) => item.id === id);
      if (!current || current.percent >= target) return;
      await sleep(16);
    }
  }, []);

  const clearUploadItem = useCallback((id: string, delayMs = 0) => {
    const remove = () =>
      setUploadItems((prev) => prev.filter((item) => item.id !== id));
    if (delayMs > 0) {
      window.setTimeout(remove, delayMs);
    } else {
      remove();
    }
  }, []);

  const failUploadQueue = useCallback(
    (queue: UploadFileProgress[], message: string) => {
      const ids = new Set(queue.map((item) => item.id));
      setUploadItems((prev) =>
        prev.map((item) =>
          ids.has(item.id) && item.status !== "done"
            ? { ...item, status: "failed", error: message, percent: item.percent || 0 }
            : item,
        ),
      );
    },
    [],
  );

  const handleFiles = useCallback(
    async (newFiles: File[]) => {
      let queue: UploadFileProgress[] = [];
      try {
        setError(null);
        const bid = await ensureBatch();
        queue = enqueueUploadItems(bid, newFiles);
        const total = newFiles.length;
        const showProgress = total > 1;
        const progressId = "upload-progress";
        if (showProgress) {
          pushToast({
            id: progressId,
            tone: "info",
            message: `Uploading 0 of ${total}…`,
            ttl: 0,
          });
        }
        let done = 0;
        let cursor = 0;
        let firstUploadError: unknown = null;
        const uploadOne = async (index: number) => {
          const f = newFiles[index];
          const upload = queue[index];
          updateUploadItem(upload.id, { status: "uploading", percent: 0 });
          const startedAt = performance.now();
          let targetPercent = 8;
          const stopAnimation = startUploadAnimation(upload.id, () => targetPercent);
          try {
            await api.uploadFile(bid, f, (progress) => {
              targetPercent = Math.max(
                targetPercent,
                Math.min(92, progress.percent * 0.92),
              );
            });
            targetPercent = 100;
            const elapsed = performance.now() - startedAt;
            if (elapsed < 800) {
              await sleep(800 - elapsed);
            }
            await waitForUploadAnimation(upload.id, 96);
            // Refresh before hiding the temporary upload row so the
            // real file row replaces it without a visual gap.
            await refreshFiles(bid);
            updateUploadItem(upload.id, { status: "done", percent: 100 });
            done += 1;
            clearUploadItem(upload.id, 900);
            if (showProgress) {
              pushToast({
                id: progressId,
                tone: "info",
                message: `Uploading ${done} of ${total}…`,
                ttl: 0,
              });
            }
          } catch (error) {
            const message = getFriendlyErrorMessage(error, "Upload files");
            updateUploadItem(upload.id, {
              status: "failed",
              error: message,
              percent: upload.percent || 0,
            });
            if (!firstUploadError) firstUploadError = error;
          } finally {
            stopAnimation();
          }
        };
        const workerCount = Math.min(UPLOAD_PARALLEL_LIMIT, newFiles.length);
        await Promise.all(
          Array.from({ length: workerCount }, async () => {
            while (cursor < newFiles.length) {
              const index = cursor;
              cursor += 1;
              await uploadOne(index);
            }
          }),
        );
        await refreshFiles(bid);
        if (firstUploadError) {
          throw firstUploadError;
        }
        if (showProgress) {
          pushToast({
            id: progressId,
            tone: "success",
            message: `Uploaded ${total} file${total === 1 ? "" : "s"}.`,
            ttl: 3000,
          });
        }
        setPreview(null);
        setReview([]);
        setEdits({});
        setSelectedRowIndex(null);
        setHasExport(false);
      } catch (e) {
        failUploadQueue(queue, getFriendlyErrorMessage(e, "Upload files"));
        dismissToast("upload-progress");
        setError(getFriendlyErrorMessage(e, "Upload files"));
        // eslint-disable-next-line no-console
        console.warn("upload failed:", e);
      }
    },
    [
      clearUploadItem,
      enqueueUploadItems,
      ensureBatch,
      failUploadQueue,
      refreshFiles,
      pushToast,
      dismissToast,
      startUploadAnimation,
      updateUploadItem,
      waitForUploadAnimation,
    ],
  );

  const handleFilesForBatch = useCallback(
    async (targetBatchId: string, newFiles: File[]) => {
      if (newFiles.length === 0) return;
      const queue = enqueueUploadItems(targetBatchId, newFiles);
      try {
        setError(null);
        const switched = await handleSwitchBatch(targetBatchId);
        if (switched === false) {
          queue.forEach((item) => clearUploadItem(item.id));
          return;
        }
        const total = newFiles.length;
        const showProgress = total > 1;
        const progressId = "upload-progress";
        const listed = batchList.find((b) => b.batch_id === targetBatchId);
        const friendly = listed?.batch_name?.trim() || "batch";
        if (showProgress) {
          pushToast({
            id: progressId,
            tone: "info",
            message: `Uploading 0 of ${total} to "${friendly}"…`,
            ttl: 0,
          });
        }
        let done = 0;
        let nextSelected: string | null = null;
        let cursor = 0;
        let firstUploadError: unknown = null;
        const uploadOne = async (index: number) => {
          const f = newFiles[index];
          const upload = queue[index];
          updateUploadItem(upload.id, { status: "uploading", percent: 0 });
          const startedAt = performance.now();
          let targetPercent = 8;
          const stopAnimation = startUploadAnimation(upload.id, () => targetPercent);
          try {
            await api.uploadFile(targetBatchId, f, (progress) => {
              targetPercent = Math.max(
                targetPercent,
                Math.min(92, progress.percent * 0.92),
              );
            });
            targetPercent = 100;
            const elapsed = performance.now() - startedAt;
            if (elapsed < 800) {
              await sleep(800 - elapsed);
            }
            await waitForUploadAnimation(upload.id, 96);
            // Refresh before hiding the temporary upload row so the
            // real file row replaces it without a visual gap.
            const res = await api.listFiles(targetBatchId);
            const orderedFiles = mergeFilesPreserveAppend(filesRef.current, res.files);
            setFiles(orderedFiles);
            if (nextSelected === null && orderedFiles[0]) {
              nextSelected = orderedFiles[0].filename;
              setSelected(nextSelected);
              setDocumentPageTarget(targetBatchId, nextSelected, 1);
            }
            updateUploadItem(upload.id, { status: "done", percent: 100 });
            done += 1;
            clearUploadItem(upload.id, 900);
            if (showProgress) {
              pushToast({
                id: progressId,
                tone: "info",
                message: `Uploading ${done} of ${total} to "${friendly}"…`,
                ttl: 0,
              });
            }
          } catch (error) {
            const message = getFriendlyErrorMessage(error, "Upload files");
            updateUploadItem(upload.id, {
              status: "failed",
              error: message,
              percent: upload.percent || 0,
            });
            if (!firstUploadError) firstUploadError = error;
          } finally {
            stopAnimation();
          }
        };
        const workerCount = Math.min(UPLOAD_PARALLEL_LIMIT, newFiles.length);
        await Promise.all(
          Array.from({ length: workerCount }, async () => {
            while (cursor < newFiles.length) {
              const index = cursor;
              cursor += 1;
              await uploadOne(index);
            }
          }),
        );
        const finalFiles = await api.listFiles(targetBatchId);
        setFiles(mergeFilesPreserveAppend(filesRef.current, finalFiles.files));
        if (firstUploadError) {
          throw firstUploadError;
        }
        setPreview(null);
        setReview([]);
        setReviewedKeys(new Set());
        setEdits({});
        setSelectedRowIndex(null);
        setHasExport(false);
        void refreshBatchList();
        pushToast({
          id: progressId,
          tone: "success",
          message: `Uploaded ${total} file${total === 1 ? "" : "s"} to "${friendly}".`,
          ttl: 4000,
        });
      } catch (e) {
        failUploadQueue(queue, getFriendlyErrorMessage(e, "Upload files"));
        dismissToast("upload-progress");
        setError(getFriendlyErrorMessage(e, "Upload files"));
        pushToast({
          tone: "error",
          message: getFriendlyErrorMessage(e, "Upload files"),
          ttl: 5000,
        });
        // eslint-disable-next-line no-console
        console.warn("targeted upload failed:", e);
      }
    },
    [
      batchList,
      clearUploadItem,
      enqueueUploadItems,
      failUploadQueue,
      handleSwitchBatch,
      pushToast,
      dismissToast,
      refreshBatchList,
      setDocumentPageTarget,
      startUploadAnimation,
      updateUploadItem,
      waitForUploadAnimation,
    ],
  );

  // ---- Phase 1F: progress polling ----
  const stopPolling = useCallback(() => {
    if (pollingTimerRef.current !== null) {
      window.clearInterval(pollingTimerRef.current);
      pollingTimerRef.current = null;
    }
  }, []);

  const startPolling = useCallback(
    (bid: string) => {
      stopPolling();
      const tick = async () => {
        try {
          const snap = await api.getBatchProgress(bid);
          setProgress(snap);
          if (
            snap.status === "completed" ||
            snap.status === "failed" ||
            snap.status === "cancelled"
          ) {
            stopPolling();
          }
        } catch {
          // network blip — keep polling
        }
      };
      void tick();
      pollingTimerRef.current = window.setInterval(tick, PROGRESS_POLL_MS);
    },
    [stopPolling],
  );

  useEffect(() => stopPolling, [stopPolling]);

  const waitForProcessingDone = useCallback(
    async (bid: string): Promise<BatchProgress | null> => {
      const start = Date.now();
      while (Date.now() - start < MAX_PROCESSING_WAIT_MS) {
        try {
          const snap = await api.getBatchProgress(bid);
          setProgress(snap);
          if (
            snap.status === "completed" ||
            snap.status === "failed" ||
            snap.status === "cancelled"
          ) {
            return snap;
          }
        } catch {
          /* network blip */
        }
        await new Promise((res) => setTimeout(res, PROGRESS_POLL_MS));
      }
      return null;
    },
    [],
  );

  const runProcessBatch = useCallback(async (targetBatchId: string, confirmEdits: boolean) => {
    if (confirmEdits && editedCellCount > 0) {
      const ok = await requestConfirm({
        title: "Discard edits and reprocess?",
        message: `Re-processing will discard ${editedCellCount} unsaved preview edit${editedCellCount === 1 ? "" : "s"}.`,
        confirmLabel: "Reprocess",
        tone: "warning",
      });
      if (!ok) return;
    }
    setIsProcessing(true);
    setError(null);
    setProgress({
      batch_id: targetBatchId,
      status: "processing",
      percent: 0,
      current_step: "Starting…",
    });
    startPolling(targetBatchId);
    try {
      await api.process(targetBatchId);
      const final = await waitForProcessingDone(targetBatchId);
      // Phase 2D — refresh revisions whenever any terminal state lands
      // so the dropdown reflects the new run.
      if (targetBatchId === batchId) {
        void refreshRevisions(targetBatchId);
      }
      if (final && final.status === "failed") {
        pushToast({
          tone: "error",
          message: `Processing failed: ${final.error_message || "see backend logs"}`,
          ttl: 6000,
        });
        return;
      }
      if (final && final.status === "cancelled") {
        pushToast({
          tone: "warning",
          message: "Processing cancelled.",
          ttl: 4200,
        });
        window.setTimeout(() => {
          setProgress((prev) =>
            prev?.batch_id === targetBatchId && prev.status === "cancelled"
              ? null
              : prev,
          );
        }, 2400);
        // Try to load whatever was processed before the stop. The
        // preview may not exist yet if cancelled very early.
        try {
          const prev = await api.preview(targetBatchId);
          const rev = await api.manualReview(targetBatchId);
          setPreview(prev);
          setReview(rev.items);
          setReviewedKeys(new Set());
          setEdits({});
          setSelectedRowIndex(null);
          setHasExport(false);
        } catch {
          /* nothing to load yet — that's OK */
        }
        void refreshBatchList();
        return;
      }
      const prev = await api.preview(targetBatchId);
      const rev = await api.manualReview(targetBatchId);
      setPreview(prev);
      setReview(rev.items);
      setReviewedKeys(new Set());
      setEdits({});
      setSelectedRowIndex(null);
      setHasExport(false);
      const s = prev.summary || {};
      pushToast({
        tone: rev.items.length > 0 ? "warning" : "success",
        message:
          `Processed ${s.files_supported ?? "?"}/${s.files_total ?? "?"} files · ` +
          `${s.invoices_total ?? prev.invoice_count} invoices · ${s.manual_review_total ?? rev.items.length} flagged`,
        ttl: 6000,
      });
      void refreshBatchList();
    } catch (e) {
      const message = getFriendlyErrorMessage(e, "Process batch");
      setError(message);
      // eslint-disable-next-line no-console
      console.warn("process failed:", e);
      setProgress((prev) =>
        prev
          ? { ...prev, status: "failed", error_message: message, percent: 100 }
          : null,
      );
    } finally {
      setIsProcessing(false);
      setIsCancelling(false);
      window.setTimeout(stopPolling, PROGRESS_POLL_MS + 50);
    }
  }, [editedCellCount, pushToast, refreshBatchList, requestConfirm, startPolling, stopPolling, waitForProcessingDone]);

  const handleProcess = useCallback(async () => {
    if (!batchId) return;
    await runProcessBatch(batchId, true);
  }, [batchId, runProcessBatch]);

  const handleProcessBatch = useCallback(
    async (targetBatchId: string) => {
      if (isProcessing) return;
      const isActiveTarget = targetBatchId === batchId;
      if (!isActiveTarget) {
        const switched = await handleSwitchBatch(targetBatchId);
        if (switched === false) return;
      }
      await runProcessBatch(targetBatchId, isActiveTarget);
    },
    [batchId, handleSwitchBatch, isProcessing, runProcessBatch],
  );

  // Phase 2M — single-file processing. Runs the active vendor processor
  // synchronously over a single bill, then refreshes the preview so
  // the new row is merged into the active workspace. The full-batch
  // queue is untouched, so a long batch run on another id keeps going.
  const handleProcessFile = useCallback(
    async (
      targetBatchId: string,
      filename: string,
      mode: "replace" | "merge" = "replace",
    ) => {
      const isMerge = mode === "merge";
      if (isProcessing) {
        pushToast({
          id: `single-process-${filename}`,
          tone: "info",
          message: "Another process is already running. Wait for it to finish.",
          ttl: 4000,
        });
        return;
      }

      if (editedCellCount > 0) {
        const ok = await requestConfirm({
          title: isMerge
            ? "Discard edits and add this file?"
            : "Discard edits and create a file template?",
          message: `${isMerge ? "Adding this file" : "Creating a new file template"} will refresh the preview and discard ${editedCellCount} unsaved edit${editedCellCount === 1 ? "" : "s"}.`,
          confirmLabel: isMerge ? "Add file" : "Create template",
          tone: "warning",
        });
        if (!ok) return;
      }

      const isActiveTarget = targetBatchId === batchId;

      if (!isActiveTarget) {
        const switched = await handleSwitchBatch(targetBatchId);
        if (switched === false) {
          pushToast({
            id: `single-process-${filename}`,
            tone: "warning",
            message: "Switch cancelled. File was not processed.",
            ttl: 4000,
          });
          return;
        }
      }

      setIsProcessing(true);
      setIsCancelling(false);
      setError(null);
      setProgress({
        batch_id: targetBatchId,
        status: "processing",
        percent: 0,
        files_total: 1,
        files_done: 0,
        current_file: filename,
        current_step: isMerge
          ? `Adding ${filename} to template...`
          : `Creating template from ${filename}...`,
      });
      startPolling(targetBatchId);
      pushToast({
        id: `single-process-${filename}`,
        tone: "info",
        message: isMerge
          ? `Adding ${filename} to current template...`
          : `Creating template from ${filename}...`,
        ttl: 0,
      });

      try {
        await api.process(targetBatchId, {
          sync: true,
          file: filename,
          fileMode: mode,
        });
        stopPolling();
        setProgress((prev) =>
          prev?.batch_id === targetBatchId
            ? {
                ...prev,
                status: "completed",
                percent: 100,
                files_done: 1,
                current_step: "Done",
              }
            : prev,
        );
        setIsProcessing(false);
        const prev = await api.preview(targetBatchId);
        const rev = await api.manualReview(targetBatchId);
        setPreview(prev);
        setReview(rev.items);
        setReviewedKeys(new Set());
        setEdits({});
        setSelectedRowIndex(null);
        setHasExport(false);
        void refreshBatchList();
        void refreshRevisions(targetBatchId);
        const invoices = prev.summary?.invoices_total ?? prev.invoice_count;
        pushToast({
          id: `single-process-${filename}`,
          tone: "success",
          message: isMerge
            ? `Added "${filename}" to current template. Template now has ${invoices} invoice${invoices === 1 ? "" : "s"}.`
            : `Created a new template from "${filename}" with ${invoices} invoice${invoices === 1 ? "" : "s"}.`,
          ttl: 4000,
        });
      } catch (e) {
        const message = getFriendlyErrorMessage(e, "Process file");
        setError(message);
        setProgress((prev) =>
          prev
            ? { ...prev, status: "failed", error_message: message, percent: 100 }
            : null,
        );
        // eslint-disable-next-line no-console
        console.warn("file process failed:", e);
        pushToast({
          id: `single-process-${filename}`,
          tone: "error",
          message,
          ttl: 5000,
        });
      } finally {
        setIsProcessing(false);
        setIsCancelling(false);
        stopPolling();
      }
    },
    [
      batchId,
      editedCellCount,
      handleSwitchBatch,
      isProcessing,
      pushToast,
      refreshBatchList,
      refreshRevisions,
      requestConfirm,
      startPolling,
      stopPolling,
    ],
  );

  // Phase 1N — cancel processing.
  const handleCancel = useCallback(async () => {
    if (!batchId) return;
    if (isCancelling) return;
    // Phase 2E — spec-aligned wording.
    const ok = await requestConfirm({
      title: "Stop processing?",
      message: "Processing will stop at the next safe checkpoint.",
      confirmLabel: "Stop processing",
      cancelLabel: "Continue",
      tone: "danger",
    });
    if (!ok) return;
    setIsCancelling(true);
    try {
      await api.cancelBatch(batchId);
      pushToast({
        tone: "warning",
        message:
          "Stop requested. The current file will finish before processing halts.",
        ttl: 5000,
      });
    } catch (e) {
      setError(getFriendlyErrorMessage(e, "Cancel batch"));
      // eslint-disable-next-line no-console
      console.warn("cancel failed:", e);
      setIsCancelling(false);
    }
  }, [batchId, isCancelling, pushToast, requestConfirm]);

  const handleRefreshPreview = useCallback(async () => {
    if (!batchId) return;
    if (editedCellCount > 0) {
      const ok = await requestConfirm({
        title: "Discard edits and refresh?",
        message: `Refreshing the preview will discard ${editedCellCount} unsaved preview edit${editedCellCount === 1 ? "" : "s"}.`,
        confirmLabel: "Refresh preview",
        tone: "warning",
      });
      if (!ok) return;
    }
    try {
      const prev = await api.preview(batchId);
      const rev = await api.manualReview(batchId);
      setPreview(prev);
      setReview(rev.items);
      setReviewedKeys(new Set());
      setEdits({});
      setSelectedRowIndex(null);
    } catch (e) {
      setError(getFriendlyErrorMessage(e, "Refresh preview"));
      // eslint-disable-next-line no-console
      console.warn("refresh preview failed:", e);
    }
  }, [batchId, editedCellCount, requestConfirm]);

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
      broadcastChannelRef.current?.postMessage({
        type: "cell-edit",
        rowIndex,
        columnKey,
        value: newValue,
        source: "main",
      });
    },
    [preview],
  );

  const handleAddPreviewRow = useCallback(
    (row: PreviewRow, _afterRowIndex?: number, source: "main" | "popout" = "main") => {
      setPreview((prev) => {
        if (!prev) return prev;
        const rows = [...prev.rows];
        rows.push(row);
        return {
          ...prev,
          rows,
          row_count: rows.length,
        };
      });
      if (source === "main") {
        broadcastChannelRef.current?.postMessage({
          type: "row-add",
          row,
          source: "main",
        });
      }
    },
    [],
  );

  const handleResetEdits = useCallback(() => {
    if (editedCellCount === 0) return;
    setEdits({});
  }, [editedCellCount]);

  // Phase 2K — keep the ref pointing at the latest handleCellEdit so
  // the cell-explain "save one-off" closure can call it without a
  // dep-array dance.
  useEffect(() => {
    handleCellEditRef.current = handleCellEdit;
  }, [handleCellEdit]);

  // Phase 2K — toggle a body class while remap mode is active so the
  // CSS banner ("Draw a box…") shows.
  useEffect(() => {
    const active = remapTarget != null && remapDraft == null;
    document.body.classList.toggle("is-remap-active", active);
    if (!active) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setRemapTarget(null);
        setRemapDraft(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.classList.remove("is-remap-active");
    };
  }, [remapTarget, remapDraft]);

  const triggerDownload = useCallback(
    (filename?: string) => {
      if (!batchId) return;
      // Phase 2D — `filename` here is the *on-disk* file selector
      // (used to pick the correct historical workbook when the export
      // dir has multiple). The browser-visible filename comes from the
      // backend's Content-Disposition, which now reads from
      // batch_metadata.export_name. We deliberately do NOT set
      // `a.download` so the operator's chosen export name wins; the
      // browser would otherwise prefer `a.download` over the header.
      const url = filename
        ? `${api.downloadUrl(batchId)}?filename=${encodeURIComponent(filename)}`
        : api.downloadUrl(batchId);
      const a = document.createElement("a");
      a.href = url;
      a.style.display = "none";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
    },
    [batchId],
  );

  // Phase 2C — rename the export workbook display name. Sanitised on
  // the backend; UI only shows the canonical value the API returns.
  const handleRenameExport = useCallback(
    async (newName: string) => {
      if (!batchId) return;
      try {
        const res = await api.updateBatch(batchId, { exportName: newName });
        const saved =
          (res.metadata && (res.metadata as any).export_name) || newName;
        setExportName(saved);
        // Phase 2D — toast wording aligned with the spec ("Export name updated.").
        pushToast({
          tone: "success",
          message: `Export name updated. Workbook will download as “${saved}”.`,
          ttl: 3500,
        });
      } catch (e) {
        pushToast({
          tone: "error",
          message: getFriendlyErrorMessage(e, "Rename export"),
        });
        throw e;
      }
    },
    [batchId, pushToast],
  );

  // Phase 2C — popout windows (read-only). Open a new browser window
  // pointed at /#/popout/template?batch=<id> (or /document?batch=...).
  // The same SPA renders the popout view via PopoutPage.tsx.
  const openPopout = useCallback(
    (kind: "template" | "document", extra: Record<string, string> = {}) => {
      if (!batchId) return;
      const params = new URLSearchParams({ batch: batchId, ...extra });
      const url = `${window.location.origin}${window.location.pathname}#popout/${kind}?${params.toString()}`;
      const features = "popup=yes,width=1200,height=820,resizable=yes,scrollbars=yes";
      const w = window.open(url, `bill-popout-${kind}-${batchId}`, features);
      if (!w) {
        pushToast({
          tone: "error",
          message: "Could not open popout window — check your browser's pop-up blocker.",
        });
      }
    },
    [batchId, pushToast],
  );
  // Detach the Template panel: opens the popout window (read-only
  // companion view), tracks the window handle, and marks the panel
  // detached so the embedded copy hides and the Document panel
  // expands. The popout window's URL is the same as `openPopout` would
  // produce; we replicate the call here because we need the WindowProxy
  // back to track close events.
  const handleDetachTemplate = useCallback(() => {
    if (!batchId) return;
    if (templatePopoutRef.current && !templatePopoutRef.current.closed) {
      try {
        templatePopoutRef.current.focus();
      } catch {
        /* cross-origin focus is allowed for same-origin popouts */
      }
      setTemplateDetached(true);
      return;
    }
    const params = new URLSearchParams({ batch: batchId });
    const url = `${window.location.origin}${window.location.pathname}#popout/template?${params.toString()}`;
    const features = "popup=yes,width=1200,height=820,resizable=yes,scrollbars=yes";
    const w = window.open(url, `bill-popout-template-${batchId}`, features);
    if (!w) {
      pushToast({
        tone: "error",
        message: "Could not open popout window — check your browser's pop-up blocker.",
      });
      return;
    }
    templatePopoutRef.current = w;
    setTemplateDetached(true);
  }, [batchId, pushToast]);

  const handleReattachTemplate = useCallback(() => {
    const w = templatePopoutRef.current;
    if (w && !w.closed) {
      try {
        w.close();
      } catch {
        /* ignore — user may have already closed it */
      }
    }
    templatePopoutRef.current = null;
    setTemplateDetached(false);
  }, []);

  // Watch the popout window: if the user closes it from the OS / tab
  // strip, automatically reattach so the embedded panel comes back.
  useEffect(() => {
    if (!templateDetached) return;
    const id = window.setInterval(() => {
      const w = templatePopoutRef.current;
      if (!w || w.closed) {
        templatePopoutRef.current = null;
        setTemplateDetached(false);
      }
    }, 600);
    return () => window.clearInterval(id);
  }, [templateDetached]);

  // Cross-window row-selection sync — bidirectional. The popout
  // broadcasts a `row-select` message whenever the user picks a row
  // inside the detached template; we mirror that selection here so
  // the Document panel scrolls to the matching bill page. We also
  // rebroadcast our own selection so the popout's table highlights
  // the same row.
  //
  // Indirection via a ref: `handleSelectRow` is declared further down
  // in this component (it depends on values defined below), so the
  // BroadcastChannel handler reaches it through `handleSelectRowRef`
  // which is patched on every render below.
  const broadcastChannelRef = useRef<BroadcastChannel | null>(null);
  const lastBroadcastRowRef = useRef<number | null>(null);
  const handleSelectRowRef = useRef<((idx: number | null) => void) | null>(null);
  useEffect(() => {
    if (!batchId || !templateDetached) {
      broadcastChannelRef.current?.close();
      broadcastChannelRef.current = null;
      return;
    }
    if (typeof BroadcastChannel === "undefined") return;
    const ch = new BroadcastChannel(`bill-popout-sync-${batchId}`);
    broadcastChannelRef.current = ch;
    ch.onmessage = (ev) => {
      const data = ev.data as
        | { type: "row-select"; rowIndex: number | null; source: string }
        | { type: "cell-edit"; rowIndex: number; columnKey: string; value: unknown; source: string }
        | { type: "row-add"; row: PreviewRow; source: string }
        | undefined;
      if (!data || data.source === "main") return;
      if (data.type === "cell-edit") {
        handleCellEditRef.current?.(data.rowIndex, data.columnKey, data.value);
        return;
      }
      if (data.type === "row-add") {
        handleAddPreviewRow(data.row, undefined, "popout");
        return;
      }
      if (data.type !== "row-select") return;
      lastBroadcastRowRef.current = data.rowIndex; // suppress echo
      handleSelectRowRef.current?.(data.rowIndex);
    };
    return () => {
      ch.close();
      if (broadcastChannelRef.current === ch) broadcastChannelRef.current = null;
    };
  }, [batchId, templateDetached]);

  // Mirror our own row selection out to the popout so its table
  // highlights the same row the operator picked here.
  useEffect(() => {
    const ch = broadcastChannelRef.current;
    if (!ch) return;
    if (lastBroadcastRowRef.current === selectedRowIndex) {
      // The selection came in FROM the popout; don't echo it back.
      lastBroadcastRowRef.current = null;
      return;
    }
    ch.postMessage({
      type: "row-select",
      rowIndex: selectedRowIndex,
      source: "main",
    });
  }, [selectedRowIndex]);

  // Backward-compat alias for legacy callers (kept so popout deep-links
  // and any other code paths that still call `handlePopoutTemplate`
  // continue to work).
  const handlePopoutTemplate = handleDetachTemplate;
  const handlePopoutDocument = useCallback(() => {
    if (!selected) {
      pushToast({ tone: "info", message: "Pick a file first to pop out the document viewer." });
      return;
    }
    openPopout("document", { file: selected });
  }, [openPopout, pushToast, selected]);

  // Phase 2C — focus mode for the template. Escape exits.
  const toggleFocusMode = useCallback(() => {
    setFocusModeTemplate((v) => !v);
  }, []);
  useEffect(() => {
    if (!focusModeTemplate) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        // Don't fight modals/inputs — only fire when nothing else owns the key.
        const tgt = e.target as HTMLElement | null;
        if (
          tgt &&
          (tgt.tagName === "INPUT" ||
            tgt.tagName === "TEXTAREA" ||
            tgt.isContentEditable)
        ) {
          return;
        }
        setFocusModeTemplate(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [focusModeTemplate]);

  // Phase 2E — match the backend's `_slug_for_default` (in
  // webapp/backend/api/export.py) byte-for-byte so the title in the
  // template header equals the actual filename the workbook will
  // download as. The earlier formula (`${batchName}.xlsx`) was the
  // root cause of "Richmond 3.xlsx" appearing as a fake title.
  const defaultExportName = (() => {
    const raw = (batchName || "").trim() || "ResMan_Import";
    // 1. Replace whitespace runs with `_` (matches backend `re.sub(r"\s+", "_")`).
    let slug = raw.replace(/\s+/g, "_");
    // 2. Strip Windows-illegal filename characters.
    slug = slug.replace(/[\\/:\*\?"<>\|]+/g, "_");
    // 3. Trim trailing dots/spaces (defensive — backend does this too).
    slug = slug.replace(/^[.\s]+|[.\s]+$/g, "");
    if (!slug) slug = "ResMan_Import";
    return `${slug}_ResMan_Import.xlsx`;
  })();

  // Total page count of the active document (for the breadcrumb).
  const activeDocumentPageCount =
    activeDocumentPage && activeDocumentPage.filename
      ? files.find((f) => f.filename === activeDocumentPage.filename)
          ? // We don't have a direct page count in FileEntry; derive from
            // the preview's document_pages metadata if present.
            (preview?.rows || []).reduce((max, r) => {
              const meta = (r as any)?._meta;
              if (
                meta?.source_file === activeDocumentPage.filename &&
                typeof meta.source_page === "number"
              ) {
                return Math.max(max, meta.source_page);
              }
              return max;
            }, 0) || null
          : null
      : null;

  const handleExport = useCallback(async () => {
    if (!batchId) return;
    if (review.length > 0) {
      pushToast({
        tone: "warning",
        message: `Exporting with ${review.length} unresolved issue${review.length === 1 ? "" : "s"}.`,
        ttl: 6000,
      });
    }
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
      pushToast({
        tone: "success",
        message: `Exported ${exported.length} file${exported.length === 1 ? "" : "s"}${editedLabel}. Download starting…`,
      });
      if (exported.length > 0) {
        const filename = exported[exported.length - 1]?.filename;
        setTimeout(() => triggerDownload(filename), 50);
      }
    } catch (e) {
      setError(getFriendlyErrorMessage(e, "Export batch"));
      // eslint-disable-next-line no-console
      console.warn("export failed:", e);
    } finally {
      setIsExporting(false);
    }
  }, [batchId, edits, editedCellCount, preview, review.length, pushToast, triggerDownload]);

  const handleDownload = useCallback(() => {
    if (!batchId) return;
    triggerDownload();
  }, [batchId, triggerDownload]);

  // Phase 1W — delete any batch by id (used by the picker rows so the
  // operator doesn't have to switch to a batch before deleting it).
  // Confirms with the in-app dialog. If the deleted batch happens to
  // be the active one, the local UI state is cleared so the user
  // doesn't keep editing a batch that no longer exists.
  const handleDeleteBatchById = useCallback(
    async (targetId: string) => {
      const listed = batchList.find((b) => b.batch_id === targetId);
      const friendly = listed?.batch_name?.trim() || targetId;
      const ok = await requestConfirm({
        title: "Delete batch?",
        message: `Delete "${friendly}"? This removes uploaded files, preview data, and exports for this batch on the server.`,
        confirmLabel: "Delete batch",
        tone: "danger",
      });
      if (!ok) return;
      try {
        await api.deleteBatch(targetId);
      } catch (e) {
        pushToast({
          tone: "error",
          message: getFriendlyErrorMessage(e, "Delete batch"),
          ttl: 5000,
        });
        // eslint-disable-next-line no-console
        console.warn("delete batch failed:", e);
        return;
      }
      pushToast({
        tone: "success",
        message: `Deleted "${friendly}".`,
        ttl: 3500,
      });
      // If the active batch was the one we just deleted, drop all the
      // local state attached to it.
      if (targetId === batchId) {
        try {
          localStorage.removeItem(ACTIVE_BATCH_LS_KEY);
        } catch {
          /* non-fatal */
        }
        setBatchId(null);
        setBatchName("");
        setFiles([]);
        setSelected(null);
        setDocumentPageTarget(null, null);
        setPreview(null);
        setReview([]);
        setReviewedKeys(new Set());
        setEdits({});
        setSelectedRowIndex(null);
        setHasExport(false);
        setError(null);
        setProgress(null);
      }
      void refreshBatchList();
    },
    [batchId, batchList, pushToast, refreshBatchList, requestConfirm, setDocumentPageTarget],
  );

  // Phase 1X — inline rename a batch from the explorer (no modal).
  // Throws on backend error so the explorer's input can keep showing
  // the value the operator typed; the toast surfaces the failure.
  const handleInlineRenameBatch = useCallback(
    async (targetId: string, newName: string) => {
      try {
        await api.updateBatch(targetId, { batchName: newName });
        if (targetId === batchId) {
          setBatchName(newName);
        }
        void refreshBatchList();
        pushToast({ tone: "success", message: `Renamed to "${newName}".`, ttl: 3000 });
      } catch (e) {
        pushToast({
          tone: "error",
          message: getFriendlyErrorMessage(e, "Rename batch"),
          ttl: 5000,
        });
        // eslint-disable-next-line no-console
        console.warn("inline rename failed:", e);
        throw e;
      }
    },
    [batchId, pushToast, refreshBatchList],
  );

  // Phase 1X — delete a single uploaded file from the active batch.
  const handleDeleteFile = useCallback(
    async (targetBatchId: string, filename: string): Promise<FileEntry[] | void> => {
      const ok = await requestConfirm({
        title: "Delete file?",
        message: `Remove "${filename}" from this batch?`,
        confirmLabel: "Delete file",
        tone: "danger",
      });
      if (!ok) return;
      try {
        await api.deleteFile(targetBatchId, filename);
      } catch (e) {
        pushToast({
          tone: "error",
          message: getFriendlyErrorMessage(e, "Delete file"),
          ttl: 5000,
        });
        // eslint-disable-next-line no-console
        console.warn("delete file failed:", e);
        return;
      }
      // Refresh local file list. If the deleted file was the active
      // selection, fall back to the next remaining file (or null).
      try {
        const res = await api.listFiles(targetBatchId);
        if (targetBatchId === batchId) {
          setFiles(res.files);
        }
        if (targetBatchId === batchId && selected === filename) {
          const nextSelected = res.files[0]?.filename ?? null;
          setSelected(nextSelected);
          setDocumentPageTarget(targetBatchId, nextSelected, 1);
        }
        pushToast({
          tone: "success",
          message: "File removed from batch.",
          ttl: 3000,
        });
        void refreshBatchList();
        return res.files;
      } catch {
        /* non-fatal — next switch will refresh */
      }
      pushToast({
        tone: "success",
        message: "File removed from batch.",
        ttl: 3000,
      });
      void refreshBatchList();
    },
    [batchId, pushToast, refreshBatchList, requestConfirm, selected, setDocumentPageTarget],
  );

  const handleClear = useCallback(async () => {
    if (batchId) {
      const ok = await requestConfirm({
        title: "Delete batch?",
        message: `Delete "${batchName || batchId}"? This removes uploaded files, preview data, and exports for this batch on the server.`,
        confirmLabel: "Delete batch",
        tone: "danger",
      });
      if (!ok) return;
      try {
        await api.deleteBatch(batchId);
      } catch (e) {
        pushToast({
          tone: "error",
          message: getFriendlyErrorMessage(e, "Delete batch"),
          ttl: 5000,
        });
        // eslint-disable-next-line no-console
        console.warn("delete batch failed:", e);
        return;
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
    setDocumentPageTarget(null, null);
    setPreview(null);
    setReview([]);
    setEdits({});
    setSelectedRowIndex(null);
    setHasExport(false);
    setError(null);
    setProgress(null);
    void refreshBatchList();
  }, [batchId, batchName, refreshBatchList, requestConfirm, setDocumentPageTarget]);

  useEffect(() => {
    api.health().catch((e) => {
      setError(getFriendlyErrorMessage(e, "Backend health"));
      // eslint-disable-next-line no-console
      console.warn("backend health failed:", e);
    });
    void refreshBatchList();
  }, [refreshBatchList]);

  // ---- Phase 1E: rehydrate active batch from localStorage on first load ----
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
        setBatchName(status.batch_name || "Untitled batch");
        // Phase 2I.11 — pull the revisions manifest as part of the
        // restore. Without this, a refreshed page resurrects the batch
        // but leaves `revisions` at its initial [] so the dropdown
        // wrongly shows "No saved revisions yet" until the next switch
        // or processing run.
        void refreshRevisions(status.batch_id);
        setFiles(status.files);
        if (status.files.length > 0) {
          const nextSelected = status.files[0].filename;
          setSelected(nextSelected);
          setDocumentPageTarget(status.batch_id, nextSelected, 1);
        } else {
          setDocumentPageTarget(null, null);
        }
        setHasExport(status.export_available);
        if (status.preview_available) {
          try {
            const prev = await api.preview(status.batch_id);
            const rev = await api.manualReview(status.batch_id);
            if (cancelled) return;
            setPreview(prev);
            setReview(rev.items);
            setReviewedKeys(new Set());
          } catch (e) {
            if (!cancelled) {
              setError(getFriendlyErrorMessage(e, "Restore preview"));
              // eslint-disable-next-line no-console
              console.warn("restore preview failed:", e);
            }
          }
        }
        const summaryParts: string[] = [
          `Restored "${status.batch_name || "Untitled batch"}"`,
        ];
        if (status.files_total) summaryParts.push(`${status.files_total} file(s)`);
        if (status.preview_available) summaryParts.push("preview available");
        if (status.export_available) summaryParts.push("export available");
        pushToast({
          tone: "info",
          message: summaryParts.join(" · ") + ".",
        });
      } catch (e) {
        try {
          localStorage.removeItem(ACTIVE_BATCH_LS_KEY);
        } catch {
          /* non-fatal */
        }
        if (!cancelled) {
          if (isApiError(e) && (e.status === 404 || e.status === 400)) {
            pushToast({
              tone: "info",
              message: "Previous batch was no longer available.",
            });
          } else {
            setError(getFriendlyErrorMessage(e, "Restore previous batch"));
            // eslint-disable-next-line no-console
            console.warn("restore previous batch failed:", e);
          }
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [pushToast, setDocumentPageTarget, refreshRevisions]);

  // ---- Global drag/drop guard --------------------------------------------
  useEffect(() => {
    const isInsideDropzone = (e: DragEvent): boolean => {
      const t = e.target as HTMLElement | null;
      if (!t) return false;
      return !!t.closest('[data-dropzone="true"]');
    };

    const handler = (e: DragEvent) => {
      const hasFiles = Array.from(e.dataTransfer?.types ?? []).includes("Files");
      if (!hasFiles) return;
      if (isInsideDropzone(e)) return;
      e.preventDefault();
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

  // When the user selects an issue from the inspector, switch to the
  // related file in the document workspace.
  const handleSelectFile = useCallback((filename: string) => {
    setSelected(filename);
    setDocumentPageTarget(batchId, filename, 1);
  }, [batchId, setDocumentPageTarget]);

  const handleSelectExplorerFile = useCallback(
    async (targetBatchId: string, filename: string) => {
      if (targetBatchId !== batchId) {
        const switched = await handleSwitchBatch(targetBatchId);
        if (switched === false) return;
      }
      setSelected(filename);
      setDocumentPageTarget(targetBatchId, filename, 1);
    },
    [batchId, handleSwitchBatch, setDocumentPageTarget],
  );

  const handleSelectExplorerPage = useCallback(
    async (targetBatchId: string, filename: string, pageNumber: number) => {
      if (targetBatchId !== batchId) {
        const switched = await handleSwitchBatch(targetBatchId);
        if (switched === false) return;
      }
      setSelected(filename);
      setDocumentPageTarget(targetBatchId, filename, pageNumber);
    },
    [batchId, handleSwitchBatch, setDocumentPageTarget],
  );

  const handleDocumentActivePageChange = useCallback(
    (page: ActiveDocumentPage) => {
      setActiveDocumentPage(page);
    },
    [],
  );

  const handleSelectRow = useCallback(
    (rowIndex: number | null) => {
      setSelectedRowIndex(rowIndex);
      if (rowIndex != null) {
        setInspectorTab("row");
        const ref = rowDocumentRef(preview?.rows[rowIndex]);
        if (ref && batchId) {
          setSelected(ref.filename);
          setDocumentPageTarget(batchId, ref.filename, ref.pageNumber);
        }
      }
    },
    [batchId, preview?.rows, setDocumentPageTarget],
  );
  // Keep the BroadcastChannel handler's view of `handleSelectRow`
  // up to date (it's referenced from a useEffect declared earlier).
  handleSelectRowRef.current = handleSelectRow;

  const batchesVisible =
    activeModule === "batches" &&
    !closedPanels.has("batches") &&
    !minimizedPanels.has("batches") &&
    (!maximizedPanel || maximizedPanel === "batches");
  const documentVisible =
    activeModule === "batches" &&
    !closedPanels.has("document") &&
    !minimizedPanels.has("document") &&
    (!maximizedPanel || maximizedPanel === "document");
  const templateVisible =
    activeModule === "batches" &&
    !closedPanels.has("template") &&
    !minimizedPanels.has("template") &&
    (!maximizedPanel || maximizedPanel === "template");
  const anyWorkspacePanelVisible = batchesVisible || documentVisible || templateVisible;

  return (
    <div className="app">
      <div className="topbar">
        <div className="topbar-brand">
          <button
            type="button"
            className={`shell-sidebar-toggle ${navCollapsed ? "is-collapsed" : ""}`}
            onClick={() => setNavCollapsed((v) => !v)}
            title={navCollapsed ? "Open sidebar" : "Hide sidebar"}
            aria-label={navCollapsed ? "Open sidebar" : "Hide sidebar"}
            data-testid="nav-rail-toggle"
          >
            <SidebarToggleIcon />
          </button>
        </div>
        <WorkflowSteps
          fileCount={files.length}
          isProcessing={isProcessing}
          invoiceCount={preview?.invoice_count ?? 0}
          manualReviewCount={review.length}
          hasExport={hasExport}
        />
        <div className="topbar-command-area desktop-menu-bar" aria-label="Application menus">
          <DesktopMenu
            label="File"
            items={[
              { label: "New Batch", shortcut: "Ctrl+N", onSelect: handleCreateNewBatch },
              {
                label: isProcessing ? "Processing..." : "Process Batch",
                disabled: !batchId || files.length === 0 || isProcessing,
                onSelect: () => void handleProcess(),
              },
              {
                label: isExporting ? "Exporting..." : "Export Template",
                disabled: !batchId || !preview || isExporting,
                onSelect: () => void handleExport(),
              },
              { kind: "separator" },
              {
                label: "Download Last Export",
                disabled: !batchId || !hasExport,
                onSelect: handleDownload,
              },
            ]}
          />
          <DesktopMenu
            label="Edit"
            items={[
              {
                label: "Rename Batch",
                disabled: !batchId,
                onSelect: handleRenameBatch,
              },
              {
                label: "Reset Template Edits",
                disabled: editedCellCount === 0,
                onSelect: handleResetEdits,
              },
              { kind: "separator" },
              {
                label: "Settings",
                shortcut: "Ctrl+,",
                onSelect: () => setSettingsOpen(true),
              },
            ]}
          />
          <DesktopMenu
            label="View"
            items={[
              {
                label: "Batch Workspace",
                checked: activeModule === "batches",
                onSelect: () => setActiveModule("batches"),
              },
              { kind: "separator" },
              { label: "Settings", shortcut: "Ctrl+,", onSelect: () => setSettingsOpen(true) },
              { kind: "separator" },
              { label: "Restore Panels", onSelect: restoreAllPanels },
              { label: "Minimize All Panels", onSelect: minimizeAllPanels },
            ]}
          />
          {activeModule === "batches" && (
            <WindowsMenu
              closedPanels={closedPanels}
              minimizedPanels={minimizedPanels}
              onRestorePanel={restorePanel}
              onClosePanel={closePanel}
              onRestoreAll={restoreAllPanels}
              onMinimizeAll={minimizeAllPanels}
            />
          )}
          <DesktopMenu
            label="Help"
            items={[
              {
                label: "About Web Console",
                onSelect: () =>
                  pushToast({
                    tone: "info",
                    message: "Billing Refactoring Web Console",
                    ttl: 3000,
                  }),
              },
              {
                label: "Route verifier: scripts\\verify_backend_routes.py",
                disabled: true,
              },
            ]}
          />
        </div>
        <div className="topbar-actions">
          {review.length > 0 && (
            <IssuesPill
              count={review.length}
              hasErrors={review.some((r) =>
                r.reasons.some((reason) =>
                  /fail|error|missing.*total|total_mismatch|not_found|invalid/i.test(
                    reason,
                  ),
                ),
              )}
              onClick={() => setIssuesOpen((v) => !v)}
            />
          )}
          <AiFallbackStatusBadge />
        </div>
      </div>

      <div
        className={`layout ${isSwitchingBatch ? "switching-batch" : ""} module-${activeModule} ${
          focusModeTemplate && activeModule === "batches" ? "focus-mode-template" : ""
        } ${maximizedPanel ? `module-max-${maximizedPanel}` : ""} ${
          closedPanels.has("batches") ? "panel-closed-batches" : ""
        } ${
          closedPanels.has("document") ? "panel-closed-document" : ""
        } ${
          closedPanels.has("template") ? "panel-closed-template" : ""
        } ${
          minimizedPanels.has("batches") ? "panel-minimized-batches" : ""
        } ${
          minimizedPanels.has("document") ? "panel-minimized-document" : ""
        } ${
          minimizedPanels.has("template") ? "panel-minimized-template" : ""
        } ${templateDetached ? "template-detached" : ""}`}
      >
        <NavRail
          active={activeModule}
          onSelect={setActiveModule}
          collapsed={navCollapsed}
        />
        {/* The original batch workspace JSX below is hidden via CSS when
            activeModule is not "batches"; this avoids reshuffling thousands of
            lines and keeps batch-related effects/state intact for instant
            switch-back. */}

        {batchesVisible && (
          <aside
            className="file-sidebar"
            style={{ width: sidebarPanel.size }}
            aria-label="File sidebar"
            data-testid="panel-batches"
          >
            <div className="file-sidebar-card">
              <div className="file-sidebar-header">
                <div className="file-sidebar-title">Batches</div>
                {isProcessing && (
                  <button
                    type="button"
                    className="file-sidebar-stop-btn"
                    disabled={isCancelling}
                    onClick={handleCancel}
                    title={
                      isCancelling
                        ? "Cancellation already requested"
                        : "Stop this batch at the next safe checkpoint"
                    }
                  >
                    <span aria-hidden />
                    {isCancelling ? "Stopping" : "Stop"}
                  </button>
                )}
              </div>

              {/* Phase 2I.14 — the floating ProgressBar card was
                  retired. Live progress now lives directly on each
                  batch folder header and file row inside BatchExplorer
                  so the operator sees per-file progress in place
                  instead of a card pushing the list down. */}
              <BatchExplorer
                batchList={batchList}
                activeBatchId={batchId}
                onSwitchBatch={handleSwitchBatch}
                onCreateBatch={handleSubmitCreateBatch}
                createRequestToken={createBatchRequestToken}
                onRenameBatch={handleInlineRenameBatch}
                onDeleteBatch={handleDeleteBatchById}
                onRefreshBatchList={() => void refreshBatchList()}
                files={files}
                selectedFile={selected}
                activeDocumentPage={activeDocumentPage}
                onSelectFile={handleSelectExplorerFile}
                onSelectPage={handleSelectExplorerPage}
                onDeleteFile={handleDeleteFile}
                onUploadFiles={handleFiles}
                onUploadFilesToBatch={handleFilesForBatch}
                uploadItems={uploadItems}
                onProcessBatch={handleProcessBatch}
                onProcessFile={handleProcessFile}
                processingBatchId={isProcessing ? progress?.batch_id ?? batchId : null}
                isProcessing={isProcessing}
                isSwitchingBatch={isSwitchingBatch}
                queueStatus={queueStatus}
                progress={progress}
              />
            </div>
          </aside>
        )}
        {batchesVisible && (documentVisible || templateVisible) && (
          <div
            className="resizer resizer-h resizer-sidebar"
            {...sidebarPanel.dragHandleProps}
          />
        )}

        {documentVisible && (
          <section
            className="document-pane"
            style={{ width: documentPanel.size }}
            aria-label="Document workspace"
            data-testid="panel-document"
          >
            <DocumentPreviewPanel
              batchId={batchId}
              filename={selected}
              targetPage={
                documentTarget &&
                documentTarget.batchId === batchId &&
                documentTarget.filename === selected
                  ? documentTarget
                  : null
              }
              onActivePageChange={handleDocumentActivePageChange}
              // Window controls removed from the Document panel per UX
              // directive. We still pass `onPopout` so any internal
              // shortcuts that need it can call out, but no button is
              // rendered for it in the header.
              onPopout={handlePopoutDocument}
              highlightedTraceIds={selectedRowTraceIds}
              onTraceClick={handleTraceClick}
              remapActive={remapTarget != null && remapDraft == null}
              onRemapDrawn={handleRemapDrawn}
              aiProgress={progress}
            />
          </section>
        )}
        {documentVisible && templateVisible && !templateDetached && (
          <div
            className="resizer resizer-h resizer-document"
            {...documentPanel.dragHandleProps}
          />
        )}

        {/* Template workspace fills the remaining width. While detached
            into a popout window, the embedded copy hides so the
            Document panel grows to fill the freed space. */}
        {templateVisible && !templateDetached && (
        <main className="template-and-inspector" data-testid="panel-template">
          {error && <div className="error-banner">{error}</div>}

          <div className="template-area">
            <TemplateWorkspace
              preview={preview}
              edits={edits}
              onCellEdit={handleCellEdit}
              onAddPreviewRow={handleAddPreviewRow}
              batchId={batchId}
              onAiMappingApplied={handleAiMappingApplied}
              fileCount={files.length}
              selectedRowIndex={selectedRowIndex}
              activeDocumentPage={activeDocumentPage}
              activeDocumentPageCount={activeDocumentPageCount}
              onSelectRow={handleSelectRow}
              onExport={handleExport}
              isExporting={isExporting}
              hasExport={hasExport}
              isSwitchingBatch={isSwitchingBatch}
              loadingBatchName={loadingBatchName}
              isProcessing={isProcessing}
              isCancelling={isCancelling}
              progress={progress}
              onCancel={handleCancel}
              batchName={batchName}
              vendorLabel={vendorLabel}
              exportName={exportName}
              defaultExportName={defaultExportName}
              onRenameExport={handleRenameExport}
              focusMode={focusModeTemplate}
              onToggleFocusMode={toggleFocusMode}
              onPopoutTemplate={handlePopoutTemplate}
              onPopoutDocument={handlePopoutDocument}
              // Detach / reattach are the ONLY window controls on the
              // Template panel. Everything else (minimize / maximize /
              // close) was removed per UX directive.
              isDetached={templateDetached}
              onDetach={handleDetachTemplate}
              onReattach={handleReattachTemplate}
              revisions={revisions}
              currentRevisionId={currentRevisionId}
              onActivateRevision={handleActivateRevision}
              onDeleteRevision={handleDeleteRevision}
              onSaveEdits={handleSaveEdits}
              isSavingEdits={isSavingEdits}
              selectedColumnKey={selectedColumnKey}
              onSelectCell={handleSelectCell}
              onCellContextMenu={handleCellContextMenu}
            />
          </div>
        </main>
        )}

        {activeModule === "batches" && !anyWorkspacePanelVisible && (
          <div className="workspace-empty-state" data-testid="workspace-empty-state">
            <div className="workspace-empty-card">
              <div className="workspace-empty-title">Workspace minimized</div>
              <div className="workspace-empty-text">
                Restore a panel from the dock or open it from Windows.
              </div>
            </div>
          </div>
        )}

        {activeModule === "batches" && minimizedPanels.size > 0 && (
          <WorkspaceDock
            minimizedPanels={minimizedPanels}
            onRestorePanel={restorePanel}
          />
        )}

        {/* Phase 1U — global blur overlay removed. Each panel renders
            its own local skeleton / overlay (see FileList +
            TemplateWorkspace + DocumentPreviewPanel). A small
            non-blocking toast already announces the switch. */}
      </div>

      {/* Phase 1L — Issues drawer (replaces the fixed inspector pane) */}
      <IssuesDrawer
        open={issuesOpen}
        onClose={() => setIssuesOpen(false)}
        items={review}
        rows={preview?.rows ?? []}
        selectedRowIndex={selectedRowIndex}
        onSelectRow={handleSelectRow}
        onSelectFile={handleSelectFile}
        activeTab={inspectorTab}
        onTabChange={setInspectorTab}
        reviewedKeys={reviewedKeys}
        onToggleReviewed={toggleReviewed}
      />

      {/* Phase 1P — app-native rename batch modal (replaces window.prompt) */}
      <RenameBatchModal
        open={showRenameDialog}
        initialName={batchName}
        onCancel={() => setShowRenameDialog(false)}
        onSubmit={handleSubmitRename}
      />

      <SettingsDialog
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        pushToast={pushToast}
        requestConfirm={requestConfirm}
      />

      <ConfirmDialog
        open={confirmDialog !== null}
        title={confirmDialog?.title ?? ""}
        message={confirmDialog?.message ?? ""}
        confirmLabel={confirmDialog?.confirmLabel ?? "Continue"}
        cancelLabel={confirmDialog?.cancelLabel}
        tone={confirmDialog?.tone}
        onCancel={() => resolveConfirm(false)}
        onConfirm={() => resolveConfirm(true)}
      />

      {/* Phase 1K — global toast queue (replaces in-page banners) */}
      <Toasts toasts={toasts} onDismiss={dismissToast} />

      {/* Phase 2K — cell-level affordances (right-click menu, explain
          modal, remap scope chooser). Mounted at the App root so they
          float over everything; state is owned here. */}
      {cellMenu && (
        <CellContextMenu
          x={cellMenu.x}
          y={cellMenu.y}
          onExplain={() =>
            void openExplainForCell(cellMenu.rowIndex, cellMenu.column)
          }
          onShowTrace={() => {
            // Selecting cell already narrows the trace overlay; the
            // user just wants the document panel open. Best-effort
            // navigate to the row's source page.
            const row = preview?.rows[cellMenu.rowIndex];
            const meta = (row?._meta as any) || {};
            if (meta.source_file) {
              setDocumentPageTarget(
                batchId,
                meta.source_file,
                meta.source_page || 1,
              );
            }
          }}
          onEditValue={() => {
            // Opens the same explain modal but pre-positioned on the
            // edit field. (Modal is the canonical edit surface now.)
            void openExplainForCell(cellMenu.rowIndex, cellMenu.column);
          }}
          onRemapSource={async () => {
            // Need the vendor key — fetch the explain payload first
            // (cheap and provides everything we need to start remap).
            try {
              const ex = await api.explainCell(
                batchId!,
                cellMenu.rowIndex,
                cellMenu.column,
              );
              handleStartRemap(
                cellMenu.rowIndex,
                cellMenu.column,
                ex.vendor_key,
              );
              // Make sure the document panel is open and shows the
              // right page.
              if (ex.source_file) {
                setDocumentPageTarget(
                  batchId,
                  ex.source_file,
                  ex.source_page || 1,
                );
              }
              pushToast({
                tone: "info",
                message:
                  "Remap mode: draw a box on the document around the correct region.",
                ttl: 6000,
              });
            } catch (e) {
              pushToast({
                tone: "error",
                message: getFriendlyErrorMessage(e, "Start remap"),
              });
            }
          }}
          onClose={() => setCellMenu(null)}
        />
      )}
      {cellExplain && (
        <CellExplainModal
          explain={cellExplain}
          onClose={() => setCellExplain(null)}
          onTeach={handleCellExplainSave}
          onRemap={() => {
            handleStartRemap(
              cellExplain.row_index,
              cellExplain.column,
              cellExplain.vendor_key,
            );
            if (cellExplain.source_file) {
              setDocumentPageTarget(
                batchId,
                cellExplain.source_file,
                cellExplain.source_page || 1,
              );
            }
            pushToast({
              tone: "info",
              message:
                "Remap mode: draw a box on the document around the correct region.",
              ttl: 6000,
            });
          }}
        />
      )}
      {remapDraft && (
        <RemapScopeChooser
          vendorKey={remapDraft.vendorKey}
          onConfirm={handleRemapConfirm}
          onCancel={() => {
            setRemapDraft(null);
            setRemapTarget(null);
          }}
        />
      )}
    </div>
  );
}

function WorkspaceDock({
  minimizedPanels,
  onRestorePanel,
}: {
  minimizedPanels: Set<PanelKey>;
  onRestorePanel: (panel: PanelKey) => void;
}) {
  const ordered: PanelKey[] = ["batches", "document", "template"];
  const items = ordered.filter((panel) => minimizedPanels.has(panel));
  if (items.length === 0) return null;
  return (
    <div className="workspace-dock" role="toolbar" aria-label="Minimized panels" data-testid="workspace-dock">
      {items.map((panel) => (
        <button
          key={panel}
          type="button"
          className="workspace-dock-item"
          onClick={() => onRestorePanel(panel)}
          title={`Restore ${panelLabel(panel)}`}
          data-testid={`workspace-dock-${panel}`}
        >
          <span className="workspace-dock-icon" aria-hidden>
            <DockPanelIcon panel={panel} />
          </span>
          <span>{panelLabel(panel)}</span>
        </button>
      ))}
    </div>
  );
}

function panelLabel(panel: PanelKey): string {
  switch (panel) {
    case "batches":
      return "Batches";
    case "document":
      return "Document Viewer";
    case "template":
      return "Template";
  }
}

function SidebarToggleIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 18 18"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="2.5" y="2.5" width="13" height="13" rx="3.2" />
      <line x1="7" y1="4.9" x2="7" y2="13.1" />
    </svg>
  );
}

function DockPanelIcon({ panel }: { panel: PanelKey }) {
  if (panel === "batches") {
    return (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 7h6l2 2h10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
      </svg>
    );
  }
  if (panel === "document") {
    return (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
        <path d="M14 2v6h6" />
      </svg>
    );
  }
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4 6h16" />
      <path d="M4 12h16" />
      <path d="M4 18h10" />
    </svg>
  );
}
