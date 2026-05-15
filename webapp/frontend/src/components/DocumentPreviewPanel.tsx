import { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import { api, getFriendlyErrorMessage } from "../api";
import type { BatchProgress, FilePreview } from "../types";
import { AiScanOverlay } from "./AiScanOverlay";

const PdfWorkspace = lazy(() =>
  import("./pdf_workspace/PdfWorkspace").then((m) => ({ default: m.PdfWorkspace })),
);

type Props = {
  batchId: string | null;
  filename: string | null;
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
  onClose?: () => void;
  isMaximized?: boolean;
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
};

type DisplayPreview = {
  batchId: string;
  filename: string;
  preview: FilePreview;
};

export function DocumentPreviewPanel({
  batchId,
  filename,
  collapsed,
  targetPage,
  onActivePageChange,
  onToggleCollapsed,
  onMaximize,
  onPopout,
  onClose,
  isMaximized,
  highlightedTraceIds,
  onTraceClick,
  onTraceHover,
  remapActive,
  onRemapDrawn,
  aiProgress,
}: Props) {
  const [display, setDisplay] = useState<DisplayPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setError(null);
    if (!batchId || !filename) {
      setDisplay(null);
      setLoading(false);
      return;
    }

    let cancelled = false;
    setLoading(true);
    (async () => {
      try {
        const p = await api.filePreview(batchId, filename);
        if (!cancelled) {
          setDisplay({ batchId, filename, preview: p });
          setLoading(false);
        }
      } catch (e) {
        if (!cancelled) {
          setError(getFriendlyErrorMessage(e, "Load preview"));
          setLoading(false);
          // eslint-disable-next-line no-console
          console.warn("document preview failed:", e);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [batchId, filename]);

  const activePreview = display?.preview ?? null;
  const isPdf =
    activePreview?.kind === "binary" && activePreview.extension === ".pdf";

  const header = (
    <div className="card-header doc-preview-header">
      <span className="doc-preview-title" title={filename ?? ""}>
        <DocIcon />
        <span className="doc-preview-name">
          {filename ?? display?.filename ?? "Document"}
        </span>
      </span>
      {/* Window controls intentionally removed per UX directive: the
          Document panel no longer carries minimize / maximize / popout /
          close buttons. The Template panel is the only module that still
          has controls (detach / reattach). */}
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

  if (!batchId || !filename) {
    return (
      <div className="card doc-preview-card">
        {header}
        <div className="empty-state small">
          Select a document to preview and mark extraction fields.
        </div>
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
                  key={`${display.batchId}:${display.filename}`}
                  batchId={display.batchId}
                  fileUrl={api.fileContentUrl(display.batchId, display.filename)}
                  fileId={display.filename}
                  targetPage={
                    targetPage &&
                    targetPage.batchId === display.batchId &&
                    targetPage.filename === display.filename
                      ? targetPage
                      : null
                  }
                  onActivePageChange={(pageNumber) =>
                    onActivePageChange?.({
                      batchId: display.batchId,
                      filename: display.filename,
                      pageNumber,
                    })
                  }
                  highlightedTraceIds={highlightedTraceIds}
                  onTraceClick={onTraceClick}
                  onTraceHover={onTraceHover}
                  remapActive={remapActive}
                  onRemapDrawn={onRemapDrawn}
                  aiProgress={aiProgress}
                />
              </Suspense>
            ) : (
              <BinaryPreview
                url={api.fileContentUrl(display.batchId, display.filename)}
                extension={activePreview.extension}
                filename={activePreview.filename}
                aiProgress={aiProgress}
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

function DocumentLoadingSkeleton() {
  return (
    <div className="doc-preview-skeleton" data-testid="document-preview-skeleton">
      <div className="doc-preview-skeleton-page" />
    </div>
  );
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

function BinaryPreview({
  url,
  extension,
  filename,
  aiProgress,
}: {
  url: string;
  extension: string;
  filename: string;
  aiProgress?: BatchProgress | null;
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
    return <ImagePreview url={url} filename={filename} aiProgress={aiProgress} />;
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
}: {
  url: string;
  filename: string;
  aiProgress?: BatchProgress | null;
}) {
  const stageRef = useRef<HTMLDivElement | null>(null);
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

  useEffect(() => {
    setZoom(1);
    zoomRef.current = 1;
    targetZoomRef.current = 1;
    zoomAnchorRef.current = null;
    if (lerpRafRef.current != null) {
      window.cancelAnimationFrame(lerpRafRef.current);
      lerpRafRef.current = null;
    }
    setIsPanning(false);
    panRef.current = null;
    const node = stageRef.current;
    if (node) {
      node.scrollLeft = 0;
      node.scrollTop = 0;
    }
  }, [url]);

  useEffect(() => {
    zoomRef.current = zoom;
  }, [zoom]);

  const clampZoom = useCallback((next: number) => {
    return Math.min(4, Math.max(0.35, Number(next.toFixed(2))));
  }, []);

  const startZoomLerp = useCallback(() => {
    if (lerpRafRef.current != null) return;
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
      lerpRafRef.current = window.requestAnimationFrame(tick);
    };
    lerpRafRef.current = window.requestAnimationFrame(tick);
  }, []);

  const changeZoom = useCallback(
    (delta: number) => {
      zoomAnchorRef.current = null;
      if (lerpRafRef.current != null) {
        window.cancelAnimationFrame(lerpRafRef.current);
        lerpRafRef.current = null;
      }
      const next = clampZoom(zoomRef.current + delta);
      targetZoomRef.current = next;
      zoomRef.current = next;
      setZoom(next);
    },
    [clampZoom],
  );

  const resetZoom = useCallback(() => {
    zoomAnchorRef.current = null;
    if (lerpRafRef.current != null) {
      window.cancelAnimationFrame(lerpRafRef.current);
      lerpRafRef.current = null;
    }
    zoomRef.current = 1;
    targetZoomRef.current = 1;
    setZoom(1);
    const node = stageRef.current;
    if (node) {
      node.scrollLeft = 0;
      node.scrollTop = 0;
    }
  }, []);

  useEffect(() => {
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

    document.addEventListener("wheel", onWheel, { passive: false, capture: true });
    return () => {
      document.removeEventListener(
        "wheel",
        onWheel as EventListener,
        { capture: true } as EventListenerOptions,
      );
      if (lerpRafRef.current != null) {
        window.cancelAnimationFrame(lerpRafRef.current);
        lerpRafRef.current = null;
      }
    };
  }, [clampZoom, startZoomLerp]);

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

  return (
    <div className="image-workspace" data-testid="image-preview-workspace">
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
          style={{ width: `${Math.round(zoom * 100)}%` }}
        >
          <img src={url} alt={filename} draggable={false} />
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
