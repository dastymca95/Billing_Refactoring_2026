import { lazy, Suspense, useEffect, useState } from "react";

import { api } from "../api";
import type { FilePreview } from "../types";

// Lazy-load the workspace so the native preview path doesn't pay for
// pdfjs-dist when the operator never opens Field Region Mode.
const PdfWorkspace = lazy(() =>
  import("./pdf_workspace/PdfWorkspace").then((m) => ({ default: m.PdfWorkspace })),
);

type Mode = "native" | "workspace";

type Props = {
  batchId: string | null;
  filename: string | null;
  collapsed?: boolean;
  onToggleCollapsed?: () => void;
};

export function DocumentPreviewPanel({
  batchId,
  filename,
  collapsed,
  onToggleCollapsed,
}: Props) {
  const [preview, setPreview] = useState<FilePreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<Mode>("native");

  useEffect(() => {
    setPreview(null);
    setError(null);
    if (!batchId || !filename) return;
    let cancelled = false;
    (async () => {
      try {
        const p = await api.filePreview(batchId, filename);
        if (!cancelled) setPreview(p);
      } catch (e) {
        if (!cancelled) setError(String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [batchId, filename]);

  // Reset to native mode when switching files.
  useEffect(() => {
    setMode("native");
  }, [filename]);

  const isPdf = preview?.kind === "binary" && preview.extension === ".pdf";

  const header = (
    <div className="card-header">
      <span className="doc-preview-title" title={filename ?? ""}>
        Document preview
        {filename ? (
          <span className="doc-preview-filename"> · {filename}</span>
        ) : null}
      </span>
      <div className="doc-preview-toggles">
        {isPdf && (
          <div className="mode-toggle" role="tablist" aria-label="Document view">
            <button
              role="tab"
              aria-selected={mode === "native"}
              className={`mode-toggle-btn ${mode === "native" ? "active" : ""}`}
              onClick={() => setMode("native")}
              title="Read-only document view"
            >
              Document
            </button>
            <button
              role="tab"
              aria-selected={mode === "workspace"}
              className={`mode-toggle-btn ${mode === "workspace" ? "active" : ""}`}
              onClick={() => setMode("workspace")}
              title="Mark extraction fields with rectangles to guide processing"
            >
              Mark Fields
            </button>
          </div>
        )}
        {onToggleCollapsed && (
          <button onClick={onToggleCollapsed} className="icon-button">
            {collapsed ? "Expand" : "Collapse"}
          </button>
        )}
      </div>
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
          Select a file from the list to preview it.
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="card doc-preview-card">
        {header}
        <div className="card-body">
          <div className="error-banner">Could not load preview: {error}</div>
        </div>
      </div>
    );
  }

  return (
    <div className="card doc-preview-card">
      {header}
      <div className="card-body tight doc-preview-body">
        {!preview && <div className="empty-state small">Loading preview…</div>}
        {preview?.kind === "table" && (
          <div className="doc-preview-table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  {preview.headers.map((h, i) => (
                    <th key={i}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {preview.rows.map((row, i) => (
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
        {preview?.kind === "binary" && (
          <>
            {isPdf && mode === "workspace" ? (
              <Suspense
                fallback={
                  <div className="empty-state small">Loading workspace…</div>
                }
              >
                <PdfWorkspace
                  batchId={batchId}
                  fileUrl={api.fileContentUrl(batchId, filename)}
                  fileId={filename}
                />
              </Suspense>
            ) : (
              <BinaryPreview
                url={api.fileContentUrl(batchId, filename)}
                extension={preview.extension}
                filename={preview.filename}
              />
            )}
          </>
        )}
        {preview?.kind === "metadata" && (
          <div className="empty-state small">{preview.note}</div>
        )}
      </div>
    </div>
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
