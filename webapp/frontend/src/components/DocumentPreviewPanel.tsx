import {
  lazy,
  memo,
  Suspense,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ClipboardEvent as ReactClipboardEvent,
  type DragEvent,
  type ReactNode,
} from "react";

import { api, getFriendlyErrorMessage } from "../api";
import { perfStart } from "../perf";
import type {
  BatchProgress,
  FileEntry,
  FilePreview,
  ProcessingRouteMode,
  ProcessingRouteSnapshot,
  UploadFileProgress,
} from "../types";
import { AiScanOverlay } from "./AiScanOverlay";
import { ProcessingRouteControl } from "./ProcessingRouteControl";

const PdfWorkspace = lazy(() =>
  import("./pdf_workspace/PdfWorkspace").then((m) => ({ default: m.PdfWorkspace })),
);

const ACTIVE_PAGE_LOCK_MS = 5000;
const ACTIVE_PAGE_SETTLE_LOCK_MS = 1200;

type Props = {
  batchId: string | null;
  filename: string | null;
  reloadToken?: number;
  collapsed?: boolean;
  targetPage?: {
    batchId: string;
    filename: string;
    pageNumber: number;
    nonce: number;
  } | null;
  onActivePageChange?: (page: {
    batchId: string;
    filename: string;
    pageNumber: number;
  }) => void;
  onToggleCollapsed?: () => void;
  // Phase 2D — module window controls. The Document panel hosts the
  // viewer for the active file; these hooks let the parent (App.tsx)
  // drive minimize / maximize / close from a unified workspace shell.
  onMaximize?: () => void;
  onPopout?: () => void;
  onReattach?: () => void;
  onClose?: () => void;
  isMaximized?: boolean;
  isDetachedWindow?: boolean;
  // Phase 2J — extraction trace overlay forwarding.
  highlightedTraceIds?: ReadonlyArray<string>;
  onTraceClick?: (traceId: string) => void;
  onTraceHover?: (traceId: string | null) => void;
  // Phase 2K — Remap mode forwarding. When `remapActive` is true the
  // PdfWorkspace forces draw mode; the next drawn bbox is reported
  // through `onRemapDrawn` instead of being persisted as a region.
  remapActive?: boolean;
  onRemapDrawn?: (page: number, bbox: { x: number; y: number; w: number; h: number }) => void;
  aiProgress?: BatchProgress | null;
  batchSelector?: ReactNode;
  files?: FileEntry[];
  fileAttachmentGroups?: Record<string, string[]>;
  uploadItems?: UploadFileProgress[];
  onAddDocuments?: (files: File[]) => void | Promise<void>;
  onProcessBatch?: () => void | Promise<void>;
  onProcessPage?: (
    pageNumber: number,
    mode?: "replace" | "merge",
    filename?: string,
  ) => void | Promise<void>;
  isProcessing?: boolean;
};

type DisplayPreview = {
  batchId: string;
  filename: string;
  preview: FilePreview;
};

type PdfDocumentSource = {
  fileId: string;
  fileUrl: string;
  renderFileUrl?: string;
  pageCount?: number | null;
};

function filesFromClipboard(data: DataTransfer | null | undefined): File[] {
  if (!data) return [];
  const files = Array.from(data.files || []).filter((file) =>
    file.type.startsWith("image/"),
  );
  if (files.length > 0) return files.map(normalizePastedScreenshot);
  return Array.from(data.items || [])
    .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
    .map((item) => item.getAsFile())
    .filter((file): file is File => Boolean(file))
    .map(normalizePastedScreenshot);
}

function normalizePastedScreenshot(file: File): File {
  const genericName = !file.name || /^(image|blob|clipboard)(\.\w+)?$/i.test(file.name);
  if (!file.type.startsWith("image/") || !genericName) return file;
  const ext = extensionForImageType(file.type) || extensionFromName(file.name) || "png";
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

function clipboardFilesKey(files: File[]): string {
  return files
    .map((file) => `${file.type || "file"}:${file.size}:${file.name || ""}`)
    .join("|");
}

async function filesFromAsyncClipboard(
  ownerWindow: Window & typeof globalThis,
): Promise<File[]> {
  const read = ownerWindow.navigator.clipboard?.read;
  if (typeof read !== "function") return [];
  try {
    const items = await read.call(ownerWindow.navigator.clipboard);
    const out: File[] = [];
    const stamp = new Date()
      .toISOString()
      .slice(0, 19)
      .replace("T", "-")
      .replace(/:/g, "");
    for (const item of items) {
      const imageType = item.types.find((type) => type.startsWith("image/"));
      if (!imageType) continue;
      const blob = await item.getType(imageType);
      const ext = extensionForImageType(blob.type) || "png";
      out.push(
        new ownerWindow.File(
          [blob],
          `screenshot-${stamp}-${out.length + 1}.${ext}`,
          {
            type: blob.type || `image/${ext}`,
            lastModified: Date.now(),
          },
        ),
      );
    }
    return out;
  } catch {
    return [];
  }
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
  return name.match(/\.([^.]+)$/)?.[1]?.toLowerCase() || "";
}

function isEditablePasteTarget(
  target: EventTarget | null,
  ownerWindow: Window & typeof globalThis,
): boolean {
  if (!(target instanceof ownerWindow.HTMLElement)) return false;
  const element = target as HTMLElement;
  const tag = element.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || element.isContentEditable;
}

function versionedUrl(url: string, token: string | number): string {
  if (!token) return url;
  const separator = url.includes("?") ? "&" : "?";
  return `${url}${separator}v=${encodeURIComponent(String(token))}`;
}

const BINARY_PREVIEW_EXTENSIONS = new Set([
  ".pdf",
  ".png",
  ".jpg",
  ".jpeg",
  ".gif",
  ".webp",
  ".bmp",
  ".tif",
  ".tiff",
]);

function previewFromFileEntry(file: FileEntry | undefined): FilePreview | null {
  if (!file) return null;
  const extension = (file.extension || `.${extensionFromName(file.filename)}`).toLowerCase();
  if (!BINARY_PREVIEW_EXTENSIONS.has(extension)) return null;
  return {
    kind: "binary",
    filename: file.filename,
    extension,
    size_bytes: file.size_bytes,
    page_count: file.page_count ?? null,
    note: "Preview is rendered by the document viewer.",
  };
}

function DocumentPreviewPanelImpl({
  batchId,
  filename,
  reloadToken = 0,
  collapsed,
  targetPage,
  onActivePageChange,
  onToggleCollapsed,
  onMaximize,
  onPopout,
  onReattach,
  onClose,
  isMaximized,
  isDetachedWindow = false,
  highlightedTraceIds,
  onTraceClick,
  onTraceHover,
  remapActive,
  onRemapDrawn,
  aiProgress,
  batchSelector,
  files = [],
  fileAttachmentGroups = {},
  uploadItems = [],
  onAddDocuments,
  onProcessBatch,
  onProcessPage,
  isProcessing = false,
}: Props) {
  const [display, setDisplay] = useState<DisplayPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [visibleFilename, setVisibleFilename] = useState<string | null>(filename);
  const [processingRoutes, setProcessingRoutes] = useState<ProcessingRouteSnapshot | null>(null);
  const [processingRoutesBusy, setProcessingRoutesBusy] = useState(false);
  const [processingRoutesError, setProcessingRoutesError] = useState<string | null>(null);
  const [emptyDropDragging, setEmptyDropDragging] = useState(false);
  const [emptyUploadCount, setEmptyUploadCount] = useState<number | null>(null);
  const emptyDropDepthRef = useRef(0);
  const emptyFileInputRef = useRef<HTMLInputElement | null>(null);
  const emptyUploadRef = useRef<HTMLDivElement | null>(null);
  const lastClipboardUploadRef = useRef<{ key: string; at: number } | null>(null);
  const previewCacheRef = useRef<Map<string, DisplayPreview>>(new Map());
  const activePageLockRef = useRef<
    | {
        batchId: string;
        filename: string;
        pageNumber: number;
        nonce: number;
        expiresAt: number;
      }
    | null
  >(null);

  const refreshProcessingRoutes = useCallback(async () => {
    if (!batchId) {
      setProcessingRoutes(null);
      setProcessingRoutesError(null);
      return;
    }
    try {
      setProcessingRoutesError(null);
      const snapshot = await api.processingRoutes(batchId);
      setProcessingRoutes(snapshot);
    } catch (routeError) {
      setProcessingRoutesError(
        getFriendlyErrorMessage(routeError, "Load processing routes"),
      );
    }
  }, [batchId]);

  useEffect(() => {
    let cancelled = false;
    if (!batchId) {
      setProcessingRoutes(null);
      setProcessingRoutesError(null);
      return;
    }
    const controller = new AbortController();
    setProcessingRoutesError(null);
    void api
      .processingRoutes(batchId, { signal: controller.signal })
      .then((snapshot) => {
        if (!cancelled) setProcessingRoutes(snapshot);
      })
      .catch((routeError) => {
        if (cancelled || controller.signal.aborted) return;
        setProcessingRoutesError(
          getFriendlyErrorMessage(routeError, "Load processing routes"),
        );
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [batchId, files.length, reloadToken]);

  const updateProcessingRoute = useCallback(
    async (update: {
      scope: "batch" | "document" | "page";
      mode: ProcessingRouteMode | null;
      filename?: string;
      page?: number;
      reset_exceptions?: boolean;
    }) => {
      if (!batchId) return;
      setProcessingRoutesBusy(true);
      setProcessingRoutesError(null);
      try {
        const snapshot = await api.updateProcessingRoute(batchId, {
          ...update,
          actor: "local_operator",
          expected_policy_version: processingRoutes?.policy_version,
        });
        setProcessingRoutes(snapshot);
      } catch (routeError) {
        setProcessingRoutesError(
          getFriendlyErrorMessage(routeError, "Update processing route"),
        );
        await refreshProcessingRoutes();
      } finally {
        setProcessingRoutesBusy(false);
      }
    },
    [batchId, processingRoutes?.policy_version, refreshProcessingRoutes],
  );

  const uploadEmptyFiles = useCallback(
    (incoming: File[]) => {
      const accepted = incoming.filter((file) => file && file.size >= 0);
      if (accepted.length === 0 || !onAddDocuments) return;
      setEmptyUploadCount(accepted.length);
      void Promise.resolve(onAddDocuments(accepted)).finally(() => {
        window.setTimeout(() => setEmptyUploadCount(null), 450);
      });
    },
    [onAddDocuments],
  );

  const uploadClipboardFiles = useCallback(
    (incoming: File[]) => {
      const accepted = incoming.filter((file) => file && file.size > 0);
      if (accepted.length === 0) return false;
      const key = clipboardFilesKey(accepted);
      const now = Date.now();
      const last = lastClipboardUploadRef.current;
      if (last && last.key === key && now - last.at < 1400) return true;
      lastClipboardUploadRef.current = { key, at: now };
      uploadEmptyFiles(accepted);
      return true;
    },
    [uploadEmptyFiles],
  );

  const handleEmptyPaste = useCallback(
    (event: ReactClipboardEvent<HTMLDivElement>) => {
      if (!onAddDocuments) return;
      const files = filesFromClipboard(event.clipboardData);
      if (files.length === 0) return;
      event.preventDefault();
      event.stopPropagation();
      uploadClipboardFiles(files);
    },
    [onAddDocuments, uploadClipboardFiles],
  );

  useEffect(() => {
    setError(null);
    setVisibleFilename(filename);
    if (!batchId || !filename) {
      setDisplay(null);
      setLoading(false);
      return;
    }

    const selectedFileEntry = files.find((file) => file.filename === filename);
    const fileVersion = selectedFileEntry
      ? `${selectedFileEntry.size_bytes ?? ""}:${selectedFileEntry.page_count ?? ""}`
      : "";
    const cacheKey = `${batchId}\u0000${filename}\u0000${reloadToken}\u0000${fileVersion}`;
    const cached = previewCacheRef.current.get(cacheKey);
    if (cached) {
      setDisplay(cached);
      setLoading(false);
      return;
    }

    const listedPreview = previewFromFileEntry(selectedFileEntry);
    if (listedPreview) {
      const next = { batchId, filename, preview: listedPreview };
      const cache = previewCacheRef.current;
      cache.set(cacheKey, next);
      while (cache.size > 24) {
        const firstKey = cache.keys().next().value;
        if (!firstKey) break;
        cache.delete(firstKey);
      }
      setDisplay(next);
      setLoading(false);
      return;
    }

    let cancelled = false;
    const controller = new AbortController();
    const finishPerf = perfStart("document.preview.load", {
      batchId,
      filename,
    });
    setLoading(true);
    (async () => {
      try {
        const p = await api.filePreview(batchId, filename, {
          signal: controller.signal,
        });
        if (!cancelled) {
          const next = { batchId, filename, preview: p };
          const cache = previewCacheRef.current;
          cache.set(cacheKey, next);
          while (cache.size > 24) {
            const firstKey = cache.keys().next().value;
            if (!firstKey) break;
            cache.delete(firstKey);
          }
          setDisplay(next);
          setLoading(false);
          finishPerf({
            status: "ok",
            kind: p.kind,
          });
        }
      } catch (e) {
        if ((e as Error)?.name === "AbortError") {
          finishPerf({ status: "aborted" });
          return;
        }
        if (!cancelled) {
          setError(getFriendlyErrorMessage(e, "Load preview"));
          setLoading(false);
          finishPerf({ status: "error" });
          // eslint-disable-next-line no-console
          console.warn("document preview failed:", e);
        }
      }
    })();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [batchId, files, filename, reloadToken]);

  useEffect(() => {
    if (!batchId || !onAddDocuments || collapsed) return;
    const ownerDocument = emptyUploadRef.current?.ownerDocument ?? document;
    const ownerWindow = ownerDocument.defaultView ?? window;
    const onPaste = (event: ClipboardEvent) => {
      const files = filesFromClipboard(event.clipboardData);
      if (files.length > 0) {
        event.preventDefault();
        event.stopPropagation();
        uploadClipboardFiles(files);
        return;
      }
      if (isEditablePasteTarget(event.target, ownerWindow)) return;
      event.preventDefault();
      event.stopPropagation();
      void filesFromAsyncClipboard(ownerWindow).then(uploadClipboardFiles);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey)) return;
      if (event.key.toLowerCase() !== "v") return;
      if (isEditablePasteTarget(event.target, ownerWindow)) return;
      const uploadSeenAt = lastClipboardUploadRef.current?.at ?? 0;
      void filesFromAsyncClipboard(ownerWindow).then((files) => {
        if ((lastClipboardUploadRef.current?.at ?? 0) > uploadSeenAt) return;
        uploadClipboardFiles(files);
      });
    };
    ownerDocument.addEventListener("paste", onPaste, { capture: true });
    ownerDocument.addEventListener("keydown", onKeyDown, { capture: true });
    return () => {
      ownerDocument.removeEventListener(
        "paste",
        onPaste as EventListener,
        { capture: true } as EventListenerOptions,
      );
      ownerDocument.removeEventListener(
        "keydown",
        onKeyDown as EventListener,
        { capture: true } as EventListenerOptions,
      );
    };
  }, [batchId, collapsed, onAddDocuments, uploadClipboardFiles]);

  useEffect(() => {
    if (!targetPage) {
      activePageLockRef.current = null;
      return;
    }
    activePageLockRef.current = {
      batchId: targetPage.batchId,
      filename: targetPage.filename,
      pageNumber: Math.max(1, Math.floor(targetPage.pageNumber || 1)),
      nonce: targetPage.nonce,
      expiresAt: Date.now() + ACTIVE_PAGE_LOCK_MS,
    };
  }, [
    targetPage?.batchId,
    targetPage?.filename,
    targetPage?.nonce,
    targetPage?.pageNumber,
  ]);

  const activePreview = display?.preview ?? null;
  const activeUploadItems = useMemo(
    () => uploadItems.filter((item) => !batchId || item.batchId === batchId),
    [batchId, uploadItems],
  );
  const contentVersion = activePreview
    ? [
        reloadToken,
        "size_bytes" in activePreview ? activePreview.size_bytes : "",
        "page_count" in activePreview ? activePreview.page_count ?? "" : "",
      ].join(":")
    : String(reloadToken || "");
  const isPdf =
    activePreview?.kind === "binary" && activePreview.extension === ".pdf";
  const pdfDocuments = useMemo<PdfDocumentSource[]>(() => {
    if (!batchId || !isPdf || !display) return [];
    const pdfFiles = files.filter(
      (file) => (file.extension || "").toLowerCase() === ".pdf",
    );
    const ownerEntry = Object.entries(fileAttachmentGroups).find(([, children]) =>
      children.includes(display.filename),
    );
    const groupParent = fileAttachmentGroups[display.filename]
      ? display.filename
      : ownerEntry?.[0] || "";
    const childOrder = groupParent ? fileAttachmentGroups[groupParent] || [] : [];
    const childSet = new Set(childOrder);
    const pdfFileByName = new Map(pdfFiles.map((file) => [file.filename, file]));
    const orderedPdfFiles =
      childOrder.length > 0
        ? [
            ...pdfFiles.filter((file) => !childSet.has(file.filename)),
            ...childOrder
              .map((childName) => pdfFileByName.get(childName))
              .filter((file): file is FileEntry => Boolean(file)),
          ]
        : pdfFiles;
    const seen = new Set<string>();
    const out: PdfDocumentSource[] = [];
    for (const file of orderedPdfFiles) {
      if (!file.filename || seen.has(file.filename)) continue;
      seen.add(file.filename);
      out.push({
        fileId: file.filename,
        fileUrl: versionedUrl(
          api.fileContentUrl(batchId, file.filename),
          [reloadToken, file.size_bytes, file.page_count ?? ""].join(":"),
        ),
        pageCount: file.page_count ?? undefined,
      });
    }
    if (!seen.has(display.filename)) {
      out.unshift({
        fileId: display.filename,
        fileUrl: versionedUrl(
          api.fileContentUrl(display.batchId, display.filename),
          contentVersion,
        ),
        pageCount:
          activePreview && "page_count" in activePreview
            ? activePreview.page_count
            : undefined,
      });
    }
    return out;
  }, [
    activePreview,
    batchId,
    contentVersion,
    display,
    fileAttachmentGroups,
    files,
    isPdf,
    reloadToken,
  ]);

  const pdfWorkspaceKey = useMemo(() => {
    if (!display || !isPdf) return `${batchId ?? "no-batch"}:${reloadToken}:empty`;
    return [
      display.batchId,
      reloadToken,
      pdfDocuments
        .map((doc) => `${doc.fileId}:${doc.pageCount ?? ""}:${doc.fileUrl}`)
        .join("|"),
    ].join("::");
  }, [batchId, display, isPdf, pdfDocuments, reloadToken]);

  const documentContextName =
    visibleFilename ?? filename ?? display?.filename ?? "No document selected";

  const header = (
    <div className="card-header doc-preview-header">
      <div className="doc-preview-header-main">
        <div className="doc-preview-context" aria-label="Current batch and document">
          {batchSelector ? (
            <div className="doc-preview-batch-selector-slot">{batchSelector}</div>
          ) : (
            <span className="doc-preview-batch-fallback">Select batch</span>
          )}
          <span className="doc-preview-context-separator" aria-hidden>
            {" / "}
          </span>
          <span className="doc-preview-title" title={documentContextName}>
            <DocIcon />
            <span className="doc-preview-name">{documentContextName}</span>
          </span>
        </div>
        {onProcessBatch && (
          <ProcessingRouteControl
            snapshot={processingRoutes}
            filename={visibleFilename ?? filename}
            busy={processingRoutesBusy}
            error={processingRoutesError}
            onRefresh={refreshProcessingRoutes}
            onSetDocument={(mode) => {
              const target = visibleFilename ?? filename;
              if (!target) return;
              return updateProcessingRoute({
                scope: "document",
                mode,
                filename: target,
              });
            }}
            onApplyBatch={(mode) =>
              updateProcessingRoute({
                scope: "batch",
                mode,
                reset_exceptions: true,
              })
            }
          />
        )}
        {onProcessBatch && (
          <button
            type="button"
            className="doc-preview-process-batch"
            onClick={() => void onProcessBatch()}
            disabled={!batchId || isProcessing}
            title={isProcessing ? "Processing is already running" : "Process this batch"}
          >
            {isProcessing ? "Processing" : "Process batch"}
          </button>
        )}
        {(onPopout || onReattach) && (
          <div className="doc-preview-window-actions">
            <button
              type="button"
              className={`doc-preview-window-btn ${
                isDetachedWindow ? "is-attached-window" : ""
              }`}
              onClick={isDetachedWindow ? onReattach : onPopout}
              title={
                isDetachedWindow
                  ? "Attach viewer back to the workspace"
                  : "Detach viewer to a separate window"
              }
              aria-label={
                isDetachedWindow
                  ? "Attach document viewer back to the workspace"
                  : "Detach document viewer to a separate window"
              }
            >
              {isDetachedWindow ? <AttachViewerIcon /> : <DetachViewerIcon />}
            </button>
          </div>
        )}
      </div>
      {/* Old single-collapse button retained for older clients that
          don't get the new controls; only renders if no onToggleCollapsed
          is wired into the new control set above (kept for safety). */}
      {false && onToggleCollapsed && (
        <button
          onClick={onToggleCollapsed}
          className="icon-btn"
          title={collapsed ? "Expand panel" : "Collapse panel"}
          aria-label={collapsed ? "Expand panel" : "Collapse panel"}
        >
          <svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2.4"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <polyline points={collapsed ? "9 18 15 12 9 6" : "15 18 9 12 15 6"} />
          </svg>
        </button>
      )}
    </div>
  );

  if (collapsed) {
    return <div className="card doc-preview-card collapsed">{header}</div>;
  }

  const handleEmptyDragEnter = (event: DragEvent<HTMLDivElement>) => {
    if (!onAddDocuments) return;
    event.preventDefault();
    event.stopPropagation();
    emptyDropDepthRef.current += 1;
    if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
    setEmptyDropDragging(true);
  };

  const handleEmptyDragOver = (event: DragEvent<HTMLDivElement>) => {
    if (!onAddDocuments) return;
    event.preventDefault();
    event.stopPropagation();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
    setEmptyDropDragging(true);
  };

  const handleEmptyDragLeave = (event: DragEvent<HTMLDivElement>) => {
    if (!onAddDocuments) return;
    event.preventDefault();
    event.stopPropagation();
    emptyDropDepthRef.current = Math.max(0, emptyDropDepthRef.current - 1);
    if (emptyDropDepthRef.current === 0) setEmptyDropDragging(false);
  };

  const handleEmptyDrop = (event: DragEvent<HTMLDivElement>) => {
    if (!onAddDocuments) return;
    event.preventDefault();
    event.stopPropagation();
    emptyDropDepthRef.current = 0;
    setEmptyDropDragging(false);
    uploadEmptyFiles(Array.from(event.dataTransfer?.files || []));
  };

  const renderUploadingWorkspace = ({
    kicker = "Building preview",
    title,
    subtitle = "Each file appears in Pages while it uploads.",
  }: {
    kicker?: string;
    title?: string;
    subtitle?: string;
  } = {}) => {
    const count = activeUploadItems.length;
    const previewItems =
      count > 140
        ? [
            ...activeUploadItems.slice(0, 90),
            ...activeUploadItems.slice(-30),
          ]
        : activeUploadItems;
    const hiddenCount = Math.max(0, count - previewItems.length);
    const resolvedTitle =
      title || `${count} document${count === 1 ? "" : "s"} incoming`;
    return (
      <div
        className={`doc-uploading-workspace ${
          emptyDropDragging ? "is-dragging" : ""
        }`}
        data-dropzone="true"
        onDragEnter={handleEmptyDragEnter}
        onDragOver={handleEmptyDragOver}
        onDragLeave={handleEmptyDragLeave}
        onDrop={handleEmptyDrop}
      >
        <div className="doc-uploading-canvas">
          <div className="doc-uploading-stage">
            <div className="doc-uploading-stage-icon" aria-hidden>
              <span />
            </div>
            <div className="doc-uploading-stage-copy">
              <span>{kicker}</span>
              <strong>{resolvedTitle}</strong>
              <small>{subtitle}</small>
            </div>
          </div>
          <div className="doc-uploading-rhythm" aria-hidden>
            <span />
            <span />
            <span />
          </div>
        </div>
        <aside className="doc-uploading-sidebar" aria-label="Uploading documents">
          <div className="doc-uploading-sidebar-header">
            <span>Pages</span>
            <small>{count} item{count === 1 ? "" : "s"}</small>
          </div>
          <div className="doc-uploading-list">
            {previewItems.map((item, index) => (
              <UploadPreviewCard key={item.id} item={item} index={index} />
            ))}
            {hiddenCount > 0 && (
              <div className="doc-uploading-list-overflow">
                {hiddenCount} more files continue uploading in the queue
              </div>
            )}
          </div>
        </aside>
      </div>
    );
  };

  if (!batchId || !filename) {
    if (batchId && activeUploadItems.length > 0) {
      return (
        <div className="card doc-preview-card">
          {header}
          {renderUploadingWorkspace()}
        </div>
      );
    }

    return (
      <div className="card doc-preview-card">
        {header}
        <div
          ref={emptyUploadRef}
          className={`doc-empty-upload ${emptyDropDragging ? "is-dragging" : ""} ${
            emptyUploadCount ? "is-uploading" : ""
          }`}
          data-dropzone="true"
          role="button"
          tabIndex={0}
          aria-label="Upload documents"
          onClick={() => emptyFileInputRef.current?.click()}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              emptyFileInputRef.current?.click();
            }
          }}
          onDragEnter={handleEmptyDragEnter}
          onDragOver={handleEmptyDragOver}
          onDragLeave={handleEmptyDragLeave}
          onDrop={handleEmptyDrop}
          onPaste={handleEmptyPaste}
        >
          <input
            ref={emptyFileInputRef}
            className="doc-empty-upload-input"
            type="file"
            multiple
            onChange={(event) => {
              uploadEmptyFiles(Array.from(event.currentTarget.files || []));
              event.currentTarget.value = "";
            }}
          />
          <div className="doc-empty-upload-card">
            <div className="doc-empty-upload-orbit" aria-hidden>
              <span className="doc-empty-upload-dot dot-a" />
              <span className="doc-empty-upload-dot dot-b" />
              <span className="doc-empty-upload-dot dot-c" />
              <span className="doc-empty-upload-core">
                <svg
                  width="25"
                  height="25"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                >
                  <path d="M12 3v12" />
                  <path d="m7 8 5-5 5 5" />
                  <path d="M5 15v2.5A2.5 2.5 0 0 0 7.5 20h9a2.5 2.5 0 0 0 2.5-2.5V15" />
                </svg>
              </span>
            </div>
            <div className="doc-empty-upload-copy">
              <div className="doc-empty-upload-kicker">
                {emptyUploadCount ? "Uploading to batch" : "Ready for documents"}
              </div>
              <div className="doc-empty-upload-title">
                {emptyUploadCount
                  ? `Adding ${emptyUploadCount} file${emptyUploadCount === 1 ? "" : "s"}`
                  : emptyDropDragging
                    ? "Drop to add files"
                    : "Drop files here"}
              </div>
              <div className="doc-empty-upload-subtitle">
                {emptyUploadCount
                  ? "Preparing the preview..."
                  : "PDF, images, Excel, CSV, or Ctrl+V screenshots"}
              </div>
            </div>
            <div className="doc-empty-upload-actions" aria-hidden>
              <span>{emptyUploadCount ? "Working" : "Choose files"}</span>
            </div>
          </div>
        </div>
      </div>
    );
  }

  if (!display && loading && activeUploadItems.length > 0) {
    const count = activeUploadItems.length;
    return (
      <div className="card doc-preview-card">
        {header}
        {renderUploadingWorkspace({
          kicker: "Opening preview",
          title: `${count} document${count === 1 ? "" : "s"} readying`,
          subtitle: "Live upload status stays visible while the first preview opens.",
        })}
      </div>
    );
  }

  if (error && !display) {
    return (
      <div className="card doc-preview-card">
        {header}
        <div className="card-body">
          <div className="error-banner">Could not load preview.</div>
        </div>
      </div>
    );
  }

  return (
    <div className="card doc-preview-card">
      {header}
      <div
        className={`card-body tight doc-preview-body ${
          loading ? "is-loading-document" : ""
        }`}
        data-testid="document-preview-body"
      >
        {!display && loading && <DocumentLoadingSkeleton />}

        {activePreview?.kind === "table" && (
          <div className="doc-preview-table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  {activePreview.headers.map((h, i) => (
                    <th key={i}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {activePreview.rows.map((row, i) => (
                  <tr key={i}>
                    {row.map((c, j) => (
                      <td key={j}>{c == null ? "" : String(c)}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {display && activePreview?.kind === "binary" && (
          <>
            {isPdf ? (
              <Suspense fallback={<DocumentLoadingSkeleton />}>
                <PdfWorkspace
                  key={pdfWorkspaceKey}
                  batchId={display.batchId}
                  fileUrl={versionedUrl(
                    api.fileContentUrl(display.batchId, display.filename),
                    contentVersion,
                  )}
                  fileId={display.filename}
                  documents={pdfDocuments}
                  targetPage={
                    targetPage &&
                    targetPage.batchId === display.batchId
                      ? targetPage
                      : null
                  }
                  onActivePageChange={(pageNumber, activeFilename) => {
                    const nextFilename = activeFilename || display.filename;
                    const normalizedPage = Math.max(1, Math.floor(pageNumber || 1));
                    const lock = activePageLockRef.current;
                    if (lock) {
                      const now = Date.now();
                      const lockMatches =
                        lock.batchId === display.batchId &&
                        lock.filename === nextFilename &&
                        lock.pageNumber === normalizedPage;
                      if (now > lock.expiresAt) {
                        activePageLockRef.current = null;
                      } else if (!lockMatches) {
                        return;
                      } else {
                        lock.expiresAt = Math.min(
                          lock.expiresAt,
                          now + ACTIVE_PAGE_SETTLE_LOCK_MS,
                        );
                      }
                    }
                    setVisibleFilename(nextFilename);
                    onActivePageChange?.({
                      batchId: display.batchId,
                      filename: nextFilename,
                      pageNumber: normalizedPage,
                    });
                  }}
                  highlightedTraceIds={highlightedTraceIds}
                  onTraceClick={onTraceClick}
                  onTraceHover={onTraceHover}
                  remapActive={remapActive}
                  onRemapDrawn={onRemapDrawn}
                  aiProgress={aiProgress}
                  onAddDocuments={onAddDocuments}
                  onProcessPage={onProcessPage}
                  processingRoutes={processingRoutes}
                  processingRouteBusy={processingRoutesBusy}
                  onSetPageRoute={(targetFilename, pageNumber, mode) =>
                    updateProcessingRoute({
                      scope: "page",
                      mode,
                      filename: targetFilename,
                      page: pageNumber,
                    })
                  }
                  processPageDisabled={isProcessing}
                  uploadItems={activeUploadItems}
                />
              </Suspense>
            ) : (
              <BinaryPreview
                url={versionedUrl(
                  api.fileContentUrl(display.batchId, display.filename),
                  contentVersion,
                )}
                extension={activePreview.extension}
                filename={activePreview.filename}
                aiProgress={aiProgress}
                onAddDocuments={onAddDocuments}
              />
            )}
          </>
        )}

        {activePreview?.kind === "metadata" && (
          <div className="empty-state small">{activePreview.note}</div>
        )}

        {loading && display && (
          <div
            className="doc-preview-loading-overlay"
            data-testid="document-loading-overlay"
          >
            <div className="doc-preview-loading-card">
              <span className="pdf-loading-dots" aria-hidden>
                <span />
                <span />
                <span />
              </span>
              <span>Loading document</span>
            </div>
          </div>
        )}

        {error && display && !loading && (
          <div className="doc-preview-inline-error">
            Could not load the selected document.
          </div>
        )}
        <AiScanOverlay
          progress={aiProgress}
          currentFilename={display?.filename ?? filename}
          variant="status"
        />
      </div>
    </div>
  );
}

export const DocumentPreviewPanel = memo(DocumentPreviewPanelImpl);

function DocumentLoadingSkeleton() {
  return (
    <div className="doc-preview-skeleton" data-testid="document-preview-skeleton">
      <div className="doc-preview-skeleton-page" />
    </div>
  );
}

function UploadPreviewCard({
  item,
  index,
}: {
  item: UploadFileProgress;
  index: number;
}) {
  const pct = clampPercent(item.percent);
  const ext = uploadExtension(item);
  const isFailed = item.status === "failed";
  const isDone = item.status === "done";
  const statusText = isFailed
    ? "Failed"
    : isDone
      ? "Ready"
      : item.status === "saving"
        ? "Saving"
      : item.status === "queued"
        ? "Waiting"
        : `${Math.round(pct)}%`;

  return (
    <article
      className={`doc-uploading-thumb phase-${item.status}`}
      style={
        { "--upload-progress": `${isFailed ? 100 : isDone ? 100 : pct}%` } as
          CSSProperties & Record<"--upload-progress", string>
      }
      aria-label={`${statusText}: ${item.filename}`}
    >
      <div className="doc-uploading-thumb-paper">
        <div className="doc-uploading-thumb-filetype">{ext}</div>
        <div className="doc-uploading-thumb-lines" aria-hidden>
          <span />
          <span />
          <span />
        </div>
        <div className="doc-uploading-thumb-progress" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(pct)}>
          <span />
        </div>
        <span className="doc-uploading-thumb-index">{index + 1}</span>
      </div>
      <div className="doc-uploading-thumb-meta">
        <span title={item.filename}>{item.filename}</span>
        <small>{isFailed && item.error ? item.error : statusText}</small>
      </div>
    </article>
  );
}

function clampPercent(value: unknown): number {
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return 0;
  return Math.min(100, Math.max(0, n));
}

function uploadExtension(item: UploadFileProgress): string {
  const raw =
    item.extension ||
    item.filename.match(/\.([^.]+)$/)?.[1] ||
    "file";
  return raw.replace(/^\./, "").slice(0, 4).toUpperCase();
}

function DocIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <polyline points="14 2 14 8 20 8" />
    </svg>
  );
}

function DetachViewerIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.9"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="4" y="6" width="11" height="11" rx="2.4" />
      <path d="M9 4h9a2 2 0 0 1 2 2v9" />
      <path d="M13 5h6v6" />
      <path d="m12 12 7-7" />
    </svg>
  );
}

function AttachViewerIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.9"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="4" y="5" width="16" height="14" rx="2.5" />
      <path d="M8 9h8" />
      <path d="M8 13h5" />
      <path d="M15 16h4v4" />
      <path d="m20 15-5 5" />
    </svg>
  );
}

function BinaryPreview({
  url,
  extension,
  filename,
  aiProgress,
  onAddDocuments,
}: {
  url: string;
  extension: string;
  filename: string;
  aiProgress?: BatchProgress | null;
  onAddDocuments?: (files: File[]) => void | Promise<void>;
}) {
  const [iframeFailed, setIframeFailed] = useState(false);

  if (extension === ".pdf") {
    if (iframeFailed) {
      return (
        <div className="empty-state small">
          PDF preview unavailable. File can still be processed.{" "}
          <a href={url} target="_blank" rel="noreferrer">
            Open in new tab
          </a>
          .
        </div>
      );
    }
    return (
      <iframe
        src={url}
        title={`Preview of ${filename}`}
        className="pdf-frame"
        onError={() => setIframeFailed(true)}
      />
    );
  }
  if ([".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"].includes(extension)) {
    return (
      <ImagePreview
        url={url}
        filename={filename}
        aiProgress={aiProgress}
        onAddDocuments={onAddDocuments}
      />
    );
  }
  return (
    <div className="empty-state small">
      Inline preview not supported for {extension}.{" "}
      <a href={url} target="_blank" rel="noreferrer">
        Open file
      </a>
      .
    </div>
  );
}

function ImagePreview({
  url,
  filename,
  aiProgress,
  onAddDocuments,
}: {
  url: string;
  filename: string;
  aiProgress?: BatchProgress | null;
  onAddDocuments?: (files: File[]) => void | Promise<void>;
}) {
  const stageRef = useRef<HTMLDivElement | null>(null);
  const addInputRef = useRef<HTMLInputElement | null>(null);
  const pasteInputRef = useRef<HTMLInputElement | null>(null);
  const lastImageClipboardUploadRef = useRef<{ key: string; at: number } | null>(null);
  const panRef = useRef<{
    pointerId: number;
    startX: number;
    startY: number;
    startScrollLeft: number;
    startScrollTop: number;
  } | null>(null);
  const zoomRef = useRef(1);
  const targetZoomRef = useRef(1);
  const lerpRafRef = useRef<number | null>(null);
  const zoomAnchorRef = useRef<{
    docX: number;
    docY: number;
    viewX: number;
    viewY: number;
  } | null>(null);
  const [zoom, setZoom] = useState(1);
  const [isPanning, setIsPanning] = useState(false);
  const [isAddDragging, setIsAddDragging] = useState(false);
  const [naturalSize, setNaturalSize] = useState<{ width: number; height: number } | null>(null);
  const [stageWidth, setStageWidth] = useState(0);

  const getStageWindow = useCallback(() => {
    return stageRef.current?.ownerDocument.defaultView ?? window;
  }, []);

  const cancelZoomLerp = useCallback(() => {
    if (lerpRafRef.current == null) return;
    getStageWindow().cancelAnimationFrame(lerpRafRef.current);
    lerpRafRef.current = null;
  }, [getStageWindow]);

  useEffect(() => {
    setZoom(1);
    zoomRef.current = 1;
    targetZoomRef.current = 1;
    zoomAnchorRef.current = null;
    cancelZoomLerp();
    setNaturalSize(null);
    setIsPanning(false);
    panRef.current = null;
    const node = stageRef.current;
    if (node) {
      node.scrollLeft = 0;
      node.scrollTop = 0;
    }
  }, [cancelZoomLerp, url]);

  useEffect(() => {
    const ownerWindow = pasteInputRef.current?.ownerDocument.defaultView ?? window;
    const id = ownerWindow.setTimeout(() => pasteInputRef.current?.focus(), 80);
    return () => ownerWindow.clearTimeout(id);
  }, [url]);

  useEffect(() => {
    zoomRef.current = zoom;
  }, [zoom]);

  useLayoutEffect(() => {
    const node = stageRef.current;
    if (!node) return undefined;
    const update = () => setStageWidth(node.clientWidth);
    update();
    const ownerWindow = node.ownerDocument.defaultView ?? window;
    const observer = new ownerWindow.ResizeObserver(update);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  const clampZoom = useCallback((next: number) => {
    return Math.min(4, Math.max(0.35, Number(next.toFixed(2))));
  }, []);

  const startZoomLerp = useCallback(() => {
    if (lerpRafRef.current != null) return;
    const ownerWindow = getStageWindow();
    const tick = () => {
      lerpRafRef.current = null;
      const target = targetZoomRef.current;
      const current = zoomRef.current;
      if (Math.abs(target - current) < 0.002) {
        zoomRef.current = target;
        setZoom(target);
        zoomAnchorRef.current = null;
        return;
      }
      const next = Number((current + (target - current) * 0.3).toFixed(4));
      zoomRef.current = next;
      setZoom(next);
      lerpRafRef.current = ownerWindow.requestAnimationFrame(tick);
    };
    lerpRafRef.current = ownerWindow.requestAnimationFrame(tick);
  }, [getStageWindow]);

  const changeZoom = useCallback(
    (delta: number) => {
      zoomAnchorRef.current = null;
      cancelZoomLerp();
      const next = clampZoom(zoomRef.current + delta);
      targetZoomRef.current = next;
      zoomRef.current = next;
      setZoom(next);
    },
    [cancelZoomLerp, clampZoom],
  );

  const resetZoom = useCallback(() => {
    zoomAnchorRef.current = null;
    cancelZoomLerp();
    zoomRef.current = 1;
    targetZoomRef.current = 1;
    setZoom(1);
    const node = stageRef.current;
    if (node) {
      node.scrollLeft = 0;
      node.scrollTop = 0;
    }
  }, [cancelZoomLerp]);

  const addFilesToBatch = useCallback(
    (incoming: File[]) => {
      if (!onAddDocuments) return;
      const accepted = incoming.filter((file) => file && file.size > 0);
      if (accepted.length === 0) return;
      const key = clipboardFilesKey(accepted);
      const now = Date.now();
      const last = lastImageClipboardUploadRef.current;
      if (last && last.key === key && now - last.at < 1400) return;
      lastImageClipboardUploadRef.current = { key, at: now };
      void Promise.resolve(onAddDocuments(accepted));
    },
    [onAddDocuments],
  );

  const handleImagePaste = useCallback(
    (event: ReactClipboardEvent<HTMLElement>) => {
      const files = filesFromClipboard(event.clipboardData);
      event.preventDefault();
      event.stopPropagation();
      if (files.length > 0) {
        addFilesToBatch(files);
        return;
      }
      const ownerWindow = event.currentTarget.ownerDocument.defaultView ?? window;
      void filesFromAsyncClipboard(ownerWindow).then(addFilesToBatch);
    },
    [addFilesToBatch],
  );

  const handleImageDragEnter = useCallback(
    (event: DragEvent<HTMLDivElement>) => {
      if (!onAddDocuments) return;
      event.preventDefault();
      event.stopPropagation();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
      setIsAddDragging(true);
    },
    [onAddDocuments],
  );

  const handleImageDragOver = useCallback(
    (event: DragEvent<HTMLDivElement>) => {
      if (!onAddDocuments) return;
      event.preventDefault();
      event.stopPropagation();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
      setIsAddDragging(true);
    },
    [onAddDocuments],
  );

  const handleImageDragLeave = useCallback((event: DragEvent<HTMLDivElement>) => {
    if (!onAddDocuments) return;
    event.preventDefault();
    event.stopPropagation();
    const related = event.relatedTarget as Node | null;
    if (related && event.currentTarget.contains(related)) return;
    setIsAddDragging(false);
  }, [onAddDocuments]);

  const handleImageDrop = useCallback(
    (event: DragEvent<HTMLDivElement>) => {
      if (!onAddDocuments) return;
      event.preventDefault();
      event.stopPropagation();
      setIsAddDragging(false);
      addFilesToBatch(Array.from(event.dataTransfer?.files || []));
    },
    [addFilesToBatch, onAddDocuments],
  );

  useEffect(() => {
    const node = stageRef.current;
    const ownerDocument = node?.ownerDocument ?? document;
    const ownerWindow = ownerDocument.defaultView ?? window;
    const onWheel = (e: WheelEvent) => {
      if (!(e.ctrlKey || e.metaKey)) return;
      const node = stageRef.current;
      if (!node) return;
      const target = e.target as Node | null;
      if (!target || !node.contains(target)) return;

      // Match the PDF viewer: capture Ctrl+wheel before Chrome can zoom
      // the whole app window. React's synthetic onWheel can be passive in
      // Chromium, so this native capture listener is intentional.
      e.preventDefault();
      e.stopPropagation();

      const rect = node.getBoundingClientRect();
      const viewX = e.clientX - rect.left;
      const viewY = e.clientY - rect.top;
      const liveZoom = Math.max(0.0001, zoomRef.current);
      zoomAnchorRef.current = {
        docX: (node.scrollLeft + viewX) / liveZoom,
        docY: (node.scrollTop + viewY) / liveZoom,
        viewX,
        viewY,
      };

      const factor = Math.exp(-e.deltaY * 0.0008);
      targetZoomRef.current = clampZoom(targetZoomRef.current * factor);
      startZoomLerp();
    };

    const onKeyDown = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey)) return;
      const key = e.key.toLowerCase();
      if (!["+", "=", "-", "_", "0"].includes(key)) return;
      const node = stageRef.current;
      if (!node) return;
      const target = ownerDocument.activeElement;
      const focusIsInViewer = target === ownerDocument.body || (target ? node.contains(target) : false);
      if (!focusIsInViewer) return;
      e.preventDefault();
      e.stopPropagation();
      if (key === "0") {
        resetZoom();
      } else if (key === "-" || key === "_") {
        changeZoom(-0.15);
      } else {
        changeZoom(0.15);
      }
    };

    ownerDocument.addEventListener("wheel", onWheel, { passive: false, capture: true });
    ownerDocument.addEventListener("keydown", onKeyDown, { capture: true });
    return () => {
      ownerDocument.removeEventListener(
        "wheel",
        onWheel as EventListener,
        { capture: true } as EventListenerOptions,
      );
      ownerDocument.removeEventListener(
        "keydown",
        onKeyDown as EventListener,
        { capture: true } as EventListenerOptions,
      );
      if (lerpRafRef.current != null) {
        ownerWindow.cancelAnimationFrame(lerpRafRef.current);
        lerpRafRef.current = null;
      }
    };
  }, [changeZoom, clampZoom, resetZoom, startZoomLerp]);

  useLayoutEffect(() => {
    const node = stageRef.current;
    const anchor = zoomAnchorRef.current;
    if (!node || !anchor) return;
    const desiredScrollX = anchor.docX * zoom - anchor.viewX;
    const desiredScrollY = anchor.docY * zoom - anchor.viewY;
    if (Math.abs(node.scrollLeft - desiredScrollX) > 0.5) {
      node.scrollLeft = desiredScrollX;
    }
    if (Math.abs(node.scrollTop - desiredScrollY) > 0.5) {
      node.scrollTop = desiredScrollY;
    }
  }, [zoom]);

  const endPan = useCallback((pointerId?: number) => {
    const node = stageRef.current;
    if (pointerId != null) {
      try {
        node?.releasePointerCapture(pointerId);
      } catch {
        // Pointer capture can already be released by the browser.
      }
    }
    panRef.current = null;
    setIsPanning(false);
  }, []);

  const fitZoom = useMemo(() => {
    if (!naturalSize || !stageWidth) return 1;
    const availableWidth = Math.max(280, stageWidth - 64);
    return Math.min(1, availableWidth / naturalSize.width);
  }, [naturalSize, stageWidth]);

  const renderedZoom = Math.max(0.01, fitZoom * zoom);
  const imagePageStyle = naturalSize
    ? {
        width: `${Math.max(180, Math.round(naturalSize.width * renderedZoom))}px`,
        height: `${Math.max(180, Math.round(naturalSize.height * renderedZoom))}px`,
      }
    : { width: `${Math.round(zoom * 100)}%` };

  return (
    <div
      className={`image-workspace ${isAddDragging ? "is-add-dragging" : ""}`}
      data-dropzone="true"
      data-testid="image-preview-workspace"
      tabIndex={0}
      onPaste={handleImagePaste}
      onDragEnter={handleImageDragEnter}
      onDragOver={handleImageDragOver}
      onDragLeave={handleImageDragLeave}
      onDrop={handleImageDrop}
    >
      <div className="viewer-toolbar image-viewer-toolbar">
        <div className="toolbar-group toolbar-group-zoom">
          <button
            className="tool-btn tool-btn-icon"
            type="button"
            onClick={() => changeZoom(-0.15)}
            title="Zoom out"
            aria-label="Zoom out"
          >
            -
          </button>
          <button
            className="tool-btn tool-btn-narrow"
            type="button"
            onClick={resetZoom}
            title="Fit image"
          >
            {Math.round(zoom * 100)}%
          </button>
          <button
            className="tool-btn tool-btn-icon"
            type="button"
            onClick={() => changeZoom(0.15)}
            title="Zoom in"
            aria-label="Zoom in"
          >
            +
          </button>
        </div>
        <div className="toolbar-spacer" />
        <div className="toolbar-meta">Drag to pan</div>
        {onAddDocuments && (
          <>
            <input
              ref={pasteInputRef}
              className="image-paste-capture"
              type="text"
              value=""
              placeholder="Paste screenshot"
              aria-label="Paste screenshots into this batch"
              onChange={() => undefined}
              onPaste={handleImagePaste}
              onKeyDown={(event) => {
                if (!(event.ctrlKey || event.metaKey)) return;
                if (event.key.toLowerCase() !== "v") return;
                const ownerWindow =
                  event.currentTarget.ownerDocument.defaultView ?? window;
                void filesFromAsyncClipboard(ownerWindow).then(addFilesToBatch);
              }}
              onFocus={(event) => event.currentTarget.select()}
            />
            <button
              type="button"
              className="tool-btn image-add-document-btn"
              onClick={() => addInputRef.current?.click()}
              title="Add screenshots or files to this batch"
            >
              + Add
            </button>
            <input
              ref={addInputRef}
              className="image-add-document-input"
              type="file"
              multiple
              onChange={(event) => {
                addFilesToBatch(Array.from(event.currentTarget.files || []));
                event.currentTarget.value = "";
              }}
              tabIndex={-1}
              aria-hidden="true"
            />
          </>
        )}
      </div>

      <div
        ref={stageRef}
        className={`image-preview-stage ${isPanning ? "is-panning" : ""}`}
        onPointerDown={(e) => {
          if (e.button !== 0) return;
          const node = stageRef.current;
          if (!node) return;
          panRef.current = {
            pointerId: e.pointerId,
            startX: e.clientX,
            startY: e.clientY,
            startScrollLeft: node.scrollLeft,
            startScrollTop: node.scrollTop,
          };
          node.setPointerCapture(e.pointerId);
          setIsPanning(true);
          e.preventDefault();
        }}
        onPointerMove={(e) => {
          const pan = panRef.current;
          const node = stageRef.current;
          if (!pan || !node) return;
          node.scrollLeft = pan.startScrollLeft - (e.clientX - pan.startX);
          node.scrollTop = pan.startScrollTop - (e.clientY - pan.startY);
        }}
        onPointerUp={(e) => endPan(e.pointerId)}
        onPointerCancel={(e) => endPan(e.pointerId)}
      >
        <div
          className="image-preview-page"
          style={imagePageStyle}
        >
          <img
            src={url}
            alt={filename}
            draggable={false}
            onLoad={(event) => {
              const img = event.currentTarget;
              setNaturalSize({
                width: img.naturalWidth || img.clientWidth || 1,
                height: img.naturalHeight || img.clientHeight || 1,
              });
            }}
          />
          <AiScanOverlay
            progress={aiProgress}
            currentFilename={filename}
            variant="document"
          />
        </div>
      </div>
    </div>
  );
}
