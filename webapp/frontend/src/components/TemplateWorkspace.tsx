// Phase 1J — premium template workspace.
//
// Wraps ResManTemplatePreview with:
//   * Summary bar (files / invoices / rows / flagged / edited / total)
//   * View presets (Required / Review / Full)
//   * Search box (invoice / vendor / property / location / address)
//   * Row filters (all / needs review / edited / missing property /
//                  missing location / amount mismatch / missing url)
//   * Sticky-row selection that drives the inspector panel
//
// The actual editable grid is still rendered by `ResManTemplatePreview`
// — this component just curates the rows and columns it sees.

import { useEffect, useMemo, useRef, useState } from "react";

import type { BatchProgress, PreviewResponse, PreviewRow } from "../types";
import { ColumnFilterMenu } from "./ColumnFilterMenu";
import { GroupedTotalsTable } from "./GroupedTotalsTable";
import { ProcessingTimeline } from "./ProcessingTimeline";
import {
  ResManTemplatePreview,
  type CellEdits,
} from "./ResManTemplatePreview";
import { TemplateLoadingState } from "./TemplateLoadingState";

type ViewPreset = "required" | "review" | "full";
type RowFilter =
  | "all"
  | "needs_review"
  | "edited"
  | "missing_property"
  | "missing_location"
  | "amount_mismatch"
  | "missing_url";

type Props = {
  preview: PreviewResponse | null;
  edits: CellEdits;
  onCellEdit: (rowIndex: number, columnKey: string, newValue: unknown) => void;
  fileCount: number;
  selectedRowIndex: number | null;
  activeDocumentPage?: {
    batchId: string;
    filename: string;
    pageNumber: number;
  } | null;
  // Phase 2C — total page count for the active document so the
  // breadcrumb can show "Page 1 of 14" instead of just "Page 1".
  activeDocumentPageCount?: number | null;
  onSelectRow: (rowIndex: number | null) => void;
  // Phase 1P — Export lives in the template header now (was the
  // sidebar's actions bar). Optional so test renders can omit it.
  onExport?: () => void;
  isExporting?: boolean;
  hasExport?: boolean;
  // Phase 1U — when a batch switch is in flight, show a panel-local
  // skeleton overlay instead of the previous full-screen blur.
  isSwitchingBatch?: boolean;
  loadingBatchName?: string | null;
  // Phase 1V — processing state lives here (was in the sidebar).
  isProcessing?: boolean;
  isCancelling?: boolean;
  progress?: BatchProgress | null;
  onCancel?: () => void;
  // Phase 2C — breadcrumb / context.
  batchName?: string | null;
  vendorLabel?: string | null;
  exportName?: string | null;
  defaultExportName?: string | null;
  onRenameExport?: (newName: string) => Promise<void> | void;
  // Phase 2C — focus mode + popout.
  focusMode?: boolean;
  onToggleFocusMode?: () => void;
  onPopoutTemplate?: () => void;
  onPopoutDocument?: () => void;
  // The Template module is the only panel that keeps controls — and
  // it has only TWO: detach (popout to a separate window) and reattach
  // (close the popout and embed the panel back). When detached, the
  // Document panel expands to take the freed horizontal space.
  isDetached?: boolean;
  onDetach?: () => void;
  onReattach?: () => void;
  // Revisions API.
  revisions?: import("../types").RevisionEntry[];
  currentRevisionId?: string | null;
  onActivateRevision?: (revisionId: string) => Promise<void> | void;
  onDeleteRevision?: (revisionId: string) => Promise<void> | void;
  onSaveEdits?: () => Promise<void> | void;
  isSavingEdits?: boolean;
  // Phase 2K — cell selection + context-menu hooks.
  selectedColumnKey?: string | null;
  onSelectCell?: (rowIndex: number | null, column: string | null) => void;
  onCellContextMenu?: (params: {
    rowIndex: number;
    column: string;
    x: number;
    y: number;
  }) => void;
  // When rendered inside a popout window, the host injects readOnly
  // and hides edit-only affordances (export, rename).
  readOnly?: boolean;
};

const FILTERS: { key: RowFilter; label: string }[] = [
  { key: "all", label: "All rows" },
  { key: "needs_review", label: "Needs review" },
  { key: "edited", label: "Edited" },
  { key: "missing_property", label: "Missing property" },
  { key: "missing_location", label: "Missing location" },
  { key: "amount_mismatch", label: "Amount mismatch" },
  { key: "missing_url", label: "Missing link" },
];

export function TemplateWorkspace({
  preview,
  edits,
  onCellEdit,
  fileCount,
  selectedRowIndex,
  activeDocumentPage,
  activeDocumentPageCount,
  onSelectRow,
  onExport,
  isExporting,
  hasExport,
  isSwitchingBatch,
  loadingBatchName,
  isProcessing,
  isCancelling,
  progress,
  onCancel,
  batchName,
  vendorLabel,
  exportName,
  defaultExportName,
  onRenameExport,
  focusMode,
  onToggleFocusMode,
  onPopoutTemplate,
  onPopoutDocument,
  isDetached,
  onDetach,
  onReattach,
  revisions,
  currentRevisionId,
  onActivateRevision,
  onDeleteRevision,
  onSaveEdits,
  isSavingEdits,
  selectedColumnKey,
  onSelectCell,
  onCellContextMenu,
  readOnly,
}: Props) {
  void onPopoutDocument;
  void onToggleFocusMode;
  void focusMode;
  const [view, setView] = useState<ViewPreset>("required");
  const [filter, setFilter] = useState<RowFilter>("all");
  const [search, setSearch] = useState("");
  // Phase 2M — Excel-style per-column filters and group-by aggregation.
  // ``columnFilters`` is keyed by column name; the value is the list
  // of allowed cell values for that column (string-coerced). Absent
  // key = no filter on that column.
  const [columnFilters, setColumnFilters] = useState<Record<string, string[]>>({});
  const [filterMenu, setFilterMenu] = useState<
    { column: string; anchor: DOMRect } | null
  >(null);
  const [groupBy, setGroupBy] = useState<string>("");
  // Phase 2D — zebra rows toggle, persisted as a UI preference.
  const [zebra, setZebra] = useState<boolean>(() => {
    if (typeof window === "undefined") return false;
    try {
      return window.localStorage.getItem("billing_template_zebra") === "1";
    } catch {
      return false;
    }
  });
  useEffect(() => {
    try {
      window.localStorage.setItem(
        "billing_template_zebra",
        zebra ? "1" : "0",
      );
    } catch {
      /* ignore quota / disabled storage */
    }
  }, [zebra]);

  // Reset filter to "all" whenever a new preview lands so a stale
  // filter doesn't hide every row.
  useEffect(() => {
    setFilter("all");
    setSearch("");
    setColumnFilters({});
    setGroupBy("");
    setFilterMenu(null);
  }, [preview?.row_count]);

  const summary = useMemo(() => {
    if (!preview) {
      return {
        files: fileCount,
        invoices: 0,
        rows: 0,
        flagged: 0,
        edited: 0,
        total: 0,
        urlsMissing: 0,
      };
    }
    const rows = preview.rows;
    const total = rows.reduce((acc, r) => {
      const a = (r as any).Amount;
      const n = typeof a === "number" ? a : Number(a);
      return acc + (Number.isFinite(n) ? n : 0);
    }, 0);
    const flagged = rows.filter(
      (r) => (r._meta?.manual_review_reasons ?? []).length > 0,
    ).length;
    const editedCells = Object.values(edits).reduce(
      (s, m) => s + Object.keys(m).length,
      0,
    );
    const urlsMissing = rows.filter(
      (r) => !r["Document Url"] || r["Document Url"] === "",
    ).length;
    return {
      files: fileCount,
      invoices: preview.invoice_count,
      rows: preview.row_count,
      flagged,
      edited: editedCells,
      total,
      urlsMissing,
    };
  }, [preview, fileCount, edits]);

  // Curate columns based on view preset.
  const curatedPreview = useMemo<PreviewResponse | null>(() => {
    if (!preview) return null;
    if (view === "full") return preview;
    if (view === "required") {
      // "Required" means required + recommended only.
      const keep = new Set([
        ...preview.required_columns,
        ...preview.recommended_columns,
      ]);
      const cols = preview.columns.filter((c) => keep.has(c));
      return { ...preview, columns: cols };
    }
    // "review" = required + recommended + Document Url + Reference Number.
    const keep = new Set([
      ...preview.required_columns,
      ...preview.recommended_columns,
      "Document Url",
      "Reference Number",
      "Invoice Description",
    ]);
    const cols = preview.columns.filter((c) => keep.has(c));
    return { ...preview, columns: cols };
  }, [preview, view]);

  // Pre-build per-column allow sets for fast lookup inside the row loop.
  const columnFilterSets = useMemo(() => {
    const out: Record<string, Set<string>> = {};
    for (const [k, v] of Object.entries(columnFilters)) {
      out[k] = new Set(v);
    }
    return out;
  }, [columnFilters]);

  // Apply filter + search + per-column filters to row visibility.
  const visibleIndexes = useMemo<Set<number> | null>(() => {
    if (!preview) return null;
    const q = search.trim().toLowerCase();
    const colFilterEntries = Object.entries(columnFilterSets);
    const indexes = new Set<number>();
    preview.rows.forEach((row, i) => {
      const reasons = row._meta?.manual_review_reasons ?? [];
      // Filter
      let pass = true;
      switch (filter) {
        case "all":
          break;
        case "needs_review":
          pass = reasons.length > 0;
          break;
        case "edited":
          pass = !!edits[i] && Object.keys(edits[i]).length > 0;
          break;
        case "missing_property":
          pass = !row["Property Abbreviation"];
          break;
        case "missing_location":
          pass = !row["Location"];
          break;
        case "amount_mismatch":
          pass = reasons.some((r) =>
            /amount_total_mismatch|extracted_total_mismatch|bill_total_does_not_match/.test(
              r,
            ),
          );
          break;
        case "missing_url":
          pass = !row["Document Url"];
          break;
      }
      if (!pass) return;
      // Search
      if (q) {
        const haystack = [
          row["Invoice Number"],
          row["Vendor"],
          row["Property Abbreviation"],
          row["Location"],
          (row as any).service_address,
          row["Invoice Description"],
        ]
          .filter(Boolean)
          .map((v) => String(v).toLowerCase())
          .join(" ");
        if (!haystack.includes(q)) return;
      }
      // Per-column filters (Excel-style autofilter).
      if (colFilterEntries.length > 0) {
        let allMatch = true;
        for (const [col, allow] of colFilterEntries) {
          const cell = (row as Record<string, unknown>)[col];
          const key = cell == null || cell === "" ? "" : String(cell);
          if (!allow.has(key)) {
            allMatch = false;
            break;
          }
        }
        if (!allMatch) return;
      }
      indexes.add(i);
    });
    return indexes;
  }, [preview, edits, filter, search, columnFilterSets]);

  // Distinct values for the column the filter menu is currently
  // pointing at. We only compute it for the open menu so picking a
  // column with thousands of unique values doesn't penalize the
  // whole grid render.
  const filterMenuValues = useMemo<string[]>(() => {
    if (!filterMenu || !preview) return [];
    const seen = new Set<string>();
    const out: string[] = [];
    for (const row of preview.rows) {
      const v = (row as Record<string, unknown>)[filterMenu.column];
      const s = v == null || v === "" ? "" : String(v);
      if (!seen.has(s)) {
        seen.add(s);
        out.push(s);
      }
    }
    out.sort((a, b) => {
      if (a === "") return 1;
      if (b === "") return -1;
      return a.localeCompare(b, undefined, { numeric: true });
    });
    return out;
  }, [filterMenu, preview]);

  const filteredColumnsSet = useMemo(
    () => new Set(Object.keys(columnFilters)),
    [columnFilters],
  );

  const editedCount = summary.edited;
  const canExport = !!onExport && !!preview && summary.rows > 0;
  // Phase 2C — titleMeta retired in favour of the breadcrumb header.

  return (
    <div
      className={`template-workspace ${isSwitchingBatch ? "is-switching" : ""} ${
        zebra ? "is-zebra" : ""
      }`}
      data-testid="template-workspace"
    >
      {/* Phase 2F — desktop-style window chrome.
          The export filename now lives HERE (next to the panel label)
          so a single bar identifies the panel + its primary artefact.
          Revisions dropdown sits in the chrome too — it is metadata
          about the file/window, not about the data inside it. The body
          below stays focused on the table. */}
      {(onDetach || onReattach || !readOnly) && (
        <div className="template-window-chrome" data-testid="template-window-chrome">
          <span className="template-window-label">Template</span>
          <span className="template-window-sep" aria-hidden>·</span>
          <span className="template-window-filename" title="Click to rename the export workbook">
            <ExportNameField
              value={exportName || ""}
              placeholderText={defaultExportName || "Untitled export"}
              placeholder={defaultExportName || "Untitled export"}
              onCommit={onRenameExport}
              disabled={!onRenameExport || readOnly === true}
            />
            {!readOnly && (
              <RevisionsDropdown
                revisions={revisions || []}
                currentRevisionId={currentRevisionId || null}
                onActivate={onActivateRevision}
                onDelete={onDeleteRevision}
              />
            )}
          </span>
          <div className="template-window-actions">
            <div className="panel-window-controls">
              {/* Two-state control: Detach (popout to a separate window)
                  when embedded; Reattach (close the popout, embed back)
                  when detached. The Document panel auto-expands to fill
                  the freed width while detached. */}
              {!isDetached && onDetach && (
                <button
                  type="button"
                  className="panel-window-btn panel-window-btn-popout"
                  onClick={onDetach}
                  title="Detach to separate window"
                  aria-label="Detach Template to separate window"
                  data-testid="template-detach"
                >
                  <svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
                    <rect x="2" y="3" width="6" height="6" rx="0.8" />
                    <path d="M4 1.8h6.2v6.2" />
                  </svg>
                </button>
              )}
              {isDetached && onReattach && (
                <button
                  type="button"
                  className="panel-window-btn panel-window-btn-popout is-active"
                  onClick={onReattach}
                  title="Reattach to main window"
                  aria-label="Reattach Template to main window"
                  data-testid="template-reattach"
                >
                  <svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
                    <rect x="3" y="3" width="6" height="6" rx="0.8" />
                    <path d="M10.2 1.8 L7 5" />
                    <path d="M10.2 1.8 H7.5" />
                    <path d="M10.2 1.8 V4.5" />
                  </svg>
                </button>
              )}
            </div>
          </div>
        </div>
      )}
      {isSwitchingBatch && (
        <div className="template-switch-pulse" aria-hidden>
          <div className="template-switch-pulse-bar" />
          <div className="template-switch-pulse-label">
            Loading {loadingBatchName ? loadingBatchName : "batch"}…
          </div>
        </div>
      )}
      {/* Phase 2G — inline Template loading state.
          Replaces the floating ProcessingPanel / "Building ResMan
          template" card. While a run is in flight we render the
          illustration in place of the controls + table below; the
          window chrome stays visible. */}
      {/* Phase 2F — body header retired.
          Filename + revisions moved to the window chrome. The breadcrumb,
          context line, KPI strip, "Total" amount, and the per-doc /
          per-page hint are gone — a template can be assembled from
          many files, so a single-doc context line was misleading. The
          Issues count remains visible via the topbar Issues pill (the
          single canonical place for it). */}

      {isProcessing ? (
        <TemplateLoadingState
          progress={progress}
          isCancelling={isCancelling}
          onCancel={onCancel}
        />
      ) : (
      <>
      <div className="template-controls" data-testid="template-controls">
        <div
          className="view-presets"
          role="tablist"
          aria-label="Visible columns"
          data-testid="column-view-tabs"
        >
          <span className="template-controls-label" aria-hidden>
            Columns:
          </span>
          <button
            role="tab"
            aria-selected={view === "required"}
            className={`view-preset-btn ${view === "required" ? "active" : ""}`}
            onClick={() => setView("required")}
            title="Required and recommended columns only — the core fields needed for ResMan import."
          >
            Required
          </button>
          {/* Phase 2F — "Issues" column preset removed. Issues are
              already visible in the table via row tinting + the
              Issues pill in the topbar; a column preset just for
              issue triage was redundant. */}
          <button
            role="tab"
            aria-selected={view === "full"}
            className={`view-preset-btn ${view === "full" ? "active" : ""}`}
            onClick={() => setView("full")}
            title="Every column from the official ResMan Template.xlsx."
          >
            All
          </button>
        </div>

        <div className="template-filter-tools">
          <input
            type="search"
            className="template-search"
            placeholder="Search invoice / property / location..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            aria-label="Search rows"
            data-testid="template-search"
          />

          <select
            className="template-filter-select"
            value={filter}
            onChange={(e) => setFilter(e.target.value as RowFilter)}
            aria-label="Row filter"
            data-testid="template-row-filter"
          >
            {FILTERS.map((f) => (
              <option key={f.key} value={f.key}>
                {f.label}
              </option>
            ))}
          </select>

          {/* Phase 2M — Group-by selector. Picking a column collapses
              the grid into one row per distinct value with the SUM of
              Amount per group plus a row-count. */}
          {curatedPreview && curatedPreview.columns.length > 0 && (
            <select
              className="template-filter-select"
              value={groupBy}
              onChange={(e) => setGroupBy(e.target.value)}
              aria-label="Group rows by column"
              title="Group rows by a column and show totals per group"
              data-testid="template-group-by"
            >
              <option value="">Group by: none</option>
              {curatedPreview.columns
                .filter((c) => c !== "Amount")
                .map((c) => (
                  <option key={c} value={c}>
                    Group by: {c}
                  </option>
                ))}
            </select>
          )}
          {Object.keys(columnFilters).length > 0 && (
            <button
              type="button"
              className="btn btn-compact"
              onClick={() => setColumnFilters({})}
              title="Remove every per-column filter"
              data-testid="template-clear-column-filters"
            >
              Clear filters ({Object.keys(columnFilters).length})
            </button>
          )}
          {/* Phase 2D — Zebra rows toggle. Persisted in localStorage. */}
          <button
            type="button"
            className={`template-zebra-toggle ${zebra ? "is-on" : ""}`}
            onClick={() => setZebra((z) => !z)}
            aria-pressed={zebra}
            title={zebra ? "Turn off zebra rows" : "Turn on zebra rows"}
            data-testid="template-zebra-toggle"
          >
            Zebra {zebra ? "on" : "off"}
          </button>
          {/* Phase 2I.13 — Save edits to the active revision. Only
              visible while local cell edits exist; on click we POST
              them to /save-edits which mirrors the changes into both
              the active cache and the current revision's snapshot, so
              switching revisions and coming back keeps them. */}
          {!readOnly && onSaveEdits && editedCount > 0 && (
            <button
              type="button"
              className="btn btn-compact template-save-btn"
              disabled={isSavingEdits}
              onClick={() => {
                void onSaveEdits();
              }}
              data-testid="template-save-button"
              title={`Save ${editedCount} edit${editedCount === 1 ? "" : "s"} to this revision.`}
            >
              {isSavingEdits ? "Saving…" : `Save (${editedCount})`}
            </button>
          )}
          {/* Phase 2F — Export sits at the right edge of the controls
              strip so the toolbar reads "view -> filter -> action". */}
          {!readOnly && onExport && (
            <button
              type="button"
              className={`btn btn-compact template-export-btn ${
                canExport ? "btn-accent" : ""
              }`}
              disabled={!canExport || isExporting}
              onClick={onExport}
              data-testid="template-export-button"
              title={
                summary.flagged > 0
                  ? "Export with current edits — flagged rows will be included as-is."
                  : "Build the ResMan workbook from the current preview and download."
              }
            >
              {isExporting ? (
                "Exporting…"
              ) : (
                <>
                  <ExportIcon />
                  {editedCount > 0 ? `Export (${editedCount})` : "Export"}
                </>
              )}
            </button>
          )}
        </div>
      </div>

      {groupBy && curatedPreview ? (
        <GroupedTotalsTable
          preview={curatedPreview}
          visibleRowIndexes={visibleIndexes}
          groupBy={groupBy}
          onSelectGroupRow={onSelectRow}
          selectedRowIndex={selectedRowIndex}
        />
      ) : (
        <ResManTemplatePreview
          preview={curatedPreview}
          edits={edits}
          onCellEdit={onCellEdit}
          visibleRowIndexes={visibleIndexes}
          selectedRowIndex={selectedRowIndex}
          activeDocumentRef={
            activeDocumentPage
              ? {
                  filename: activeDocumentPage.filename,
                  pageNumber: activeDocumentPage.pageNumber,
                }
              : null
          }
          onSelectRow={onSelectRow}
          forceShowOptional={view === "full"}
          selectedColumnKey={selectedColumnKey}
          onSelectCell={onSelectCell}
          onCellContextMenu={onCellContextMenu}
          onColumnFilterClick={(col, anchor) => setFilterMenu({ column: col, anchor })}
          filteredColumns={filteredColumnsSet}
        />
      )}
      {filterMenu && (
        <ColumnFilterMenu
          column={filterMenu.column}
          anchorRect={filterMenu.anchor}
          allValues={filterMenuValues}
          selected={columnFilters[filterMenu.column] ?? null}
          onApply={(next) => {
            setColumnFilters((prev) => {
              const out = { ...prev };
              if (next == null) delete out[filterMenu.column];
              else out[filterMenu.column] = next;
              return out;
            });
          }}
          onClose={() => setFilterMenu(null)}
        />
      )}
      </>
      )}
    </div>
  );
}

function ExportIcon() {
  return (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <line x1="12" y1="5" x2="12" y2="19" />
      <polyline points="19 12 12 19 5 12" />
    </svg>
  );
}

// Phase 2C — header icons. Using stroke-based SVGs to match the rest of
// the icon set.
function ExpandIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="15 3 21 3 21 9" />
      <polyline points="9 21 3 21 3 15" />
      <line x1="21" y1="3" x2="14" y2="10" />
      <line x1="3" y1="21" x2="10" y2="14" />
    </svg>
  );
}
function ContractIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="4 14 10 14 10 20" />
      <polyline points="20 10 14 10 14 4" />
      <line x1="14" y1="10" x2="21" y2="3" />
      <line x1="3" y1="21" x2="10" y2="14" />
    </svg>
  );
}
function PopoutIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
      <polyline points="15 3 21 3 21 9" />
      <line x1="10" y1="14" x2="21" y2="3" />
    </svg>
  );
}
function PopoutTemplateIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3" y="4" width="13" height="13" rx="1.5" />
      <polyline points="15 3 21 3 21 9" />
      <line x1="10" y1="14" x2="21" y2="3" />
    </svg>
  );
}
function DocumentMiniIcon({ className }: { className?: string }) {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className={className}>
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <polyline points="14 2 14 8 20 8" />
    </svg>
  );
}
function PencilIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z" />
    </svg>
  );
}

// Editable export-name field. Click anywhere on the title to edit;
// Enter saves, Esc cancels, blur saves. Disabled in read-only popouts.
function ExportNameField({
  value,
  placeholder,
  placeholderText,
  onCommit,
  disabled,
}: {
  value: string;
  placeholder?: string;
  // Phase 2C.1 — text shown in display mode when no value has been
  // saved yet. Distinct from `placeholder` (which is the input's
  // attribute when editing); both default to the same string.
  placeholderText?: string;
  onCommit?: ((v: string) => Promise<void> | void) | null;
  disabled?: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const inputRef = useRef<HTMLInputElement | null>(null);

  // Re-sync draft when the canonical value changes outside this widget.
  useEffect(() => {
    setDraft(value);
  }, [value]);

  useEffect(() => {
    if (editing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [editing]);

  const commit = async () => {
    const trimmed = draft.trim();
    setEditing(false);
    if (!onCommit) return;
    if (!trimmed || trimmed === value) {
      setDraft(value);
      return;
    }
    try {
      await onCommit(trimmed);
    } catch {
      // Parent surfaces the error; just snap back to the canonical value.
      setDraft(value);
    }
  };

  const displayText = value || placeholderText || "Untitled export";
  const isPlaceholder = !value;

  if (disabled) {
    return (
      <span className="template-title-export">
        <span
          className="template-title-export-name"
          style={{ cursor: "default" }}
        >
          {displayText}
        </span>
      </span>
    );
  }
  if (editing) {
    return (
      <span className="template-title-export">
        <input
          ref={inputRef}
          type="text"
          className="template-title-export-input"
          value={draft}
          maxLength={120}
          placeholder={placeholder}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => void commit()}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              void commit();
            } else if (e.key === "Escape") {
              e.preventDefault();
              setDraft(value);
              setEditing(false);
            }
          }}
          data-testid="export-name-input"
        />
      </span>
    );
  }
  return (
    <span className="template-title-export">
      <span
        className="template-title-export-name"
        title={isPlaceholder ? "Click to rename this export" : "Click to rename the export workbook"}
        onClick={() => setEditing(true)}
        role="button"
        tabIndex={0}
        // Phase 2D — drop the italic muted treatment. The displayed
        // value is now always the *real* default export filename
        // (computed by the backend), so it deserves a strong, clean
        // semibold style consistent with the rest of the title.
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setEditing(true);
          }
        }}
        data-testid="export-name-display"
      >
        {displayText}
      </span>
      <button
        type="button"
        className="template-title-export-pencil"
        onClick={() => setEditing(true)}
        title="Rename export"
        aria-label="Rename export"
      >
        <PencilIcon />
      </button>
    </span>
  );
}

// =============================================================================
// Phase 2C.1 — Reduced KPI cluster.
// Issues pill (only loud when issues > 0), Total amount, and a "More"
// popover that exposes Files / Invoices / Rows / Edited / Missing link
// without crowding the header.
// =============================================================================
type KpiSummary = {
  files: number;
  invoices: number;
  rows: number;
  flagged: number;
  edited: number;
  total: number;
  urlsMissing: number;
};
function KpiCluster({ summary }: { summary: KpiSummary }) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);

  // Click-outside + Escape to dismiss.
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      const node = wrapRef.current;
      if (!node) return;
      if (e.target instanceof Node && !node.contains(e.target)) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const hasIssues = summary.flagged > 0;
  const totalText = `$${summary.total.toFixed(2)}`;

  return (
    <div className="template-kpi-cluster" ref={wrapRef}>
      <span
        className={`template-kpi-issues ${hasIssues ? "is-flagged" : "is-clean"}`}
        title={
          hasIssues
            ? `${summary.flagged} row${summary.flagged === 1 ? "" : "s"} need review`
            : "No issues"
        }
        data-testid="template-kpi-issues"
      >
        <span className="template-kpi-issues-icon" aria-hidden>
          {hasIssues ? <AlertCircleIcon /> : <CheckCircleIcon />}
        </span>
        {hasIssues ? `${summary.flagged} issue${summary.flagged === 1 ? "" : "s"}` : "No issues"}
      </span>

      <span className="template-kpi-total" title="Sum of all line items">
        <span className="template-kpi-total-value">{totalText}</span>
        <span className="template-kpi-total-label">Total</span>
      </span>

      <button
        type="button"
        className={`template-kpi-more ${open ? "is-open" : ""}`}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-haspopup="dialog"
        title="More stats"
        data-testid="template-kpi-more"
      >
        More
      </button>

      {open && (
        <div
          className="template-kpi-popover"
          role="dialog"
          aria-label="Template stats"
          data-testid="template-kpi-popover"
        >
          <dl>
            <dt>Files</dt>
            <dd>{summary.files}</dd>
            <dt>Invoices</dt>
            <dd>{summary.invoices}</dd>
            <dt>Rows</dt>
            <dd>{summary.rows}</dd>
            <dt>Edited</dt>
            <dd>{summary.edited}</dd>
            <dt>Missing link</dt>
            <dd>{summary.urlsMissing}</dd>
          </dl>
        </div>
      )}
    </div>
  );
}
function AlertCircleIcon() {
  return (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="10" />
      <line x1="12" y1="8" x2="12" y2="12" />
      <line x1="12" y1="16" x2="12.01" y2="16" />
    </svg>
  );
}
function CheckCircleIcon() {
  return (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="20 6 9 17 4 12" />
    </svg>
  );
}

// =============================================================================
// Phase 2D — Revisions dropdown.
// =============================================================================
function RevisionsDropdown({
  revisions,
  currentRevisionId,
  onActivate,
  onDelete,
}: {
  revisions: import("../types").RevisionEntry[];
  currentRevisionId: string | null;
  onActivate?: (revisionId: string) => Promise<void> | void;
  onDelete?: (revisionId: string) => Promise<void> | void;
}) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const btnRef = useRef<HTMLButtonElement | null>(null);
  const popoverRef = useRef<HTMLDivElement | null>(null);
  // Phase 2I.10 — the popover lives inside `.template-window-filename`,
  // which has `overflow: hidden` to ellipsize long filenames. That clip
  // also chops the popover. We dodge it by positioning the popover with
  // `position: fixed` against the icon button's bounding rect, so the
  // ancestor's overflow no longer applies.
  const [popoverPos, setPopoverPos] = useState<{
    top: number;
    left: number;
  } | null>(null);
  useEffect(() => {
    if (!open) {
      setPopoverPos(null);
      return;
    }
    const recompute = () => {
      const btn = btnRef.current;
      if (!btn) return;
      const rect = btn.getBoundingClientRect();
      setPopoverPos({ top: rect.bottom + 6, left: rect.left });
    };
    recompute();
    const onDoc = (e: MouseEvent) => {
      const wrap = wrapRef.current;
      const pop = popoverRef.current;
      if (e.target instanceof Node) {
        if (wrap?.contains(e.target)) return;
        if (pop?.contains(e.target)) return;
      }
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("resize", recompute);
    window.addEventListener("scroll", recompute, true);
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("resize", recompute);
      window.removeEventListener("scroll", recompute, true);
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const current = revisions.find((r) => r.revision_id === currentRevisionId)
    || revisions[0];
  const currentIndex = revisions.findIndex((r) => r.revision_id === currentRevisionId);
  // Phase 2F — template data can be a live preview before it has been
  // saved as a revision. Avoid the confusing "No runs" label.
  const positionLabel =
    revisions.length === 0
      ? "Current preview"
      : currentIndex >= 0
      ? `v${revisions.length - currentIndex}`
      : `v${revisions.length}`;

  return (
    // Phase 2H — minimal icon button (clock-with-history) that sits
    // beside the filename. Click opens a section-header action menu
    // styled like the rest of the app's overflow menus.
    <div className="template-revisions-wrapper" ref={wrapRef}>
      <button
        ref={btnRef}
        type="button"
        className={`revisions-icon-btn ${open ? "is-open" : ""}`}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-haspopup="menu"
        title={`Template revisions (${revisions.length} so far) · current: ${positionLabel}`}
        data-testid="template-revisions-btn"
      >
        {/* Phase 2H — SVG inlined directly (instead of via the helper)
            so a missing/late-bound function never produces an empty
            button. Explicit width/height + stroke colour means no
            ambient CSS can collapse it to 0 px. */}
        <svg
          width="15"
          height="15"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.9"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
          style={{ display: "block" }}
        >
          <path d="M3 12a9 9 0 1 0 3-6.71" />
          <polyline points="3 4 3 9 8 9" />
          <polyline points="12 7 12 12 15.5 13.8" />
        </svg>
      </button>
      {open && popoverPos && (
        <div
          ref={popoverRef}
          className="revisions-popover is-floating"
          role="menu"
          data-testid="template-revisions-popover"
          style={{ top: popoverPos.top, left: popoverPos.left }}
        >
          <div className="revisions-section-label">Revisions</div>
          {revisions.length === 0 ? (
            <div className="revisions-empty-row">
              <ClockHistoryIcon />
              <span>No saved revisions yet</span>
            </div>
          ) : (
            <ul className="revisions-list">
              {revisions.map((r, i) => {
                const isCurrent = r.revision_id === (current?.revision_id || null);
                const versionLabel = `v${revisions.length - i}`;
                return (
                  <li
                    key={r.revision_id}
                    className={`revisions-item ${isCurrent ? "is-current" : ""}`}
                    role="menuitemradio"
                    aria-checked={isCurrent}
                    tabIndex={0}
                    onClick={async () => {
                      if (!isCurrent && onActivate) {
                        await onActivate(r.revision_id);
                      }
                      setOpen(false);
                    }}
                    onKeyDown={async (e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        if (!isCurrent && onActivate) {
                          await onActivate(r.revision_id);
                        }
                        setOpen(false);
                      }
                    }}
                  >
                    <span className="revisions-item-icon" aria-hidden>
                      {isCurrent ? <CheckIcon /> : <ClockHistoryIcon />}
                    </span>
                    <span className="revisions-item-body">
                      <span className="revisions-item-title">
                        {versionLabel}
                        <span className="revisions-item-stamp">
                          {formatRevisionStamp(r.created_at)}
                        </span>
                      </span>
                      <span className="revisions-item-meta">
                        {r.invoices_count} invoice{r.invoices_count === 1 ? "" : "s"}
                        {" · "}
                        {r.rows_count} row{r.rows_count === 1 ? "" : "s"}
                        {r.manual_review_count > 0 && (
                          <> · {r.manual_review_count} issue{r.manual_review_count === 1 ? "" : "s"}</>
                        )}
                      </span>
                    </span>
                    {onDelete && (
                      <button
                        type="button"
                        className="revisions-item-delete"
                        title={`Delete ${versionLabel}`}
                        aria-label={`Delete revision ${versionLabel}`}
                        data-testid={`template-revisions-delete-${r.revision_id}`}
                        onClick={async (e) => {
                          e.stopPropagation();
                          const ok = window.confirm(
                            `Delete ${versionLabel} (${formatRevisionStamp(r.created_at)})? This cannot be undone.`,
                          );
                          if (!ok) return;
                          await onDelete(r.revision_id);
                        }}
                        onKeyDown={(e) => {
                          // Don't let Space/Enter on the trash also activate the row.
                          if (e.key === "Enter" || e.key === " ") {
                            e.stopPropagation();
                          }
                        }}
                      >
                        <TrashIcon />
                      </button>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

// Small clock-with-arrow icon used by the revisions affordance.
function ClockHistoryIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3 12a9 9 0 1 0 3-6.71" />
      <polyline points="3 4 3 9 8 9" />
      <polyline points="12 7 12 12 15.5 13.8" />
    </svg>
  );
}
function CheckIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="20 6 9 17 4 12" />
    </svg>
  );
}
function TrashIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="3 6 5 6 21 6" />
      <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
      <path d="M10 11v6" />
      <path d="M14 11v6" />
      <path d="M9 6V4a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2" />
    </svg>
  );
}

function formatRevisionStamp(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleString();
  } catch {
    return iso;
  }
}

function SummaryStat({
  label,
  value,
  tone,
}: {
  label: string;
  value: number | string;
  tone?: "neutral" | "warn" | "info" | "strong";
}) {
  return (
    <div className={`summary-stat tone-${tone ?? "neutral"}`}>
      <span className="summary-stat-label">{label}</span>
      <span className="summary-stat-value">{value}</span>
    </div>
  );
}

/* Phase 1V — template-local processing panel.
 *
 * Renders inside the template area while a batch run is in flight,
 * replacing the previous sidebar timeline that pushed the file list
 * down. Centred card sits above the (possibly empty) preview rows
 * with: title, current file, percent + bar, expandable timeline,
 * Stop button.
 */
function ProcessingPanel({
  progress,
  isCancelling,
  onCancel,
}: {
  progress?: BatchProgress | null;
  isCancelling?: boolean;
  onCancel?: () => void;
}) {
  const pct = Math.max(0, Math.min(100, progress?.percent ?? 0));
  const filesTotal = progress?.files_total ?? 0;
  const filesDone = progress?.files_done ?? 0;
  const currentFile = progress?.current_file || "";
  const currentStep = progress?.current_step || "Working…";

  const summaryLine: string[] = [];
  if (filesTotal > 0) {
    summaryLine.push(`${filesDone}/${filesTotal} files`);
  }
  if (progress?.invoices_created) {
    summaryLine.push(`${progress.invoices_created} invoices`);
  }
  if (progress?.warnings_count) {
    summaryLine.push(`${progress.warnings_count} flagged`);
  }

  return (
    <div className="template-processing-panel" role="status" aria-live="polite">
      <div className="template-processing-card">
        <div className="template-processing-header">
          <div className="template-processing-title">
            <span className="pdf-loading-dots" aria-hidden>
              <span /><span /><span />
            </span>
            <span>
              {isCancelling ? "Cancelling…" : "Building ResMan template"}
            </span>
          </div>
          {onCancel && !isCancelling && (
            // Phase 2E — the parent (App.tsx::handleCancel) already opens
            // the app-native ConfirmDialog. The local `window.confirm`
            // popup that lived here in Phase 1V was the source of the
            // double-prompt; firing onCancel directly is correct.
            <button
              type="button"
              className="btn btn-mini btn-danger"
              onClick={onCancel}
              title="Stop processing this batch"
              data-testid="template-stop-btn"
            >
              Stop
            </button>
          )}
        </div>
        <div className="template-processing-step" title={currentStep}>
          {currentStep}
        </div>
        {currentFile && (
          <div className="template-processing-file" title={currentFile}>
            Current: <strong>{currentFile}</strong>
          </div>
        )}
        <div className="template-processing-bar">
          <div
            className="template-processing-bar-fill"
            style={{ width: `${pct.toFixed(1)}%` }}
          />
        </div>
        <div className="template-processing-meta">
          <span>{pct.toFixed(0)}%</span>
          {summaryLine.length > 0 && <span>{summaryLine.join(" · ")}</span>}
        </div>
        {progress?.stages && progress.stages.length > 0 && (
          <div className="template-processing-timeline">
            <ProcessingTimeline progress={progress} />
          </div>
        )}
      </div>
    </div>
  );
}
