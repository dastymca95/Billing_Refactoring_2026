type Props = {
  zoom: number;
  onZoomIn: () => void;
  onZoomOut: () => void;
  onResetZoom: () => void;
  pageNumber: number;
  pageCount: number;
  isLayoutReady?: boolean;
  onPrevPage: () => void;
  onNextPage: () => void;
  regionsCount: number;
  traceCount?: number;
  tracesEnabled?: boolean;
  onToggleTraces?: () => void;
};

export function ViewerToolbar({
  zoom,
  onZoomIn,
  onZoomOut,
  onResetZoom,
  pageNumber,
  pageCount,
  isLayoutReady = true,
  onPrevPage,
  onNextPage,
  regionsCount,
  traceCount = 0,
  tracesEnabled = false,
  onToggleTraces,
}: Props) {
  const hasTraces = traceCount > 0 && onToggleTraces;

  return (
    <div className="viewer-toolbar">
      <div className="toolbar-group toolbar-group-page">
        <button
          className="tool-btn tool-btn-icon"
          onClick={onPrevPage}
          disabled={!isLayoutReady || pageNumber <= 1}
          title="Previous page"
          aria-label="Previous page"
        >
          &lt;
        </button>
        <span className="toolbar-page">
          Page {pageNumber} / {pageCount || "?"}
        </span>
        <button
          className="tool-btn tool-btn-icon"
          onClick={onNextPage}
          disabled={!isLayoutReady || pageNumber >= pageCount}
          title="Next page"
          aria-label="Next page"
        >
          &gt;
        </button>
      </div>

      <div className="toolbar-group toolbar-group-zoom">
        <button
          className="tool-btn tool-btn-icon"
          onClick={onZoomOut}
          disabled={!isLayoutReady}
          title="Zoom out"
          aria-label="Zoom out"
        >
          -
        </button>
        <button
          className="tool-btn tool-btn-narrow"
          onClick={onResetZoom}
          disabled={!isLayoutReady}
          title="Reset zoom"
        >
          {isLayoutReady ? (
            `${Math.round(zoom * 100)}%`
          ) : (
            <span className="toolbar-zoom-loading" aria-hidden />
          )}
        </button>
        <button
          className="tool-btn tool-btn-icon"
          onClick={onZoomIn}
          disabled={!isLayoutReady}
          title="Zoom in"
          aria-label="Zoom in"
        >
          +
        </button>
      </div>

      <div className="toolbar-spacer" />

      {regionsCount > 0 && (
        <div className="toolbar-meta">
          {regionsCount} mark{regionsCount === 1 ? "" : "s"}
        </div>
      )}

      {hasTraces && (
        <button
          type="button"
          className={`pdf-trace-toggle ${tracesEnabled ? "is-on" : ""}`}
          onClick={onToggleTraces}
          title={
            tracesEnabled
              ? `Hide extraction traces (${traceCount})`
              : `Show extraction traces (${traceCount})`
          }
          aria-label={
            tracesEnabled
              ? `Hide extraction traces (${traceCount})`
              : `Show extraction traces (${traceCount})`
          }
          aria-pressed={tracesEnabled}
          data-testid="pdf-trace-toggle"
        >
          <svg
            width="12"
            height="12"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <path d="M4 7h10" />
            <path d="M4 12h16" />
            <path d="M4 17h7" />
          </svg>
          <span>Traces</span>
          <span className="pdf-trace-count">{traceCount}</span>
        </button>
      )}
    </div>
  );
}
