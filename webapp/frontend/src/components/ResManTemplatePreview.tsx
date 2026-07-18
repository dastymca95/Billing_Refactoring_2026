import {
  useCallback,
  useEffect,
  memo,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type MouseEvent as ReactMouseEvent,
  type RefObject,
} from "react";

import type {
  HumanAdjudicationBadge,
  PreviewResponse,
  PreviewRow,
  ReadinessIssue,
} from "../types";
import { useWheelHorizontalScroll } from "../hooks/useWheelHorizontalScroll";
import { GlAccountExplanation } from "./GlAccountExplanation";
import { RequiredFieldExplanation } from "./RequiredFieldExplanation";

function isMissing(value: unknown) {
  return value == null || value === "" || value === undefined;
}

function readinessIssueForCell(
  preview: PreviewResponse | null,
  rowIndex: number,
  field: string,
): ReadinessIssue | undefined {
  return preview?.accounting_readiness?.blockers.find(
    (issue) =>
      !issue.resolved &&
      issue.field === field &&
      (issue.line_item_id == null || issue.line_item_id === String(rowIndex)),
  );
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
  // Phase 2K — cell-level menu hook. Parent receives the right-click
  // location (viewport coords), the row index, and the column key,
  // and decides what to render (typically <CellContextMenu>).
  onCellContextMenu?: (params: {
    rowIndex: number;
    column: string;
    x: number;
    y: number;
    selectedRowIndexes?: number[];
    selectedColumns?: string[];
  }) => void;
  onDeleteRows?: (rowIndexes: number[]) => void;
  onDeleteColumns?: (columns: string[]) => void;
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

function ResManTemplatePreviewImpl({
  preview,
  edits,
  onCellEdit,
  visibleRowIndexes = null,
  selectedRowIndex = null,
  activeDocumentRef = null,
  onSelectRow,
  onCellContextMenu,
  selectedColumnKey = null,
  onSelectCell,
  onColumnFilterClick,
  filteredColumns = null,
  getCellAiSuggestions,
  getCellDisplayValue,
  getDocumentUrl,
  onDeleteRows,
  onDeleteColumns,
}: Props) {
  const [editing, setEditing] = useState<{ row: number; col: string } | null>(
    null,
  );
  const [draft, setDraft] = useState<string>("");
  const inputRef = useRef<HTMLInputElement>(null);
  const structureMenuRef = useRef<HTMLDivElement | null>(null);
  const [selectedRows, setSelectedRows] = useState<Set<number>>(() => new Set());
  const [selectedColumns, setSelectedColumns] = useState<Set<string>>(
    () => new Set(),
  );
  const [structureMenu, setStructureMenu] = useState<{
    x: number;
    y: number;
    rowIndexes: number[];
    columns: string[];
  } | null>(null);
  const rowAnchorRef = useRef<number | null>(null);
  const columnAnchorRef = useRef<string | null>(null);
  // Phase 2L — wheel-while-hovering-the-bottom-scrollbar redirects to
  // horizontal scrolling, so the operator can reach off-screen
  // columns without dragging the scrollbar thumb.
  const scrollPaneRef = useWheelHorizontalScroll();

  const columns = preview?.columns ?? [];
  const rows = (preview?.rows ?? []) as PreviewRow[];
  const required = useMemo(
    () => new Set(preview?.required_columns ?? []),
    [preview?.required_columns],
  );
  const recommended = useMemo(
    () => new Set(preview?.recommended_columns ?? []),
    [preview?.recommended_columns],
  );
  const visibleColumns = columns;

  const renderedRows = useMemo(
    () =>
      rows
        .map((row, index) => ({ row, index }))
        .filter(({ index }) => !visibleRowIndexes || visibleRowIndexes.has(index)),
    [rows, visibleRowIndexes],
  );
  const visibleRowOrder = useMemo(
    () => renderedRows.map(({ index }) => index),
    [renderedRows],
  );

  useEffect(() => {
    setSelectedRows((prev) => {
      const next = new Set([...prev].filter((rowIndex) => rowIndex >= 0 && rowIndex < rows.length));
      return next.size === prev.size ? prev : next;
    });
  }, [rows.length]);

  useEffect(() => {
    setSelectedColumns((prev) => {
      const allowed = new Set(visibleColumns);
      const next = new Set([...prev].filter((column) => allowed.has(column)));
      return next.size === prev.size ? prev : next;
    });
  }, [visibleColumns]);

  useEffect(() => {
    if (!structureMenu) return;
    const onDoc = (event: MouseEvent) => {
      if (
        structureMenuRef.current &&
        event.target instanceof Node &&
        structureMenuRef.current.contains(event.target)
      ) {
        return;
      }
      setStructureMenu(null);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setStructureMenu(null);
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [structureMenu]);

  const categoryFor = useCallback((col: string): ColumnCategory => {
    if (required.has(col)) return "required";
    if (recommended.has(col)) return "recommended";
    return "optional";
  }, [recommended, required]);

  useEffect(() => {
    if (editing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [editing]);

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

  const rowRange = useCallback(
    (from: number, to: number) => {
      const order = visibleRowOrder.length ? visibleRowOrder : rows.map((_, index) => index);
      const fromPos = order.indexOf(from);
      const toPos = order.indexOf(to);
      if (fromPos === -1 || toPos === -1) {
        const start = Math.min(from, to);
        const end = Math.max(from, to);
        return Array.from({ length: end - start + 1 }, (_, offset) => start + offset);
      }
      const start = Math.min(fromPos, toPos);
      const end = Math.max(fromPos, toPos);
      return order.slice(start, end + 1);
    },
    [rows, visibleRowOrder],
  );

  const columnRange = useCallback(
    (from: string, to: string) => {
      const fromPos = visibleColumns.indexOf(from);
      const toPos = visibleColumns.indexOf(to);
      if (fromPos === -1 || toPos === -1) return [to];
      const start = Math.min(fromPos, toPos);
      const end = Math.max(fromPos, toPos);
      return visibleColumns.slice(start, end + 1);
    },
    [visibleColumns],
  );

  const selectRow = useCallback(
    (rowIndex: number, event: ReactMouseEvent) => {
      event.preventDefault();
      event.stopPropagation();
      setSelectedColumns(new Set());
      setSelectedRows((prev) => {
        let next: Set<number>;
        if (event.shiftKey && rowAnchorRef.current != null) {
          next = new Set(rowRange(rowAnchorRef.current, rowIndex));
        } else if (event.metaKey || event.ctrlKey) {
          next = new Set(prev);
          if (next.has(rowIndex)) next.delete(rowIndex);
          else next.add(rowIndex);
          rowAnchorRef.current = rowIndex;
        } else {
          next = new Set([rowIndex]);
          rowAnchorRef.current = rowIndex;
        }
        return next;
      });
      onSelectRow?.(rowIndex);
      onSelectCell?.(rowIndex, null);
    },
    [onSelectCell, onSelectRow, rowRange],
  );

  const selectColumn = useCallback(
    (column: string, event: ReactMouseEvent) => {
      event.preventDefault();
      event.stopPropagation();
      setSelectedRows(new Set());
      setSelectedColumns((prev) => {
        let next: Set<string>;
        if (event.shiftKey && columnAnchorRef.current) {
          next = new Set(columnRange(columnAnchorRef.current, column));
        } else if (event.metaKey || event.ctrlKey) {
          next = new Set(prev);
          if (next.has(column)) next.delete(column);
          else next.add(column);
          columnAnchorRef.current = column;
        } else {
          next = new Set([column]);
          columnAnchorRef.current = column;
        }
        return next;
      });
      onSelectRow?.(null);
      onSelectCell?.(null, column);
    },
    [columnRange, onSelectCell, onSelectRow],
  );

  const selectAllVisibleRows = useCallback((event: ReactMouseEvent) => {
    event.preventDefault();
    event.stopPropagation();
    const next = new Set(visibleRowOrder);
    setSelectedRows(next);
    setSelectedColumns(new Set());
    rowAnchorRef.current = visibleRowOrder[0] ?? null;
    onSelectRow?.(visibleRowOrder[0] ?? null);
    onSelectCell?.(visibleRowOrder[0] ?? null, null);
  }, [onSelectCell, onSelectRow, visibleRowOrder]);

  const openStructureMenu = useCallback(
    (
      event: ReactMouseEvent,
      payload: { rowIndexes?: number[]; columns?: string[] },
    ) => {
      event.preventDefault();
      event.stopPropagation();
      setStructureMenu({
        x: event.clientX,
        y: event.clientY,
        rowIndexes: payload.rowIndexes ?? [],
        columns: payload.columns ?? [],
      });
    },
    [],
  );

  if (!preview) {
    return (
      <div className="card template-grid-card" data-testid="template-grid-card">
        <div
          ref={scrollPaneRef}
          className="card-body tight preview-pane"
          data-testid="template-grid-scroll"
        >
          <div className="empty-state">
            No template rows yet. Click <b>Process</b> to populate the preview.
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="card template-grid-card" data-testid="template-grid-card">
      <div
        ref={scrollPaneRef}
        className="card-body tight preview-pane"
        data-testid="template-grid-scroll"
      >
        <table className="data-table template-grid-table">
          <thead>
            <tr>
              <th
                className={`row-selector-header ${selectedRows.size > 0 ? "is-selected" : ""}`}
                title="Select visible rows"
                onClick={selectAllVisibleRows}
                onContextMenu={(e) => {
                  const rowIndexes = selectedRows.size
                    ? [...selectedRows]
                    : visibleRowOrder;
                  openStructureMenu(e, { rowIndexes });
                }}
              />
              {visibleColumns.map((c) => {
                const cat = categoryFor(c);
                const hasFilter = !!filteredColumns?.has(c);
                const isColumnSelected = selectedColumns.has(c);
                return (
                  <th
                    key={c}
                    className={`col-${cat} ${isColumnSelected ? "selected-column-header" : ""}`}
                    title={categoryTitle(cat)}
                    onClick={(e) => selectColumn(c, e)}
                    onContextMenu={(e) => {
                      const columnsForMenu = isColumnSelected
                        ? [...selectedColumns]
                        : [c];
                      setSelectedRows(new Set());
                      setSelectedColumns(new Set(columnsForMenu));
                      columnAnchorRef.current = c;
                      openStructureMenu(e, { columns: columnsForMenu });
                    }}
                  >
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
            {renderedRows.map(({ row: r, index: i }) => {
              const reasons = r._meta?.manual_review_reasons ?? [];
              const isFlagged = reasons.length > 0;
              const isAiGenerated = r._meta?.ai_generated === true;
              const isAiLowConfidence =
                r._meta?.ai_confidence_low === true ||
                (typeof r._meta?.ai_confidence === "number" &&
                  r._meta.ai_confidence < 0.7);
              const rowEdits = edits[i] ?? {};
              const isSelected = selectedRowIndex === i;
              const isRangeSelected = selectedRows.has(i);
              const isCurrentDocumentPage =
                !!activeDocumentRef && rowMatchesDocument(r, activeDocumentRef);
              const rowClasses = [
                isFlagged ? "review-row" : "",
                isSelected ? "selected-row" : "",
                isRangeSelected ? "selected-row-range" : "",
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
                  <td
                    className={`row-selector-cell ${isRangeSelected ? "is-selected" : ""}`}
                    onClick={(e) => selectRow(i, e)}
                    onContextMenu={(e) => {
                      const rowIndexes = isRangeSelected ? [...selectedRows] : [i];
                      setSelectedRows(new Set(rowIndexes));
                      setSelectedColumns(new Set());
                      rowAnchorRef.current = i;
                      onSelectRow?.(i);
                      onSelectCell?.(i, null);
                      openStructureMenu(e, { rowIndexes });
                    }}
                    title="Select row"
                  >
                    <span>{i + 1}</span>
                  </td>
                  {visibleColumns.map((c) => {
                    const original = (r as any)[c];
                    const overridden = c in rowEdits;
                    const value = overridden ? (rowEdits as any)[c] : original;
                    const cat = categoryFor(c);
                    const isRequired = cat === "required";
                    const cellMissing = isRequired && isMissing(value);
                    const readinessIssue = isMissing(value)
                      ? readinessIssueForCell(preview, i, c)
                      : undefined;
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
                    const glExplanationValue =
                      c === "GL Account" && !isEditingCell ? String(value ?? displayValue ?? "").trim() : "";
                    const showGlExplanation = c === "GL Account" && !!glExplanationValue;
                    const itemMeaning = c === "Line Item Description"
                      ? String(r._meta?.ai_item_plain_language_description || "").trim()
                      : "";
                    const adjudicationBadges = r._meta?.human_adjudication_badges?.[c] ?? [];
                    const explanationRow =
                      showGlExplanation && Object.keys(rowEdits).length
                        ? ({ ...r, ...rowEdits } as PreviewRow)
                        : r;
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
                    const style: CSSProperties = {
                      ...(cellMissing ? { background: "rgba(245, 158, 11, 0.10)" } : null),
                      ...(overridden && !cellMissing
                        ? {
                            background: "#dafbe1",
                            outline: "1px solid #1a7f37",
                            outlineOffset: "-1px",
                          }
                        : null),
                      cursor: isEditingCell ? "text" : "default",
                      maxWidth: 280,
                      overflow: aiSuggestions || showGlExplanation ? "visible" : "hidden",
                      textOverflow: "ellipsis",
                    };

                    const isSelectedCell =
                      selectedRowIndex === i && selectedColumnKey === c;
                    const isSelectedColumn = selectedColumns.has(c);
                    return (
                      <td
                        key={c}
                        className={`${baseClass} ${isSelectedCell ? "selected-cell" : ""} ${
                          isSelectedColumn ? "selected-column-cell" : ""
                        } ${isRangeSelected ? "selected-row-range-cell" : ""} ${
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
                            selectedRowIndexes: isRangeSelected ? [...selectedRows] : [i],
                            selectedColumns: isSelectedColumn ? [...selectedColumns] : [],
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
                              {readinessIssue ? (
                                <span className="required-field-empty">
                                  <RequiredFieldExplanation issue={readinessIssue} />
                                </span>
                              ) : isUrl && ((typeof value === "string" && value) || fallbackDocumentUrl) ? (
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
                            <HumanAdjudicationBadges badges={adjudicationBadges} />
                            {itemMeaning && (
                              <span className="template-item-meaning" title={itemMeaning}>
                                {itemMeaning}
                              </span>
                            )}
                            {aiSuggestions && (
                              <GridCellAiControl config={aiSuggestions} />
                            )}
                            {showGlExplanation && (
                              <GlAccountExplanation
                                row={explanationRow}
                                glAccount={glExplanationValue}
                              />
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
        {structureMenu && (
          <GridStructureMenu
            refEl={structureMenuRef}
            x={structureMenu.x}
            y={structureMenu.y}
            rowCount={structureMenu.rowIndexes.length}
            columnCount={structureMenu.columns.length}
            onDeleteRows={
              structureMenu.rowIndexes.length && onDeleteRows
                ? () => {
                    onDeleteRows(structureMenu.rowIndexes);
                    setSelectedRows(new Set());
                    setStructureMenu(null);
                  }
                : undefined
            }
            onDeleteColumns={
              structureMenu.columns.length && onDeleteColumns
                ? () => {
                    onDeleteColumns(structureMenu.columns);
                    setSelectedColumns(new Set());
                    setStructureMenu(null);
                  }
                : undefined
            }
          />
        )}
      </div>
    </div>
  );
}

export const ResManTemplatePreview = memo(ResManTemplatePreviewImpl);

export function HumanAdjudicationBadges({
  badges,
}: {
  badges: HumanAdjudicationBadge[];
}) {
  if (!badges.length) return null;
  const labels: Record<HumanAdjudicationBadge, { short: string; title: string }> = {
    manually_corrected: { short: "H", title: "Manually corrected" },
    benchmark_approved: { short: "B", title: "Benchmark-approved" },
    learning_approved: { short: "L", title: "Learning-approved" },
    governed_by_rule: { short: "R", title: "Governed by an approved rule" },
  };
  return (
    <span className="human-adjudication-badges" aria-label={badges.map((badge) => labels[badge].title).join(", ")}>
      {badges.map((badge) => (
        <span key={badge} className={`human-adjudication-badge is-${badge}`} title={labels[badge].title}>
          {labels[badge].short}
        </span>
      ))}
    </span>
  );
}

function GridStructureMenu({
  refEl,
  x,
  y,
  rowCount,
  columnCount,
  onDeleteRows,
  onDeleteColumns,
}: {
  refEl: RefObject<HTMLDivElement>;
  x: number;
  y: number;
  rowCount: number;
  columnCount: number;
  onDeleteRows?: () => void;
  onDeleteColumns?: () => void;
}) {
  const width = 220;
  const height = 96;
  const left = Math.min(x, window.innerWidth - width - 8);
  const top = Math.min(y, window.innerHeight - height - 8);
  return (
    <div
      ref={refEl}
      className="grid-structure-menu"
      role="menu"
      style={{ left, top }}
      data-testid="grid-structure-menu"
    >
      {onDeleteRows && (
        <button type="button" role="menuitem" className="danger" onClick={onDeleteRows}>
          Delete {rowCount === 1 ? "row" : `${rowCount} rows`}
        </button>
      )}
      {onDeleteColumns && (
        <button type="button" role="menuitem" className="danger" onClick={onDeleteColumns}>
          Delete {columnCount === 1 ? "column" : `${columnCount} columns`}
        </button>
      )}
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
