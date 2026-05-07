// Phase 1L — Issues drawer.
//
// Replaces the always-visible right-side inspector panel from Phase 1J.
// The drawer slides in from the right when the user clicks the "Issues"
// pill in the topbar. Closing happens via:
//   * the X button in the drawer header
//   * the Escape key
//   * clicking the dimmed backdrop
//
// The drawer reuses ReviewInspectorPanel for its body so the actual
// issue card layout / "Mark reviewed" behaviour stays identical to
// Phase 1K. Only the *containment* changes: drawer instead of fixed
// column.

import { useEffect, useRef } from "react";

import { ReviewInspectorPanel } from "./ReviewInspectorPanel";
import type { ManualReviewItem, PreviewRow } from "../types";

type Props = {
  open: boolean;
  onClose: () => void;
  items: ManualReviewItem[];
  rows: PreviewRow[];
  selectedRowIndex: number | null;
  onSelectRow: (rowIndex: number) => void;
  onSelectFile: (filename: string) => void;
  activeTab: "issues" | "row";
  onTabChange: (tab: "issues" | "row") => void;
  reviewedKeys?: Set<string>;
  onToggleReviewed?: (key: string) => void;
};

export function IssuesDrawer({
  open,
  onClose,
  items,
  rows,
  selectedRowIndex,
  onSelectRow,
  onSelectFile,
  activeTab,
  onTabChange,
  reviewedKeys,
  onToggleReviewed,
}: Props) {
  const drawerRef = useRef<HTMLDivElement | null>(null);

  // Escape closes the drawer.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="drawer-backdrop"
      data-testid="issues-drawer-backdrop"
      onClick={(e) => {
        // Click on the dim background closes; clicks inside the drawer
        // body (handled by drawerRef) are ignored.
        if (!drawerRef.current?.contains(e.target as Node)) onClose();
      }}
      role="presentation"
    >
      <aside
        ref={drawerRef}
        className="drawer drawer-right"
        data-testid="issues-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="issues-drawer-title"
      >
        <header className="drawer-header">
          <span id="issues-drawer-title" className="drawer-title">
            Issues
            {items.length > 0 && (
              <span className="drawer-title-count">{items.length}</span>
            )}
          </span>
          <button
            type="button"
            className="icon-btn"
            data-testid="issues-drawer-close"
            onClick={onClose}
            aria-label="Close issues panel"
            title="Close"
          >
            <CloseIcon />
          </button>
        </header>
        <div className="drawer-body">
          <ReviewInspectorPanel
            items={items}
            rows={rows}
            selectedRowIndex={selectedRowIndex}
            onSelectRow={onSelectRow}
            onSelectFile={onSelectFile}
            activeTab={activeTab}
            onTabChange={onTabChange}
            reviewedKeys={reviewedKeys}
            onToggleReviewed={onToggleReviewed}
          />
        </div>
      </aside>
    </div>
  );
}

function CloseIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <line x1="6" y1="6" x2="18" y2="18" />
      <line x1="18" y1="6" x2="6" y2="18" />
    </svg>
  );
}
