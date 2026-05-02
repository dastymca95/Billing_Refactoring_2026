import { useEffect, useMemo, useRef, useState } from "react";

import type { PreviewResponse, PreviewRow } from "../types";

// Small helper — `null` / `undefined` / `""` all count as "missing".
function isMissing(value: unknown) {
  return value == null || value === "" || value === undefined;
}

export type CellEdits = Record<number, Record<string, unknown>>;

type Props = {
  preview: PreviewResponse | null;
  edits: CellEdits;
  onCellEdit: (rowIndex: number, columnKey: string, newValue: unknown) => void;
  /** Phase 1J — restrict which rows are visible. `null` means show all. */
  visibleRowIndexes?: Set<number> | null;
  /** Phase 1J — currently selected row (drives the inspector panel). */
  selectedRowIndex?: number | null;
  /** Phase 1J — toggled when an operator clicks any non-editable area
   *  of a row (e.g. the Document Url cell or whitespace). */
  onSelectRow?: (rowIndex: number | null) => void;
};

type ColumnCategory = "required" | "recommended" | "optional";

export function ResManTemplatePreview({
  preview,
  edits,
  onCellEdit,
  visibleRowIndexes = null,
  selectedRowIndex = null,
  onSelectRow,
}: Props) {
  const [collapsed, setCollapsed] = useState(false);
  const [editing, setEditing] = useState<{ row: number; col: string } | null>(
    null,
  );
  const [draft, setDraft] = useState<string>("");
  const inputRef = useRef<HTMLInputElement>(null);

  // Phase 1E: the preview now declares its full column list + which are
  // required / recommended / optional. We build O(1) lookups and a "show
  // optional" toggle whose default comes from the YAML.
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
  const hideOptionalDefault =
    preview?.optional_columns_hidden_by_default ?? true;

  // Per-preview default for the "show optional columns" toggle. We reset
  // when the column set changes (e.g. on first load vs after a process run).
  const [showOptional, setShowOptional] = useState<boolean>(!hideOptionalDefault);
  useEffect(() => {
    setShowOptional(!hideOptionalDefault);
  }, [hideOptionalDefault, columns.length]);

  const visibleColumns = useMemo(() => {
    if (!collapsibleEnabled || showOptional) return columns;
    return columns.filter((c) => !optional.has(c));
  }, [collapsibleEnabled, showOptional, columns, optional]);

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
      <div className="card">
        <div className="card-header">ResMan template preview</div>
        <div className="empty-state">
          No data yet. Click <b>Process Batch</b> to populate the preview.
        </div>
      </div>
    );
  }

  const rows = preview.rows as PreviewRow[];

  // Compute totals using the EDITED values where present.
  const merged = rows.map((r, i) => ({ ...r, ...(edits[i] ?? {}) }));
  const totalAmount = merged.reduce((s, r) => {
    const v = (r as any).Amount;
    const n = typeof v === "number" ? v : Number(v);
    return s + (Number.isFinite(n) ? n : 0);
  }, 0);
  const flaggedCount = rows.filter(
    (r) => (r._meta?.manual_review_reasons ?? []).length > 0,
  ).length;
  const editedCellCount = Object.values(edits).reduce(
    (s, m) => s + Object.keys(m).length,
    0,
  );

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

  const optionalCount = optional.size;
  const hiddenOptionalCount =
    collapsibleEnabled && !showOptional ? optionalCount : 0;

  return (
    <div className="card">
      <div className="card-header template-header">
        <span>
          ResMan template preview&nbsp;
          <span className="muted" style={{ fontWeight: 400 }}>
            · {preview.invoice_count} invoices · {preview.row_count} rows · $
            {totalAmount.toFixed(2)}
            {flaggedCount > 0 ? ` · ${flaggedCount} flagged` : ""}
            {editedCellCount > 0 ? ` · ${editedCellCount} cells edited` : ""}
            {hiddenOptionalCount > 0
              ? ` · ${hiddenOptionalCount} optional column${
                  hiddenOptionalCount > 1 ? "s" : ""
                } hidden`
              : ""}
          </span>
        </span>
        <div className="template-header-actions">
          {collapsibleEnabled && optionalCount > 0 && (
            <button
              onClick={() => setShowOptional((v) => !v)}
              className="icon-button"
              title={
                showOptional
                  ? "Hide optional template columns from view (export still includes them)"
                  : "Show every column from the official Template.xlsx"
              }
            >
              {showOptional ? "Hide optional cols" : "Show optional cols"}
            </button>
          )}
          <button
            onClick={() => setCollapsed(!collapsed)}
            className="icon-button"
          >
            {collapsed ? "Expand" : "Collapse"}
          </button>
        </div>
      </div>
      {!collapsed && (
        <>
          {editedCellCount === 0 && (
            <div className="muted" style={{ padding: "6px 14px" }}>
              Click any cell to edit. Press Enter to save, Escape to cancel.
              Required columns have orange headers; optional columns can be
              hidden via the toggle (export still uses every template column).
            </div>
          )}
          <div className="card-body tight preview-pane">
            <table className="data-table">
              <thead>
                <tr>
                  {visibleColumns.map((c) => {
                    const cat = categoryFor(c);
                    return (
                      <th key={c} className={`col-${cat}`} title={categoryTitle(cat)}>
                        {c}
                        {cat === "required" ? (
                          <span className="col-marker"> *</span>
                        ) : null}
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
                  const rowClasses = [
                    isFlagged ? "review-row" : "",
                    isSelected ? "selected-row" : "",
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

                        return (
                          <td
                            key={c}
                            className={baseClass}
                            style={style}
                            onClick={() => {
                              if (!isEditingCell) startEdit(i, c, value);
                            }}
                            title={
                              overridden
                                ? `Original: ${original ?? ""}`
                                : undefined
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
                                className="url-cell"
                                href={value as string}
                                target="_blank"
                                rel="noreferrer"
                                onClick={(e) => e.stopPropagation()}
                              >
                                {value as string}
                              </a>
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
        </>
      )}
    </div>
  );
}

function categoryTitle(cat: ColumnCategory): string {
  if (cat === "required") return "Required column (must have a value)";
  if (cat === "recommended") return "Recommended column";
  return "Optional column (hidden by default; export still uses it)";
}
