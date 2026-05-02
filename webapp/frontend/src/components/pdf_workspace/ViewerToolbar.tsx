// Phase 1H — workspace toolbar.
//
// Tool buttons (select / draw / pan / delete / zoom in/out / reset zoom)
// plus a region-label dropdown that controls which label new draws get.

import type { RegionLabel } from "../../types";
import { REGION_LABEL_OPTIONS, type Tool } from "./types";

type Props = {
  tool: Tool;
  onToolChange: (t: Tool) => void;
  zoom: number;
  onZoomIn: () => void;
  onZoomOut: () => void;
  onResetZoom: () => void;
  drawLabel: RegionLabel;
  onDrawLabelChange: (l: RegionLabel) => void;
  pageNumber: number;
  pageCount: number;
  onPrevPage: () => void;
  onNextPage: () => void;
  regionsCount: number;
};

export function ViewerToolbar({
  tool,
  onToolChange,
  zoom,
  onZoomIn,
  onZoomOut,
  onResetZoom,
  drawLabel,
  onDrawLabelChange,
  pageNumber,
  pageCount,
  onPrevPage,
  onNextPage,
  regionsCount,
}: Props) {
  return (
    <div className="viewer-toolbar">
      <div className="toolbar-group">
        <button
          className={`tool-btn ${tool === "select" ? "active" : ""}`}
          onClick={() => onToolChange("select")}
          title="Select / move regions"
        >
          ↖ Select
        </button>
        <button
          className={`tool-btn ${tool === "draw" ? "active" : ""}`}
          onClick={() => onToolChange("draw")}
          title="Draw a new region"
        >
          ▭ Draw
        </button>
        <button
          className={`tool-btn ${tool === "pan" ? "active" : ""}`}
          onClick={() => onToolChange("pan")}
          title="Pan the page"
        >
          ✋ Pan
        </button>
        <button
          className={`tool-btn ${tool === "delete" ? "active" : ""}`}
          onClick={() => onToolChange("delete")}
          title="Click a region to delete it"
        >
          ✕ Delete
        </button>
      </div>

      <div className="toolbar-group">
        <label className="toolbar-label">
          Region:
          <select
            className="toolbar-select"
            value={drawLabel}
            onChange={(e) => onDrawLabelChange(e.target.value as RegionLabel)}
          >
            {REGION_LABEL_OPTIONS.map((o) => (
              <option key={o.label} value={o.label}>
                {o.title}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="toolbar-group">
        <button
          className="tool-btn"
          onClick={onPrevPage}
          disabled={pageNumber <= 1}
          title="Previous page"
        >
          ‹
        </button>
        <span className="toolbar-page">
          Page {pageNumber} / {pageCount || "?"}
        </span>
        <button
          className="tool-btn"
          onClick={onNextPage}
          disabled={pageNumber >= pageCount}
          title="Next page"
        >
          ›
        </button>
      </div>

      <div className="toolbar-group">
        <button className="tool-btn" onClick={onZoomOut} title="Zoom out">
          −
        </button>
        <button
          className="tool-btn tool-btn-narrow"
          onClick={onResetZoom}
          title="Reset zoom"
        >
          {Math.round(zoom * 100)}%
        </button>
        <button className="tool-btn" onClick={onZoomIn} title="Zoom in">
          +
        </button>
      </div>

      <div className="toolbar-spacer" />
      <div className="toolbar-meta">
        {regionsCount} region{regionsCount === 1 ? "" : "s"} on this page
      </div>
    </div>
  );
}
