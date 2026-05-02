// Phase 1H — interactive overlay above the PDF canvas.
//
// Captures pointer events for drawing new rectangles (in "draw" mode),
// clicking/moving existing regions ("select" mode), and deleting
// ("delete" mode). All coordinates persist normalized 0–1 so they
// survive zoom changes.

import { useCallback, useRef, useState } from "react";

import type { RegionHint, RegionLabel } from "../../types";
import { clamp, newRegionId, normaliseDragBox, pxToNorm } from "./geometry";
import { RegionBox } from "./RegionBox";
import { colorForLabel, type Tool } from "./types";

type Props = {
  pageWidth: number;
  pageHeight: number;
  pageNumber: number;
  fileId: string;
  tool: Tool;
  drawLabel: RegionLabel;
  regions: RegionHint[];
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  onAdd: (region: RegionHint) => void;
  onUpdate: (region: RegionHint) => void;
  onDelete: (id: string) => void;
};

type DragState =
  | { kind: "draw"; startX: number; startY: number; endX: number; endY: number }
  | {
      kind: "move";
      regionId: string;
      offsetX: number;
      offsetY: number;
    }
  | {
      kind: "resize";
      regionId: string;
      handle: "nw" | "ne" | "sw" | "se";
    }
  | null;

export function PdfOverlay({
  pageWidth,
  pageHeight,
  pageNumber,
  fileId,
  tool,
  drawLabel,
  regions,
  selectedId,
  onSelect,
  onAdd,
  onUpdate,
  onDelete,
}: Props) {
  const overlayRef = useRef<HTMLDivElement | null>(null);
  const [drag, setDrag] = useState<DragState>(null);

  const eventToLocalPx = useCallback(
    (e: { clientX: number; clientY: number }) => {
      const el = overlayRef.current;
      if (!el) return { x: 0, y: 0 };
      const r = el.getBoundingClientRect();
      return {
        x: clamp(e.clientX - r.left, 0, pageWidth),
        y: clamp(e.clientY - r.top, 0, pageHeight),
      };
    },
    [pageWidth, pageHeight],
  );

  const handlePointerDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      if (tool === "draw") {
        const p = eventToLocalPx(e);
        setDrag({ kind: "draw", startX: p.x, startY: p.y, endX: p.x, endY: p.y });
        (e.target as Element).setPointerCapture?.(e.pointerId);
        return;
      }
      if (tool === "select") {
        // Background click clears selection.
        onSelect(null);
      }
    },
    [tool, eventToLocalPx, onSelect],
  );

  const handlePointerMove = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      if (!drag) return;
      const p = eventToLocalPx(e);
      if (drag.kind === "draw") {
        setDrag({ ...drag, endX: p.x, endY: p.y });
        return;
      }
      if (drag.kind === "move") {
        const region = regions.find((r) => r.id === drag.regionId);
        if (!region) return;
        const newX = p.x - drag.offsetX;
        const newY = p.y - drag.offsetY;
        const wPx = region.bbox.w * pageWidth;
        const hPx = region.bbox.h * pageHeight;
        const nb = pxToNorm(
          {
            x: clamp(newX, 0, pageWidth - wPx),
            y: clamp(newY, 0, pageHeight - hPx),
            w: wPx,
            h: hPx,
          },
          pageWidth,
          pageHeight,
        );
        onUpdate({ ...region, bbox: nb });
        return;
      }
      if (drag.kind === "resize") {
        const region = regions.find((r) => r.id === drag.regionId);
        if (!region) return;
        let { x, y, w, h } = {
          x: region.bbox.x * pageWidth,
          y: region.bbox.y * pageHeight,
          w: region.bbox.w * pageWidth,
          h: region.bbox.h * pageHeight,
        };
        if (drag.handle === "nw") {
          const newW = w + (x - p.x);
          const newH = h + (y - p.y);
          x = p.x;
          y = p.y;
          w = Math.max(8, newW);
          h = Math.max(8, newH);
        } else if (drag.handle === "ne") {
          const newH = h + (y - p.y);
          y = p.y;
          w = Math.max(8, p.x - x);
          h = Math.max(8, newH);
        } else if (drag.handle === "sw") {
          const newW = w + (x - p.x);
          x = p.x;
          w = Math.max(8, newW);
          h = Math.max(8, p.y - y);
        } else if (drag.handle === "se") {
          w = Math.max(8, p.x - x);
          h = Math.max(8, p.y - y);
        }
        const nb = pxToNorm({ x, y, w, h }, pageWidth, pageHeight);
        onUpdate({ ...region, bbox: nb });
      }
    },
    [drag, eventToLocalPx, onUpdate, pageHeight, pageWidth, regions],
  );

  const handlePointerUp = useCallback(() => {
    if (!drag) return;
    if (drag.kind === "draw") {
      const nb = normaliseDragBox(
        { x: drag.startX, y: drag.startY },
        { x: drag.endX, y: drag.endY },
        pageWidth,
        pageHeight,
      );
      // Ignore tiny accidental drags.
      if (nb.w * pageWidth >= 8 && nb.h * pageHeight >= 8) {
        const id = newRegionId();
        onAdd({
          id,
          file_id: fileId,
          page_number: pageNumber,
          bbox: nb,
          label: drawLabel,
          color: colorForLabel(drawLabel),
          source: "user",
          confidence: 1.0,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        });
        onSelect(id);
      }
    }
    setDrag(null);
  }, [drag, drawLabel, fileId, onAdd, onSelect, pageHeight, pageNumber, pageWidth]);

  const handleRegionMoveStart = useCallback(
    (id: string, e: React.PointerEvent<HTMLDivElement>) => {
      if (tool === "delete") {
        onDelete(id);
        return;
      }
      if (tool !== "select") return;
      const region = regions.find((r) => r.id === id);
      if (!region) return;
      const p = eventToLocalPx(e);
      setDrag({
        kind: "move",
        regionId: id,
        offsetX: p.x - region.bbox.x * pageWidth,
        offsetY: p.y - region.bbox.y * pageHeight,
      });
      (e.target as Element).setPointerCapture?.(e.pointerId);
    },
    [tool, regions, eventToLocalPx, onDelete, pageWidth, pageHeight],
  );

  const handleRegionResizeStart = useCallback(
    (id: string, handle: "nw" | "ne" | "sw" | "se", e: React.PointerEvent<HTMLDivElement>) => {
      if (tool !== "select") return;
      setDrag({ kind: "resize", regionId: id, handle });
      (e.target as Element).setPointerCapture?.(e.pointerId);
    },
    [tool],
  );

  const cursor =
    tool === "draw"
      ? "crosshair"
      : tool === "pan"
        ? "grab"
        : tool === "delete"
          ? "not-allowed"
          : "default";

  return (
    <div
      ref={overlayRef}
      className={`pdf-overlay tool-${tool}`}
      style={{ width: pageWidth, height: pageHeight, cursor }}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerUp}
      onPointerCancel={handlePointerUp}
    >
      {regions.map((r) => (
        <RegionBox
          key={r.id}
          region={r}
          pageWidth={pageWidth}
          pageHeight={pageHeight}
          selected={selectedId === r.id}
          onSelect={onSelect}
          onDelete={onDelete}
          onMoveStart={handleRegionMoveStart}
          onResizeStart={handleRegionResizeStart}
        />
      ))}
      {drag?.kind === "draw" && (
        <DraftBox
          start={{ x: drag.startX, y: drag.startY }}
          end={{ x: drag.endX, y: drag.endY }}
          color={colorForLabel(drawLabel)}
        />
      )}
    </div>
  );
}

function DraftBox({
  start,
  end,
  color,
}: {
  start: { x: number; y: number };
  end: { x: number; y: number };
  color: string;
}) {
  const x = Math.min(start.x, end.x);
  const y = Math.min(start.y, end.y);
  const w = Math.abs(end.x - start.x);
  const h = Math.abs(end.y - start.y);
  return (
    <div
      className="region-draft"
      style={{
        left: x,
        top: y,
        width: w,
        height: h,
        borderColor: color,
        background: `${color}26`,
      }}
    />
  );
}
