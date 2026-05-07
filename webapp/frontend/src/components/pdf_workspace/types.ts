// Phase 1H — PDF workspace local types.
//
// Region types are mirrored from src/types.ts so the workspace is self
// contained. The "tool" type is workspace-internal — it tracks which
// drawing/manipulation mode the toolbar is in.

import type { RegionHint, RegionLabel } from "../../types";

export type Tool = "select" | "draw" | "pan" | "delete";

export type DraftRegion = {
  id: string;
  // Live (in-progress) bbox in normalized coords.
  bbox: { x: number; y: number; w: number; h: number };
  label: RegionLabel;
  color?: string;
};

export type WorkspaceMode = "native" | "workspace";

export type RegionWithMeta = RegionHint;

// Phase 1K — friendly display titles. The internal label stays
// snake_case (used by the backend region store and by AI policy
// allow-lists); only the rendered text changes.
export const REGION_LABEL_OPTIONS: { label: RegionLabel; title: string; color: string }[] = [
  { label: "service_address", title: "Service address", color: "#0969da" },
  { label: "account_number",  title: "Account number",  color: "#bf8700" },
  { label: "invoice_date",    title: "Invoice date",    color: "#8250df" },
  { label: "due_date",        title: "Due date",        color: "#9a6700" },
  { label: "total_amount",    title: "Total amount",    color: "#1a7f37" },
  { label: "line_items",      title: "Line items",      color: "#0a7a72" },
  { label: "notice_block",    title: "Notice block",    color: "#cf222e" },
  { label: "ignore_zone",     title: "Ignore zone",     color: "#57606a" },
  { label: "custom",          title: "Custom",          color: "#5a3ca8" },
];

export function friendlyRegionLabel(label: RegionLabel): string {
  return REGION_LABEL_OPTIONS.find((o) => o.label === label)?.title || label;
}

export function colorForLabel(label: RegionLabel): string {
  return (
    REGION_LABEL_OPTIONS.find((o) => o.label === label)?.color || "#0969da"
  );
}
