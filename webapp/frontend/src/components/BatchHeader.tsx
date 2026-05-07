// Phase 1M — batch management header inside the file sidebar.
//
// Replaces the topbar batch picker. The sidebar now owns:
//   * the current batch name (renameable)
//   * a switch-batch dropdown
//   * actions: New batch · Rename · Delete
//   * the document mode tag (digital_pdf / scanned_pdf / etc.)
//
// Topbar stays clean and focused on workflow / AI / issues only.

import { useEffect, useRef, useState } from "react";

import type { BatchListEntry } from "../types";

type Props = {
  batchId: string | null;
  batchName: string;
  documentMode?: string;
  batchList: BatchListEntry[];
  onSwitch: (batchId: string) => void;
  onCreateNew: () => void;
  onRename: () => void;
  onDelete: () => void;
  onRefreshList: () => void;
  // Phase 1W — delete any batch from the picker dropdown without
  // switching to it first. Optional so test renders can omit it.
  onDeleteFromPicker?: (batchId: string) => void;
};

export function BatchHeader({
  batchId,
  batchName,
  documentMode,
  batchList,
  onSwitch,
  onCreateNew,
  onRename,
  onDelete,
  onRefreshList,
  onDeleteFromPicker,
}: Props) {
  const [pickerOpen, setPickerOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const menuWrapRef = useRef<HTMLDivElement | null>(null);

  // Click-outside closes menus.
  useEffect(() => {
    if (!pickerOpen && !menuOpen) return;
    const close = (e: MouseEvent) => {
      const target = e.target as Node;
      if (pickerOpen && !wrapRef.current?.contains(target)) {
        setPickerOpen(false);
      }
      if (menuOpen && !menuWrapRef.current?.contains(target)) {
        setMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [pickerOpen, menuOpen]);

  const activeName = batchName.trim() || (batchId ? "Untitled batch" : "No batch yet");

  return (
    <div className="batch-header" ref={wrapRef} data-testid="batch-header">
      <div className="batch-header-row">
        <button
          type="button"
          className="batch-name-button"
          data-testid="current-batch-button"
          title={batchId ? `Switch batch · ${batchName || batchId}` : "Pick or create a batch"}
          onClick={() => {
            onRefreshList();
            setPickerOpen((v) => !v);
            setMenuOpen(false);
          }}
        >
          <span className="batch-name-text">
            {activeName}
          </span>
          <span className="batch-name-caret" aria-hidden>
            ▾
          </span>
        </button>

        <div ref={menuWrapRef} className="batch-header-menu-wrap">
          <button
            type="button"
            className="icon-btn"
            data-testid="batch-actions-button"
            title="Batch actions"
            aria-label="Batch actions"
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            onClick={() => {
              setMenuOpen((v) => !v);
              setPickerOpen(false);
            }}
          >
            <DotsIcon />
          </button>
          {menuOpen && (
            <div className="batch-header-menu" role="menu">
              <button
                type="button"
                role="menuitem"
                className="batch-menu-item"
                data-testid="new-batch-menu-item"
                onClick={() => {
                  setMenuOpen(false);
                  onCreateNew();
                }}
              >
                <PlusIcon /> New batch
              </button>
              <button
                type="button"
                role="menuitem"
                className="batch-menu-item"
                data-testid="rename-batch-menu-item"
                disabled={!batchId}
                onClick={() => {
                  setMenuOpen(false);
                  onRename();
                }}
              >
                <PencilIcon /> Rename batch
              </button>
              <div className="batch-menu-sep" role="separator" />
              <button
                type="button"
                role="menuitem"
                className="batch-menu-item danger"
                data-testid="delete-batch-menu-item"
                disabled={!batchId}
                onClick={() => {
                  setMenuOpen(false);
                  onDelete();
                }}
              >
                <TrashIcon /> Delete batch
              </button>
            </div>
          )}
        </div>
      </div>

      {documentMode && (
        <div className="batch-meta">
          <span className="batch-meta-tag" title="Document mode for this batch">
            {prettyMode(documentMode)}
          </span>
        </div>
      )}

      {pickerOpen && (
        <div className="batch-picker-list" role="listbox" data-testid="batch-picker-list">
          {batchList.length === 0 && (
            <div className="batch-picker-empty">No batches yet.</div>
          )}
          {batchList.slice(0, 14).map((b) => {
            const friendly = (b.batch_name || "").trim() || "Untitled batch";
            return (
              <div
                key={b.batch_id}
                className={`batch-picker-row ${
                  b.batch_id === batchId ? "active" : ""
                }`}
                data-testid="batch-picker-row"
                data-batch-id={b.batch_id}
                title={b.batch_id}
              >
                <button
                  type="button"
                  className="batch-picker-row-main"
                  onClick={() => {
                    setPickerOpen(false);
                    onSwitch(b.batch_id);
                  }}
                >
                  <span className="batch-picker-row-name">{friendly}</span>
                  <span className="batch-picker-row-meta">
                    {b.files_count ?? 0} file
                    {(b.files_count ?? 0) === 1 ? "" : "s"}
                    {" · "}
                    {b.invoices_count ?? 0} inv
                    {b.export_available ? " · ✓" : ""}
                  </span>
                </button>
                {onDeleteFromPicker && (
                  <button
                    type="button"
                    className="batch-picker-row-delete"
                    title={`Delete "${friendly}"`}
                    aria-label={`Delete batch "${friendly}"`}
                    data-testid="batch-picker-row-delete"
                    onClick={(e) => {
                      e.stopPropagation();
                      setPickerOpen(false);
                      onDeleteFromPicker(b.batch_id);
                    }}
                  >
                    <svg
                      width="12"
                      height="12"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2.4"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      aria-hidden="true"
                    >
                      <line x1="6" y1="6" x2="18" y2="18" />
                      <line x1="18" y1="6" x2="6" y2="18" />
                    </svg>
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function prettyMode(mode: string): string {
  switch (mode) {
    case "digital_pdf":
      return "Digital PDFs";
    case "scanned_pdf":
      return "Scanned PDFs";
    case "mixed_pdf":
      return "Mixed PDFs";
    case "csv_excel":
      return "CSV / Excel";
    case "auto_detect":
      return "Auto-detect";
    default:
      return mode.replace(/_/g, " ");
  }
}

function DotsIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <circle cx="5" cy="12" r="1.6" />
      <circle cx="12" cy="12" r="1.6" />
      <circle cx="19" cy="12" r="1.6" />
    </svg>
  );
}

function PlusIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <line x1="12" y1="5" x2="12" y2="19" />
      <line x1="5" y1="12" x2="19" y2="12" />
    </svg>
  );
}

function PencilIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4z" />
    </svg>
  );
}

function TrashIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="3 6 5 6 21 6" />
      <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
      <path d="M10 11v6" />
      <path d="M14 11v6" />
    </svg>
  );
}
