import { useEffect, useMemo, useRef, useState } from "react";

import type { PreviewResponse, PreviewRow } from "../types";
import { useWheelHorizontalScroll } from "../hooks/useWheelHorizontalScroll";

function isMissing(value: unknown) {
  return value == null || value === "" || value === undefined;
}

export type CellEdits = Record<number, Record<string, unknown>>;

type Props = {
  preview: PreviewResponse | null;
  edits: CellEdits;
  onCellEdit: (rowIndex: number, columnKey: string, newValue: unknown) => void;
  visibleRowIndexes?: Set<number> | null;
  selectedRowIndex?: number | null;
  activeDocumentRef?: {
    filename: string;
    pageNumber: number;
  } | null;
  onSelectRow?: (rowIndex: number | null) => void;
  forceShowOptional?: boolean;
  // Phase 2K — cell-level menu hook. Parent receives the right-click
  // location (viewport coords), the row index, and the column key,
  // and decides what to render (typically <CellContextMenu>).
  onCellContextMenu?: (params: {
    rowIndex: number;
    column: string;
    x: number;
    y: number;
  }) => void;
  // Cell-scoped trace highlight: when both selectedRowIndex AND
  // selectedColumnKey are set, the parent dims non-cell traces.
  selectedColumnKey?: string | null;
  onSelectCell?: (rowIndex: number | null, column: string | null) => void;
  // Phase 2M — Excel-style per-column filtering. Parent owns the
  // filter state; the grid only renders the funnel button on each
  // header and dispatches a click with the column name + the button's
  // bounding rect so the popover can position itself.
  onColumnFilterClick?: (column: string, anchorRect: DOMRect) => void;
  filteredColumns?: Set<string> | null;
};

type ColumnCategory = "required" | "recommended" | "optional";

export function ResManTemplatePreview({
  preview,
  edits,
  onCellEdit,
  visibleRowIndexes = null,
  selectedRowIndex = null,
  activeDocumentRef = null,
  onSelectRow,
  forceShowOptional = false,
  onCellContextMenu,
  selectedColumnKey = null,
  onSelectCell,
  onColumnFilterClick,
  filteredColumns = null,
}: Props) {
  const [editing, setEditing] = useState<{ row: number; col: string } | null>(
    null,
  );
  const [draft, setDraft] = useState<string>("");
  const inputRef = useRef<HTMLInputElement>(null);
  // Phase 2L — wheel-while-hovering-the-bottom-scrollbar redirects to
  // horizontal scrolling, so the operator can reach off-screen
  // columns without dragging the scrollbar thumb.
  const scrollPaneRef = useWheelHorizontalScroll();

  const columns = preview?.columns ?? [];
  const required = useMemo(
    () => new Set(preview?.required_columns ?? []),
    [preview?.required_columns],
  );
  const recommended = useMemo(
    () => new Set(preview?.recommended_columns ?? []),
    [preview?.recommended_columns],
  );
  const optional = useMemo(
    () => new Set(preview?.optional_columns ?? []),
    [preview?.optional_columns],
  );

  const collapsibleEnabled = preview?.optional_columns_collapsible ?? true;
  const visibleColumns = useMemo(() => {
    if (forceShowOptional || !collapsibleEnabled) return columns;
    return columns.filter((c) => !optional.has(c));
  }, [forceShowOptional, collapsibleEnabled, columns, optional]);

  const categoryFor = (col: string): ColumnCategory => {
    if (required.has(col)) return "required";
    if (recommended.has(col)) return "recommended";
    return "optional";
  };

  useEffect(() => {
    if (editing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [editing]);

  if (!preview) {
    return (
      <div className="card template-grid-card" data-testid="template-grid-card">
        <div className="empty-state">
          No template rows yet. Click <b>Process</b> to populate the preview.
        </div>
      </div>
    );
  }

  const rows = preview.rows as PreviewRow[];

  const startEdit = (rowIndex: number, col: string, current: unknown) => {
    setEditing({ row: rowIndex, col });
    setDraft(current == null ? "" : String(current));
  };

  const commit = () => {
    if (!editing) return;
    const original = (rows[editing.row] as any)[editing.col];
    let nextValue: unknown = draft;
    if (typeof original === "number" || editing.col === "Amount") {
      const asNum = Number(draft);
      if (Number.isFinite(asNum)) nextValue = asNum;
    } else if (typeof original === "boolean") {
      nextValue = draft === "true" || draft === "True" || draft === "TRUE";
    }
    onCellEdit(editing.row, editing.col, nextValue);
    setEditing(null);
  };

  const cancel = () => setEditing(null);

  return (
    <div className="card template-grid-card" data-testid="template-grid-card">
      <div
        ref={scrollPaneRef}
        className="card-body tight preview-pane"
        data-testid="template-grid-scroll"
      >
        <table className="data-table">
          <thead>
            <tr>
              {visibleColumns.map((c) => {
                const cat = categoryFor(c);
                const hasFilter = !!filteredColumns?.has(c);
                return (
                  <th key={c} className={`col-${cat}`} title={categoryTitle(cat)}>
                    <span className="th-label">{c}</span>
                    {cat === "required" ? (
                      <span className="col-marker"> *</span>
                    ) : null}
                    {onColumnFilterClick && (
                      <button
                        type="button"
                        className={`th-filter-btn ${hasFilter ? "is-active" : ""}`}
                        onClick={(e) => {
                          e.stopPropagation();
                          const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
                          onColumnFilterClick(c, rect);
                        }}
                        title={hasFilter ? "Filter applied — click to edit" : `Filter ${c}`}
                        aria-label={`Filter ${c}`}
                        data-testid={`th-filter-${c}`}
                      >
                        <FunnelIcon active={hasFilter} />
                      </button>
                    )}
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => {
              if (visibleRowIndexes && !visibleRowIndexes.has(i)) return null;
              const reasons = r._meta?.manual_review_reasons ?? [];
              const isFlagged = reasons.length > 0;
              const rowEdits = edits[i] ?? {};
              const isSelected = selectedRowIndex === i;
              const isCurrentDocumentPage =
                !!activeDocumentRef && rowMatchesDocument(r, activeDocumentRef);
              const rowClasses = [
                isFlagged ? "review-row" : "",
                isSelected ? "selected-row" : "",
                isCurrentDocumentPage ? "document-page-row" : "",
              ]
                .filter(Boolean)
                .join(" ");
              const handleRowClick = () => {
                if (onSelectRow) onSelectRow(i);
              };
              return (
                <tr
                  key={i}
                  className={rowClasses}
                  title={reasons.join("; ")}
                  onClick={handleRowClick}
                  data-testid="template-row"
                  data-source-file={
                    typeof r._meta?.source_file === "string"
                      ? r._meta.source_file
                      : undefined
                  }
                  data-source-page={r._meta?.source_page ?? undefined}
                >
                  {visibleColumns.map((c) => {
                    const original = (r as any)[c];
                    const overridden = c in rowEdits;
                    const value = overridden ? (rowEdits as any)[c] : original;
                    const cat = categoryFor(c);
                    const isRequired = cat === "required";
                    const cellMissing = isRequired && isMissing(value);
                    const isAmount = c === "Amount";
                    const isUrl = c === "Document Url";
                    const isEditingCell =
                      editing?.row === i && editing?.col === c;

                    const baseClass = `${isAmount ? "num" : ""} ${
                      cellMissing ? "error-row" : ""
                    } cell-${cat}`;
                    const style: React.CSSProperties = {
                      ...(cellMissing ? { background: "#ffebe9" } : null),
                      ...(overridden && !cellMissing
                        ? {
                            background: "#dafbe1",
                            outline: "1px solid #1a7f37",
                            outlineOffset: "-1px",
                          }
                        : null),
                      cursor: isEditingCell ? "text" : "cell",
                      maxWidth: 280,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                    };

                    const isSelectedCell =
                      selectedRowIndex === i && selectedColumnKey === c;
                    return (
                      <td
                        key={c}
                        className={`${baseClass} ${isSelectedCell ? "selected-cell" : ""}`}
                        style={style}
                        onClick={() => {
                          // Single click selects the cell so right-click /
                          // context actions and per-cell trace highlighting
                          // know which column the user is looking at.
                          onSelectCell?.(i, c);
                        }}
                        onDoubleClick={(e) => {
                          e.stopPropagation();
                          if (!isEditingCell) startEdit(i, c, value);
                        }}
                        onContextMenu={(e) => {
                          if (!onCellContextMenu) return;
                          e.preventDefault();
                          e.stopPropagation();
                          onSelectCell?.(i, c);
                          onCellContextMenu({
                            rowIndex: i,
                            column: c,
                            x: e.clientX,
                            y: e.clientY,
                          });
                        }}
                        title={
                          overridden ? `Original: ${original ?? ""}` : undefined
                        }
                      >
                        {isEditingCell ? (
                          <input
                            ref={inputRef}
                            value={draft}
                            onChange={(e) => setDraft(e.target.value)}
                            onBlur={commit}
                            onKeyDown={(e) => {
                              if (e.key === "Enter") {
                                e.preventDefault();
                                commit();
                              } else if (e.key === "Escape") {
                                cancel();
                              }
                            }}
                            style={{
                              width: "100%",
                              border: "1px solid var(--accent)",
                              outline: "none",
                              padding: "1px 3px",
                              font: "inherit",
                            }}
                          />
                        ) : isUrl && typeof value === "string" && value ? (
                          <a
                            className="doc-url-icon"
                            href={value as string}
                            target="_blank"
                            rel="noreferrer"
                            onClick={(e) => e.stopPropagation()}
                            title="Open support document"
                          >
                            Open
                          </a>
                        ) : isUrl ? (
                          <span className="doc-url-empty">-</span>
                        ) : isAmount && typeof value === "number" ? (
                          value.toFixed(2)
                        ) : value == null ? (
                          ""
                        ) : (
                          String(value)
                        )}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function FunnelIcon({ active }: { active?: boolean }) {
  return (
    <svg
      width="10"
      height="10"
      viewBox="0 0 12 12"
      fill={active ? "currentColor" : "none"}
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M1.5 2h9l-3.5 4.5v3.5l-2 1V6.5z" />
    </svg>
  );
}

function categoryTitle(cat: ColumnCategory): string {
  if (cat === "required") return "Required column (must have a value)";
  if (cat === "recommended") return "Recommended column";
  return "Optional column";
}

function rowMatchesDocument(
  row: PreviewRow,
  ref: { filename: string; pageNumber: number },
): boolean {
  const sourceFile =
    typeof row._meta?.source_file === "string" ? row._meta.source_file : "";
  const sourcePage =
    typeof row._meta?.source_page === "number" && Number.isFinite(row._meta.source_page)
      ? Math.floor(row._meta.source_page)
      : 1;
  return sourceFile === ref.filename && sourcePage === ref.pageNumber;
}
