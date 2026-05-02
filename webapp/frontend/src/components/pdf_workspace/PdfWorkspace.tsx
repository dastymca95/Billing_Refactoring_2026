// Phase 1H — top-level PDF workspace.
//
// Composes the toolbar, the canvas (PDF.js render), and the overlay
// (region drawing + select/move/resize/delete). Region state is persisted
// to the backend via the `api.replaceRegions` PUT endpoint after each
// save. Selecting a different file resets the selection to page 1.

import { useCallback, useEffect, useMemo, useState } from "react";

import { api } from "../../api";
import type { RegionHint, RegionLabel } from "../../types";
import { PdfOverlay } from "./PdfOverlay";
import { PdfPageCanvas } from "./PdfPageCanvas";
import { ViewerToolbar } from "./ViewerToolbar";
import type { Tool } from "./types";

type Props = {
  batchId: string;
  fileUrl: string;
  fileId: string; // filename inside the batch input/ folder
};

export function PdfWorkspace({ batchId, fileUrl, fileId }: Props) {
  const [pageNumber, setPageNumber] = useState(1);
  const [pageCount, setPageCount] = useState(0);
  const [pageW, setPageW] = useState(0);
  const [pageH, setPageH] = useState(0);
  const [zoom, setZoom] = useState(1.0);
  const [tool, setTool] = useState<Tool>("select");
  const [drawLabel, setDrawLabel] = useState<RegionLabel>("service_address");
  const [allRegions, setAllRegions] = useState<RegionHint[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

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
        const msg = String(e);
        if (msg.includes("404")) {
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

  // Reset to page 1 when file changes.
  useEffect(() => {
    setPageNumber(1);
    setSelectedId(null);
  }, [fileId]);

  // Filter regions to the current file + page.
  const regionsOnPage = useMemo(
    () =>
      allRegions.filter(
        (r) => r.file_id === fileId && r.page_number === pageNumber,
      ),
    [allRegions, fileId, pageNumber],
  );

  // Persist the full list to the backend (debounced via state-write
  // batching — every change calls saveRegions once on the next tick).
  const saveRegions = useCallback(
    async (next: RegionHint[]) => {
      setSaving(true);
      setSaveError(null);
      try {
        await api.replaceRegions(batchId, next);
      } catch (e) {
        setSaveError(`Save failed: ${e}`);
      } finally {
        setSaving(false);
      }
    },
    [batchId],
  );

  const handleAdd = useCallback(
    (region: RegionHint) => {
      const next = [...allRegions, region];
      setAllRegions(next);
      void saveRegions(next);
    },
    [allRegions, saveRegions],
  );

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
    <div className="pdf-workspace">
      <ViewerToolbar
        tool={tool}
        onToolChange={setTool}
        zoom={zoom}
        onZoomIn={() => setZoom((z) => Math.min(3.0, +(z + 0.25).toFixed(2)))}
        onZoomOut={() => setZoom((z) => Math.max(0.5, +(z - 0.25).toFixed(2)))}
        onResetZoom={() => setZoom(1.0)}
        drawLabel={drawLabel}
        onDrawLabelChange={setDrawLabel}
        pageNumber={pageNumber}
        pageCount={pageCount}
        onPrevPage={() => setPageNumber((p) => Math.max(1, p - 1))}
        onNextPage={() =>
          setPageNumber((p) => (pageCount > 0 ? Math.min(pageCount, p + 1) : p + 1))
        }
        regionsCount={regionsOnPage.length}
      />
      <div className="pdf-workspace-status">
        {saving && <span className="pill pill-info">Saving regions…</span>}
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
        {!saving && !saveError && allRegions.length === 0 && (
          <span className="pill pill-muted">
            No field regions yet. Draw a box around important fields like
            service address or total amount.
          </span>
        )}
      </div>
      <div className="pdf-workspace-canvas-area">
        <div className="pdf-workspace-stack">
          <PdfPageCanvas
            fileUrl={fileUrl}
            pageNumber={pageNumber}
            zoom={zoom}
            onPageRendered={(info) => {
              setPageW(info.pageWidth);
              setPageH(info.pageHeight);
              setPageCount(info.pageCount);
            }}
          />
          {pageW > 0 && pageH > 0 && (
            <PdfOverlay
              pageWidth={pageW}
              pageHeight={pageH}
              pageNumber={pageNumber}
              fileId={fileId}
              tool={tool}
              drawLabel={drawLabel}
              regions={regionsOnPage}
              selectedId={selectedId}
              onSelect={setSelectedId}
              onAdd={handleAdd}
              onUpdate={handleUpdate}
              onDelete={handleDelete}
            />
          )}
        </div>
      </div>
    </div>
  );
}
