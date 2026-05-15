import { useEffect, useMemo, useRef, useState } from "react";

import type { PreviewResponse, PreviewRow } from "../types";
import { useWheelHorizontalScroll } from "../hooks/useWheelHorizontalScroll";

function isMissing(value: unknown) {
  return value == null || value === "" || value === undefined;
}

export type CellEdits = Record<number, Record<string, unknown>>;
export type GridCellSuggestion = {
  label: string;
  value: string;
  detail?: string;
  disabled?: boolean;
};
export type GridCellSuggestionConfig = {
  title: string;
  items: GridCellSuggestion[];
  emptyText?: string;
  onApply: (value: string, item: GridCellSuggestion) => void;
  onRegenerate?: () => void;
};

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
  getCellAiSuggestions?: (
    rowIndex: number,
    column: string,
    row: PreviewRow,
  ) => GridCellSuggestionConfig | null;
  getCellDisplayValue?: (
    rowIndex: number,
    column: string,
    row: PreviewRow,
    value: unknown,
  ) => unknown;
  getDocumentUrl?: (row: PreviewRow) => string;
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
  getCellAiSuggestions,
  getCellDisplayValue,
  getDocumentUrl,
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
              const isAiGenerated = r._meta?.ai_generated === true;
              const isAiLowConfidence =
                r._meta?.ai_confidence_low === true ||
                (typeof r._meta?.ai_confidence === "number" &&
                  r._meta.ai_confidence < 0.7);
              const rowEdits = edits[i] ?? {};
              const isSelected = selectedRowIndex === i;
              const isCurrentDocumentPage =
                !!activeDocumentRef && rowMatchesDocument(r, activeDocumentRef);
              const rowClasses = [
                isFlagged ? "review-row" : "",
                isSelected ? "selected-row" : "",
                isCurrentDocumentPage ? "document-page-row" : "",
                isAiGenerated ? "ai-generated-row" : "",
                isAiLowConfidence ? "ai-low-confidence-row" : "",
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
                  title={[
                    ...reasons,
                    isAiGenerated ? "AI extracted candidate" : "",
                    isAiLowConfidence ? "Low AI confidence" : "",
                  ].filter(Boolean).join("; ")}
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
                    const fallbackDocumentUrl =
                      isUrl && getDocumentUrl ? getDocumentUrl(r) : "";
                    const isEditingCell =
                      editing?.row === i && editing?.col === c;
                    const displayValue =
                      !isEditingCell && getCellDisplayValue
                        ? getCellDisplayValue(i, c, r, value)
                        : value;
                    const aiSuggestions =
                      !isEditingCell && getCellAiSuggestions
                        ? getCellAiSuggestions(i, c, r)
                        : null;

                    const aiFlags = r._meta?.ai_validation_flags ?? [];
                    const isAiIssueCell =
                      isAiGenerated &&
                      (isAiLowConfidence ||
                        (isRequired && aiFlags.some((flag) =>
                          /missing|invalid|mapping|ambiguous|failed/.test(flag),
                        )));
                    const baseClass = `${isAmount ? "num" : ""} ${
                      cellMissing ? "error-row" : ""
                    } cell-${cat} ${isAiIssueCell ? "ai-low-confidence-cell" : ""}`;
                    const style: React.CSSProperties = {
                      ...(cellMissing ? { background: "rgba(245, 158, 11, 0.10)" } : null),
                      ...(overridden && !cellMissing
                        ? {
                            background: "#dafbe1",
                            outline: "1px solid #1a7f37",
                            outlineOffset: "-1px",
                          }
                        : null),
                      cursor: isEditingCell ? "text" : "cell",
                      maxWidth: 280,
                      overflow: aiSuggestions ? "visible" : "hidden",
                      textOverflow: "ellipsis",
                    };

                    const isSelectedCell =
                      selectedRowIndex === i && selectedColumnKey === c;
                    return (
                      <td
                        key={c}
                        className={`${baseClass} ${isSelectedCell ? "selected-cell" : ""} ${
                          aiSuggestions ? "cell-has-ai-suggestions" : ""
                        }`}
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
                          [
                            overridden ? `Original: ${original ?? ""}` : "",
                            isAiGenerated ? `AI extracted${typeof r._meta?.ai_confidence === "number" ? ` · confidence ${(r._meta.ai_confidence * 100).toFixed(0)}%` : ""}` : "",
                            isAiIssueCell && aiFlags.length ? aiFlags.join("; ") : "",
                          ].filter(Boolean).join("\n") || undefined
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
                        ) : (
                          <div className="template-cell-shell">
                            <span
                              className={`template-cell-value ${
                                isMissing(value) && !isMissing(displayValue)
                                  ? "is-provisional"
                                  : ""
                              }`}
                            >
                              {isUrl && ((typeof value === "string" && value) || fallbackDocumentUrl) ? (
                                <a
                                  className="doc-url-icon"
                                  href={(typeof value === "string" && value ? value : fallbackDocumentUrl) as string}
                                  target="_blank"
                                  rel="noreferrer"
                                  onClick={(e) => e.stopPropagation()}
                                  title={typeof value === "string" && value ? "Open Dropbox/support document" : "Open source document"}
                                >
                                  {typeof value === "string" && value ? "Open" : "Source"}
                                </a>
                              ) : isUrl ? (
                                <span className="doc-url-empty">-</span>
                              ) : isAmount && typeof displayValue === "number" ? (
                                displayValue.toFixed(2)
                              ) : displayValue == null ? (
                                ""
                              ) : (
                                String(displayValue)
                              )}
                            </span>
                            {aiSuggestions && (
                              <GridCellAiControl config={aiSuggestions} />
                            )}
                          </div>
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

function GridCellAiControl({ config }: { config: GridCellSuggestionConfig }) {
  const [open, setOpen] = useState(false);
  const items = config.items || [];
  return (
    <span className="grid-cell-ai" onClick={(e) => e.stopPropagation()}>
      <button
        type="button"
        className={`grid-cell-ai-trigger ${open ? "is-open" : ""}`}
        title={config.title}
        aria-label={config.title}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <svg
          width="12"
          height="12"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="M12 3l1.5 4.5L18 9l-4.5 1.5L12 15l-1.5-4.5L6 9l4.5-1.5L12 3z" />
          <path d="M19 14l.8 2.2L22 17l-2.2.8L19 20l-.8-2.2L16 17l2.2-.8L19 14z" />
        </svg>
      </button>
      {open && (
        <div className="grid-cell-ai-menu" role="menu">
          <div className="grid-cell-ai-head">
            <span>{config.title}</span>
            {config.onRegenerate && (
              <button
                type="button"
                onClick={() => config.onRegenerate?.()}
                title="Generate another suggestion set"
              >
                New set
              </button>
            )}
          </div>
          <div className="grid-cell-ai-list">
            {items.length === 0 ? (
              <span className="grid-cell-ai-empty">
                {config.emptyText || "No suggestions available"}
              </span>
            ) : (
              items.map((item, idx) => (
                <button
                  type="button"
                  key={`${item.value}-${idx}`}
                  disabled={item.disabled}
                  onClick={() => {
                    config.onApply(item.value, item);
                    setOpen(false);
                  }}
                >
                  <strong>{item.label}</strong>
                  {item.detail && <span>{item.detail}</span>}
                </button>
              ))
            )}
          </div>
        </div>
      )}
    </span>
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
