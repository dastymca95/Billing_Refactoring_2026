import { lazy, Suspense, useEffect, useState } from "react";

import { api, getFriendlyErrorMessage } from "../api";
import type { FilePreview } from "../types";

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
                />
              </Suspense>
            ) : (
              <BinaryPreview
                url={api.fileContentUrl(display.batchId, display.filename)}
                extension={activePreview.extension}
                filename={activePreview.filename}
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
}: {
  url: string;
  extension: string;
  filename: string;
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
    return <img src={url} alt={filename} />;
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
