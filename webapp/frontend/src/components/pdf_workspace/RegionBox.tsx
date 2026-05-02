// Phase 1H — single region rectangle, rendered as an absolutely
// positioned div over the page canvas. Click selects; drag in select
// mode moves; corner handles resize; the X button deletes.

import type { CSSProperties } from "react";

import type { RegionHint } from "../../types";
import { colorForLabel } from "./types";

type Props = {
  region: RegionHint;
  pageWidth: number;
  pageHeight: number;
  selected: boolean;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onMoveStart?: (
    id: string,
    e: React.PointerEvent<HTMLDivElement>,
  ) => void;
  onResizeStart?: (
    id: string,
    handle: "nw" | "ne" | "sw" | "se",
    e: React.PointerEvent<HTMLDivElement>,
  ) => void;
};

export function RegionBox({
  region,
  pageWidth,
  pageHeight,
  selected,
  onSelect,
  onDelete,
  onMoveStart,
  onResizeStart,
}: Props) {
  const px = {
    x: region.bbox.x * pageWidth,
    y: region.bbox.y * pageHeight,
    w: region.bbox.w * pageWidth,
    h: region.bbox.h * pageHeight,
  };
  const colour = region.color || colorForLabel(region.label);
  const style: CSSProperties = {
    left: px.x,
    top: px.y,
    width: px.w,
    height: px.h,
    borderColor: colour,
    background: `${colour}1f`,
  };

  return (
    <div
      className={`region-box ${selected ? "selected" : ""}`}
      style={style}
      onPointerDown={(e) => {
        e.stopPropagation();
        onSelect(region.id);
        onMoveStart?.(region.id, e);
      }}
      role="button"
      aria-label={`${region.label} region`}
    >
      <span
        className="region-tag"
        style={{ background: colour }}
        title={region.label}
      >
        {region.label.replace(/_/g, " ")}
      </span>
      {selected && (
        <>
          <button
            type="button"
            className="region-delete"
            onPointerDown={(e) => {
              e.stopPropagation();
              onDelete(region.id);
            }}
            title="Delete region"
          >
            ×
          </button>
          {(["nw", "ne", "sw", "se"] as const).map((h) => (
            <div
              key={h}
              className={`region-handle handle-${h}`}
              style={{ borderColor: colour }}
              onPointerDown={(e) => {
                e.stopPropagation();
                onResizeStart?.(region.id, h, e);
              }}
            />
          ))}
        </>
      )}
    </div>
  );
}
