// Phase 2J — Extraction Trace Overlay.
//
// Sibling to PdfOverlay. Renders semi-transparent boxes over the rendered
// PDF page corresponding to the regions the backend used to extract each
// field. Click → links the trace to a template row (parent decides what
// "linked" means). Hover → tooltip with field metadata.

import { useMemo, useRef, useState } from "react";
import type { TraceItem } from "../../types";

type Props = {
  pageNumber: number;
  pageWidth: number;
  pageHeight: number;
  items: TraceItem[];                        // already filtered to this page
  highlightedTraceIds?: ReadonlySet<string>; // ids we should pulse
  hoveredTraceId?: string | null;            // single id from external hover
  onTraceHover?: (id: string | null) => void;
  onTraceClick?: (id: string) => void;
  enabled: boolean;
};

// Stable color assignment per field_key. Six soft, modern palette
// entries — cycled by hash of the key so the same field always gets
// the same colour even across pages and reloads.
const FIELD_COLORS = [
  { fill: "rgba(37, 99, 235, 0.15)", stroke: "rgba(37, 99, 235, 0.7)" },     // blue
  { fill: "rgba(16, 185, 129, 0.15)", stroke: "rgba(16, 185, 129, 0.7)" },   // emerald
  { fill: "rgba(245, 158, 11, 0.16)", stroke: "rgba(245, 158, 11, 0.75)" },  // amber
  { fill: "rgba(168, 85, 247, 0.15)", stroke: "rgba(168, 85, 247, 0.7)" },   // violet
  { fill: "rgba(236, 72, 153, 0.15)", stroke: "rgba(236, 72, 153, 0.7)" },   // pink
  { fill: "rgba(14, 165, 233, 0.15)", stroke: "rgba(14, 165, 233, 0.7)" },   // sky
];

function colorFor(fieldKey: string) {
  let h = 0;
  for (let i = 0; i < fieldKey.length; i++) {
    h = (h * 31 + fieldKey.charCodeAt(i)) | 0;
  }
  return FIELD_COLORS[Math.abs(h) % FIELD_COLORS.length];
}

export function TraceOverlay({
  pageNumber,
  pageWidth,
  pageHeight,
  items,
  highlightedTraceIds,
  hoveredTraceId,
  onTraceHover,
  onTraceClick,
  enabled,
}: Props) {
  const [localHover, setLocalHover] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);

  // Sort large boxes first so smaller boxes draw on top and remain
  // clickable (z-order). Without this a big "service line" box can
  // swallow the small "amount" box that sits inside it.
  const sortedItems = useMemo(() => {
    return [...items].sort((a, b) => {
      const aA = (a.bbox.w || 0) * (a.bbox.h || 0);
      const bA = (b.bbox.w || 0) * (b.bbox.h || 0);
      return bA - aA;
    });
  }, [items]);

  if (!enabled || items.length === 0) return null;

  return (
    <div
      ref={containerRef}
      className="trace-overlay"
      style={{ width: pageWidth, height: pageHeight }}
      data-testid="trace-overlay"
      data-page-number={pageNumber}
      aria-hidden={false}
    >
      {sortedItems.map((it) => {
        const isHovered =
          (hoveredTraceId && hoveredTraceId === it.trace_id) ||
          localHover === it.trace_id;
        const isLinked = highlightedTraceIds?.has(it.trace_id) ?? false;
        const c = colorFor(it.field_key || it.field_label);
        const left = it.bbox.x * pageWidth;
        const top = it.bbox.y * pageHeight;
        const width = it.bbox.w * pageWidth;
        const height = it.bbox.h * pageHeight;
        return (
          <button
            type="button"
            key={it.trace_id}
            className={[
              "trace-box",
              isHovered ? "is-hover" : "",
              isLinked ? "is-linked" : "",
            ]
              .filter(Boolean)
              .join(" ")}
            style={{
              left,
              top,
              width: Math.max(2, width),
              height: Math.max(2, height),
              // Soft fill + stroked border per field colour.
              background: c.fill,
              borderColor: c.stroke,
              boxShadow: isLinked
                ? `0 0 0 2px ${c.stroke}, 0 0 14px ${c.stroke}`
                : isHovered
                ? `0 0 0 1px ${c.stroke}, 0 0 8px rgba(0,0,0,0.10)`
                : "none",
            }}
            data-testid="trace-box"
            data-trace-id={it.trace_id}
            data-field-key={it.field_key}
            onMouseEnter={() => {
              setLocalHover(it.trace_id);
              onTraceHover?.(it.trace_id);
            }}
            onMouseLeave={() => {
              setLocalHover((cur) => (cur === it.trace_id ? null : cur));
              onTraceHover?.(null);
            }}
            onClick={(e) => {
              e.stopPropagation();
              onTraceClick?.(it.trace_id);
            }}
            aria-label={`Trace: ${it.field_label}`}
          >
            {/* Field label badge — only visible on hover/link to keep
                the page calm at rest. */}
            {(isHovered || isLinked) && (
              <span
                className="trace-box-label"
                style={{ background: c.stroke }}
              >
                {it.field_label}
              </span>
            )}
            {isHovered && (
              <TraceTooltip item={it} />
            )}
          </button>
        );
      })}
    </div>
  );
}

function TraceTooltip({ item }: { item: TraceItem }) {
  const conf = Math.round((item.confidence || 0) * 100);
  return (
    <div className="trace-tooltip" role="tooltip">
      <div className="trace-tooltip-title">{item.field_label}</div>
      {item.detected_text && (
        <div className="trace-tooltip-text" title={item.detected_text}>
          “{truncate(item.detected_text, 120)}”
        </div>
      )}
      <dl className="trace-tooltip-meta">
        {item.rule_id && (
          <>
            <dt>Rule</dt>
            <dd>{item.rule_id}</dd>
          </>
        )}
        {item.match_strategy && (
          <>
            <dt>Strategy</dt>
            <dd>{item.match_strategy}</dd>
          </>
        )}
        <dt>Confidence</dt>
        <dd>{conf}%</dd>
        {item.feeds_columns?.length > 0 && (
          <>
            <dt>Feeds</dt>
            <dd>{item.feeds_columns.join(", ")}</dd>
          </>
        )}
      </dl>
    </div>
  );
}

function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}
