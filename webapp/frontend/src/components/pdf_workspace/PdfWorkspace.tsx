// Phase 1H — top-level PDF workspace.
//
// Composes the toolbar, the canvas (PDF.js render), and the overlay
// (region drawing + select/move/resize/delete). Region state is persisted
// to the backend via the `api.replaceRegions` PUT endpoint after each
// save. Selecting a different file resets the selection to page 1.

import {
  type ChangeEvent,
  type ClipboardEvent,
  type CSSProperties,
  type DragEvent,
  type UIEvent,
  type WheelEvent as ReactWheelEvent,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { api, getFriendlyErrorMessage, isApiError } from "../../api";
import type {
  BatchProgress,
  ProcessingRouteMode,
  ProcessingRouteSnapshot,
  RegionHint,
  RegionLabel,
  TraceItem,
  UploadFileProgress,
} from "../../types";
import { AiScanOverlay } from "../AiScanOverlay";
import { KebabMenu } from "../KebabMenu";
import {
  findPageRouteDecision,
  shortRouteBadge,
} from "../ProcessingRouteControl";
import { PdfOverlay } from "./PdfOverlay";
import { loadPdfDocument, PdfPageCanvas } from "./PdfPageCanvas";
import { TraceOverlay } from "./TraceOverlay";
import { ViewerToolbar } from "./ViewerToolbar";
import type { Tool } from "./types";

type Props = {
  batchId: string;
  fileUrl: string;
  fileId: string; // filename inside the batch input/ folder
  documents?: PdfDocumentSource[];
  targetPage?: { filename?: string; pageNumber: number; nonce: number } | null;
  onActivePageChange?: (pageNumber: number, filename?: string) => void;
  // Phase 2J — Extraction Trace Overlay.
  // Trace ids the parent wants visually highlighted (e.g. set when a
  // template row is selected and we want to surface the regions that
  // fed it). Empty / undefined leaves nothing pre-highlighted.
  highlightedTraceIds?: ReadonlyArray<string>;
  // Called when the user clicks an overlay region. The parent uses
  // this to focus the corresponding template row.
  onTraceClick?: (traceId: string) => void;
  // Called when the user hovers an overlay region (id) or leaves it (null).
  onTraceHover?: (traceId: string | null) => void;
  // Phase 2K — Remap mode. When active, drawing a region calls
  // `onRemapDrawn` instead of persisting a regular region hint.
  remapActive?: boolean;
  onRemapDrawn?: (
    page: number,
    bbox: { x: number; y: number; w: number; h: number },
  ) => void;
  aiProgress?: BatchProgress | null;
  onAddDocuments?: (files: File[]) => void | Promise<void>;
  onProcessPage?: (
    pageNumber: number,
    mode?: "replace" | "merge",
    filename?: string,
  ) => void | Promise<void>;
  processingRoutes?: ProcessingRouteSnapshot | null;
  processingRouteBusy?: boolean;
  onSetPageRoute?: (
    filename: string,
    pageNumber: number,
    mode: ProcessingRouteMode | null,
  ) => void | Promise<void>;
  processPageDisabled?: boolean;
  uploadItems?: UploadFileProgress[];
};

type PageSize = { width: number; height: number };
type PdfDocumentSource = {
  fileId: string;
  fileUrl: string;
  renderFileUrl?: string;
  pageCount?: number | null;
};
type PdfDocumentMetadata = {
  pageCount: number;
  naturalSize: PageSize;
};
type PdfDocumentPage = {
  globalPageNumber: number;
  fileId: string;
  fileUrl: string;
  renderFileUrl: string;
  renderPageNumber: number;
  localPageNumber: number;
  naturalSize: PageSize;
};
type ScrollToPageOptions = {
  force?: boolean;
};
type ZoomAnchor = {
  docX: number;
  docY: number;
  viewX: number;
  viewY: number;
  pageNumber?: number;
  pageRatioX?: number;
  pageRatioY?: number;
};

function PdfUploadThumbnail({ item }: { item: UploadFileProgress }) {
  const pct = clampUploadPercent(item.percent);
  const ext = uploadThumbExtension(item);
  const isFailed = item.status === "failed";
  const isDone = item.status === "done";
  const label = isFailed
    ? "Failed"
    : isDone
      ? "Ready"
      : item.status === "saving"
        ? "Saving"
      : item.status === "queued"
        ? "Waiting"
        : `${Math.round(pct)}%`;

  return (
    <div
      className={`pdf-page-upload-thumb phase-${item.status}`}
      style={
        { "--upload-progress": `${isFailed ? 100 : isDone ? 100 : pct}%` } as
          CSSProperties & Record<"--upload-progress", string>
      }
      data-testid="pdf-page-upload-thumbnail"
      aria-label={`${label}: ${item.filename}`}
    >
      <div className="pdf-page-upload-thumb-paper">
        <span className="pdf-page-upload-type">{ext}</span>
        <span className="pdf-page-upload-scan" aria-hidden />
        <span className="pdf-page-upload-progress" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(pct)}>
          <span />
        </span>
      </div>
      <div className="pdf-page-upload-meta">
        <span title={item.filename}>{item.filename}</span>
        <small>{isFailed && item.error ? item.error : label}</small>
      </div>
    </div>
  );
}

function clampUploadPercent(value: unknown): number {
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return 0;
  return Math.min(100, Math.max(0, n));
}

function uploadThumbExtension(item: UploadFileProgress): string {
  const raw =
    item.extension ||
    item.filename.match(/\.([^.]+)$/)?.[1] ||
    "file";
  return raw.replace(/^\./, "").slice(0, 4).toUpperCase();
}

const PAGE_THUMB_WIDTH = 104;
const DEFAULT_PAGE_SIZE: PageSize = { width: 612, height: 792 };
const PROGRAMMATIC_SCROLL_SETTLE_MS = 1200;
const PAGE_REEL_SCROLL_MS = 260;
const PAGE_SCROLL_TOP_GUTTER = 12;
const BACKGROUND_METADATA_LIMIT = 24;
const BACKGROUND_METADATA_CONCURRENCY = 2;
const THUMBNAIL_ITEM_ESTIMATED_HEIGHT = 158;
const THUMBNAIL_WINDOW_OVERSCAN = 8;
const MAIN_PAGE_GAP = 16;
const MAIN_PAGE_OVERSCAN_PX = 3200;
const MAIN_SCROLL_RENDER_STEP_PX = 220;
const ACTIVE_PAGE_ECHO_SUPPRESS_MS = 1200;

function sourcePageCount(source: PdfDocumentSource): number {
  const n = Number(source.pageCount);
  return Number.isFinite(n) && n > 0 ? Math.max(1, Math.floor(n)) : 1;
}

function totalPagesForSources(
  sources: PdfDocumentSource[],
  metadata: Record<string, PdfDocumentMetadata>,
): number {
  return Math.max(
    1,
    sources.reduce(
      (sum, source) =>
        sum + Math.max(1, metadata[source.fileId]?.pageCount || sourcePageCount(source)),
      0,
    ),
  );
}

function firstPageIndexAtOffset(
  offsets: number[],
  heights: number[],
  offset: number,
): number {
  if (offsets.length === 0) return 0;
  let lo = 0;
  let hi = offsets.length - 1;
  let answer = offsets.length - 1;
  while (lo <= hi) {
    const mid = Math.floor((lo + hi) / 2);
    const bottom = offsets[mid] + heights[mid] + MAIN_PAGE_GAP;
    if (bottom >= offset) {
      answer = mid;
      hi = mid - 1;
    } else {
      lo = mid + 1;
    }
  }
  return answer;
}

function strongestPageIndexForViewport(
  offsets: number[],
  heights: number[],
  viewportTop: number,
  viewportHeight: number,
  currentIndex: number,
): number {
  if (offsets.length === 0) return 0;
  const viewportBottom = viewportTop + Math.max(1, viewportHeight);
  const viewportCenter = viewportTop + Math.max(1, viewportHeight) / 2;
  const startIndex = Math.max(0, firstPageIndexAtOffset(offsets, heights, viewportTop) - 2);
  const endIndex = Math.min(
    offsets.length - 1,
    firstPageIndexAtOffset(offsets, heights, viewportBottom) + 2,
  );
  let bestIndex = Math.min(Math.max(0, currentIndex), offsets.length - 1);
  let bestScore = Number.NEGATIVE_INFINITY;
  let currentScore = Number.NEGATIVE_INFINITY;

  for (let index = startIndex; index <= endIndex; index += 1) {
    const top = offsets[index] ?? 0;
    const height = Math.max(1, heights[index] ?? DEFAULT_PAGE_SIZE.height);
    const bottom = top + height;
    const visibleHeight = Math.max(
      0,
      Math.min(bottom, viewportBottom) - Math.max(top, viewportTop),
    );
    if (visibleHeight <= 0 && index !== currentIndex) continue;

    const visibleRatio = visibleHeight / height;
    const pageCenter = top + height / 2;
    const distance = Math.abs(pageCenter - viewportCenter);
    const score = visibleHeight + visibleRatio * 90 - distance * 0.028;
    if (index === currentIndex) currentScore = score;
    if (score > bestScore) {
      bestScore = score;
      bestIndex = index;
    }
  }

  if (
    bestIndex !== currentIndex &&
    Number.isFinite(currentScore) &&
    Number.isFinite(bestScore) &&
    bestScore - currentScore < 56
  ) {
    return currentIndex;
  }
  return bestIndex;
}

function offsetTopWithinScroller(root: HTMLElement, target: HTMLElement): number {
  let top = 0;
  let node: HTMLElement | null = target;
  while (node && node !== root) {
    top += node.offsetTop;
    node = node.offsetParent as HTMLElement | null;
  }
  if (node === root) return top;

  const rootRect = root.getBoundingClientRect();
  const targetRect = target.getBoundingClientRect();
  return root.scrollTop + targetRect.top - rootRect.top;
}

function keepElementInsideScroller(
  scroller: HTMLElement,
  target: HTMLElement,
  padding = 16,
): void {
  const scrollerRect = scroller.getBoundingClientRect();
  const targetRect = target.getBoundingClientRect();
  const upperBound = scrollerRect.top + padding;
  const lowerBound = scrollerRect.bottom - padding;

  if (targetRect.top < upperBound) {
    scroller.scrollTop += targetRect.top - upperBound;
  } else if (targetRect.bottom > lowerBound) {
    scroller.scrollTop += targetRect.bottom - lowerBound;
  }
}

function clampUnit(value: number): number {
  if (!Number.isFinite(value)) return 0.5;
  return Math.min(1, Math.max(0, value));
}

function easeOutCubic(t: number): number {
  return 1 - Math.pow(1 - t, 3);
}

function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    Boolean(window.matchMedia?.("(prefers-reduced-motion: reduce)").matches)
  );
}

function formatFileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 KB";
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0)} MB`;
}

function extensionFromMime(type: string): string {
  if (type === "image/jpeg") return "jpg";
  if (type === "image/webp") return "webp";
  if (type === "image/gif") return "gif";
  if (type === "image/bmp") return "bmp";
  return "png";
}

function timestampForPastedFile(): string {
  return new Date()
    .toISOString()
    .slice(0, 19)
    .replace("T", "-")
    .replace(/:/g, "");
}

function normalizeIncomingFiles(files: File[], source: "browse" | "drop" | "paste"): File[] {
  if (source !== "paste") return files;
  const stamp = timestampForPastedFile();
  return files.map((file, index) => {
    const genericName = !file.name || /^(image|blob|clipboard)(\.\w+)?$/i.test(file.name);
    if (!file.type.startsWith("image/") || !genericName) return file;
    return new File([file], `screenshot-${stamp}-${index + 1}.${extensionFromMime(file.type)}`, {
      type: file.type || "image/png",
      lastModified: Date.now(),
    });
  });
}

function StagedDocumentAttachment({
  file,
  index,
  onRemove,
}: {
  file: File;
  index: number;
  onRemove: (index: number) => void;
}) {
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);

  useEffect(() => {
    if (!file.type.startsWith("image/")) {
      setPreviewUrl(null);
      return;
    }
    const url = URL.createObjectURL(file);
    setPreviewUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);

  const extension = file.name.split(".").pop()?.slice(0, 5).toUpperCase() || "FILE";

  return (
    <div
      className={`pdf-add-chat-attachment ${previewUrl ? "is-image" : "is-file"}`}
      title={`${file.name || "Untitled file"} - ${formatFileSize(file.size)}`}
    >
      {previewUrl ? (
        <img src={previewUrl} alt="" />
      ) : (
        <span className="pdf-add-chat-file-badge">{extension}</span>
      )}
      <button
        type="button"
        className="pdf-add-chat-remove"
        onClick={() => onRemove(index)}
        aria-label={`Remove ${file.name || "file"}`}
      >
        x
      </button>
    </div>
  );
}

function PdfPageThumbnail({
  fileUrl,
  pageNumber,
  displayPageNumber,
  naturalSize,
  deferRender = false,
}: {
  fileUrl: string;
  pageNumber: number;
  displayPageNumber?: number;
  naturalSize: PageSize | null;
  deferRender?: boolean;
}) {
  const holderRef = useRef<HTMLDivElement | null>(null);
  const [shouldRender, setShouldRender] = useState(false);

  useEffect(() => {
    setShouldRender(false);
    const node = holderRef.current;
    if (!node) return;
    if (typeof IntersectionObserver === "undefined") {
      setShouldRender(true);
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setShouldRender(true);
          observer.disconnect();
        }
      },
      { root: null, rootMargin: "260px 0px", threshold: 0.01 },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [fileUrl, pageNumber]);

  const thumbnailZoom = useMemo(() => {
    const width = naturalSize?.width ?? 612;
    return PAGE_THUMB_WIDTH / width;
  }, [naturalSize?.width]);
  const aspectRatio = naturalSize
    ? `${naturalSize.width} / ${naturalSize.height}`
    : "8.5 / 11";

  return (
    <div
      ref={holderRef}
      className="pdf-page-thumb-paper"
      style={{ aspectRatio }}
      aria-hidden="true"
    >
      {shouldRender && !deferRender ? (
        <div className="pdf-page-thumb-render">
          <PdfPageCanvas
            fileUrl={fileUrl}
            pageNumber={pageNumber}
            zoom={thumbnailZoom}
            initialNaturalSize={naturalSize}
            suppressFirstFramePlaceholder
          />
        </div>
      ) : (
        <div className="pdf-page-thumb-placeholder">
          <span />
          <span />
          <span />
          <span />
        </div>
      )}
      <span className="pdf-page-thumb-number">{displayPageNumber ?? pageNumber}</span>
    </div>
  );
}

export function PdfWorkspace({
  batchId,
  fileUrl,
  fileId,
  documents,
  targetPage,
  onActivePageChange,
  highlightedTraceIds,
  onTraceClick,
  onTraceHover,
  remapActive,
  onRemapDrawn,
  aiProgress,
  onAddDocuments,
  onProcessPage,
  processingRoutes,
  processingRouteBusy = false,
  onSetPageRoute,
  processPageDisabled = false,
  uploadItems = [],
}: Props) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const workspaceRef = useRef<HTMLDivElement | null>(null);
  const pageRefs = useRef<Record<number, HTMLDivElement | null>>({});
  const thumbnailRefs = useRef<Record<number, HTMLDivElement | null>>({});
  const thumbnailListRef = useRef<HTMLDivElement | null>(null);
  const addDocumentsInputRef = useRef<HTMLInputElement | null>(null);
  const addDocumentsTextRef = useRef<HTMLTextAreaElement | null>(null);
  const addDocumentsButtonRef = useRef<HTMLButtonElement | null>(null);
  const addComposerRef = useRef<HTMLElement | null>(null);
  const ratiosRef = useRef<Map<number, number>>(new Map());
  const activePageNumberRef = useRef(1);
  const programmaticScrollTargetRef = useRef<number | null>(null);
  const programmaticScrollTimerRef = useRef<number | null>(null);
  const pendingScrollTargetRef = useRef<number | null>(null);
  const pendingScrollBehaviorRef = useRef<ScrollBehavior>("auto");
  const scrollAnimationFrameRef = useRef<number | null>(null);
  const scrollAnimationGenerationRef = useRef(0);
  const scrollNavigationSerialRef = useRef(0);
  const scrollTrailingSyncTimerRef = useRef<number | null>(null);
  const zoomSelectionLockRef = useRef<{ pageNumber: number; serial: number; until: number } | null>(null);
  const zoomSelectionLockTimerRef = useRef<number | null>(null);
  const zoomSelectionLockSerialRef = useRef(0);
  const zoomAnchorClearTimerRef = useRef<number | null>(null);
  const zoomAnchorSerialRef = useRef(0);
  const scrollRootCleanupRef = useRef<(() => void) | null>(null);
  const syncActivePageFromScrollRef = useRef<(() => void) | null>(null);
  const activePageNotifyTimerRef = useRef<number | null>(null);
  const pendingActivePageNotifyRef = useRef<number | null>(null);
  const lastManualScrollAtRef = useRef(0);
  const lastEmittedActivePageRef = useRef<{
    fileId?: string;
    localPageNumber: number;
    globalPageNumber: number;
    at: number;
  } | null>(null);
  const panelTogglePreservePageRef = useRef<number | null>(null);
  const previousPagePanelOpenRef = useRef(true);
  const selectedDocumentNavigationKeyRef = useRef<string | null>(null);
  const selectedDocumentSetKeyRef = useRef<string | null>(null);
  const appliedTargetPageKeyRef = useRef<string | null>(null);
  const mainScrollMetricsRef = useRef({ top: 0, renderTop: 0, height: 0 });
  const [activePageNumber, setActivePageNumber] = useState(1);
  const commitActivePageNumber = useCallback((pageNumber: number) => {
    const nextPage = Math.max(1, Math.floor(pageNumber || 1));
    activePageNumberRef.current = nextPage;
    setActivePageNumber(nextPage);
  }, []);
  const [scrollRootVersion, setScrollRootVersion] = useState(0);
  const [scrollRequestVersion, setScrollRequestVersion] = useState(0);
  const [pagePanelOpen, setPagePanelOpen] = useState(true);
  const [mainScrollTop, setMainScrollTop] = useState(0);
  const [mainViewportHeight, setMainViewportHeight] = useState(0);
  const [thumbnailScrollTop, setThumbnailScrollTop] = useState(0);
  const [thumbnailViewportHeight, setThumbnailViewportHeight] = useState(0);
  const [pageCount, setPageCount] = useState(0);
  const [pageSizes, setPageSizes] = useState<Record<number, PageSize>>({});
  const [documentMetadata, setDocumentMetadata] = useState<Record<string, PdfDocumentMetadata>>({});
  const [addComposerOpen, setAddComposerOpen] = useState(false);
  const [addComposerAnchor, setAddComposerAnchor] = useState({ left: 12, top: 64 });
  const [stagedDocuments, setStagedDocuments] = useState<File[]>([]);
  const [addComposerDragging, setAddComposerDragging] = useState(false);
  const [addComposerUploading, setAddComposerUploading] = useState(false);
  const [naturalPageSize, setNaturalPageSize] = useState<PageSize | null>(null);
  const [firstFrameReady, setFirstFrameReady] = useState(false);
  const [metadataFileUrl, setMetadataFileUrl] = useState<string | null>(null);
  const [firstFrameFileUrl, setFirstFrameFileUrl] = useState<string | null>(null);
  const [focusedPageNumber, setFocusedPageNumber] = useState<number | null>(null);
  // ``userZoom`` is what the operator chose via the toolbar / Ctrl+wheel.
  // ``effectiveZoom`` (computed below) is what the canvas actually
  // renders at — it can be clamped by the container's width so the
  // page auto-shrinks on a narrow window. Auto-grow is intentionally
  // NOT implemented: widening the window past the user's zoom keeps
  // the page at userZoom (no jumpy reflow).
  const [userZoom, setUserZoom] = useState(1.0);
  const zoomRef = useRef(1.0);
  const [containerWidth, setContainerWidth] = useState<number | null>(null);
  const updateMainScrollMetrics = useCallback((node: HTMLDivElement) => {
    const nextTop = Math.max(0, node.scrollTop);
    const nextHeight = Math.max(0, node.clientHeight);
    const current = mainScrollMetricsRef.current;
    if (Math.abs(current.top - nextTop) > 0.5) {
      current.top = nextTop;
    }
    if (
      Math.abs(current.renderTop - nextTop) > MAIN_SCROLL_RENDER_STEP_PX ||
      (nextTop === 0 && current.renderTop !== 0)
    ) {
      current.renderTop = nextTop;
      setMainScrollTop(nextTop);
    }
    if (Math.abs(current.height - nextHeight) > 0.5) {
      current.height = nextHeight;
      setMainViewportHeight(nextHeight);
    }
  }, []);
  const setScrollRoot = useCallback((node: HTMLDivElement | null) => {
    if (scrollRef.current === node) return;
    scrollRootCleanupRef.current?.();
    scrollRootCleanupRef.current = null;
    scrollRef.current = node;
    if (node) {
      updateMainScrollMetrics(node);
      const nodeWindow = node.ownerDocument.defaultView ?? window;
      let syncFrame: number | null = null;
      const syncNow = () => {
        if (syncFrame != null) return;
        syncFrame = nodeWindow.requestAnimationFrame(() => {
          syncFrame = null;
          updateMainScrollMetrics(node);
          syncActivePageFromScrollRef.current?.();
        });
        if (scrollTrailingSyncTimerRef.current != null) {
          nodeWindow.clearTimeout(scrollTrailingSyncTimerRef.current);
        }
        scrollTrailingSyncTimerRef.current = nodeWindow.setTimeout(() => {
          scrollTrailingSyncTimerRef.current = null;
          updateMainScrollMetrics(node);
          syncActivePageFromScrollRef.current?.();
        }, 90);
      };
      node.addEventListener("scroll", syncNow, { passive: true });
      scrollRootCleanupRef.current = () => {
        node.removeEventListener("scroll", syncNow);
        if (syncFrame != null) nodeWindow.cancelAnimationFrame(syncFrame);
        if (scrollTrailingSyncTimerRef.current != null) {
          nodeWindow.clearTimeout(scrollTrailingSyncTimerRef.current);
          scrollTrailingSyncTimerRef.current = null;
        }
      };
    }
    setScrollRootVersion((version) => version + 1);
  }, [updateMainScrollMetrics]);
  const setThumbnailListNode = useCallback((node: HTMLDivElement | null) => {
    thumbnailListRef.current = node;
    if (node) {
      setThumbnailScrollTop(node.scrollTop);
      setThumbnailViewportHeight(node.clientHeight);
    }
  }, []);
  const handleThumbnailListScroll = useCallback(
    (event: UIEvent<HTMLDivElement>) => {
      setThumbnailScrollTop(event.currentTarget.scrollTop);
    },
    [],
  );
  // First page width at zoom=1 — used as the reference for fit-to-width.
  // We back this out from the rendered ``pageSizes[1].width`` divided by
  // the zoom that produced it, then cache the value across re-renders.
  const naturalPageWidthRef = useRef<number | null>(null);
  const [tool, setTool] = useState<Tool>("select");
  const [drawLabel, setDrawLabel] = useState<RegionLabel>("service_address");
  const [allRegions, setAllRegions] = useState<RegionHint[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  // Phase 2J — Extraction Trace Overlay.
  const [traces, setTraces] = useState<TraceItem[]>([]);
  const [tracesByFileId, setTracesByFileId] = useState<Record<string, TraceItem[]>>({});
  const [tracesEnabled, setTracesEnabled] = useState<boolean>(true);
  const [hoveredTraceId, setHoveredTraceId] = useState<string | null>(null);
  const onActivePageChangeRef = useRef(onActivePageChange);

  const documentSources = useMemo<PdfDocumentSource[]>(() => {
    const sourceList = documents && documents.length > 0
      ? documents
      : [{ fileId, fileUrl, pageCount: null }];
    const seen = new Set<string>();
    const normalized: PdfDocumentSource[] = [];
    for (const source of sourceList) {
      const sourceId = source.fileId || fileId;
      if (!sourceId || seen.has(sourceId)) continue;
      seen.add(sourceId);
      normalized.push({
        fileId: sourceId,
        fileUrl: source.fileUrl || fileUrl,
        renderFileUrl: source.renderFileUrl,
        pageCount: source.pageCount ?? null,
      });
    }
    return normalized.length > 0 ? normalized : [{ fileId, fileUrl, pageCount: null }];
  }, [documents, fileId, fileUrl]);

  const documentSetKey = useMemo(
    () =>
      documentSources
        .map((source) => `${source.fileId}:${source.fileUrl}:${source.renderFileUrl ?? ""}:${source.pageCount ?? ""}`)
        .join("|"),
    [documentSources],
  );

  const documentPages = useMemo<PdfDocumentPage[]>(() => {
    if (metadataFileUrl !== documentSetKey) return [];
    const pages: PdfDocumentPage[] = [];
    let globalPageNumber = 1;
    let renderPageOffset = 0;
    for (const source of documentSources) {
      const metadata = documentMetadata[source.fileId];
      const pageTotal = Math.max(1, metadata?.pageCount || source.pageCount || 1);
      const naturalSize = metadata?.naturalSize || naturalPageSize || DEFAULT_PAGE_SIZE;
      for (let localPageNumber = 1; localPageNumber <= pageTotal; localPageNumber += 1) {
        pages.push({
          globalPageNumber,
          fileId: source.fileId,
          fileUrl: source.fileUrl,
          renderFileUrl: source.renderFileUrl || source.fileUrl,
          renderPageNumber: source.renderFileUrl
            ? renderPageOffset + localPageNumber
            : localPageNumber,
          localPageNumber,
          naturalSize,
        });
        globalPageNumber += 1;
      }
      renderPageOffset += pageTotal;
    }
    return pages;
  }, [documentMetadata, documentSetKey, documentSources, metadataFileUrl, naturalPageSize]);

  const pageByGlobalNumber = useMemo(() => {
    const byNumber = new Map<number, PdfDocumentPage>();
    for (const page of documentPages) byNumber.set(page.globalPageNumber, page);
    return byNumber;
  }, [documentPages]);

  const globalPageForFilePage = useCallback(
    (targetFileId: string | undefined, targetPageNumber: number) => {
      const fileToFind = targetFileId || fileId;
      return (
        documentPages.find(
          (page) =>
            page.fileId === fileToFind &&
            page.localPageNumber === Math.max(1, targetPageNumber),
        )?.globalPageNumber ?? Math.max(1, targetPageNumber)
      );
    },
    [documentPages, fileId],
  );

  const notifyActivePageChange = useCallback(
    (globalPageNumber: number) => {
      const page = pageByGlobalNumber.get(globalPageNumber);
      const localPageNumber = page?.localPageNumber ?? globalPageNumber;
      const activeFileId = page?.fileId;
      lastEmittedActivePageRef.current = {
        fileId: activeFileId,
        localPageNumber,
        globalPageNumber,
        at: performance.now(),
      };
      onActivePageChangeRef.current?.(localPageNumber, activeFileId);
    },
    [pageByGlobalNumber],
  );

  const cancelDeferredActivePageNotify = useCallback(() => {
    const ownerWindow = scrollRef.current?.ownerDocument.defaultView ?? window;
    if (activePageNotifyTimerRef.current != null) {
      ownerWindow.clearTimeout(activePageNotifyTimerRef.current);
      activePageNotifyTimerRef.current = null;
    }
    pendingActivePageNotifyRef.current = null;
  }, []);

  const deferActivePageNotify = useCallback(
    (globalPageNumber: number) => {
      const root = scrollRef.current;
      const ownerWindow = root?.ownerDocument.defaultView ?? window;
      pendingActivePageNotifyRef.current = globalPageNumber;
      if (activePageNotifyTimerRef.current != null) {
        ownerWindow.clearTimeout(activePageNotifyTimerRef.current);
      }
      activePageNotifyTimerRef.current = ownerWindow.setTimeout(() => {
        const pendingPage = pendingActivePageNotifyRef.current;
        activePageNotifyTimerRef.current = null;
        pendingActivePageNotifyRef.current = null;
        if (pendingPage != null && programmaticScrollTargetRef.current == null) {
          notifyActivePageChange(pendingPage);
        }
      }, 80);
    },
    [notifyActivePageChange],
  );

  const setPagePanelVisible = useCallback((open: boolean) => {
    panelTogglePreservePageRef.current = activePageNumberRef.current;
    setPagePanelOpen(open);
  }, []);

  const updateAddComposerAnchor = useCallback(() => {
    const root = workspaceRef.current;
    const trigger = addDocumentsButtonRef.current;
    if (!root || !trigger) return;
    const rootRect = root.getBoundingClientRect();
    const triggerRect = trigger.getBoundingClientRect();
    const composerWidth = Math.min(330, Math.max(280, rootRect.width - 36));
    const composerHeight = stagedDocuments.length > 0 ? 430 : 320;
    const gap = 12;
    const minLeft = 12;
    const maxLeft = Math.max(minLeft, rootRect.width - composerWidth - 12);
    const triggerCenterX = triggerRect.left - rootRect.left + triggerRect.width / 2;
    const leftOfTrigger = triggerCenterX - composerWidth - gap;
    const rightOfTrigger = triggerCenterX + gap;
    const rawLeft = leftOfTrigger >= minLeft ? leftOfTrigger : rightOfTrigger;
    const left = Math.min(Math.max(rawLeft, minLeft), maxLeft);
    const minTop = 42;
    const maxTop = Math.max(minTop, rootRect.height - composerHeight - 12);
    const triggerCenter = triggerRect.top - rootRect.top + triggerRect.height / 2;
    const top = Math.min(Math.max(triggerCenter - composerHeight / 2, minTop), maxTop);
    setAddComposerAnchor({ left, top });
  }, [stagedDocuments.length]);

  const handleAddDocumentsClick = useCallback(() => {
    if (!onAddDocuments) return;
    updateAddComposerAnchor();
    setAddComposerOpen(true);
    window.requestAnimationFrame(updateAddComposerAnchor);
  }, [onAddDocuments, updateAddComposerAnchor]);

  const stageDocuments = useCallback((files: File[], source: "browse" | "drop" | "paste") => {
    const normalized = normalizeIncomingFiles(files, source).filter((file) => file.size > 0);
    if (normalized.length === 0) return;
    setStagedDocuments((prev) => [...prev, ...normalized]);
  }, []);

  const handleBrowseDocuments = useCallback(() => {
    addDocumentsInputRef.current?.click();
  }, []);

  const handleAddDocumentsChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      const files = Array.from(event.currentTarget.files ?? []);
      event.currentTarget.value = "";
      stageDocuments(files, "browse");
    },
    [stageDocuments],
  );

  const handleAddComposerPaste = useCallback(
    (event: ClipboardEvent<HTMLTextAreaElement>) => {
      const clipboardFiles = Array.from(event.clipboardData.files ?? []);
      const itemFiles = Array.from(event.clipboardData.items ?? [])
        .map((item) => (item.kind === "file" ? item.getAsFile() : null))
        .filter((file): file is File => file != null);
      const files = clipboardFiles.length > 0 ? clipboardFiles : itemFiles;
      if (files.length === 0) return;
      event.preventDefault();
      stageDocuments(files, "paste");
    },
    [stageDocuments],
  );

  const acceptAddComposerDrag = useCallback((event: DragEvent<HTMLElement>) => {
    event.preventDefault();
    event.stopPropagation();
    event.dataTransfer.dropEffect = "copy";
    setAddComposerDragging(true);
  }, []);

  const handleAddComposerDragLeave = useCallback((event: DragEvent<HTMLElement>) => {
    event.preventDefault();
    event.stopPropagation();
    if (event.currentTarget.contains(event.relatedTarget as Node | null)) return;
    setAddComposerDragging(false);
  }, []);

  const handleAddComposerDrop = useCallback(
    (event: DragEvent<HTMLElement>) => {
      event.preventDefault();
      event.stopPropagation();
      event.dataTransfer.dropEffect = "copy";
      setAddComposerOpen(true);
      setAddComposerDragging(false);
      stageDocuments(Array.from(event.dataTransfer.files ?? []), "drop");
      window.requestAnimationFrame(updateAddComposerAnchor);
    },
    [stageDocuments, updateAddComposerAnchor],
  );

  const handleViewerDirectDrop = useCallback(
    (event: DragEvent<HTMLElement>) => {
      event.preventDefault();
      event.stopPropagation();
      event.dataTransfer.dropEffect = "copy";
      setAddComposerDragging(false);
      const files = Array.from(event.dataTransfer.files ?? []).filter((file) => file.size > 0);
      if (files.length === 0 || !onAddDocuments) return;
      void onAddDocuments(files);
    },
    [onAddDocuments],
  );

  const handleRemoveStagedDocument = useCallback((index: number) => {
    setStagedDocuments((prev) => prev.filter((_, i) => i !== index));
  }, []);

  const handleCloseAddComposer = useCallback(() => {
    if (addComposerUploading) return;
    setAddComposerOpen(false);
    setAddComposerDragging(false);
  }, [addComposerUploading]);

  const handleSubmitStagedDocuments = useCallback(async () => {
    if (!onAddDocuments || stagedDocuments.length === 0 || addComposerUploading) return;
    setAddComposerUploading(true);
    try {
      await Promise.resolve(onAddDocuments(stagedDocuments));
      setStagedDocuments([]);
      setAddComposerOpen(false);
      setAddComposerDragging(false);
    } finally {
      setAddComposerUploading(false);
    }
  }, [addComposerUploading, onAddDocuments, stagedDocuments]);

  useEffect(() => {
    if (!addComposerOpen) return;
    const handle = window.setTimeout(() => addDocumentsTextRef.current?.focus(), 0);
    return () => window.clearTimeout(handle);
  }, [addComposerOpen]);

  useLayoutEffect(() => {
    if (!addComposerOpen) return;
    updateAddComposerAnchor();
    window.addEventListener("resize", updateAddComposerAnchor);
    return () => window.removeEventListener("resize", updateAddComposerAnchor);
  }, [addComposerOpen, updateAddComposerAnchor]);

  useEffect(() => {
    if (!addComposerOpen) return;
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node | null;
      if (!target) return;
      if (addComposerRef.current?.contains(target)) return;
      if (addDocumentsButtonRef.current?.contains(target)) return;
      handleCloseAddComposer();
    };
    document.addEventListener("pointerdown", handlePointerDown);
    return () => document.removeEventListener("pointerdown", handlePointerDown);
  }, [addComposerOpen, handleCloseAddComposer]);

  useEffect(() => {
    activePageNumberRef.current = activePageNumber;
  }, [activePageNumber]);

  useEffect(() => {
    onActivePageChangeRef.current = onActivePageChange;
  }, [onActivePageChange]);

  // ----- Phase 2I — fit-to-width on narrow + Ctrl+wheel zoom -----
  //
  // Track the canvas-area width; recompute on every resize. We use a
  // ResizeObserver instead of window.resize so we react to layout
  // changes (sidebar collapse, panel maximize) too.
  useLayoutEffect(() => {
    const node = scrollRef.current;
    if (!node) return;
    setContainerWidth(node.clientWidth);
    updateMainScrollMetrics(node);
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect?.width;
      if (typeof w === "number") setContainerWidth(w);
      updateMainScrollMetrics(node);
    });
    ro.observe(node);
    return () => ro.disconnect();
  }, [scrollRootVersion, updateMainScrollMetrics]);

  // Compute the fit-to-width zoom from the latest natural page width.
  // When we don't have a natural width yet (PDF still loading), fit
  // is unconstrained → display zoom equals userZoom.
  const FIT_HORIZONTAL_PADDING = 32; // matches the canvas-area gutter
  const fitZoom = useMemo(() => {
    const natural = naturalPageSize?.width ?? naturalPageWidthRef.current;
    if (!natural || !containerWidth || containerWidth <= 0) return Infinity;
    const usable = Math.max(120, containerWidth - FIT_HORIZONTAL_PADDING);
    return usable / natural;
  }, [containerWidth, naturalPageSize?.width]);

  // Display zoom resolution.
  //
  //   • userZoom <= 1.0  →  auto-fit when the container is narrower
  //                          than the page (so a default view never
  //                          forces horizontal scroll). This is the
  //                          "shrink to fit" behaviour the user asked
  //                          for in Phase 2I.
  //   • userZoom  > 1.0  →  honour the user's chosen zoom EXACTLY,
  //                          even if the page now overflows the
  //                          container. The canvas-area has
  //                          ``overflow: auto`` so a horizontal scroll
  //                          bar appears, letting the operator pan
  //                          through the page to read small print.
  //
  // Floor at 0.2 so a tiny container doesn't produce a 0-pixel canvas.
  const zoom = useMemo(() => {
    if (userZoom > 1.0) return Math.max(0.2, userZoom);
    return Math.max(0.2, Math.min(userZoom, fitZoom));
  }, [userZoom, fitZoom]);
  useEffect(() => {
    zoomRef.current = zoom;
  }, [zoom]);

  // Phase 2I — Ctrl+wheel zoom, rewritten with the document-level
  // capture-phase technique used by react-pdf-viewer, Figma, etc.
  //
  // Why this approach beats the previous element-level listener:
  //
  //   1. The handler runs in the *capture* phase on `document`, which
  //      is the very first hook into the wheel event — before any
  //      bubbling, before any React synthetic listener, and before
  //      Chrome's built-in Ctrl+wheel page-zoom handler. preventDefault
  //      works reliably here even when the cursor is over a nested
  //      element with its own pointer state (canvas, overlay, scrollbar).
  //
  //   2. Some Chromium builds force passive on listeners attached to
  //      scrollable elements. A document listener is exempt from that
  //      heuristic, so `{ passive: false }` is always honoured.
  //
  //   3. We only act when the cursor is inside our workspace, gated by
  //      a single `contains()` DOM check. No element-tree assumptions.
  //
  // Wheel deltas are still rAF-batched: at most one zoom state update
  // per frame, regardless of how many wheel events fire. That keeps
  // React reconciliation off the critical path so the CSS-scaled
  // canvas can stay at 60 fps GPU compositing.
  // Phase 2I.2 — iPhone-pinch-feeling Ctrl+wheel zoom.
  //
  // Three things working together:
  //
  //   1. Direction follows the natural-scroll convention. Scrolling
  //      forward (positive deltaY on most laptops) GROWS the page;
  //      scrolling back shrinks it. The user explicitly asked for
  //      this orientation.
  //
  //   2. Multiplicative (exponential) deltas. A single wheel tick
  //      produces the *same perceived* change at 50 % zoom and at
  //      200 % zoom, exactly how trackpad pinch behaves on iOS.
  //
  //   3. Spring-style interpolation toward a target. Each wheel event
  //      bumps a target ref; a long-running rAF loop lerps the
  //      visible zoom toward that target by ~30 % per frame and
  //      stops when the difference is negligible. This converts a
  //      single discrete mouse-wheel notch (which would otherwise
  //      produce a one-frame jump) into a 5–10-frame smooth ramp.
  //      Trackpad pinch already fires fine-grained events, and the
  //      interpolation just rides along with them at high frequency
  //      — no perceptible lag.
  const ZOOM_SENSITIVITY = 0.0008;
  // How aggressive the per-frame interpolation is. 0.30 = 30 % of the
  // remaining gap each frame → settles in roughly 5-8 frames at 60 Hz.
  const ZOOM_LERP = 0.30;
  // When |target - current| drops below this, snap to target and stop
  // the animation loop. 0.0005 is well below visible threshold.
  const ZOOM_EPSILON = 0.0005;

  const targetZoomRef = useRef<number>(1.0);
  const lerpRafRef = useRef<number | null>(null);
  const lerpWindowRef = useRef<Window | null>(null);
  const userZoomRef = useRef<number>(1.0);
  useEffect(() => {
    userZoomRef.current = userZoom;
  }, [userZoom]);

  // Phase 2I.5 — Photoshop-style Space-to-pan.
  //
  // Hold Space → cursor flips to grab and the workspace becomes
  // pannable regardless of which tool is currently active. Click+
  // drag scrolls the canvas-area; release Space to revert. Mirrors
  // Photoshop / Figma / Illustrator. Implemented at the workspace
  // level so the overlay's draw/select handlers keep working when
  // Space is NOT held.
  //
  // Lifecycle:
  //  - keydown(Space) → spacePanActiveRef.current = true; CSS class.
  //  - pointerdown while active → start pan; capture pointer; record
  //    starting scroll + cursor.
  //  - pointermove while panning → delta-scroll the canvas-area.
  //  - pointerup / pointercancel → end pan (CSS class still on while
  //    Space is held).
  //  - keyup(Space) → clear active flag.
  //  - Space-down inside an input/textarea is ignored so typing keeps
  //    working.
  const [spacePanActive, setSpacePanActive] = useState(false);
  const panStateRef = useRef<{
    startX: number;
    startY: number;
    startScrollLeft: number;
    startScrollTop: number;
    pointerId: number;
  } | null>(null);
  // Phase 2I.8 — true while a pan is in flight AND for a short cooldown
  // afterwards. Suppresses scroll-mutating side effects (targetPage
  // scrollIntoView, IntersectionObserver→onActivePageChange callback)
  // that would otherwise fight the pan and "fly" the viewport away.
  const panActiveRef = useRef<boolean>(false);
  const panCooldownRef = useRef<number | null>(null);
  const spacePanAnchorRef = useRef<{ left: number; top: number } | null>(null);

  const finishPanGesture = useCallback(() => {
    const pan = panStateRef.current;
    const node = scrollRef.current;
    const ownerWindow = node?.ownerDocument.defaultView ?? window;
    if (pan) {
      try {
        node?.releasePointerCapture?.(pan.pointerId);
      } catch {
        /* ignore */
      }
    }
    panStateRef.current = null;
    if (panCooldownRef.current != null) {
      ownerWindow.clearTimeout(panCooldownRef.current);
    }
    panCooldownRef.current = ownerWindow.setTimeout(() => {
      panActiveRef.current = false;
      panCooldownRef.current = null;
    }, 250);
  }, []);

  useEffect(() => {
    const ownerDocument = scrollRef.current?.ownerDocument ?? document;
    const ownerWindow = ownerDocument.defaultView ?? window;
    const isEditableTarget = (el: EventTarget | null) => {
      if (!(el instanceof ownerWindow.HTMLElement)) return false;
      const tag = el.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
      if (el.isContentEditable) return true;
      return false;
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.code !== "Space" && e.key !== " ") return;
      if (isEditableTarget(e.target)) return;
      // Only engage when the pointer is over the workspace, OR when
      // the workspace is in the focus path. Otherwise Space could
      // hijack scrolling on totally unrelated parts of the app.
      const node = scrollRef.current;
      if (!node) return;
      // Heuristic: cursor is currently inside the workspace if the
      // mouse last moved over it. We don't track mouse globally, so
      // accept Space whenever the workspace exists in the layout —
      // the cursor change makes the intent obvious to the operator.
      // Prevent the browser's native repeated-Space scroll. Holding
      // Space fires repeat keydown events; if any repeat is allowed to
      // perform its default action, long multi-page PDFs drift toward
      // the last page while the operator is trying to pan.
      e.preventDefault();
      e.stopPropagation();
      panActiveRef.current = true;
      if (!spacePanAnchorRef.current) {
        spacePanAnchorRef.current = {
          left: node.scrollLeft,
          top: node.scrollTop,
        };
      } else if (!panStateRef.current) {
        node.scrollLeft = spacePanAnchorRef.current.left;
        node.scrollTop = spacePanAnchorRef.current.top;
      }
      setSpacePanActive(true);
    };
    const onKeyUp = (e: KeyboardEvent) => {
      if (e.code !== "Space" && e.key !== " ") return;
      e.preventDefault();
      spacePanAnchorRef.current = null;
      setSpacePanActive(false);
      // If a pan was in flight, end it cleanly.
      finishPanGesture();
    };
    // Also drop pan if the window loses focus (Cmd/Alt-Tab) — otherwise
    // we'd come back to a "stuck Space" state.
    const onBlur = () => {
      spacePanAnchorRef.current = null;
      setSpacePanActive(false);
      finishPanGesture();
    };
    ownerWindow.addEventListener("keydown", onKeyDown);
    ownerWindow.addEventListener("keyup", onKeyUp);
    ownerWindow.addEventListener("blur", onBlur);
    return () => {
      ownerWindow.removeEventListener("keydown", onKeyDown);
      ownerWindow.removeEventListener("keyup", onKeyUp);
      ownerWindow.removeEventListener("blur", onBlur);
      if (panCooldownRef.current != null) {
        ownerWindow.clearTimeout(panCooldownRef.current);
        panCooldownRef.current = null;
      }
    };
  }, [finishPanGesture, scrollRootVersion]);

  // Pointer drag handlers wired to the scroll container. They no-op
  // unless Space is currently held, so the overlay's draw/select
  // pointer handlers continue to work in normal mode.
  const handlePanPointerDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      if (!spacePanActive) return;
      const node = scrollRef.current;
      if (!node) return;
      // Phase 2I.7 — kill any in-flight zoom animation BEFORE the pan
      // starts. Otherwise the lerp's setUserZoom would keep firing and
      // the zoom-to-cursor useLayoutEffect would snap scrollTop back
      // to the anchor on every frame, which felt like the page was
      // "flying down" while the operator tried to pan upward.
      if (lerpRafRef.current != null) {
        (lerpWindowRef.current ?? node.ownerDocument.defaultView ?? window).cancelAnimationFrame(
          lerpRafRef.current,
        );
        lerpRafRef.current = null;
        lerpWindowRef.current = null;
      }
      // Sync the lerp target to the visible zoom so a future Ctrl+
      // wheel doesn't suddenly resume from a stale target.
      targetZoomRef.current = userZoomRef.current;
      // Drop the cursor anchor so the layout-effect early-returns
      // through the entire pan gesture.
      zoomAnchorRef.current = null;

      panStateRef.current = {
        startX: e.clientX,
        startY: e.clientY,
        startScrollLeft: node.scrollLeft,
        startScrollTop: node.scrollTop,
        pointerId: e.pointerId,
      };
      panActiveRef.current = true;
      if (panCooldownRef.current != null) {
        window.clearTimeout(panCooldownRef.current);
        panCooldownRef.current = null;
      }
      // setPointerCapture so the move stays bound to the canvas-area
      // even if the cursor leaves it during the drag.
      node.setPointerCapture?.(e.pointerId);
      // Stop the overlay from interpreting this as a draw / select.
      e.preventDefault();
      e.stopPropagation();
    },
    [spacePanActive],
  );
  const handlePanPointerMove = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      const pan = panStateRef.current;
      if (!pan) return;
      const node = scrollRef.current;
      if (!node) return;
      const dx = e.clientX - pan.startX;
      const dy = e.clientY - pan.startY;
      // Phase 2I.7 — Photoshop hand-tool convention.
      //
      //   The cursor and the page move in OPPOSITE directions: you
      //   "grab" the document and drag it. So:
      //     • cursor RIGHT → page slides RIGHT (you see what was on
      //       the left) → scrollLeft DECREASES
      //     • cursor LEFT  → page slides LEFT  → scrollLeft INCREASES
      //     • cursor DOWN  → page slides DOWN  → scrollTop DECREASES
      //                                          (reveals top half)
      //     • cursor UP    → page slides UP    → scrollTop INCREASES
      //                                          (reveals bottom half)
      //
      // Browser scroll clamps at [0, max], so no fly-off at the
      // edges; on page 1 a downward drag stops at scrollTop=0
      // exactly when the top is reached.
      node.scrollLeft = pan.startScrollLeft - dx;
      node.scrollTop = pan.startScrollTop - dy;
      e.preventDefault();
    },
    [],
  );
  const handlePanPointerUp = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      const pan = panStateRef.current;
      if (!pan) return;
      // Keep the suppression flag on briefly: a smooth-scroll started
      // before pan-end can still be in flight, and the
      // IntersectionObserver fires async after the last scrollTop
      // mutation. 250 ms is enough for the observer to settle without
      // being noticeable to the operator.
      finishPanGesture();
      e.preventDefault();
    },
    [finishPanGesture],
  );

  // Phase 2I.4 — zoom-to-cursor anchor.
  //
  // Each wheel event captures the document point under the cursor at
  // *that* moment. While the lerp animates the zoom toward its target,
  // a useLayoutEffect adjusts ``scrollLeft / scrollTop`` so that the
  // captured document point stays under the cursor at every
  // intermediate zoom level. Toolbar buttons clear the anchor so they
  // continue to scroll-from-current-center without surprising jumps.
  //
  // ``docX / docY`` are in unscaled (zoom = 1) document coordinates,
  // ``viewX / viewY`` are the cursor's offset inside the scroll
  // container at capture time. The math: at any new zoom z,
  //   newScrollX = docX * z - viewX
  //   newScrollY = docY * z - viewY
  // keeps the anchor under the cursor.
  const zoomAnchorRef = useRef<ZoomAnchor | null>(null);

  const clearZoomSelectionLock = useCallback(() => {
    const ownerWindow = scrollRef.current?.ownerDocument.defaultView ?? window;
    zoomSelectionLockRef.current = null;
    if (zoomSelectionLockTimerRef.current != null) {
      ownerWindow.clearTimeout(zoomSelectionLockTimerRef.current);
      zoomSelectionLockTimerRef.current = null;
    }
  }, []);

  const lockActivePageForZoom = useCallback((durationMs = 700) => {
    const ownerWindow = scrollRef.current?.ownerDocument.defaultView ?? window;
    const pageNumber = activePageNumberRef.current;
    const serial = zoomSelectionLockSerialRef.current + 1;
    zoomSelectionLockSerialRef.current = serial;
    zoomSelectionLockRef.current = {
      pageNumber,
      serial,
      until: performance.now() + durationMs,
    };
    if (zoomSelectionLockTimerRef.current != null) {
      ownerWindow.clearTimeout(zoomSelectionLockTimerRef.current);
    }
    zoomSelectionLockTimerRef.current = ownerWindow.setTimeout(() => {
      const lock = zoomSelectionLockRef.current;
      if (lock?.serial === serial) {
        zoomSelectionLockRef.current = null;
      }
      if (zoomSelectionLockTimerRef.current != null) {
        ownerWindow.clearTimeout(zoomSelectionLockTimerRef.current);
        zoomSelectionLockTimerRef.current = null;
      }
    }, durationMs + 80);
  }, []);

  const captureZoomAnchorAtPoint = useCallback((viewX: number, viewY: number) => {
    const node = scrollRef.current;
    if (!node) return false;
    const ownerDocument = node.ownerDocument;
    const rootRect = node.getBoundingClientRect();
    const clientX = rootRect.left + viewX;
    const clientY = rootRect.top + viewY;
    const elementAtPoint = ownerDocument.elementFromPoint(clientX, clientY);
    const pageAtPoint = elementAtPoint?.closest<HTMLElement>(".pdf-page-shell[data-page-number]");
    const activePage = pageRefs.current[activePageNumberRef.current];
    const anchorPage = pageAtPoint || activePage || null;
    const liveZoom = Math.max(0.0001, zoomRef.current);
    const nextAnchor: ZoomAnchor = {
      docX: (node.scrollLeft + viewX) / liveZoom,
      docY: (node.scrollTop + viewY) / liveZoom,
      viewX,
      viewY,
    };
    if (anchorPage) {
      const rawPage = Number(anchorPage.dataset.pageNumber);
      const pageRect = anchorPage.getBoundingClientRect();
      if (Number.isFinite(rawPage) && pageRect.width > 0 && pageRect.height > 0) {
        nextAnchor.pageNumber = Math.floor(rawPage);
        nextAnchor.pageRatioX = clampUnit((clientX - pageRect.left) / pageRect.width);
        nextAnchor.pageRatioY = clampUnit((clientY - pageRect.top) / pageRect.height);
      }
    }
    zoomAnchorRef.current = nextAnchor;
    return true;
  }, []);

  const captureZoomAnchorAtViewportCenter = useCallback(() => {
    const node = scrollRef.current;
    if (!node) return false;
    return captureZoomAnchorAtPoint(node.clientWidth / 2, node.clientHeight / 2);
  }, [captureZoomAnchorAtPoint]);

  const scheduleZoomAnchorClear = useCallback((delayMs = 260) => {
    const ownerWindow = scrollRef.current?.ownerDocument.defaultView ?? window;
    const serial = zoomAnchorSerialRef.current + 1;
    zoomAnchorSerialRef.current = serial;
    if (zoomAnchorClearTimerRef.current != null) {
      ownerWindow.clearTimeout(zoomAnchorClearTimerRef.current);
    }
    zoomAnchorClearTimerRef.current = ownerWindow.setTimeout(() => {
      if (zoomAnchorSerialRef.current === serial) {
        zoomAnchorRef.current = null;
      }
      if (zoomAnchorClearTimerRef.current != null) {
        ownerWindow.clearTimeout(zoomAnchorClearTimerRef.current);
        zoomAnchorClearTimerRef.current = null;
      }
    }, delayMs);
  }, []);

  const commitToolbarZoom = useCallback((nextZoom: number | ((current: number) => number)) => {
    lockActivePageForZoom();
    captureZoomAnchorAtViewportCenter();
    setUserZoom((current) => {
      const rawNext = typeof nextZoom === "function" ? nextZoom(current) : nextZoom;
      const next = Math.max(0.25, Math.min(4, +rawNext.toFixed(4)));
      targetZoomRef.current = next;
      userZoomRef.current = next;
      return next;
    });
    scheduleZoomAnchorClear();
  }, [captureZoomAnchorAtViewportCenter, lockActivePageForZoom, scheduleZoomAnchorClear]);

  useEffect(() => {
    const ownerDocument = scrollRef.current?.ownerDocument ?? document;
    const ownerWindow = ownerDocument.defaultView ?? window;
    const cancelLerp = () => {
      if (lerpRafRef.current != null) {
        (lerpWindowRef.current ?? ownerWindow).cancelAnimationFrame(lerpRafRef.current);
        lerpRafRef.current = null;
        lerpWindowRef.current = null;
      }
    };
    const startLerpLoop = () => {
      if (lerpRafRef.current != null) return;
      const tick = () => {
        lerpRafRef.current = null;
        const target = targetZoomRef.current;
        const current = userZoomRef.current;
        if (Math.abs(target - current) < ZOOM_EPSILON) {
          // Snap exactly to target (kills tiny floating-point drift).
          if (current !== target) {
            setUserZoom(+target.toFixed(4));
          }
          // Animation done — clear the cursor anchor so subsequent
          // toolbar zoom changes don't get pinned to a stale point.
          zoomAnchorRef.current = null;
          return;
        }
        const next = current + (target - current) * ZOOM_LERP;
        setUserZoom(+next.toFixed(4));
        lerpRafRef.current = ownerWindow.requestAnimationFrame(tick);
        lerpWindowRef.current = ownerWindow;
      };
      lerpRafRef.current = ownerWindow.requestAnimationFrame(tick);
      lerpWindowRef.current = ownerWindow;
    };

    const onWheel = (e: WheelEvent) => {
      if (!(e.ctrlKey || e.metaKey)) return;
      const node = scrollRef.current;
      if (!node) return;
      const target = e.target as Node | null;
      if (!target || !node.contains(target)) return;
      // We're handling this — stop the browser's default Ctrl+wheel
      // page-zoom AND any element-level scroll fall-through.
      e.preventDefault();
      e.stopPropagation();
      lockActivePageForZoom();

      // Phase 2I.4 — capture the document point under the cursor BEFORE
      // we update the target zoom, so subsequent lerp frames can keep
      // that point pinned to the cursor.
      const rect = node.getBoundingClientRect();
      const viewX = e.clientX - rect.left;
      const viewY = e.clientY - rect.top;
      // Stay anchored to the page-relative point under the cursor.
      // Acrobat behaves this way: the thumbnail selection does not
      // wander merely because page gaps and page heights changed.
      captureZoomAnchorAtPoint(viewX, viewY);

      // Multiplicative target update. Math.exp keeps the visual
      // step uniform regardless of where in the [0.25, 4.0] range we
      // currently sit. Sign chosen so scrolling FORWARD / fingers UP
      // grows the page and scroll BACK shrinks it (matches the user's
      // mental model regardless of OS natural-scroll setting).
      const factor = Math.exp(-e.deltaY * ZOOM_SENSITIVITY);
      const nextTarget = Math.max(
        0.25,
        Math.min(4.0, +(targetZoomRef.current * factor).toFixed(4)),
      );
      targetZoomRef.current = nextTarget;
      startLerpLoop();
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey)) return;
      const node = scrollRef.current;
      if (!node) return;
      const key = e.key;
      const isZoomIn = key === "+" || key === "=" || e.code === "NumpadAdd";
      const isZoomOut = key === "-" || key === "_" || e.code === "NumpadSubtract";
      const isReset = key === "0" || e.code === "Numpad0";
      if (!isZoomIn && !isZoomOut && !isReset) return;

      // In the detached window the document is dedicated to the bill
      // viewer, so browser zoom should never win. In the embedded app,
      // only intercept when focus is inside the viewer so global app
      // shortcuts keep their normal browser behaviour.
      const active = ownerDocument.activeElement;
      const detachedWindow = ownerDocument.body.classList.contains("detached-document-window");
      const focusInsideViewer =
        active === ownerDocument.body ||
        active === ownerDocument.documentElement ||
        (active instanceof ownerWindow.Node && node.contains(active));
      if (!detachedWindow && !focusInsideViewer) return;

      e.preventDefault();
      e.stopPropagation();
      cancelLerp();
      lockActivePageForZoom();
      captureZoomAnchorAtViewportCenter();
      setUserZoom((current) => {
        const next = isReset
          ? 1
          : Math.max(0.25, Math.min(4, +(current * (isZoomIn ? 1.1 : 1 / 1.1)).toFixed(4)));
        targetZoomRef.current = next;
        userZoomRef.current = next;
        return next;
      });
      scheduleZoomAnchorClear();
    };
    // Capture-phase + passive:false at the document level. This is the
    // most reliable hook to outrun Chrome's built-in Ctrl+wheel zoom.
    // Use the workspace's ownerDocument, not the opener document: a
    // detached viewer lives in a separate browser window with its own
    // event stream, and Chrome will zoom that whole window if we attach
    // to the wrong document.
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
      cancelLerp();
    };
  }, [
    captureZoomAnchorAtViewportCenter,
    captureZoomAnchorAtPoint,
    lockActivePageForZoom,
    scheduleZoomAnchorClear,
    scrollRootVersion,
  ]);

  // Keep the lerp target aligned with the user's chosen zoom whenever
  // it's nudged via the toolbar buttons (not via wheel). Otherwise the
  // next wheel event would suddenly snap back to whatever the target
  // ref still holds.
  useEffect(() => {
    if (
      lerpRafRef.current == null &&
      Math.abs(targetZoomRef.current - userZoom) > ZOOM_EPSILON
    ) {
      targetZoomRef.current = userZoom;
    }
  }, [userZoom]);

  // Phase 2I.4 — zoom-to-cursor scroll adjustment.
  //
  // Runs synchronously after each zoom commit but before the browser
  // paints, so the operator sees the page already scrolled to the
  // right position; no jitter, no double-paint. Only fires while a
  // cursor anchor is active (i.e. a Ctrl+wheel gesture is in flight)
  // — toolbar zoom keeps the page centered on whatever was at the
  // top-left of the viewport, which is the conventional behaviour.
  useLayoutEffect(() => {
    const node = scrollRef.current;
    const anchor = zoomAnchorRef.current;
    if (!node || !anchor) return;
    let desiredScrollX = anchor.docX * zoom - anchor.viewX;
    let desiredScrollY = anchor.docY * zoom - anchor.viewY;
    if (
      anchor.pageNumber != null &&
      anchor.pageRatioX != null &&
      anchor.pageRatioY != null
    ) {
      const pageEl = pageRefs.current[anchor.pageNumber];
      if (pageEl) {
        const rootRect = node.getBoundingClientRect();
        const pageRect = pageEl.getBoundingClientRect();
        const pageLeft = node.scrollLeft + pageRect.left - rootRect.left;
        const pageTop = node.scrollTop + pageRect.top - rootRect.top;
        desiredScrollX = pageLeft + pageRect.width * anchor.pageRatioX - anchor.viewX;
        desiredScrollY = pageTop + pageRect.height * anchor.pageRatioY - anchor.viewY;
      }
    }
    // Only assign if a meaningful change — avoids resetting subpixel
    // scroll values when nothing actually moved.
    if (Math.abs(node.scrollLeft - desiredScrollX) > 0.5) {
      node.scrollLeft = desiredScrollX;
    }
    if (Math.abs(node.scrollTop - desiredScrollY) > 0.5) {
      node.scrollTop = desiredScrollY;
    }
  }, [zoom]);

  useEffect(() => {
    let cancelled = false;
    setPageCount(0);
    setPageSizes({});
    setDocumentMetadata({});
    setNaturalPageSize(null);
    setFirstFrameReady(false);
    setMetadataFileUrl(null);
    setFirstFrameFileUrl(null);
    ratiosRef.current.clear();
    // Phase 2I — natural width is per-file; reset when fileUrl changes.
    naturalPageWidthRef.current = null;

    const optimisticMetadata: Record<string, PdfDocumentMetadata> = {};
    for (const source of documentSources) {
      optimisticMetadata[source.fileId] = {
        pageCount: sourcePageCount(source),
        naturalSize: DEFAULT_PAGE_SIZE,
      };
    }
    setNaturalPageSize(DEFAULT_PAGE_SIZE);
    setDocumentMetadata(optimisticMetadata);
    setPageCount(totalPagesForSources(documentSources, optimisticMetadata));
    setMetadataFileUrl(documentSetKey);

    const selectedSource =
      documentSources.find((source) => source.fileId === fileId) || documentSources[0];
    const loadSourceMetadata = async (
      source: PdfDocumentSource,
    ): Promise<PdfDocumentMetadata> => {
      try {
        const { doc } = await loadPdfDocument(source.fileUrl);
        const firstPage = await doc.getPage(1);
        const viewport = firstPage.getViewport({ scale: 1 });
        return {
          pageCount: doc.numPages || source.pageCount || 1,
          naturalSize: { width: viewport.width, height: viewport.height },
        };
      } catch (e) {
        // eslint-disable-next-line no-console
        console.warn("PDF metadata load failed:", e);
        return {
          pageCount: sourcePageCount(source),
          naturalSize: DEFAULT_PAGE_SIZE,
        };
      }
    };

    (async () => {
      if (!selectedSource) return;
      const selectedMetadata = await loadSourceMetadata(selectedSource);
      if (cancelled) return;

      const selectedNatural = selectedMetadata.naturalSize;
      const nextMetadata: Record<string, PdfDocumentMetadata> = {};
      for (const source of documentSources) {
        const optimistic = optimisticMetadata[source.fileId];
        nextMetadata[source.fileId] =
          source.fileId === selectedSource.fileId
            ? selectedMetadata
            : {
                pageCount: optimistic?.pageCount || sourcePageCount(source),
                naturalSize: selectedNatural,
              };
      }
      naturalPageWidthRef.current = selectedNatural.width;
      setNaturalPageSize(selectedNatural);
      setDocumentMetadata(nextMetadata);
      setPageCount(totalPagesForSources(documentSources, nextMetadata));

      const remainingWithoutCounts = documentSources.filter(
        (source) => source.fileId !== selectedSource.fileId && !source.pageCount,
      );
      if (remainingWithoutCounts.length === 0) return;
      await new Promise((resolve) => window.setTimeout(resolve, 250));
      if (cancelled) return;

      const selectedIndex = Math.max(
        0,
        documentSources.findIndex((source) => source.fileId === selectedSource.fileId),
      );
      const prioritized = remainingWithoutCounts
        .map((source) => ({
          source,
          distance: Math.abs(
            documentSources.findIndex((candidate) => candidate.fileId === source.fileId) -
              selectedIndex,
          ),
        }))
        .sort((a, b) => a.distance - b.distance)
        .slice(0, BACKGROUND_METADATA_LIMIT);
      const loaded: Array<{ fileId: string; metadata: PdfDocumentMetadata }> = [];
      let cursor = 0;
      const workerCount = Math.min(BACKGROUND_METADATA_CONCURRENCY, prioritized.length);
      await Promise.all(
        Array.from({ length: workerCount }, async () => {
          while (!cancelled) {
            const item = prioritized[cursor];
            cursor += 1;
            if (!item) break;
            loaded.push({
              fileId: item.source.fileId,
              metadata: await loadSourceMetadata(item.source),
            });
          }
        }),
      );
      if (!cancelled) {
        const enrichedMetadata = { ...nextMetadata };
        for (const item of loaded) {
          enrichedMetadata[item.fileId] = item.metadata;
        }
        setDocumentMetadata(enrichedMetadata);
        setPageCount(totalPagesForSources(documentSources, enrichedMetadata));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [documentSetKey, documentSources, fileId]);

  // Reload regions on batch change. Failures are tolerated: a missing
  // region_hints.json is NOT an error — the workspace simply starts
  // empty. Real network/server errors surface a compact retry button
  // instead of a raw HTTP message.
  const [loadAttempt, setLoadAttempt] = useState(0);
  useEffect(() => {
    let cancelled = false;
    setSaveError(null);
    (async () => {
      try {
        const r = await api.listRegions(batchId);
        if (!cancelled) setAllRegions(r.regions || []);
      } catch (e) {
        // 404 (no regions yet, or batch lookup miss) is not user-facing
        // noise — quietly start with an empty list. Anything else is a
        // real error worth surfacing in a compact, non-technical way.
        if (isApiError(e) && e.status === 404) {
          if (!cancelled) setAllRegions([]);
        } else if (!cancelled) {
          setSaveError(
            "Region hints could not be loaded.",
          );
          // Detailed error to console so a developer can still see it.
          // eslint-disable-next-line no-console
          console.warn("listRegions failed:", e);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [batchId, loadAttempt]);

  const activeDocumentSourcePage = pageByGlobalNumber.get(activePageNumber);
  const activeTraceFileId = activeDocumentSourcePage?.fileId || fileId;

  useEffect(() => {
    setTraces([]);
    setTracesByFileId({});
  }, [batchId, documentSetKey]);

  // Phase 2J — fetch extraction traces for the active document.
  // Failures are silent (the feature is best-effort): an empty list
  // means "the active vendor doesn't emit traces yet" and the toggle
  // stays disabled in the toolbar.
  useEffect(() => {
    let cancelled = false;
    if (!batchId || !activeTraceFileId) return;
    if (Object.prototype.hasOwnProperty.call(tracesByFileId, activeTraceFileId)) {
      return;
    }
    (async () => {
      let items: TraceItem[] = [];
      try {
        const res = await api.getDocumentTrace(batchId, activeTraceFileId);
        items = res.items || [];
      } catch {
        items = [];
      }
      if (!cancelled) {
        setTracesByFileId((current) =>
          Object.prototype.hasOwnProperty.call(current, activeTraceFileId)
            ? current
            : { ...current, [activeTraceFileId]: items },
        );
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [activeTraceFileId, batchId, documentSetKey, tracesByFileId]);

  useEffect(() => {
    setTraces(Object.values(tracesByFileId).flat());
  }, [tracesByFileId]);

  // Group traces by page once so the per-page render loop doesn't
  // re-filter the array on every render.
  const tracesByPage = useMemo(() => {
    const out: Record<number, TraceItem[]> = {};
    for (const page of documentPages) {
      for (const t of tracesByFileId[page.fileId] || []) {
        const p = Number(t.page) || 1;
        if (p === page.localPageNumber) {
          (out[page.globalPageNumber] ||= []).push(t);
        }
      }
    }
    return out;
  }, [documentPages, tracesByFileId]);

  const highlightedSet = useMemo(() => {
    return new Set<string>(highlightedTraceIds || []);
  }, [highlightedTraceIds]);

  const handleTraceHover = useCallback(
    (id: string | null) => {
      setHoveredTraceId(id);
      onTraceHover?.(id);
    },
    [onTraceHover],
  );

  // Reset to the first page of the selected document when the selected
  // source changes. This must also queue a physical scroll; otherwise the
  // toolbar/header can switch to the new file while the scroll container
  // stays parked on the previous file's page.
  useEffect(() => {
    if (documentPages.length === 0) return;
    const navigationKey = `${documentSetKey}:${fileId}`;
    if (selectedDocumentNavigationKeyRef.current === navigationKey) return;
    const shouldAnimate = selectedDocumentSetKeyRef.current === documentSetKey;
    selectedDocumentNavigationKeyRef.current = navigationKey;
    selectedDocumentSetKeyRef.current = documentSetKey;
    const requestedSelectedPage =
      targetPage?.filename === fileId
        ? globalPageForFilePage(targetPage.filename, targetPage.pageNumber)
        : null;
    const firstSelectedPage =
      requestedSelectedPage ||
      documentPages.find((page) => page.fileId === fileId)?.globalPageNumber ||
      1;
    commitActivePageNumber(firstSelectedPage);
    if (programmaticScrollTimerRef.current != null) {
      window.clearTimeout(programmaticScrollTimerRef.current);
    }
    programmaticScrollTargetRef.current = firstSelectedPage;
    pendingScrollTargetRef.current = firstSelectedPage;
    pendingScrollBehaviorRef.current = shouldAnimate ? "smooth" : "auto";
    setFocusedPageNumber(firstSelectedPage);
    setScrollRequestVersion((version) => version + 1);
    programmaticScrollTimerRef.current = window.setTimeout(() => {
      programmaticScrollTargetRef.current = null;
      if (pendingScrollTargetRef.current === firstSelectedPage) {
        pendingScrollTargetRef.current = null;
      }
      programmaticScrollTimerRef.current = null;
    }, PROGRAMMATIC_SCROLL_SETTLE_MS);
    notifyActivePageChange(firstSelectedPage);
    setSelectedId(null);
  }, [
    documentSetKey,
    documentPages,
    commitActivePageNumber,
    fileId,
    globalPageForFilePage,
    notifyActivePageChange,
    pageCount,
    targetPage?.filename,
    targetPage?.pageNumber,
  ]);

  const hasCurrentMetadata = metadataFileUrl === documentSetKey;
  const hasCurrentFirstFrame = firstFrameReady && firstFrameFileUrl === documentSetKey;
  const isPdfLayoutReady =
    hasCurrentMetadata && pageCount > 0 && naturalPageSize != null && containerWidth != null;
  const isDocumentReady = isPdfLayoutReady;

  const pageNumbers = useMemo(() => {
    if (!isPdfLayoutReady) return [];
    return documentPages.map((page) => page.globalPageNumber);
  }, [documentPages, isPdfLayoutReady]);
  const visibleThumbnailPages = useMemo(() => {
    if (!isPdfLayoutReady || documentPages.length === 0) {
      return {
        pages: [] as PdfDocumentPage[],
        topSpacer: 0,
        bottomSpacer: 0,
      };
    }
    const viewport =
      thumbnailViewportHeight || THUMBNAIL_ITEM_ESTIMATED_HEIGHT * 8;
    const startIndex = Math.max(
      0,
      Math.floor(thumbnailScrollTop / THUMBNAIL_ITEM_ESTIMATED_HEIGHT) -
        THUMBNAIL_WINDOW_OVERSCAN,
    );
    const visibleCount =
      Math.ceil(viewport / THUMBNAIL_ITEM_ESTIMATED_HEIGHT) +
      THUMBNAIL_WINDOW_OVERSCAN * 2;
    const endIndex = Math.min(documentPages.length, startIndex + visibleCount);
    return {
      pages: documentPages.slice(startIndex, endIndex),
      topSpacer: startIndex * THUMBNAIL_ITEM_ESTIMATED_HEIGHT,
      bottomSpacer: Math.max(0, documentPages.length - endIndex) * THUMBNAIL_ITEM_ESTIMATED_HEIGHT,
    };
  }, [
    documentPages,
    isPdfLayoutReady,
    thumbnailScrollTop,
    thumbnailViewportHeight,
  ]);
  const mainPageLayout = useMemo(() => {
    const offsets: number[] = [];
    const heights: number[] = [];
    let cursor = 0;
    for (const page of documentPages) {
      const height = page.naturalSize.height * zoom;
      offsets.push(cursor);
      heights.push(height);
      cursor += height + MAIN_PAGE_GAP;
    }
    return {
      offsets,
      heights,
      totalHeight: cursor,
    };
  }, [documentPages, zoom]);
  const visibleMainPages = useMemo(() => {
    if (!isPdfLayoutReady || documentPages.length === 0) {
      return {
        pages: [] as Array<{ page: PdfDocumentPage; index: number }>,
        topSpacer: 0,
        bottomSpacer: 0,
      };
    }
    const viewport = mainViewportHeight || 900;
    const visibleStart = Math.max(0, mainScrollTop - MAIN_PAGE_OVERSCAN_PX);
    const visibleEnd = mainScrollTop + viewport + MAIN_PAGE_OVERSCAN_PX;
    const rawStart = firstPageIndexAtOffset(
      mainPageLayout.offsets,
      mainPageLayout.heights,
      visibleStart,
    );
    const rawEnd =
      firstPageIndexAtOffset(
        mainPageLayout.offsets,
        mainPageLayout.heights,
        visibleEnd,
      ) + 1;
    const startIndex = Math.max(0, rawStart);
    const endIndex = Math.min(documentPages.length, rawEnd);
    const offsetAfterWindow =
      endIndex < documentPages.length
        ? mainPageLayout.offsets[endIndex]
        : mainPageLayout.totalHeight;
    return {
      pages: documentPages.slice(startIndex, endIndex).map((page, index) => ({
        page,
        index: startIndex + index,
      })),
      topSpacer: mainPageLayout.offsets[startIndex] || 0,
      bottomSpacer: Math.max(0, mainPageLayout.totalHeight - offsetAfterWindow),
    };
  }, [
    documentPages,
    isPdfLayoutReady,
    mainPageLayout,
    mainScrollTop,
    mainViewportHeight,
  ]);
  const visibleMainPageNumbers = useMemo(
    () => visibleMainPages.pages.map(({ page }) => page.globalPageNumber),
    [visibleMainPages.pages],
  );
  const visibleUploadItems = useMemo(
    () =>
      uploadItems.length > 120
        ? [...uploadItems.slice(0, 80), ...uploadItems.slice(-20)]
        : uploadItems,
    [uploadItems],
  );
  const hiddenUploadItemCount = Math.max(0, uploadItems.length - visibleUploadItems.length);

  const syncActivePageFromScroll = useCallback(() => {
    const root = scrollRef.current;
    if (!root) return;
    updateMainScrollMetrics(root);
    const currentPage = activePageNumberRef.current;
    const currentIndex = documentPages.findIndex(
      (page) => page.globalPageNumber === currentPage,
    );
    const bestIndex = strongestPageIndexForViewport(
      mainPageLayout.offsets,
      mainPageLayout.heights,
      Math.max(0, root.scrollTop),
      Math.max(1, root.clientHeight),
      currentIndex >= 0 ? currentIndex : 0,
    );
    let bestPage = documentPages[bestIndex]?.globalPageNumber ?? currentPage;
    const programmaticTarget = programmaticScrollTargetRef.current;
    if (programmaticTarget != null) {
      bestPage = programmaticTarget;
    } else {
      const zoomLock = zoomSelectionLockRef.current;
      if (zoomLock && performance.now() <= zoomLock.until) {
        bestPage = zoomLock.pageNumber;
      }
    }
    const lockedPage = panelTogglePreservePageRef.current;
    if (lockedPage != null) {
      bestPage = lockedPage;
    }
    if (bestPage !== activePageNumberRef.current || bestPage !== activePageNumber) {
      activePageNumberRef.current = bestPage;
      commitActivePageNumber(bestPage);
      if (!panActiveRef.current && programmaticScrollTargetRef.current == null) {
        deferActivePageNotify(bestPage);
      }
    }
  }, [
    activePageNumber,
    commitActivePageNumber,
    deferActivePageNotify,
    documentPages,
    mainPageLayout.heights,
    mainPageLayout.offsets,
    pageCount,
    pageNumbers.length,
    updateMainScrollMetrics,
  ]);
  syncActivePageFromScrollRef.current = syncActivePageFromScroll;

  useLayoutEffect(() => {
    if (!isDocumentReady || pageNumbers.length === 0) return;
    const root = scrollRef.current;
    if (!root) return;
    const ownerWindow = root.ownerDocument.defaultView ?? window;
    const handle = ownerWindow.requestAnimationFrame(syncActivePageFromScroll);
    return () => ownerWindow.cancelAnimationFrame(handle);
  }, [
    isDocumentReady,
    pageNumbers.length,
    scrollRootVersion,
    syncActivePageFromScroll,
  ]);

  const regionsByPage = useMemo(() => {
    const grouped: Record<number, RegionHint[]> = {};
    for (const page of documentPages) {
      const pageRegions = allRegions.filter(
        (region) =>
          region.file_id === page.fileId &&
          (region.page_number || 1) === page.localPageNumber,
      );
      if (pageRegions.length > 0) {
        grouped[page.globalPageNumber] = pageRegions;
      }
    }
    return grouped;
  }, [allRegions, documentPages]);

  const regionsOnActivePage = useMemo(
    () => regionsByPage[activePageNumber] ?? [],
    [activePageNumber, regionsByPage],
  );

  const setPageRef = useCallback(
    (pageNumber: number) => (el: HTMLDivElement | null) => {
      pageRefs.current[pageNumber] = el;
    },
    [],
  );

  const setThumbnailRef = useCallback(
    (pageNumber: number) => (el: HTMLDivElement | null) => {
      thumbnailRefs.current[pageNumber] = el;
    },
    [],
  );

  const cancelScrollAnimation = useCallback(() => {
    scrollAnimationGenerationRef.current += 1;
    if (scrollAnimationFrameRef.current != null) {
      (scrollRef.current?.ownerDocument.defaultView ?? window).cancelAnimationFrame(
        scrollAnimationFrameRef.current,
      );
      scrollAnimationFrameRef.current = null;
    }
  }, []);

  const scheduleActivePageSync = useCallback(() => {
    const root = scrollRef.current;
    const ownerWindow = root?.ownerDocument.defaultView ?? window;
    ownerWindow.requestAnimationFrame(() => {
      syncActivePageFromScrollRef.current?.();
    });
  }, []);

  const cancelProgrammaticScroll = useCallback(() => {
    const ownerWindow = scrollRef.current?.ownerDocument.defaultView ?? window;
    clearZoomSelectionLock();
    lastManualScrollAtRef.current = performance.now();
    if (programmaticScrollTimerRef.current != null) {
      ownerWindow.clearTimeout(programmaticScrollTimerRef.current);
      programmaticScrollTimerRef.current = null;
    }
    programmaticScrollTargetRef.current = null;
    pendingScrollTargetRef.current = null;
    cancelDeferredActivePageNotify();
    cancelScrollAnimation();
    scheduleActivePageSync();
  }, [
    cancelDeferredActivePageNotify,
    cancelScrollAnimation,
    clearZoomSelectionLock,
    scheduleActivePageSync,
  ]);

  const getPageScrollTop = useCallback((pageNumber: number) => {
    const target = Math.max(1, Math.floor(pageNumber || 1));
    const virtualTop = mainPageLayout.offsets[target - 1];
    if (typeof virtualTop === "number" && Number.isFinite(virtualTop)) {
      return Math.max(0, virtualTop - PAGE_SCROLL_TOP_GUTTER);
    }
    const el = pageRefs.current[target];
    const root = scrollRef.current;
    if (!el || !root) return null;
    return Math.max(0, offsetTopWithinScroller(root, el) - PAGE_SCROLL_TOP_GUTTER);
  }, [mainPageLayout.offsets]);

  const alignPageToTop = useCallback((pageNumber: number, behavior: ScrollBehavior = "auto") => {
    const root = scrollRef.current;
    if (!root) return false;
    const ownerWindow = root.ownerDocument.defaultView ?? window;
    const top = getPageScrollTop(pageNumber);
    if (top == null) return false;
    if (
      behavior === "smooth" &&
      !prefersReducedMotion() &&
      Math.abs(root.scrollTop - top) > 8
    ) {
      cancelScrollAnimation();
      const startTop = root.scrollTop;
      const startedAt = performance.now();
      const animationGeneration = scrollAnimationGenerationRef.current;
      let lastAnimatedTop = startTop;
      const step = (now: number) => {
        if (scrollAnimationGenerationRef.current !== animationGeneration) {
          scrollAnimationFrameRef.current = null;
          return;
        }
        if (
          programmaticScrollTargetRef.current !== pageNumber &&
          pendingScrollTargetRef.current !== pageNumber
        ) {
          scrollAnimationFrameRef.current = null;
          return;
        }
        if (Math.abs(root.scrollTop - lastAnimatedTop) > 6) {
          if (programmaticScrollTargetRef.current !== pageNumber) {
            scrollAnimationGenerationRef.current += 1;
            scrollAnimationFrameRef.current = null;
            programmaticScrollTargetRef.current = null;
            pendingScrollTargetRef.current = null;
            lastManualScrollAtRef.current = performance.now();
            return;
          }
          lastAnimatedTop = root.scrollTop;
        }
        const elapsed = now - startedAt;
        const progress = Math.min(1, elapsed / PAGE_REEL_SCROLL_MS);
        const latestTop = getPageScrollTop(pageNumber) ?? top;
        const nextTop = startTop + (latestTop - startTop) * easeOutCubic(progress);
        root.scrollTop = nextTop;
        lastAnimatedTop = nextTop;
        if (progress < 1) {
          scrollAnimationFrameRef.current = ownerWindow.requestAnimationFrame(step);
          return;
        }
        scrollAnimationFrameRef.current = null;
        const finalTop = getPageScrollTop(pageNumber) ?? latestTop;
        if (Math.abs(root.scrollTop - finalTop) > 1) {
          root.scrollTop = finalTop;
        }
      };
      scrollAnimationFrameRef.current = ownerWindow.requestAnimationFrame(step);
      return true;
    }
    cancelScrollAnimation();
    if (Math.abs(root.scrollTop - top) > 1) {
      root.scrollTop = top;
    }
    return true;
  }, [cancelScrollAnimation, getPageScrollTop]);

  const scrollToPage = useCallback((
    pageNumber: number,
    behavior: ScrollBehavior = "smooth",
    options: ScrollToPageOptions = {},
  ) => {
    const target = Math.max(1, Math.floor(pageNumber || 1));
    const root = scrollRef.current;
    if (!root) return;
    const ownerWindow = root.ownerDocument.defaultView ?? window;
    const manualScrollIsRecent = performance.now() - lastManualScrollAtRef.current < 1800;
    const targetScrollTop = getPageScrollTop(target);
    const targetAlreadyAligned =
      targetScrollTop != null && Math.abs(root.scrollTop - targetScrollTop) <= 10;
    if (
      !options.force &&
      manualScrollIsRecent &&
      target === activePageNumberRef.current &&
      targetAlreadyAligned
    ) {
      cancelProgrammaticScroll();
      return;
    }
    if (options.force) {
      cancelScrollAnimation();
    }
    const navigationSerial = scrollNavigationSerialRef.current + 1;
    scrollNavigationSerialRef.current = navigationSerial;
    if (programmaticScrollTimerRef.current != null) {
      ownerWindow.clearTimeout(programmaticScrollTimerRef.current);
    }
    cancelDeferredActivePageNotify();
    programmaticScrollTargetRef.current = target;
    setFocusedPageNumber(target);
    if (activePageNumberRef.current !== target) {
      activePageNumberRef.current = target;
      commitActivePageNumber(target);
      notifyActivePageChange(target);
    } else if (options.force) {
      notifyActivePageChange(target);
    }
    pendingScrollTargetRef.current = target;
    pendingScrollBehaviorRef.current = behavior;
    setScrollRequestVersion((version) => version + 1);
    programmaticScrollTimerRef.current = ownerWindow.setTimeout(() => {
      if (scrollNavigationSerialRef.current !== navigationSerial) return;
      programmaticScrollTargetRef.current = null;
      if (pendingScrollTargetRef.current === target) {
        pendingScrollTargetRef.current = null;
      }
      programmaticScrollTimerRef.current = null;
    }, behavior === "smooth" ? 1400 : PROGRAMMATIC_SCROLL_SETTLE_MS);
    ownerWindow.setTimeout(() => {
      if (scrollNavigationSerialRef.current !== navigationSerial) return;
      setFocusedPageNumber((current) => (current === target ? null : current));
    }, 1400);
  }, [
    cancelScrollAnimation,
    cancelProgrammaticScroll,
    cancelDeferredActivePageNotify,
    commitActivePageNumber,
    getPageScrollTop,
    notifyActivePageChange,
    pageCount,
    pageNumbers.length,
  ]);

  useEffect(() => {
    const root = scrollRef.current;
    if (!root) return;
    const ownerDocument = root.ownerDocument;
    const eventHitsViewer = (event: Event) => {
      const target = event.target as Node | null;
      if (target && root.contains(target)) return true;
      const rect = root.getBoundingClientRect();
      if (event instanceof WheelEvent || event instanceof PointerEvent) {
        return (
          event.clientX >= rect.left &&
          event.clientX <= rect.right &&
          event.clientY >= rect.top &&
          event.clientY <= rect.bottom
        );
      }
      if (event instanceof TouchEvent) {
        const touch = event.touches[0] || event.changedTouches[0];
        if (!touch) return false;
        return (
          touch.clientX >= rect.left &&
          touch.clientX <= rect.right &&
          touch.clientY >= rect.top &&
          touch.clientY <= rect.bottom
        );
      }
      return false;
    };
  const cancelForUserScroll = (event: Event) => {
      if (event instanceof WheelEvent && (event.ctrlKey || event.metaKey)) return;
      if (!eventHitsViewer(event)) return;
      cancelProgrammaticScroll();
    };
    root.addEventListener("wheel", cancelForUserScroll, { passive: true });
    root.addEventListener("pointerdown", cancelForUserScroll, { capture: true });
    root.addEventListener("touchstart", cancelForUserScroll, { passive: true });
    ownerDocument.addEventListener("wheel", cancelForUserScroll, {
      passive: true,
      capture: true,
    });
    return () => {
      root.removeEventListener("wheel", cancelForUserScroll);
      root.removeEventListener(
        "pointerdown",
        cancelForUserScroll,
        { capture: true } as EventListenerOptions,
      );
      root.removeEventListener("touchstart", cancelForUserScroll);
      ownerDocument.removeEventListener(
        "wheel",
        cancelForUserScroll,
        { capture: true } as EventListenerOptions,
      );
    };
  }, [cancelProgrammaticScroll, scrollRootVersion]);

  const handleManualWheelCapture = useCallback(
    (event: ReactWheelEvent<HTMLDivElement>) => {
      if (event.ctrlKey || event.metaKey) return;
      cancelProgrammaticScroll();
    },
    [cancelProgrammaticScroll],
  );

  const handleManualPointerCapture = useCallback(() => {
    cancelProgrammaticScroll();
  }, [cancelProgrammaticScroll]);

  const handleCanvasScroll = useCallback(
    (event: UIEvent<HTMLDivElement>) => {
      updateMainScrollMetrics(event.currentTarget);
      syncActivePageFromScrollRef.current?.();
    },
    [updateMainScrollMetrics],
  );

  useLayoutEffect(() => {
    const target = pendingScrollTargetRef.current;
    if (target == null) return;
    const root = scrollRef.current;
    const ownerWindow = root?.ownerDocument.defaultView ?? window;
    const behavior = pendingScrollBehaviorRef.current;
    const stillPending = () =>
      pendingScrollTargetRef.current === target ||
      programmaticScrollTargetRef.current === target;
    const alignIfPending = (nextBehavior: ScrollBehavior) => {
      if (!stillPending()) return false;
      return alignPageToTop(target, nextBehavior);
    };
    const aligned = alignIfPending(behavior);
    const raf1 = ownerWindow.requestAnimationFrame(() => {
      if (!aligned && stillPending()) {
        alignIfPending(behavior);
      }
      ownerWindow.requestAnimationFrame(() => {
        if (!aligned && stillPending()) {
          alignIfPending(behavior);
        }
      });
    });
    const settleDelay = behavior === "smooth" ? PAGE_REEL_SCROLL_MS + 60 : 120;
    const settle1 = ownerWindow.setTimeout(() => alignIfPending("auto"), settleDelay);
    const settle2 = ownerWindow.setTimeout(() => alignIfPending("auto"), 700);
    const settle3 = ownerWindow.setTimeout(() => alignIfPending("auto"), 1100);
    return () => {
      ownerWindow.cancelAnimationFrame(raf1);
      ownerWindow.clearTimeout(settle1);
      ownerWindow.clearTimeout(settle2);
      ownerWindow.clearTimeout(settle3);
    };
  }, [alignPageToTop, scrollRequestVersion]);

  useEffect(() => {
    return () => {
      const ownerWindow = scrollRef.current?.ownerDocument.defaultView ?? window;
      if (programmaticScrollTimerRef.current != null) {
        ownerWindow.clearTimeout(programmaticScrollTimerRef.current);
        programmaticScrollTimerRef.current = null;
      }
      if (zoomSelectionLockTimerRef.current != null) {
        ownerWindow.clearTimeout(zoomSelectionLockTimerRef.current);
        zoomSelectionLockTimerRef.current = null;
      }
      if (zoomAnchorClearTimerRef.current != null) {
        ownerWindow.clearTimeout(zoomAnchorClearTimerRef.current);
        zoomAnchorClearTimerRef.current = null;
      }
      if (scrollTrailingSyncTimerRef.current != null) {
        ownerWindow.clearTimeout(scrollTrailingSyncTimerRef.current);
        scrollTrailingSyncTimerRef.current = null;
      }
      cancelDeferredActivePageNotify();
      cancelScrollAnimation();
      scrollRootCleanupRef.current?.();
      scrollRootCleanupRef.current = null;
    };
  }, [cancelDeferredActivePageNotify, cancelScrollAnimation]);

  useLayoutEffect(() => {
    if (!pagePanelOpen) return;
    const node = thumbnailListRef.current;
    if (!node) return;
    const updateSize = () => setThumbnailViewportHeight(node.clientHeight);
    updateSize();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(updateSize);
    observer.observe(node);
    return () => observer.disconnect();
  }, [pagePanelOpen]);

  useLayoutEffect(() => {
    if (!pagePanelOpen) return;
    const scroller = thumbnailListRef.current;
    if (!scroller) return;
    const target = thumbnailRefs.current[activePageNumber];
    if (target) {
      keepElementInsideScroller(scroller, target, 18);
      return;
    }
    const targetTop = Math.max(0, activePageNumber - 1) * THUMBNAIL_ITEM_ESTIMATED_HEIGHT;
    const targetBottom = targetTop + THUMBNAIL_ITEM_ESTIMATED_HEIGHT;
    const pad = 24;
    if (targetTop < scroller.scrollTop + pad) {
      scroller.scrollTop = Math.max(0, targetTop - pad);
      setThumbnailScrollTop(scroller.scrollTop);
    } else if (targetBottom > scroller.scrollTop + scroller.clientHeight - pad) {
      scroller.scrollTop = Math.max(
        0,
        targetBottom - scroller.clientHeight + pad,
      );
      setThumbnailScrollTop(scroller.scrollTop);
    }
  }, [activePageNumber, pagePanelOpen, pageNumbers.length]);

  useEffect(() => {
    const panelActuallyToggled = previousPagePanelOpenRef.current !== pagePanelOpen;
    previousPagePanelOpenRef.current = pagePanelOpen;
    if (!panelActuallyToggled) return;
    if (!isDocumentReady || pageNumbers.length === 0) return;
    const target = panelTogglePreservePageRef.current ?? activePageNumberRef.current;
    const handle = window.setTimeout(() => {
      scrollToPage(target, "auto");
      window.setTimeout(() => {
        if (panelTogglePreservePageRef.current === target) {
          panelTogglePreservePageRef.current = null;
        }
      }, 500);
    }, 0);
    return () => window.clearTimeout(handle);
  }, [isDocumentReady, pageNumbers.length, pagePanelOpen, scrollToPage]);

  useEffect(() => {
    if (!targetPage) {
      appliedTargetPageKeyRef.current = null;
      return;
    }
    if (targetPage.pageNumber < 1) return;
    if (!isDocumentReady || pageNumbers.length === 0) return;
    const targetGlobalPage = globalPageForFilePage(
      targetPage.filename,
      targetPage.pageNumber,
    );
    const targetKey = [
      targetPage.filename || fileId,
      targetPage.pageNumber,
      targetPage.nonce,
      targetGlobalPage,
      documentSetKey,
    ].join(":");
    if (appliedTargetPageKeyRef.current === targetKey) return;
    appliedTargetPageKeyRef.current = targetKey;
    // Skip while a pan is in flight: a programmatic scrollIntoView
    // would compete with the user's drag and "fly" them to whatever
    // page the parent thinks is active.
    if (panActiveRef.current) return;
    const emitted = lastEmittedActivePageRef.current;
    const targetFileId = targetPage.filename || fileId;
    const isEchoOfActivePage =
      emitted != null &&
      performance.now() - emitted.at < ACTIVE_PAGE_ECHO_SUPPRESS_MS &&
      emitted.globalPageNumber === targetGlobalPage &&
      (!targetFileId || !emitted.fileId || emitted.fileId === targetFileId);
    const manualScrollIsRecent = performance.now() - lastManualScrollAtRef.current < 800;
    const targetIsCurrentOrAdjacent =
      Math.abs((activePageNumberRef.current || 1) - targetGlobalPage) <= 1;
    if (isEchoOfActivePage || (manualScrollIsRecent && targetIsCurrentOrAdjacent)) {
      return;
    }
    const handle = window.setTimeout(() => {
      if (panActiveRef.current) return;
      scrollToPage(targetGlobalPage, "smooth");
    }, 0);
    return () => window.clearTimeout(handle);
  }, [
    documentSetKey,
    fileId,
    globalPageForFilePage,
    isDocumentReady,
    pageCount,
    pageNumbers.length,
    scrollToPage,
    targetPage?.filename,
    targetPage?.nonce,
    targetPage?.pageNumber,
  ]);

  useEffect(() => {
    const root = scrollRef.current;
    if (!root || pageNumbers.length === 0) return;
    const observer = new IntersectionObserver(
      (entries) => {
        let changed = false;
        for (const entry of entries) {
          const raw = (entry.target as HTMLElement).dataset.pageNumber;
          const n = raw ? Number(raw) : 0;
          if (!n) continue;
          if (entry.isIntersecting) {
            ratiosRef.current.set(n, entry.intersectionRatio);
          } else {
            ratiosRef.current.delete(n);
          }
          changed = true;
        }
        if (changed) {
          const ownerWindow = root.ownerDocument.defaultView ?? window;
          ownerWindow.requestAnimationFrame(() => {
            syncActivePageFromScrollRef.current?.();
          });
        }
      },
      {
        root,
        rootMargin: "900px 0px",
        threshold: [0, 0.05, 0.18, 0.35, 0.5, 0.7],
      },
    );
    visibleMainPageNumbers.forEach((n) => {
      const el = pageRefs.current[n];
      if (el) observer.observe(el);
    });
    return () => observer.disconnect();
  }, [pageCount, pageNumbers.length, scrollRootVersion, visibleMainPageNumbers]);

  // Persist the full list to the backend (debounced via state-write
  // batching — every change calls saveRegions once on the next tick).
  const saveRegions = useCallback(
    async (next: RegionHint[]) => {
      setSaving(true);
      setSaveError(null);
      try {
        await api.replaceRegions(batchId, next);
      } catch (e) {
        setSaveError(getFriendlyErrorMessage(e, "Save regions"));
        // eslint-disable-next-line no-console
        console.warn("save regions failed:", e);
      } finally {
        setSaving(false);
      }
    },
    [batchId],
  );

  const handleAdd = useCallback(
    (region: RegionHint) => {
      // Phase 2K — when in remap mode, intercept the drawn region and
      // hand the bbox back to the parent instead of persisting it as
      // a region hint. We also exit draw mode automatically so the
      // viewer falls back to its normal "select" behaviour.
      if (remapActive && onRemapDrawn) {
        onRemapDrawn(region.page_number || 1, {
          x: region.bbox.x,
          y: region.bbox.y,
          w: region.bbox.w,
          h: region.bbox.h,
        });
        setTool("select");
        return;
      }
      const next = [...allRegions, region];
      setAllRegions(next);
      void saveRegions(next);
    },
    [allRegions, saveRegions, remapActive, onRemapDrawn],
  );

  // Force draw mode whenever the parent activates remap; restore to
  // "select" when remap turns off so regular panning resumes.
  useEffect(() => {
    if (remapActive) setTool("draw");
  }, [remapActive]);

  const handleUpdate = useCallback(
    (region: RegionHint) => {
      const next = allRegions.map((r) => (r.id === region.id ? region : r));
      setAllRegions(next);
      // Throttle saves to mouseup-grade frequency: only save when the
      // change affects bbox (move/resize commit). Selection-only
      // updates wouldn't reach this path. For Phase 1H foundation,
      // persist on every update — there's only one user at a time.
      void saveRegions(next);
    },
    [allRegions, saveRegions],
  );

  const handleDelete = useCallback(
    (id: string) => {
      const next = allRegions.filter((r) => r.id !== id);
      setAllRegions(next);
      if (selectedId === id) setSelectedId(null);
      void saveRegions(next);
    },
    [allRegions, saveRegions, selectedId],
  );

  return (
    <div
      className={`pdf-workspace ${addComposerDragging ? "is-add-dragging" : ""}`}
      ref={workspaceRef}
      onDragEnter={acceptAddComposerDrag}
      onDragOver={acceptAddComposerDrag}
      onDragLeave={handleAddComposerDragLeave}
      onDrop={handleViewerDirectDrop}
    >
      {/* Phase 2J — Extraction Trace toggle. Only rendered when the
          active document has at least one trace; otherwise the toggle
          would be a dead control. Click toggles the overlay on/off; the
          subtle accent fill signals the active state. */}
      {false && traces.length > 0 && (
        <button
          type="button"
          className={`pdf-trace-toggle ${tracesEnabled ? "is-on" : ""}`}
          onClick={() => setTracesEnabled((v) => !v)}
          title={
            tracesEnabled
              ? `Hide extraction traces (${traces.length})`
              : `Show extraction traces (${traces.length})`
          }
          aria-pressed={tracesEnabled}
          data-testid="pdf-trace-toggle"
        >
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <rect x="4" y="6" width="14" height="3" rx="1" />
            <rect x="4" y="13" width="10" height="3" rx="1" />
            <circle cx="20" cy="7.5" r="1.5" />
            <circle cx="16" cy="14.5" r="1.5" />
          </svg>
          <span>Traces ({traces.length})</span>
        </button>
      )}
      <ViewerToolbar
        zoom={zoom}
        // Phase 2I — toolbar now mutates ``userZoom`` (the *intent*).
        // The displayed zoom is still the auto-fit-clamped value, but
        // a click on +/- moves the underlying preference up or down by
        // a fine 10 % step (was 25 %) so the buttons feel as fluid as
        // Ctrl+wheel.
        onZoomIn={() => commitToolbarZoom((z) => +(z + 0.1).toFixed(2))}
        onZoomOut={() => commitToolbarZoom((z) => +(z - 0.1).toFixed(2))}
        onResetZoom={() => commitToolbarZoom(1.0)}
        pageNumber={activePageNumber}
        pageCount={pageCount}
        isLayoutReady={isDocumentReady}
        onPrevPage={() =>
          scrollToPage(Math.max(1, activePageNumber - 1), "smooth", { force: true })
        }
        onNextPage={() =>
          scrollToPage(
            Math.min(pageCount || activePageNumber + 1, activePageNumber + 1),
            "smooth",
            { force: true },
          )
        }
        regionsCount={regionsOnActivePage.length}
        traceCount={traces.length}
        tracesEnabled={tracesEnabled}
        onToggleTraces={() => setTracesEnabled((v) => !v)}
        pagePanelOpen={pagePanelOpen}
        onTogglePagePanel={() => setPagePanelVisible(!pagePanelOpen)}
      />
      {/* Phase 1W cleanup — only render the status row when there's
          actually something to say (saving / error). The previous
          "No field regions yet…" placeholder pill cluttered the
          toolbar; the Draw / Field controls in the toolbar already
          communicate what to do. When marks are present we surface
          the count via the toolbar's "N marks on this page" meta. */}
      {(saving || saveError) && (
        <div className="pdf-workspace-status">
          {saving && <span className="pill pill-info">Saving marks…</span>}
          {saveError && (
            <span className="pill pill-warn">
              {saveError}{" "}
              <button
                type="button"
                className="pill-link"
                onClick={() => setLoadAttempt((n) => n + 1)}
              >
                Retry
              </button>
            </span>
          )}
        </div>
      )}
      <div
        className={`pdf-workspace-body ${
          pagePanelOpen ? "is-page-panel-open" : "is-page-panel-collapsed"
        }`}
        onDragEnter={acceptAddComposerDrag}
        onDragOver={acceptAddComposerDrag}
        onDragLeave={handleAddComposerDragLeave}
        onDrop={handleViewerDirectDrop}
      >
        <div
          className={`pdf-workspace-canvas-area ${
            spacePanActive
              ? panStateRef.current
                ? "is-space-panning"
                : "is-space-pan"
              : ""
          } ${isDocumentReady ? "is-document-ready" : "is-preparing-document"}`}
          ref={setScrollRoot}
          data-testid="pdf-continuous-scroll"
          onScroll={handleCanvasScroll}
          onWheelCapture={handleManualWheelCapture}
          onPointerDownCapture={handleManualPointerCapture}
          onPointerDown={handlePanPointerDown}
          onPointerMove={handlePanPointerMove}
          onPointerUp={handlePanPointerUp}
          onPointerCancel={handlePanPointerUp}
          onDragEnter={acceptAddComposerDrag}
          onDragOver={acceptAddComposerDrag}
          onDragLeave={handleAddComposerDragLeave}
          onDrop={handleViewerDirectDrop}
        >
        {!isDocumentReady && (
          <div className="pdf-document-preparing" data-testid="pdf-document-preparing">
            <div className="pdf-document-preparing-card">
              <span className="pdf-loading-dots" aria-hidden>
                <span />
                <span />
                <span />
              </span>
              <span>Loading document</span>
            </div>
          </div>
        )}
        <div
          className={`pdf-workspace-stack pdf-workspace-continuous is-main-virtualized ${
            isDocumentReady ? "is-ready" : "is-hidden-until-frame"
          }`}
        >
          {!isPdfLayoutReady && (
            <div className="pdf-layout-reserve" data-testid="pdf-layout-reserve" />
          )}
          {visibleMainPages.topSpacer > 0 && (
            <div
              className="pdf-page-main-spacer"
              style={{ height: `${visibleMainPages.topSpacer}px` }}
              aria-hidden="true"
            />
          )}
          {visibleMainPages.pages.map(({ page }) => {
            const n = page.globalPageNumber;
            const size = pageSizes[n];
            const isActive = n === activePageNumber;
            const isFocused = n === focusedPageNumber;
            const pageDisplayWidth = page.naturalSize.width * zoom;
            const pageDisplayHeight = page.naturalSize.height * zoom;
            return (
              <div
                key={`${page.renderFileUrl}:${page.renderPageNumber}:${page.fileId}:${page.localPageNumber}:${n}`}
                ref={setPageRef(n)}
                className={`pdf-page-shell ${isActive ? "active-page" : ""} ${
                  isFocused ? "focused-page" : ""
                }`}
                style={{
                  width: `${pageDisplayWidth}px`,
                  minHeight: `${pageDisplayHeight}px`,
                  height: `${pageDisplayHeight}px`,
                }}
                data-page-number={n}
                data-testid="pdf-page-shell"
              >
                <div className="pdf-page-label">Page {n}</div>
                <PdfPageCanvas
                  fileUrl={page.renderFileUrl}
                  pageNumber={page.renderPageNumber}
                  zoom={zoom}
                  initialNaturalSize={hasCurrentMetadata ? page.naturalSize : null}
                  onFirstFrame={() => {
                    setFirstFrameReady(true);
                    setFirstFrameFileUrl(documentSetKey);
                  }}
                  suppressFirstFramePlaceholder
                  onPageRendered={(info) => {
                    // Phase 2I defensive: never accept pageCount <= 0
                    // from a child render. A bug in earlier code emitted
                    // 0 on each zoom tick, which collapsed pageNumbers
                    // to [1] and killed continuous scroll. Keep whatever
                    // count we already have if the child can't supply
                    // a fresh, positive number.
                    if (typeof info.pageCount === "number" && info.pageCount > 0) {
                      setPageCount((prev) => Math.max(prev, documentPages.length || info.pageCount));
                    }
                    setPageSizes((prev) => ({
                      ...prev,
                      [n]: {
                        width: info.pageWidth,
                        height: info.pageHeight,
                      },
                    }));
                    // Phase 2I — back the natural (zoom=1) width out of
                    // the rendered width. We capture this once per file
                    // (page 1 is enough — every page in a vendor's PDF
                    // bundle has the same width). zoom can briefly be
                    // 0 during the very first render; guard it.
                    if (
                      n === activePageNumberRef.current &&
                      zoom > 0 &&
                      naturalPageWidthRef.current == null
                    ) {
                      const natural = info.pageWidth / zoom;
                      if (Number.isFinite(natural) && natural > 0) {
                        naturalPageWidthRef.current = natural;
                        setNaturalPageSize((current) =>
                          current ?? {
                            width: natural,
                            height: info.pageHeight / zoom,
                          },
                        );
                        // Force a re-evaluation of fitZoom by nudging
                        // pageSizes (already set above — useMemo will
                        // pick up the new ref value).
                        setContainerWidth((w) => (w == null ? w : w));
                      }
                    }
                  }}
                />
                {n === activePageNumber && (
                  <AiScanOverlay
                    progress={aiProgress}
                    currentFilename={page.fileId}
                    variant="document"
                  />
                )}
                {size && (
                  <PdfOverlay
                    pageWidth={size.width}
                    pageHeight={size.height}
                    pageNumber={page.localPageNumber}
                    fileId={page.fileId}
                    tool={tool}
                    drawLabel={drawLabel}
                    regions={regionsByPage[n] ?? []}
                    selectedId={selectedId}
                    onSelect={setSelectedId}
                    onAdd={handleAdd}
                    onUpdate={handleUpdate}
                    onDelete={handleDelete}
                  />
                )}
                {size && (
                  <TraceOverlay
                    pageNumber={page.localPageNumber}
                    pageWidth={size.width}
                    pageHeight={size.height}
                    items={tracesByPage[n] ?? []}
                    highlightedTraceIds={highlightedSet}
                    hoveredTraceId={hoveredTraceId}
                    onTraceHover={handleTraceHover}
                    onTraceClick={onTraceClick}
                    enabled={tracesEnabled}
                  />
                )}
              </div>
            );
          })}
          {visibleMainPages.bottomSpacer > 0 && (
            <div
              className="pdf-page-main-spacer"
              style={{ height: `${visibleMainPages.bottomSpacer}px` }}
              aria-hidden="true"
            />
          )}
        </div>
      </div>

        {pagePanelOpen ? (
          <aside
            className="pdf-page-sidebar"
            aria-label="Document pages"
            data-testid="pdf-page-sidebar"
            onDragEnter={acceptAddComposerDrag}
            onDragOver={acceptAddComposerDrag}
            onDragLeave={handleAddComposerDragLeave}
            onDrop={handleViewerDirectDrop}
          >
            <div className="pdf-page-sidebar-header">
              <div className="pdf-page-sidebar-title">
                <span>Pages</span>
                <small>
                  {pageCount || pageNumbers.length || "-"} total
                  {visibleUploadItems.length > 0
                    ? ` · ${visibleUploadItems.length} uploading`
                    : ""}
                </small>
              </div>
            </div>
            <div
              ref={setThumbnailListNode}
              className="pdf-page-thumb-list"
              aria-label="Document pages"
              onScroll={handleThumbnailListScroll}
              onDragEnter={acceptAddComposerDrag}
              onDragOver={acceptAddComposerDrag}
              onDragLeave={handleAddComposerDragLeave}
              onDrop={handleViewerDirectDrop}
            >
              {pageNumbers.length > 0 ? (
                <>
                  {visibleThumbnailPages.topSpacer > 0 && (
                    <div
                      className="pdf-page-thumb-spacer"
                      style={{ height: `${visibleThumbnailPages.topSpacer}px` }}
                      aria-hidden="true"
                    />
                  )}
                  {visibleThumbnailPages.pages.map((page) => {
                    const n = page.globalPageNumber;
                    const isActive = n === activePageNumber;
                    const pageRoute = findPageRouteDecision(
                      processingRoutes,
                      page.fileId,
                      page.localPageNumber,
                    );
                    return (
                      <div
                        key={`${page.fileUrl}:thumb:${page.localPageNumber}:${n}`}
                        ref={setThumbnailRef(n)}
                        className={`pdf-page-thumb-item ${isActive ? "active" : ""}`}
                      >
                        <button
                          type="button"
                          className={`pdf-page-thumb ${isActive ? "active" : ""}`}
                          onClick={(event) => {
                            event.preventDefault();
                            event.stopPropagation();
                            scrollToPage(n, "smooth", { force: true });
                          }}
                          aria-current={isActive ? "page" : undefined}
                          data-testid="pdf-page-thumbnail"
                          data-page-number={n}
                        >
                          <PdfPageThumbnail
                        fileUrl={page.renderFileUrl}
                        pageNumber={page.renderPageNumber}
                            displayPageNumber={n}
                            naturalSize={page.naturalSize}
                            deferRender={!isDocumentReady}
                          />
                        </button>
                        <span
                          className={`pdf-page-route-badge route-${pageRoute?.effective_route || "auto"}`}
                          title={
                            pageRoute
                              ? `Backend route: ${pageRoute.effective_route}; ${pageRoute.reason_code}`
                              : "Backend route: auto cost-safe"
                          }
                          data-testid="pdf-page-route-badge"
                        >
                          {shortRouteBadge(pageRoute)}
                        </span>
                        <KebabMenu
                          ariaLabel={`Actions for page ${n}`}
                          testId="pdf-page-actions"
                          className="pdf-page-thumb-kebab"
                          items={[
                            {
                              label: "Process page",
                              hint: "Add only this page to the current template.",
                              onClick: () => {
                                if (onProcessPage) {
                                  void onProcessPage(page.localPageNumber, "merge", page.fileId);
                                }
                              },
                              disabled: processPageDisabled || !onProcessPage,
                              hidden: !onProcessPage,
                            },
                            {
                              label: "New template from page",
                              hint: "Use only this page.",
                              onClick: () => {
                                if (onProcessPage) {
                                  void onProcessPage(page.localPageNumber, "replace", page.fileId);
                                }
                              },
                              disabled: processPageDisabled || !onProcessPage,
                              hidden: !onProcessPage,
                            },
                            {
                              label: "Routing: use document default",
                              hint: "Remove the page exception. Used when this page is processed individually.",
                              onClick: () => {
                                if (onSetPageRoute) {
                                  void onSetPageRoute(page.fileId, page.localPageNumber, null);
                                }
                              },
                              disabled: processingRouteBusy || !onSetPageRoute,
                              hidden: !onSetPageRoute,
                            },
                            {
                              label: "Routing: deterministic only",
                              hint: "Never call AI for this page; block if no parser is available.",
                              onClick: () => {
                                if (onSetPageRoute) {
                                  void onSetPageRoute(
                                    page.fileId,
                                    page.localPageNumber,
                                    "deterministic_only",
                                  );
                                }
                              },
                              disabled: processingRouteBusy || !onSetPageRoute,
                              hidden: !onSetPageRoute,
                            },
                            {
                              label: "Routing: auto cost-safe",
                              hint: "Use a registered parser without AI; unknown pages remain AI-eligible.",
                              onClick: () => {
                                if (onSetPageRoute) {
                                  void onSetPageRoute(
                                    page.fileId,
                                    page.localPageNumber,
                                    "auto_cost_safe",
                                  );
                                }
                              },
                              disabled: processingRouteBusy || !onSetPageRoute,
                              hidden: !onSetPageRoute,
                            },
                            {
                              label: "Routing: allow AI fallback",
                              hint: "Run deterministic first and permit a paid AI call only if it produces no result.",
                              onClick: () => {
                                if (onSetPageRoute) {
                                  void onSetPageRoute(
                                    page.fileId,
                                    page.localPageNumber,
                                    "ai_fallback_allowed",
                                  );
                                }
                              },
                              disabled: processingRouteBusy || !onSetPageRoute,
                              hidden: !onSetPageRoute,
                            },
                          ]}
                        />
                      </div>
                    );
                  })}
                  {visibleThumbnailPages.bottomSpacer > 0 && (
                    <div
                      className="pdf-page-thumb-spacer"
                      style={{ height: `${visibleThumbnailPages.bottomSpacer}px` }}
                      aria-hidden="true"
                    />
                  )}
                  {visibleUploadItems.map((item) => (
                    <PdfUploadThumbnail key={item.id} item={item} />
                  ))}
                  {hiddenUploadItemCount > 0 && (
                    <div className="pdf-page-upload-overflow">
                      {hiddenUploadItemCount} more uploading
                    </div>
                  )}
                  <button
                    ref={addDocumentsButtonRef}
                    type="button"
                    className="pdf-page-add-thumb"
                    onClick={handleAddDocumentsClick}
                    disabled={!onAddDocuments}
                    title="Add pages, screenshots, or documents"
                    aria-label="Add pages, screenshots, or documents"
                    data-testid="pdf-page-add-document"
                  >
                    <span className="pdf-page-add-thumb-plus" aria-hidden>+</span>
                  </button>
                </>
              ) : (
                <div className="pdf-page-thumb-loading">Loading pages</div>
              )}
            </div>
          </aside>
        ) : null}
      </div>

      {addComposerOpen ? (
        <section
          ref={addComposerRef}
          className={`pdf-add-composer ${addComposerDragging ? "is-dragging" : ""}`}
          role="dialog"
          aria-label="Add documents"
          style={{ left: addComposerAnchor.left, top: addComposerAnchor.top }}
          onDragEnter={acceptAddComposerDrag}
          onDragOver={acceptAddComposerDrag}
          onDragLeave={handleAddComposerDragLeave}
          onDrop={handleAddComposerDrop}
        >
          <header className="pdf-add-composer-header">
            <button
              type="button"
              className="pdf-add-composer-close"
                onClick={handleCloseAddComposer}
                aria-label="Close add documents"
              >
                x
              </button>
            </header>

            <div
              className="pdf-add-composer-dropzone"
              onDragEnter={acceptAddComposerDrag}
              onDragOver={acceptAddComposerDrag}
              onDragLeave={handleAddComposerDragLeave}
              onDrop={handleAddComposerDrop}
            >
              <textarea
                ref={addDocumentsTextRef}
                className="pdf-add-composer-input"
                aria-label="Paste screenshots or files"
                placeholder=""
                value=""
                onChange={() => undefined}
                onDoubleClick={handleBrowseDocuments}
                onDragEnter={acceptAddComposerDrag}
                onDragOver={acceptAddComposerDrag}
                onDragLeave={handleAddComposerDragLeave}
                onDrop={handleAddComposerDrop}
                onPaste={handleAddComposerPaste}
              />
              <div className="pdf-add-composer-prompt" aria-hidden>
                <span>+</span>
                <strong>Drop documents here</strong>
                <small>Click here and press Ctrl+V to paste screenshots, or choose any source file</small>
              </div>
            </div>

            {stagedDocuments.length > 0 ? (
              <div
                className="pdf-add-chat-dock"
                aria-label="Files ready to upload"
                onDragEnter={acceptAddComposerDrag}
                onDragOver={acceptAddComposerDrag}
                onDragLeave={handleAddComposerDragLeave}
                onDrop={handleAddComposerDrop}
              >
                <div className="pdf-add-chat-attachments">
                  <button
                    type="button"
                    className="pdf-add-chat-plus"
                    onClick={handleBrowseDocuments}
                    aria-label="Choose more files"
                  >
                    <span aria-hidden />
                  </button>
                  <div className="pdf-add-chat-strip">
                    {stagedDocuments.map((file, index) => (
                      <StagedDocumentAttachment
                        key={`${file.name}:${file.size}:${file.lastModified}:${index}`}
                        file={file}
                        index={index}
                        onRemove={handleRemoveStagedDocument}
                      />
                    ))}
                  </div>
                </div>
                <div className="pdf-add-chat-controls">
                  <span className="pdf-add-chat-aa">Aa</span>
                  <button
                    type="button"
                    className="pdf-add-chat-send"
                    onClick={() => void handleSubmitStagedDocuments()}
                    disabled={addComposerUploading}
                    aria-label={`Upload ${stagedDocuments.length} file${stagedDocuments.length === 1 ? "" : "s"}`}
                  >
                    <span aria-hidden />
                  </button>
                </div>
              </div>
            ) : (
              <footer className="pdf-add-composer-actions">
                <button type="button" className="pdf-add-composer-secondary" onClick={handleBrowseDocuments}>
                  Choose files
                </button>
              </footer>
            )}

            <input
              ref={addDocumentsInputRef}
              className="pdf-page-add-input"
              type="file"
              multiple
              onChange={handleAddDocumentsChange}
              tabIndex={-1}
              aria-hidden="true"
            />
        </section>
      ) : null}
    </div>
  );
}
