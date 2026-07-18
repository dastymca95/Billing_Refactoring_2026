import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  lazy,
  Suspense,
  type CSSProperties,
} from "react";
import { createPortal } from "react-dom";

import {
  api,
  getFriendlyErrorMessage,
  isApiError,
  type UploadFileResponse,
} from "./api";
import { AiFallbackStatusBadge } from "./components/AiFallbackStatusBadge";
import { BatchExplorer } from "./components/BatchExplorer";
import { BatchSelectorDropdown } from "./components/BatchSelectorDropdown";
import { BillingV2 } from "./features/billing-v2/BillingV2";
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
import { perfStart } from "./perf";
import type { CellEdits } from "./components/ResManTemplatePreview";
import type {
  BatchListEntry,
  BatchProgress,
  DocumentMode,
  FileEntry,
  HumanAdjudicationOptions,
  ManualReviewItem,
  PreviewResponse,
  PreviewRow,
  UploadFileProgress,
} from "./types";

// localStorage key used to remember the active batch across page refreshes.
const ACTIVE_BATCH_LS_KEY = "billing_refactoring_active_batch_id";
const NAV_COLLAPSED_LS_KEY = "billing_refactoring_nav_collapsed";
const DOCUMENT_ATTACHMENT_GROUPS_LS_KEY =
  "billing_refactoring_document_attachment_groups_v1";
const BATCH_NAME_MAX = 80;

// How often the frontend polls /progress while processing. Phase 1O —
// dropped from 750 ms to 500 ms so the bar moves visibly with every
// per-page progress update from the backend OCR loop. Fast enough to
// feel live, slow enough that a long batch doesn't hammer the API.
const PROGRESS_POLL_MS = 500;
const UPLOAD_PARALLEL_LIMIT = 4;
const MASS_UPLOAD_THRESHOLD = 100;
const MASS_UPLOAD_PARALLEL_LIMIT = 3;
const UPLOAD_STATE_FLUSH_MS = 90;
const UPLOADED_FILE_FLUSH_MS = 220;
const UPLOAD_RESULT_LINGER_MS = 2600;
const APPENDABLE_OPEN_DOCUMENT_EXTENSIONS = new Set([
  "pdf",
  "png",
  "jpg",
  "jpeg",
  "gif",
  "bmp",
  "webp",
]);

// Maximum total time we'll wait for a background processing run before
// showing a "still working" message. The poll never auto-aborts.
const MAX_PROCESSING_WAIT_MS = 15 * 60 * 1000;
const PREVIEW_READY_RETRY_ATTEMPTS = 10;
const PREVIEW_READY_RETRY_BASE_MS = 250;
const DOCUMENT_NAVIGATION_LOCK_MS = 5000;
const DOCUMENT_NAVIGATION_SETTLE_LOCK_MS = 1200;
const DOCUMENT_POPOUT_FEATURES =
  "popup=yes,width=1180,height=900,resizable=yes,scrollbars=no";

const FloatingAccountingAssistant = lazy(() =>
  import("./components/AccountingAssistantWorkspace").then((module) => ({
    default: module.FloatingAccountingAssistant,
  })),
);
const AccountingRulesWorkspace = lazy(() =>
  import("./components/AccountingRulesWorkspace").then((module) => ({
    default: module.AccountingRulesWorkspace,
  })),
);
const ResManContextWorkspace = lazy(() =>
  import("./components/ResManContextWorkspace").then((module) => ({
    default: module.ResManContextWorkspace,
  })),
);
const ContextIntelligenceWorkspace = lazy(() =>
  import("./components/ContextIntelligenceWorkspace").then((module) => ({
    default: module.ContextIntelligenceWorkspace,
  })),
);

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
type DocumentAttachmentGroups = Record<string, Record<string, string[]>>;
type DocumentNavigationLock = ActiveDocumentPage & { expiresAt: number };
type RowNavigationGuard = { rowIndex: number; expiresAt: number };

function prepareDetachedDocumentWindow(win: Window): HTMLElement {
  const targetDoc = win.document;
  targetDoc.open();
  targetDoc.write("<!doctype html><html><head></head><body></body></html>");
  targetDoc.close();
  targetDoc.title = "Document viewer";
  targetDoc.body.className = "detached-document-window";

  const base = targetDoc.createElement("base");
  base.href = window.location.href.split("#")[0];
  targetDoc.head.appendChild(base);

  for (const node of Array.from(document.head.children)) {
    const tag = node.tagName.toLowerCase();
    const rel =
      tag === "link"
        ? (node as HTMLLinkElement).rel?.toLowerCase()
        : "";
    if (tag === "style" || (tag === "link" && rel.includes("stylesheet"))) {
      targetDoc.head.appendChild(node.cloneNode(true));
    }
  }

  const runtimeStyle = targetDoc.createElement("style");
  runtimeStyle.textContent = `
    html,
    body,
    #detached-document-root {
      width: 100%;
      height: 100%;
      margin: 0;
      overflow: hidden;
      background: #dcecff;
    }
    body.detached-document-window {
      min-width: 720px;
    }
    body.detached-document-window .doc-preview-card {
      width: 100vw;
      height: 100vh;
      border-radius: 0;
      border: 0;
      box-shadow: none;
    }
    body.detached-document-window .doc-preview-body {
      min-height: 0;
      height: calc(100vh - 36px);
    }
  `;
  targetDoc.head.appendChild(runtimeStyle);

  const root = targetDoc.createElement("div");
  root.id = "detached-document-root";
  targetDoc.body.appendChild(root);
  return root;
}

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

function findRowIndexForDocumentPage(
  rows: PreviewRow[] | undefined,
  page: ActiveDocumentPage | null,
): number {
  if (!rows || !page?.filename) return -1;
  const targetFile = page.filename.trim();
  const targetPage = Math.max(1, Math.floor(page.pageNumber || 1));
  if (!targetFile) return -1;

  const rowFile = (row: PreviewRow) =>
    typeof row._meta?.source_file === "string" ? row._meta.source_file.trim() : "";
  const rowPage = (row: PreviewRow) => {
    const raw = row._meta?.source_page;
    const n = Number(raw);
    return Number.isFinite(n) && n > 0 ? Math.floor(n) : 1;
  };

  const exactIndex = rows.findIndex(
    (row) => rowFile(row) === targetFile && rowPage(row) === targetPage,
  );
  if (exactIndex >= 0) return exactIndex;

  return rows.findIndex((row) => rowFile(row) === targetFile);
}

function sameDocumentPage(a: ActiveDocumentPage, b: ActiveDocumentPage): boolean {
  return (
    a.batchId === b.batchId &&
    a.filename === b.filename &&
    Math.max(1, Math.floor(a.pageNumber || 1)) ===
      Math.max(1, Math.floor(b.pageNumber || 1))
  );
}

function extensionFromUploadName(name: string): string {
  const match = /\.([A-Za-z0-9]+)$/.exec(name || "");
  return match ? match[1].toLowerCase() : "";
}

const VIEWER_IMAGE_UPLOAD_EXTENSIONS = new Set([
  "png",
  "jpg",
  "jpeg",
  "webp",
  "gif",
  "bmp",
]);

function shouldUploadAsViewerPdf(file: File): boolean {
  const extension = extensionFromUploadName(file.name);
  if (VIEWER_IMAGE_UPLOAD_EXTENSIONS.has(extension)) return true;
  return Boolean(file.type && file.type.toLowerCase().startsWith("image/"));
}

function ModuleLoading({ label }: { label: string }) {
  return <main className="assistant-module-shell" aria-busy="true">
    <div className="assistant-module-header"><strong>{label}…</strong></div>
  </main>;
}

function uploadItemPercent(progressPercent: number): number {
  if (!Number.isFinite(progressPercent)) return 0;
  return Math.max(1, Math.min(95, Math.round(progressPercent)));
}

function apiErrorDetailText(error: unknown): string {
  if (!isApiError(error)) return "";
  if (typeof error.detail === "string") return error.detail;
  if (
    error.detail &&
    typeof error.detail === "object" &&
    "message" in error.detail &&
    typeof (error.detail as { message?: unknown }).message === "string"
  ) {
    return String((error.detail as { message: string }).message);
  }
  return error.message || "";
}

function isPreviewStillPreparingError(error: unknown): boolean {
  if (!isApiError(error) || error.status !== 404) return false;
  const detail = apiErrorDetailText(error);
  if (/batch not found/i.test(detail)) return false;
  return /no preview|manual-review data|run process/i.test(detail);
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function uploadWorkerLimit(fileCount: number): number {
  if (fileCount >= MASS_UPLOAD_THRESHOLD) return MASS_UPLOAD_PARALLEL_LIMIT;
  return UPLOAD_PARALLEL_LIMIT;
}

function isTransientUploadError(error: unknown): boolean {
  if (isApiError(error)) return error.status >= 500 || error.status === 0;
  return error instanceof TypeError;
}

async function uploadFileWithRetry(
  batchId: string,
  file: File,
  onProgress: (progress: { loaded: number; total: number; percent: number }) => void,
  opts: { asPdf?: boolean } = {},
): Promise<UploadFileResponse> {
  const attempts = file.size > 8 * 1024 * 1024 ? 2 : 3;
  let lastError: unknown = null;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      return await api.uploadFile(batchId, file, onProgress, opts);
    } catch (error) {
      lastError = error;
      if (attempt >= attempts || !isTransientUploadError(error)) break;
      await delay(350 * attempt + Math.min(900, file.size / 1024 / 1024) * 60);
    }
  }
  throw lastError;
}

function optimisticFileEntry(uploaded: UploadFileResponse, fallback: File): FileEntry {
  const filename = uploaded.filename || fallback.name || "uploaded";
  const extension =
    uploaded.extension ||
    (extensionFromUploadName(filename)
      ? `.${extensionFromUploadName(filename)}`
      : "");
  return {
    filename,
    size_bytes: uploaded.size_bytes ?? fallback.size,
    extension,
    page_count: uploaded.page_count ?? null,
    vendor_key: "unknown",
    vendor_confidence: 0,
    vendor_detection_reason: "Detection pending after upload.",
    supported_in_phase_1: false,
    source_type: "uploaded",
    file_support_status: "pending",
    file_support_label: "Pending detection",
    file_support_reason: "The file is uploaded; metadata is still refreshing.",
  };
}

function upsertFileEntry(previous: FileEntry[], entry: FileEntry): FileEntry[] {
  const index = previous.findIndex((file) => file.filename === entry.filename);
  if (index < 0) return [...previous, entry];
  const next = previous.slice();
  next[index] = { ...previous[index], ...entry };
  return next;
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

function normalizeDocumentAttachmentGroups(value: unknown): DocumentAttachmentGroups {
  if (!value || typeof value !== "object") return {};
  const normalized: DocumentAttachmentGroups = {};
  for (const [batchId, rawGroup] of Object.entries(value as Record<string, unknown>)) {
    if (!batchId || !rawGroup || typeof rawGroup !== "object") continue;
    const group: Record<string, string[]> = {};
    for (const [parentName, rawChildren] of Object.entries(
      rawGroup as Record<string, unknown>,
    )) {
      if (!parentName || !Array.isArray(rawChildren)) continue;
      const children = Array.from(
        new Set(
          rawChildren.filter(
            (child): child is string => typeof child === "string" && child !== parentName,
          ),
        ),
      );
      if (children.length > 0) group[parentName] = children;
    }
    if (Object.keys(group).length > 0) normalized[batchId] = group;
  }
  return normalized;
}

function loadDocumentAttachmentGroups(): DocumentAttachmentGroups {
  try {
    return normalizeDocumentAttachmentGroups(
      JSON.parse(localStorage.getItem(DOCUMENT_ATTACHMENT_GROUPS_LS_KEY) || "{}"),
    );
  } catch {
    return {};
  }
}

function resolveDocumentGroupParent(
  groups: DocumentAttachmentGroups,
  batchId: string | null,
  selectedFilename: string | null,
): string | null {
  if (!batchId || !selectedFilename) return null;
  const group = groups[batchId] || {};
  if (group[selectedFilename]) return selectedFilename;
  const owner = Object.entries(group).find(([, children]) =>
    children.includes(selectedFilename),
  );
  return owner?.[0] || selectedFilename;
}

function fileExtensionForEntryOrName(
  files: FileEntry[],
  filename: string | null | undefined,
): string {
  if (!filename) return "";
  const entry = files.find((file) => file.filename === filename);
  const raw = entry?.extension || extensionFromUploadName(filename);
  return raw.replace(/^\./, "").toLowerCase();
}

function isSupplementalDocumentCandidate(file: FileEntry): boolean {
  const ext = (file.extension || "").replace(/^\./, "").toLowerCase();
  if (["png", "jpg", "jpeg", "webp", "gif", "bmp"].includes(ext)) return true;
  const filename = file.filename.toLowerCase();
  if (/(screenshot|screen shot|paystub|pay stub|receipt|support|attachment)/.test(filename)) {
    return true;
  }
  const label = (file.file_support_label || "").toLowerCase();
  const status = (file.file_support_status || "").toLowerCase();
  if (label.includes("screenshot")) return true;
  return status === "needs_review" || status === "needs review";
}

export default function App() {
  // Top-level workspace stays focused on batches. Rules, fallback behavior,
  // and output text rules now live in Settings.
  const [activeModule, setActiveModule] = useState<AppModule>("billing-v2");
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
  const uploadItemPatchQueueRef = useRef<Map<string, Partial<UploadFileProgress>>>(
    new Map(),
  );
  const uploadItemPatchTimerRef = useRef<number | null>(null);
  const uploadedFileQueueRef = useRef<FileEntry[]>([]);
  const uploadedFileFlushTimerRef = useRef<number | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [documentRefreshToken, setDocumentRefreshToken] = useState(0);
  const [documentAttachmentGroups, setDocumentAttachmentGroups] =
    useState<DocumentAttachmentGroups>(() => loadDocumentAttachmentGroups());
  const [documentTarget, setDocumentTarget] =
    useState<DocumentNavTarget | null>(null);
  const [activeDocumentPage, setActiveDocumentPage] =
    useState<ActiveDocumentPage | null>(null);
  const navNonceRef = useRef(0);
  const documentNavigationLockRef = useRef<DocumentNavigationLock | null>(null);
  const rowNavigationGuardRef = useRef<RowNavigationGuard | null>(null);

  useEffect(() => {
    filesRef.current = files;
  }, [files]);

  useEffect(() => {
    try {
      localStorage.setItem(
        DOCUMENT_ATTACHMENT_GROUPS_LS_KEY,
        JSON.stringify(documentAttachmentGroups),
      );
    } catch {
      /* localStorage unavailable; grouping remains in session state */
    }
  }, [documentAttachmentGroups]);

  useEffect(() => {
    if (!batchId) return;
    const existingNames = new Set(files.map((file) => file.filename));
    setDocumentAttachmentGroups((prev) => {
      const group = prev[batchId];
      if (!group) return prev;
      let changed = false;
      const nextGroup: Record<string, string[]> = {};
      for (const [parentName, children] of Object.entries(group)) {
        if (!existingNames.has(parentName)) {
          changed = true;
          continue;
        }
        const nextChildren = children.filter(
          (childName) =>
            childName !== parentName && existingNames.has(childName),
        );
        if (nextChildren.length !== children.length) changed = true;
        if (nextChildren.length > 0) nextGroup[parentName] = nextChildren;
      }
      if (!changed) return prev;
      return {
        ...prev,
        [batchId]: nextGroup,
      };
    });
  }, [batchId, files]);

  const explorerAttachmentGroups = useMemo<DocumentAttachmentGroups>(() => {
    const normalized = normalizeDocumentAttachmentGroups(documentAttachmentGroups);
    const parentName = resolveDocumentGroupParent(normalized, batchId, selected);
    if (!batchId || !parentName || files.length <= 1) return normalized;

    const inferredChildren = files
      .filter((file) => file.filename !== parentName)
      .filter(isSupplementalDocumentCandidate)
      .map((file) => file.filename);
    if (inferredChildren.length === 0) return normalized;

    const batchGroup = { ...(normalized[batchId] || {}) };
    batchGroup[parentName] = Array.from(
      new Set([...(batchGroup[parentName] || []), ...inferredChildren]),
    ).filter((filename) => filename !== parentName);
    return {
      ...normalized,
      [batchId]: batchGroup,
    };
  }, [batchId, documentAttachmentGroups, files, selected]);

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
  const [processingStartedAtMs, setProcessingStartedAtMs] = useState<number | null>(
    null,
  );
  const [processingElapsedMs, setProcessingElapsedMs] = useState(0);
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
  const [documentDetached, setDocumentDetached] = useState(false);
  const [documentPopoutRoot, setDocumentPopoutRoot] =
    useState<HTMLElement | null>(null);
  const documentPopoutRef = useRef<Window | null>(null);
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
  const switchAbortRef = useRef<AbortController | null>(null);

  const [preview, setPreview] = useState<PreviewResponse | null>(null);
  const [review, setReview] = useState<ManualReviewItem[]>([]);
  const previewAutoloadRef = useRef<string | null>(null);
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
  const assistantInvoiceGroupId = useMemo(() => {
    const rows = preview?.rows || [];
    const row = selectedRowIndex != null ? rows[selectedRowIndex] : rows[0];
    return String(
      row?._meta?.invoice_group_id || row?.["Invoice Number"] || "",
    ) || null;
  }, [preview, selectedRowIndex]);
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
    setMinimizedPanels(new Set<PanelKey>(["document", "template"]));
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

  useEffect(() => {
    if (!isProcessing || processingStartedAtMs == null) return;
    const updateElapsed = () => {
      setProcessingElapsedMs(Math.max(0, Date.now() - processingStartedAtMs));
    };
    updateElapsed();
    const timer = window.setInterval(updateElapsed, 500);
    return () => window.clearInterval(timer);
  }, [isProcessing, processingStartedAtMs]);

  useEffect(() => {
    if (!isProcessing || processingStartedAtMs != null) return;
    const startedAt = progress?.started_at ? Date.parse(progress.started_at) : NaN;
    if (Number.isFinite(startedAt)) {
      setProcessingStartedAtMs(startedAt);
      setProcessingElapsedMs(Math.max(0, Date.now() - startedAt));
    }
  }, [isProcessing, processingStartedAtMs, progress?.started_at]);

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
    inverted: true,
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
    | {
        rowIndex: number;
        column: string;
        x: number;
        y: number;
        selectedRowIndexes?: number[];
        selectedColumns?: string[];
      }
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
    (params: {
      rowIndex: number;
      column: string;
      x: number;
      y: number;
      selectedRowIndexes?: number[];
      selectedColumns?: string[];
    }) => {
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

  const handleSaveEdits = useCallback(async (adjudication: HumanAdjudicationOptions) => {
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
      const saved = await api.saveEdits(batchId, payload, adjudication);
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
        message: [
          `Saved ${count} adjudicated edit${count === 1 ? "" : "s"}.`,
          saved.adjudication?.benchmark_submissions
            ? `${saved.adjudication.benchmark_submissions} benchmark submission${saved.adjudication.benchmark_submissions === 1 ? "" : "s"}.`
            : "",
          saved.adjudication?.learning_approvals
            ? `${saved.adjudication.learning_approvals} learning example${saved.adjudication.learning_approvals === 1 ? "" : "s"} approved.`
            : "",
          saved.adjudication?.rule_proposals
            ? `${saved.adjudication.rule_proposals} reusable rule proposal${saved.adjudication.rule_proposals === 1 ? "" : "s"} created.`
            : "",
        ].filter(Boolean).join(" "),
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
        documentNavigationLockRef.current = null;
        setActiveDocumentPage(null);
        setDocumentTarget(null);
        return;
      }
      const next = {
        batchId: bid,
        filename,
        pageNumber: Math.max(1, Math.floor(pageNumber || 1)),
      };
      documentNavigationLockRef.current = {
        ...next,
        expiresAt: Date.now() + DOCUMENT_NAVIGATION_LOCK_MS,
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
    let cached: string | null = null;
    try {
      cached = localStorage.getItem(ACTIVE_BATCH_LS_KEY);
    } catch {
      cached = null;
    }
    if (cached) {
      try {
        const status = await api.getBatch(cached);
        const nextSelected = status.files[0]?.filename ?? null;
        setBatchId(status.batch_id);
        setBatchName(status.batch_name || "Untitled batch");
        setFiles(status.files);
        setSelected(nextSelected);
        setDocumentPageTarget(status.batch_id, nextSelected, 1);
        setHasExport(status.export_available);
        void refreshRevisions(status.batch_id);
        return status.batch_id;
      } catch (e) {
        if (isApiError(e) && (e.status === 404 || e.status === 400)) {
          try {
            localStorage.removeItem(ACTIVE_BATCH_LS_KEY);
          } catch {
            /* non-fatal */
          }
        } else {
          throw e;
        }
      }
    }
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
  }, [batchId, batchName, refreshBatchList, refreshRevisions, setDocumentPageTarget]);

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
      switchAbortRef.current?.abort();
      const controller = new AbortController();
      switchAbortRef.current = controller;
      let switchPerfDone = false;
      const finishSwitchPerfRaw = perfStart("batch.switch", {
        batchId: newId,
      });
      const finishSwitchPerf = (meta?: Record<string, unknown>) => {
        if (switchPerfDone) return;
        switchPerfDone = true;
        finishSwitchPerfRaw(meta);
      };
      const isStale = () => token !== switchTokenRef.current;

      setIsSwitchingBatch(true);
      // Pull the most recent batch list entry's name so the overlay
      // can display the destination batch's name immediately, before
      // /api/batches/<id> resolves.
      const listed = batchList.find((b) => b.batch_id === newId);
      setLoadingBatchName(listed?.batch_name?.trim() || "batch");
      setShowBatchPicker(false);

      try {
        const finishGetBatchPerf = perfStart("batch.switch.get_batch", {
          batchId: newId,
        });
        const status = await api.getBatch(newId, { signal: controller.signal });
        finishGetBatchPerf({
          status: "ok",
          preview_available: status.preview_available,
          files: status.files?.length ?? 0,
        });
        if (isStale()) {
          finishSwitchPerf({ status: "stale_after_get_batch" });
          return false;
        }
        if (status.batch_name) {
          setLoadingBatchName(status.batch_name);
        }

        // Pull preview + manual-review IN PARALLEL where applicable.
        // Use Promise.allSettled so one failure doesn't sink the
        // other — the operator can still get the preview even if the
        // manual-review fetch hits a transient hiccup.
        const previewPromise = status.preview_available
          ? api.preview(newId, { signal: controller.signal })
          : Promise.resolve(null);
        const reviewPromise = status.preview_available
          ? api.manualReview(newId, { signal: controller.signal })
          : Promise.resolve(null);
        const finishPayloadPerf = perfStart("batch.switch.payload", {
          batchId: newId,
          preview_available: status.preview_available,
        });
        const [previewSettled, reviewSettled] = await Promise.allSettled([
          previewPromise,
          reviewPromise,
        ]);
        finishPayloadPerf({
          preview: previewSettled.status,
          review: reviewSettled.status,
        });
        if (isStale()) {
          finishSwitchPerf({ status: "stale_after_payload" });
          return false;
        }

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

        finishSwitchPerf({
          status: "ok",
          preview_available: status.preview_available,
        });

        pushToast({
          id: "switch_batch",
          tone: "info",
          message: status.preview_available
            ? `Switched to "${status.batch_name || "Untitled batch"}".`
            : `Switched to "${status.batch_name || "Untitled batch"}". Click Process to populate the preview.`,
        });
        return true;
      } catch (e) {
        if ((e as Error)?.name === "AbortError") {
          finishSwitchPerf({ status: "aborted" });
          return false;
        }
        if (isStale()) {
          finishSwitchPerf({ status: "stale_after_error" });
          return false;
        }
        finishSwitchPerf({ status: "error" });
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
        if (switchAbortRef.current === controller) {
          switchAbortRef.current = null;
        }
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
    setShowBatchPicker(true);
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

  const flushUploadItemPatches = useCallback(() => {
    uploadItemPatchTimerRef.current = null;
    if (uploadItemPatchQueueRef.current.size === 0) return;
    const patches = new Map(uploadItemPatchQueueRef.current);
    uploadItemPatchQueueRef.current.clear();
    setUploadItems((prev) =>
      prev.map((item) => {
        const patch = patches.get(item.id);
        return patch ? { ...item, ...patch } : item;
      }),
    );
  }, []);

  const updateUploadItem = useCallback(
    (id: string, patch: Partial<UploadFileProgress>) => {
      const current = uploadItemPatchQueueRef.current.get(id) || {};
      uploadItemPatchQueueRef.current.set(id, { ...current, ...patch });
      if (uploadItemPatchTimerRef.current != null) return;
      uploadItemPatchTimerRef.current = window.setTimeout(
        flushUploadItemPatches,
        UPLOAD_STATE_FLUSH_MS,
      );
    },
    [flushUploadItemPatches],
  );

  const flushUploadedFileQueue = useCallback(() => {
    uploadedFileFlushTimerRef.current = null;
    if (uploadedFileQueueRef.current.length === 0) return;
    const entries = uploadedFileQueueRef.current;
    uploadedFileQueueRef.current = [];
    setFiles((prev) => entries.reduce(upsertFileEntry, prev));
  }, []);

  const queueUploadedFileEntry = useCallback(
    (entry: FileEntry, immediate = false) => {
      uploadedFileQueueRef.current.push(entry);
      if (immediate) {
        if (uploadedFileFlushTimerRef.current != null) {
          window.clearTimeout(uploadedFileFlushTimerRef.current);
          uploadedFileFlushTimerRef.current = null;
        }
        flushUploadedFileQueue();
        return;
      }
      if (uploadedFileFlushTimerRef.current != null) return;
      uploadedFileFlushTimerRef.current = window.setTimeout(
        flushUploadedFileQueue,
        UPLOADED_FILE_FLUSH_MS,
      );
    },
    [flushUploadedFileQueue],
  );

  useEffect(() => {
    return () => {
      if (uploadItemPatchTimerRef.current != null) {
        window.clearTimeout(uploadItemPatchTimerRef.current);
      }
      if (uploadedFileFlushTimerRef.current != null) {
        window.clearTimeout(uploadedFileFlushTimerRef.current);
      }
    };
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
      for (const id of ids) {
        uploadItemPatchQueueRef.current.delete(id);
      }
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
        let pickedInitialDocument = Boolean(selected || filesRef.current.length > 0);
        let firstUploadError: unknown = null;
        const uploadOne = async (index: number) => {
          const f = newFiles[index];
          const upload = queue[index];
          updateUploadItem(upload.id, { status: "uploading", percent: 1 });
          try {
            const uploaded = await uploadFileWithRetry(bid, f, (progress) => {
              updateUploadItem(upload.id, {
                status: progress.percent >= 100 ? "saving" : "uploading",
                percent: progress.percent >= 100 ? 96 : uploadItemPercent(progress.percent),
              });
            }, { asPdf: shouldUploadAsViewerPdf(f) });
            const entry = optimisticFileEntry(uploaded, f);
            const shouldPickDocument = !pickedInitialDocument;
            queueUploadedFileEntry(entry, shouldPickDocument);
            if (shouldPickDocument) {
              pickedInitialDocument = true;
              setSelected(entry.filename);
              setDocumentPageTarget(bid, entry.filename, 1);
            }
            updateUploadItem(upload.id, { status: "done", percent: 100 });
            done += 1;
            clearUploadItem(upload.id, UPLOAD_RESULT_LINGER_MS);
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
          }
        };
        const workerCount = Math.min(uploadWorkerLimit(newFiles.length), newFiles.length);
        await Promise.all(
          Array.from({ length: workerCount }, async () => {
            while (cursor < newFiles.length) {
              const index = cursor;
              cursor += 1;
              await uploadOne(index);
            }
          }),
        );
        flushUploadedFileQueue();
        void refreshFiles(bid);
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
      flushUploadedFileQueue,
      refreshFiles,
      queueUploadedFileEntry,
      pushToast,
      dismissToast,
      selected,
      updateUploadItem,
      setDocumentPageTarget,
    ],
  );

  const handleAddDocumentsToCurrentSet = useCallback(
    async (newFiles: File[]) => {
      if (newFiles.length === 0) return;
      if (!batchId || !selected) {
        await handleFiles(newFiles);
        return;
      }

      const parentFilename =
        resolveDocumentGroupParent(documentAttachmentGroups, batchId, selected) ||
        selected;
      const parentExtension = fileExtensionForEntryOrName(filesRef.current, parentFilename);
      const queue = enqueueUploadItems(batchId, newFiles);
      const total = newFiles.length;
      const showProgress = total > 1;
      const progressId = "upload-progress";
      const appendIntoOpenDocument = false;

      try {
        setError(null);
        if (!appendIntoOpenDocument || !APPENDABLE_OPEN_DOCUMENT_EXTENSIONS.has(parentExtension)) {
          if (showProgress) {
            pushToast({
              id: progressId,
              tone: "info",
              message: `Adding 0 of ${total} to this batch...`,
              ttl: 0,
            });
          }

          let done = 0;
          let cursor = 0;
          let firstUploadError: unknown = null;
          const uploadedNames: string[] = [];
          const uploadOne = async (index: number) => {
            const file = newFiles[index];
            const upload = queue[index];
            updateUploadItem(upload.id, { status: "uploading", percent: 1 });
            try {
              const uploaded = await uploadFileWithRetry(batchId, file, (progress) => {
                updateUploadItem(upload.id, {
                  status: progress.percent >= 100 ? "saving" : "uploading",
                  percent: progress.percent >= 100 ? 96 : uploadItemPercent(progress.percent),
                });
              }, { asPdf: shouldUploadAsViewerPdf(file) });
              const entry = optimisticFileEntry(uploaded, file);
              uploadedNames.push(entry.filename);
              queueUploadedFileEntry(entry, !selected && uploadedNames.length === 1);
              if (!selected) {
                setSelected(entry.filename);
                setDocumentPageTarget(batchId, entry.filename, 1);
              }
              updateUploadItem(upload.id, { status: "done", percent: 100 });
              done += 1;
              clearUploadItem(upload.id, UPLOAD_RESULT_LINGER_MS);
              if (showProgress) {
                pushToast({
                  id: progressId,
                  tone: "info",
                  message: `Adding ${done} of ${total} to this batch...`,
                  ttl: 0,
                });
              }
            } catch (error) {
              const message = getFriendlyErrorMessage(error, "Add documents");
              updateUploadItem(upload.id, {
                status: "failed",
                error: message,
                percent: upload.percent || 0,
              });
              if (!firstUploadError) firstUploadError = error;
            }
          };

          const workerCount = Math.min(uploadWorkerLimit(newFiles.length), newFiles.length);
          await Promise.all(
            Array.from({ length: workerCount }, async () => {
              while (cursor < newFiles.length) {
                const index = cursor;
                cursor += 1;
                await uploadOne(index);
              }
            }),
          );
          flushUploadedFileQueue();
          await refreshFiles(batchId);
          if (parentFilename && uploadedNames.length > 0) {
            setDocumentAttachmentGroups((prev) => {
              const batchGroup = { ...(prev[batchId] || {}) };
              batchGroup[parentFilename] = Array.from(
                new Set([
                  ...(batchGroup[parentFilename] || []),
                  ...uploadedNames.filter((name) => name !== parentFilename),
                ]),
              );
              return {
                ...prev,
                [batchId]: batchGroup,
              };
            });
          }
          const lastUploadedName = uploadedNames[uploadedNames.length - 1];
          if (lastUploadedName) {
            setSelected(lastUploadedName);
            setDocumentPageTarget(batchId, lastUploadedName, 1);
            setDocumentRefreshToken((token) => token + 1);
          }
          if (firstUploadError) {
            throw firstUploadError;
          }
          if (showProgress) {
            pushToast({
              id: progressId,
              tone: "success",
              message: `Added ${total} file${total === 1 ? "" : "s"} to this batch.`,
              ttl: 3000,
            });
          }
          void refreshBatchList();
          return;
        }

        if (showProgress) {
          pushToast({
            id: progressId,
            tone: "info",
            message: `Adding 0 of ${total} to the open document...`,
            ttl: 0,
          });
        }

        let done = 0;
        let cursor = 0;
        let firstUploadError: unknown = null;
        let appendTargetFilename = parentFilename;
        let finalPageNumber = 1;
        const uploadOne = async (index: number) => {
          const file = newFiles[index];
          const upload = queue[index];
          updateUploadItem(upload.id, { status: "uploading", percent: 1 });
          try {
            const previousTarget = appendTargetFilename;
            const appended = await api.appendFileToDocument(
              batchId,
              appendTargetFilename,
              file,
              (progress) => {
                updateUploadItem(upload.id, {
                  status: progress.percent >= 100 ? "saving" : "uploading",
                  percent:
                    progress.percent >= 100
                      ? 96
                      : uploadItemPercent(progress.percent),
                });
              },
            );
            appendTargetFilename = appended.filename;
            finalPageNumber = appended.page_count || finalPageNumber;
            setFiles((prev) => {
              const existing = prev.find((entry) => entry.filename === previousTarget);
              const base =
                previousTarget !== appended.filename
                  ? prev.filter((entry) => entry.filename !== previousTarget)
                  : prev;
              return upsertFileEntry(base, {
                filename: appended.filename,
                size_bytes: appended.size_bytes,
                extension: appended.extension || ".pdf",
                page_count: appended.page_count,
                vendor_key: existing?.vendor_key || "unknown",
                vendor_confidence: existing?.vendor_confidence ?? 0,
                vendor_detection_reason:
                  existing?.vendor_detection_reason ||
                  "Detection pending after document update.",
                supported_in_phase_1: existing?.supported_in_phase_1 ?? false,
                source_type: existing?.source_type || "uploaded",
                file_support_status: existing?.file_support_status || "pending",
                file_support_label: existing?.file_support_label || "Pending detection",
                file_support_reason:
                  existing?.file_support_reason ||
                  "The file changed; metadata is still refreshing.",
              });
            });
            if (previousTarget !== appended.filename) {
              setDocumentAttachmentGroups((prev) => {
                const batchGroup = { ...(prev[batchId] || {}) };
                const children = batchGroup[previousTarget] || [];
                delete batchGroup[previousTarget];
                const nextChildren = children.filter(
                  (child) => child !== previousTarget && child !== appended.filename,
                );
                if (nextChildren.length > 0) {
                  batchGroup[appended.filename] = Array.from(
                    new Set([...(batchGroup[appended.filename] || []), ...nextChildren]),
                  );
                }
                if (Object.keys(batchGroup).length === 0) {
                  const next = { ...prev };
                  delete next[batchId];
                  return next;
                }
                return { ...prev, [batchId]: batchGroup };
              });
            }
            setSelected(appended.filename);
            setDocumentPageTarget(batchId, appended.filename, finalPageNumber);
            setDocumentRefreshToken((token) => token + 1);
            updateUploadItem(upload.id, { status: "done", percent: 100 });
            done += 1;
            clearUploadItem(upload.id, UPLOAD_RESULT_LINGER_MS);
            void refreshFiles(batchId);
            if (showProgress) {
              pushToast({
                id: progressId,
                tone: "info",
                message: `Adding ${done} of ${total} to the open document...`,
                ttl: 0,
              });
            }
          } catch (error) {
            const message = getFriendlyErrorMessage(error, "Add documents");
            updateUploadItem(upload.id, {
              status: "failed",
              error: message,
              percent: upload.percent || 0,
            });
            if (!firstUploadError) firstUploadError = error;
          }
        };

        // Appending mutates the open PDF, so these must run in order.
        // Parallel appends can race and overwrite each other's pages.
        const workerCount = 1;
        await Promise.all(
          Array.from({ length: workerCount }, async () => {
            while (cursor < newFiles.length) {
              const index = cursor;
              cursor += 1;
              await uploadOne(index);
            }
          }),
        );
        await refreshFiles(batchId);
        setSelected(appendTargetFilename);
        setDocumentPageTarget(batchId, appendTargetFilename, finalPageNumber);
        if (firstUploadError) {
          throw firstUploadError;
        }
        if (showProgress) {
          pushToast({
            id: progressId,
            tone: "success",
            message: `Added ${total} page set${total === 1 ? "" : "s"} to the open document.`,
            ttl: 3000,
          });
        }
        void refreshBatchList();
      } catch (e) {
        failUploadQueue(queue, getFriendlyErrorMessage(e, "Add documents"));
        dismissToast(progressId);
        setError(getFriendlyErrorMessage(e, "Add documents"));
        pushToast({
          tone: "error",
          message: getFriendlyErrorMessage(e, "Add documents"),
          ttl: 5000,
        });
        // eslint-disable-next-line no-console
        console.warn("document-set upload failed:", e);
        throw e;
      }
    },
    [
      batchId,
      clearUploadItem,
      documentAttachmentGroups,
      dismissToast,
      enqueueUploadItems,
      failUploadQueue,
      flushUploadedFileQueue,
      handleFiles,
      queueUploadedFileEntry,
      pushToast,
      refreshBatchList,
      refreshFiles,
      selected,
      setDocumentPageTarget,
      updateUploadItem,
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
        let pickedInitialDocument = false;
        let cursor = 0;
        let firstUploadError: unknown = null;
        const uploadOne = async (index: number) => {
          const f = newFiles[index];
          const upload = queue[index];
          updateUploadItem(upload.id, { status: "uploading", percent: 1 });
          try {
            const uploaded = await uploadFileWithRetry(targetBatchId, f, (progress) => {
              updateUploadItem(upload.id, {
                status: progress.percent >= 100 ? "saving" : "uploading",
                percent: progress.percent >= 100 ? 96 : uploadItemPercent(progress.percent),
              });
            }, { asPdf: shouldUploadAsViewerPdf(f) });
            const entry = optimisticFileEntry(uploaded, f);
            const shouldPickDocument = !pickedInitialDocument;
            queueUploadedFileEntry(entry, shouldPickDocument);
            if (shouldPickDocument) {
              pickedInitialDocument = true;
              setSelected(entry.filename);
              setDocumentPageTarget(targetBatchId, entry.filename, 1);
            }
            updateUploadItem(upload.id, { status: "done", percent: 100 });
            done += 1;
            clearUploadItem(upload.id, UPLOAD_RESULT_LINGER_MS);
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
          }
        };
        const workerCount = Math.min(uploadWorkerLimit(newFiles.length), newFiles.length);
        await Promise.all(
          Array.from({ length: workerCount }, async () => {
            while (cursor < newFiles.length) {
              const index = cursor;
              cursor += 1;
              await uploadOne(index);
            }
          }),
        );
        flushUploadedFileQueue();
        void refreshFiles(targetBatchId);
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
      flushUploadedFileQueue,
      handleSwitchBatch,
      queueUploadedFileEntry,
      pushToast,
      dismissToast,
      refreshBatchList,
      refreshFiles,
      setDocumentPageTarget,
      updateUploadItem,
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

  const loadProcessedWorkspace = useCallback(async (targetBatchId: string) => {
    let lastError: unknown = null;
    for (let attempt = 0; attempt < PREVIEW_READY_RETRY_ATTEMPTS; attempt += 1) {
      try {
        const [prev, rev] = await Promise.all([
          api.preview(targetBatchId),
          api.manualReview(targetBatchId),
        ]);
        return { prev, rev };
      } catch (e) {
        lastError = e;
        if (
          !isPreviewStillPreparingError(e) ||
          attempt === PREVIEW_READY_RETRY_ATTEMPTS - 1
        ) {
          throw e;
        }
        setProgress((current) =>
          current?.batch_id === targetBatchId
            ? {
                ...current,
                status: "processing",
                percent: Math.min(current.percent ?? 99, 99),
                current_step: "Finalizing preview...",
              }
            : current,
        );
        await delay(PREVIEW_READY_RETRY_BASE_MS + attempt * 150);
      }
    }
    throw lastError;
  }, []);

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
    const processingStartMs = Date.now();
    const processingStartedAt = new Date(processingStartMs).toISOString();
    setProcessingStartedAtMs(processingStartMs);
    setProcessingElapsedMs(0);
    setIsProcessing(true);
    setError(null);
    setProgress({
      batch_id: targetBatchId,
      started_at: processingStartedAt,
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
      const { prev, rev } = await loadProcessedWorkspace(targetBatchId);
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
      setProcessingElapsedMs(Math.max(0, Date.now() - processingStartMs));
      setProcessingStartedAtMs(null);
      setIsProcessing(false);
      setIsCancelling(false);
      window.setTimeout(stopPolling, PROGRESS_POLL_MS + 50);
    }
  }, [editedCellCount, loadProcessedWorkspace, pushToast, refreshBatchList, requestConfirm, startPolling, stopPolling, waitForProcessingDone]);

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
      pageNumber?: number,
    ) => {
      const isMerge = mode === "merge";
      const isPageRun = Number.isFinite(pageNumber) && Number(pageNumber) > 0;
      const toastId = isPageRun
        ? `single-process-${filename}-page-${pageNumber}`
        : `single-process-${filename}`;
      if (isProcessing) {
        pushToast({
          id: toastId,
          tone: "info",
          message: "Another process is already running. Wait for it to finish.",
          ttl: 4000,
        });
        return;
      }

      if (editedCellCount > 0) {
        const ok = await requestConfirm({
          title: isPageRun
            ? isMerge
              ? "Discard edits and process this page?"
              : "Discard edits and create a page template?"
            : isMerge
              ? "Discard edits and add this file?"
              : "Discard edits and create a file template?",
          message: `${isPageRun ? "Processing this page" : isMerge ? "Adding this file" : "Creating a new file template"} will refresh the preview and discard ${editedCellCount} unsaved edit${editedCellCount === 1 ? "" : "s"}.`,
          confirmLabel: isPageRun
            ? isMerge
              ? "Process page"
              : "Create page template"
            : isMerge
              ? "Add file"
              : "Create template",
          tone: "warning",
        });
        if (!ok) return;
      }

      const isActiveTarget = targetBatchId === batchId;

      if (!isActiveTarget) {
        const switched = await handleSwitchBatch(targetBatchId);
        if (switched === false) {
          pushToast({
            id: toastId,
            tone: "warning",
            message: "Switch cancelled. File was not processed.",
            ttl: 4000,
          });
          return;
        }
      }

      const processingStartMs = Date.now();
      const processingStartedAt = new Date(processingStartMs).toISOString();
      setProcessingStartedAtMs(processingStartMs);
      setProcessingElapsedMs(0);
      setIsProcessing(true);
      setIsCancelling(false);
      setError(null);
      setProgress({
        batch_id: targetBatchId,
        started_at: processingStartedAt,
        status: "processing",
        percent: 0,
        files_total: 1,
        files_done: 0,
        current_file: filename,
        current_step: isPageRun
          ? isMerge
            ? `Processing page ${pageNumber}...`
            : `Creating template from page ${pageNumber}...`
          : isMerge
            ? `Adding ${filename} to template...`
            : `Creating template from ${filename}...`,
      });
      startPolling(targetBatchId);
      pushToast({
        id: toastId,
        tone: "info",
        message: isPageRun
          ? isMerge
            ? `Processing page ${pageNumber} and adding it to the current template...`
            : `Creating template from page ${pageNumber}...`
          : isMerge
            ? `Adding ${filename} to current template...`
            : `Creating template from ${filename}...`,
        ttl: 0,
      });

      try {
        await api.process(targetBatchId, {
          sync: true,
          file: filename,
          fileMode: mode,
          page: isPageRun ? pageNumber : undefined,
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
        const { prev, rev } = await loadProcessedWorkspace(targetBatchId);
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
          id: toastId,
          tone: "success",
          message: isPageRun
            ? isMerge
              ? `Processed page ${pageNumber} from "${filename}". Template now has ${invoices} invoice${invoices === 1 ? "" : "s"}.`
              : `Created a new template from page ${pageNumber} of "${filename}" with ${invoices} invoice${invoices === 1 ? "" : "s"}.`
            : isMerge
              ? `Added "${filename}" to current template. Template now has ${invoices} invoice${invoices === 1 ? "" : "s"}.`
              : `Created a new template from "${filename}" with ${invoices} invoice${invoices === 1 ? "" : "s"}.`,
          ttl: 4000,
        });
      } catch (e) {
        const message = getFriendlyErrorMessage(e, isPageRun ? "Process page" : "Process file");
        setError(message);
        setProgress((prev) =>
          prev
            ? { ...prev, status: "failed", error_message: message, percent: 100 }
            : null,
        );
        // eslint-disable-next-line no-console
        console.warn("file process failed:", e);
        pushToast({
          id: toastId,
          tone: "error",
          message,
          ttl: 5000,
        });
      } finally {
        setProcessingElapsedMs(Math.max(0, Date.now() - processingStartMs));
        setProcessingStartedAtMs(null);
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
      loadProcessedWorkspace,
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
      const finishEditPerf = perfStart("template.cell_edit.commit", {
        rowIndex,
        columnKey,
      });
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
      finishEditPerf();
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

  // AccountingReadiness remains the backend authority after local edits.
  // Merge the edit overlay into a snapshot and refresh only the readiness
  // contract; the frontend does not reinterpret blockers or field validity.
  useEffect(() => {
    if (!batchId || !preview?.rows) return;
    let cancelled = false;
    const rows = preview.rows.map((row, index) => ({
      ...row,
      ...(edits[index] ?? {}),
    }));
    void api.accountingReadiness(batchId, rows).then((readiness) => {
      if (cancelled) return;
      setPreview((current) => current ? {
        ...current,
        accounting_readiness: readiness,
      } : current);
    }).catch(() => {
      // Preserve the last backend decision. A failed refresh must never turn
      // a blocked or unknown snapshot into an exportable one.
    });
    return () => {
      cancelled = true;
    };
  }, [batchId, edits, preview?.rows]);

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

  const handleDeletePreviewRows = useCallback(
    (rowIndexes: number[], source: "main" | "popout" = "main") => {
      if (!preview) return;
      const remove = new Set(
        rowIndexes
          .map((value) => Math.floor(Number(value)))
          .filter((value) => Number.isFinite(value) && value >= 0 && value < preview.rows.length),
      );
      if (remove.size === 0) return;
      const sortedRemoved = [...remove].sort((a, b) => a - b);
      const nextRows = preview.rows.filter((_, index) => !remove.has(index));
      const invoiceNumbers = new Set(
        nextRows
          .map((row) => String((row as any)?.["Invoice Number"] ?? "").trim())
          .filter(Boolean),
      );
      setPreview((prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          rows: prev.rows.filter((_, index) => !remove.has(index)),
          row_count: nextRows.length,
          invoice_count: invoiceNumbers.size || nextRows.length,
          summary: {
            ...prev.summary,
            invoices_total: invoiceNumbers.size || nextRows.length,
          },
        };
      });
      setEdits((prev) => {
        const next: CellEdits = {};
        for (const [rawIndex, rowEdits] of Object.entries(prev)) {
          const oldIndex = Number(rawIndex);
          if (!Number.isFinite(oldIndex) || remove.has(oldIndex)) continue;
          const shift = sortedRemoved.filter((removed) => removed < oldIndex).length;
          next[oldIndex - shift] = rowEdits;
        }
        return next;
      });
      setSelectedRowIndex((current) => {
        if (current == null) return current;
        if (remove.has(current)) return null;
        const shift = sortedRemoved.filter((removed) => removed < current).length;
        return current - shift;
      });
      setCellMenu(null);
      pushToast({
        tone: "success",
        message: `Removed ${remove.size} row${remove.size === 1 ? "" : "s"} from the preview.`,
        ttl: 3000,
      });
      if (source === "main") {
        broadcastChannelRef.current?.postMessage({
          type: "row-delete",
          rowIndexes: [...remove],
          source: "main",
        });
      }
    },
    [preview, pushToast],
  );

  const handleDeletePreviewColumns = useCallback(
    (columns: string[], source: "main" | "popout" = "main") => {
      if (!preview) return;
      const remove = new Set(columns.filter((column) => preview.columns.includes(column)));
      if (remove.size === 0) return;
      setPreview((prev) => {
        if (!prev) return prev;
        const prune = (items: string[]) => items.filter((column) => !remove.has(column));
        const rows = prev.rows.map((row) => {
          const next: PreviewRow = { ...(row as any) };
          for (const column of remove) delete (next as any)[column];
          return next;
        });
        return {
          ...prev,
          columns: prune(prev.columns),
          required_columns: prune(prev.required_columns),
          recommended_columns: prune(prev.recommended_columns),
          optional_columns: prune(prev.optional_columns),
          rows,
        };
      });
      setEdits((prev) => {
        const next: CellEdits = {};
        for (const [rowIndex, rowEdits] of Object.entries(prev)) {
          const pruned = { ...rowEdits };
          for (const column of remove) delete pruned[column];
          if (Object.keys(pruned).length > 0) next[Number(rowIndex)] = pruned;
        }
        return next;
      });
      setSelectedColumnKey((current) => (current && remove.has(current) ? null : current));
      setCellMenu(null);
      pushToast({
        tone: "success",
        message: `Removed ${remove.size} column${remove.size === 1 ? "" : "s"} from the preview.`,
        ttl: 3000,
      });
      if (source === "main") {
        broadcastChannelRef.current?.postMessage({
          type: "column-delete",
          columns: [...remove],
          source: "main",
        });
      }
    },
    [preview, pushToast],
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
        | { type: "row-delete"; rowIndexes: number[]; source: string }
        | { type: "column-delete"; columns: string[]; source: string }
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
      if (data.type === "row-delete") {
        handleDeletePreviewRows(data.rowIndexes, "popout");
        return;
      }
      if (data.type === "column-delete") {
        handleDeletePreviewColumns(data.columns, "popout");
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
  }, [batchId, templateDetached, handleDeletePreviewColumns, handleDeletePreviewRows]);

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

  const handleReattachDocument = useCallback(() => {
    const w = documentPopoutRef.current;
    if (w && !w.closed) {
      try {
        w.close();
      } catch {
        /* ignore; the user may have already closed it */
      }
    }
    documentPopoutRef.current = null;
    setDocumentPopoutRoot(null);
    setDocumentDetached(false);
  }, []);

  const handleDetachDocument = useCallback(() => {
    const existing = documentPopoutRef.current;
    if (existing && !existing.closed && documentPopoutRoot) {
      try {
        existing.focus();
      } catch {
        /* focus is best-effort */
      }
      setDocumentDetached(true);
      return;
    }

    const name = `bill-live-document-${batchId || "workspace"}`;
    const w = window.open("", name, DOCUMENT_POPOUT_FEATURES);
    if (!w) {
      pushToast({
        tone: "error",
        message: "Could not detach the document viewer. Check the browser pop-up blocker.",
      });
      return;
    }

    const root = prepareDetachedDocumentWindow(w);
    documentPopoutRef.current = w;
    setDocumentPopoutRoot(root);
    setDocumentDetached(true);
    try {
      w.focus();
    } catch {
      /* focus is best-effort */
    }
  }, [batchId, documentPopoutRoot, pushToast]);

  const handlePopoutDocument = handleDetachDocument;

  useEffect(() => {
    if (!documentDetached) return;
    const w = documentPopoutRef.current;
    if (!w || w.closed) {
      documentPopoutRef.current = null;
      setDocumentPopoutRoot(null);
      setDocumentDetached(false);
      return;
    }
    const onBeforeUnload = () => {
      documentPopoutRef.current = null;
      setDocumentPopoutRoot(null);
      setDocumentDetached(false);
    };
    w.addEventListener("beforeunload", onBeforeUnload);
    const id = window.setInterval(() => {
      const current = documentPopoutRef.current;
      if (!current || current.closed) {
        documentPopoutRef.current = null;
        setDocumentPopoutRoot(null);
        setDocumentDetached(false);
      }
    }, 600);
    return () => {
      w.removeEventListener("beforeunload", onBeforeUnload);
      window.clearInterval(id);
    };
  }, [documentDetached]);

  useEffect(() => {
    const w = documentPopoutRef.current;
    if (!w || w.closed) return;
    w.document.title = selected
      ? `Document viewer - ${selected}`
      : "Document viewer";
  }, [selected]);

  useEffect(() => {
    return () => {
      const w = documentPopoutRef.current;
      if (w && !w.closed) {
        try {
          w.close();
        } catch {
          /* ignore cleanup failures */
        }
      }
    };
  }, []);

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

  const handleExport = useCallback(async (rowIndexes?: number[]) => {
    if (!batchId) return;
    const exportRowIndexSet =
      rowIndexes && rowIndexes.length > 0 ? new Set(rowIndexes) : null;
    if (review.length > 0) {
      pushToast({
        tone: "warning",
        message: exportRowIndexSet
          ? "Exporting this invoice with current review state."
          : `Exporting with ${review.length} unresolved issue${review.length === 1 ? "" : "s"}.`,
        ttl: 6000,
      });
    }
    setIsExporting(true);
    setError(null);
    try {
      let editedRows: Record<string, unknown>[] | undefined;
      const fullColumns = preview?.columns ?? [];
      if (preview && preview.rows.length > 0 && fullColumns.length > 0) {
        const rowsForExport = preview.rows
          .map((row, i) => ({ row, i }))
          .filter(({ i }) => !exportRowIndexSet || exportRowIndexSet.has(i));
        editedRows = rowsForExport.map(({ row, i }) => {
          const overrides = edits[i] ?? {};
          const merged: Record<string, unknown> = { ...(row as any) };
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
      const urlUpdates = res.document_url_updates;
      if (
        urlUpdates &&
        ((urlUpdates.by_source_file && Object.keys(urlUpdates.by_source_file).length > 0) ||
          (urlUpdates.by_invoice_number && Object.keys(urlUpdates.by_invoice_number).length > 0))
      ) {
        setPreview((prev) => {
          if (!prev) return prev;
          let changed = false;
          const rows = prev.rows.map((row) => {
            const sourceFile =
              typeof (row as any)?._meta?.source_file === "string"
                ? (row as any)._meta.source_file
                : "";
            const invoiceNumber = String((row as any)?.["Invoice Number"] ?? "").trim();
            const url =
              (sourceFile && urlUpdates.by_source_file?.[sourceFile]) ||
              (invoiceNumber && urlUpdates.by_invoice_number?.[invoiceNumber]) ||
              "";
            if (!url || (row as any)?.["Document Url"] === url) return row;
            changed = true;
            return {
              ...(row as any),
              "Document Url": url,
              _meta: {
                ...(((row as any)?._meta ?? {}) as Record<string, unknown>),
                support_document_status: "dropbox_uploaded",
              },
            };
          });
          return changed ? { ...prev, rows } : prev;
        });
      }
      if (exported.length === 0) {
        const reason = typeof (res as any).reason === "string" ? (res as any).reason : "no_export_file";
        pushToast({
          tone: "error",
          message: `No export file was generated (${reason}). Please review the batch and try again.`,
          ttl: 7000,
        });
        return;
      }
      const editedLabel = res.export_used_edited_rows
        ? editedCellCount > 0
          ? ` (from preview, ${editedCellCount} edited cell${editedCellCount === 1 ? "" : "s"})`
          : " (from preview)"
        : "";
      const documentUrlWarnings = Array.isArray(res.document_url_warnings)
        ? res.document_url_warnings.filter(Boolean)
        : [];
      if (documentUrlWarnings.length > 0) {
        pushToast({
          tone: "warning",
          message: documentUrlWarnings[0],
          ttl: 9000,
        });
      }
      pushToast({
        tone: "success",
        message: exportRowIndexSet
          ? `Exported this invoice${editedLabel}. Download starting...`
          : `Exported ${exported.length} file${exported.length === 1 ? "" : "s"}${editedLabel}. Download starting...`,
      });
      const filename = exported[exported.length - 1]?.filename;
      setTimeout(() => triggerDownload(filename), 50);
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
          const [previewSettled, reviewSettled] = await Promise.allSettled([
            api.preview(status.batch_id),
            api.manualReview(status.batch_id),
          ]);
          if (cancelled) return;
          if (previewSettled.status === "fulfilled") {
            setPreview(previewSettled.value);
            setReviewedKeys(new Set());
          } else {
            setPreview(null);
            setError(getFriendlyErrorMessage(previewSettled.reason, "Restore preview"));
            // eslint-disable-next-line no-console
            console.warn("restore preview failed:", previewSettled.reason);
          }
          if (reviewSettled.status === "fulfilled") {
            setReview(reviewSettled.value.items);
          } else {
            setReview([]);
            // eslint-disable-next-line no-console
            console.warn("restore manual review failed:", reviewSettled.reason);
          }
        }
        if (false) {
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
        }
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

  useEffect(() => {
    if (!batchId || preview || isProcessing) return;
    const listed = batchList.find((batch) => batch.batch_id === batchId);
    if (!listed || (listed.rows_count ?? 0) <= 0) return;
    if (previewAutoloadRef.current === batchId) return;
    previewAutoloadRef.current = batchId;
    let cancelled = false;
    (async () => {
      const [previewSettled, reviewSettled] = await Promise.allSettled([
        api.preview(batchId),
        api.manualReview(batchId),
      ]);
      if (cancelled) return;
      if (previewSettled.status === "fulfilled") {
        setPreview(previewSettled.value);
        setReviewedKeys(new Set());
        setReview(reviewSettled.status === "fulfilled" ? reviewSettled.value.items : []);
      } else {
        previewAutoloadRef.current = null;
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [batchId, batchList, isProcessing, preview]);

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
      const lock = documentNavigationLockRef.current;
      if (lock) {
        if (Date.now() > lock.expiresAt) {
          documentNavigationLockRef.current = null;
        } else if (!sameDocumentPage(lock, page)) {
          return;
        } else {
          lock.expiresAt = Math.min(
            lock.expiresAt,
            Date.now() + DOCUMENT_NAVIGATION_SETTLE_LOCK_MS,
          );
        }
      }
      setActiveDocumentPage(page);
      if (page.batchId !== batchId) return;
      const rowIndex = findRowIndexForDocumentPage(preview?.rows, page);
      if (rowIndex < 0) return;
      const rowGuard = rowNavigationGuardRef.current;
      if (rowGuard) {
        const now = Date.now();
        if (now > rowGuard.expiresAt) {
          rowNavigationGuardRef.current = null;
        } else if (rowGuard.rowIndex !== rowIndex) {
          return;
        } else {
          rowGuard.expiresAt = Math.min(
            rowGuard.expiresAt,
            now + DOCUMENT_NAVIGATION_SETTLE_LOCK_MS,
          );
        }
      }
      setSelectedRowIndex((current) => (current === rowIndex ? current : rowIndex));
    },
    [batchId, preview?.rows],
  );

  const handleSelectRow = useCallback(
    (rowIndex: number | null) => {
      rowNavigationGuardRef.current =
        rowIndex == null
          ? null
          : {
              rowIndex,
              expiresAt: Date.now() + DOCUMENT_NAVIGATION_LOCK_MS,
            };
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

  const batchesVisible = false;
  const documentVisible =
    activeModule === "batches" &&
    !documentDetached &&
    !closedPanels.has("document") &&
    !minimizedPanels.has("document") &&
    (!maximizedPanel || maximizedPanel === "document");
  const templateVisible =
    activeModule === "batches" &&
    !closedPanels.has("template") &&
    !minimizedPanels.has("template") &&
    (!maximizedPanel || maximizedPanel === "template");
  const anyWorkspacePanelVisible = batchesVisible || documentVisible || templateVisible;
  const handleRefreshBatchList = useCallback(() => {
    void refreshBatchList();
  }, [refreshBatchList]);
  const batchListForSelector = useMemo(() => {
    if (!batchId || batchList.some((batch) => batch.batch_id === batchId)) {
      return batchList;
    }
    const activeEntry: BatchListEntry = {
      batch_id: batchId,
      batch_name: batchName || "Untitled batch",
      created_at: "",
      updated_at: undefined,
      status: isProcessing ? "processing" : "idle",
      files_count: files.length,
      invoices_count: preview?.invoice_count ?? 0,
      rows_count: preview?.rows?.length ?? 0,
      manual_review_count: review.length,
      export_available: hasExport,
      last_export_file: null,
      supported_vendor_summary: {},
    };
    return [activeEntry, ...batchList];
  }, [batchId, batchList, batchName, files.length, hasExport, isProcessing, preview, review.length]);
  const batchSelector = useMemo(
    () => (
      <BatchSelectorDropdown
        compact
        variant="breadcrumb"
        open={showBatchPicker}
        onOpenChange={setShowBatchPicker}
        batchList={batchListForSelector}
        activeBatchId={batchId}
        onSwitchBatch={handleSwitchBatch}
        onCreateBatch={handleSubmitCreateBatch}
        createRequestToken={createBatchRequestToken}
        onRenameBatch={handleInlineRenameBatch}
        onDeleteBatch={handleDeleteBatchById}
        onRefreshBatchList={handleRefreshBatchList}
        files={files}
        fileAttachmentGroups={explorerAttachmentGroups}
        selectedFile={selected}
        activeDocumentPage={activeDocumentPage}
        onSelectFile={handleSelectExplorerFile}
        onSelectPage={handleSelectExplorerPage}
        onDeleteFile={handleDeleteFile}
        onUploadFiles={handleAddDocumentsToCurrentSet}
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
    ),
    [
      activeDocumentPage,
      batchId,
      batchListForSelector,
      createBatchRequestToken,
      explorerAttachmentGroups,
      files,
      handleAddDocumentsToCurrentSet,
      handleDeleteBatchById,
      handleDeleteFile,
      handleFilesForBatch,
      handleInlineRenameBatch,
      handleProcessBatch,
      handleProcessFile,
      handleRefreshBatchList,
      handleSelectExplorerFile,
      handleSelectExplorerPage,
      handleSubmitCreateBatch,
      handleSwitchBatch,
      isProcessing,
      isSwitchingBatch,
      progress,
      queueStatus,
      selected,
      showBatchPicker,
      uploadItems,
    ],
  );
  const renderDocumentPreview = (detachedWindow = false) => (
    <DocumentPreviewPanel
      batchId={batchId}
      filename={selected}
      reloadToken={documentRefreshToken}
      files={files}
      fileAttachmentGroups={batchId ? explorerAttachmentGroups[batchId] || {} : {}}
      uploadItems={uploadItems}
      targetPage={
        documentTarget &&
        documentTarget.batchId === batchId &&
        documentTarget.filename === selected
          ? documentTarget
          : null
      }
      onActivePageChange={handleDocumentActivePageChange}
      onPopout={detachedWindow ? undefined : handlePopoutDocument}
      onReattach={detachedWindow ? handleReattachDocument : undefined}
      isDetachedWindow={detachedWindow}
      highlightedTraceIds={selectedRowTraceIds}
      onTraceClick={handleTraceClick}
      remapActive={remapTarget != null && remapDraft == null}
      onRemapDrawn={handleRemapDrawn}
      aiProgress={progress}
      batchSelector={batchSelector}
      onAddDocuments={handleAddDocumentsToCurrentSet}
      onProcessBatch={() => {
        if (batchId) void handleProcessBatch(batchId);
      }}
      onProcessPage={(pageNumber, mode, filename) => {
        const targetFilename = filename || selected;
        if (batchId && targetFilename) {
          void handleProcessFile(batchId, targetFilename, mode ?? "merge", pageNumber);
        }
      }}
      isProcessing={isProcessing}
    />
  );

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
                label: "Billing V2",
                checked: activeModule === "billing-v2",
                onSelect: () => setActiveModule("billing-v2"),
              },
              {
                label: "Legacy Billing",
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
        className={`layout ui-layout-template-centered ${
          isSwitchingBatch ? "switching-batch" : ""
        } module-${activeModule} ${
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
        } ${templateDetached ? "template-detached" : ""} ${
          documentDetached ? "document-detached" : ""
        }`}
      >
        <NavRail
          active={activeModule}
          onSelect={setActiveModule}
          collapsed={navCollapsed}
        />
        {activeModule === "billing-v2" && <BillingV2 />}
        {activeModule === "accounting-rules" && <Suspense fallback={<ModuleLoading label="Loading Accounting Rules" />}><AccountingRulesWorkspace /></Suspense>}
        {activeModule === "context-intelligence" && <Suspense fallback={<ModuleLoading label="Loading Context Matrix" />}><ContextIntelligenceWorkspace /></Suspense>}
        {activeModule === "resman-vendors" && <Suspense fallback={<ModuleLoading label="Loading Vendors" />}><ResManContextWorkspace dataset="vendors" /></Suspense>}
        {activeModule === "resman-properties" && <Suspense fallback={<ModuleLoading label="Loading Properties & Units" />}><ResManContextWorkspace dataset="properties_units" /></Suspense>}
        {activeModule === "resman-gl" && <Suspense fallback={<ModuleLoading label="Loading Chart of Accounts" />}><ResManContextWorkspace dataset="gl_accounts" /></Suspense>}
        {activeModule === "resman-invoices" && <Suspense fallback={<ModuleLoading label="Loading Invoice History" />}><ResManContextWorkspace dataset="invoice_history" /></Suspense>}
        {activeModule === "resman-ledger" && <Suspense fallback={<ModuleLoading label="Loading General Ledger" />}><ResManContextWorkspace dataset="general_ledger" /></Suspense>}
        {activeModule === "batches" && <Suspense fallback={null}>
          <FloatingAccountingAssistant batchId={batchId} invoiceGroupId={assistantInvoiceGroupId} />
        </Suspense>}
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
                fileAttachmentGroups={explorerAttachmentGroups}
                selectedFile={selected}
                activeDocumentPage={activeDocumentPage}
                onSelectFile={handleSelectExplorerFile}
                onSelectPage={handleSelectExplorerPage}
                onDeleteFile={handleDeleteFile}
                onUploadFiles={handleAddDocumentsToCurrentSet}
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
            style={
              {
                width: documentPanel.size,
                "--document-pane-width": `${documentPanel.size}px`,
              } as CSSProperties & Record<"--document-pane-width", string>
            }
            aria-label="Document workspace"
            data-testid="panel-document"
          >
            {renderDocumentPreview(false)}
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
              onDeletePreviewRows={handleDeletePreviewRows}
              onDeletePreviewColumns={handleDeletePreviewColumns}
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
              processingElapsedMs={processingElapsedMs}
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

      {documentDetached &&
        documentPopoutRoot &&
        createPortal(renderDocumentPreview(true), documentPopoutRoot)}

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
          deleteRowsLabel={
            (cellMenu.selectedRowIndexes?.length ?? 0) > 1
              ? `Delete ${cellMenu.selectedRowIndexes?.length} rows`
              : "Delete row"
          }
          deleteColumnsLabel={
            (cellMenu.selectedColumns?.length ?? 0) > 1
              ? `Delete ${cellMenu.selectedColumns?.length} columns`
              : "Delete column"
          }
          onDeleteRows={
            cellMenu.selectedRowIndexes?.length
              ? () => handleDeletePreviewRows(cellMenu.selectedRowIndexes ?? [])
              : undefined
          }
          onDeleteColumns={
            cellMenu.selectedColumns?.length
              ? () => handleDeletePreviewColumns(cellMenu.selectedColumns ?? [])
              : undefined
          }
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
