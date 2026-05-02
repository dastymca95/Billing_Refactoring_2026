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

import { useEffect, useMemo, useState } from "react";

import type { PreviewResponse, PreviewRow } from "../types";
import {
  ResManTemplatePreview,
  type CellEdits,
} from "./ResManTemplatePreview";

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
  onSelectRow: (rowIndex: number | null) => void;
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
  onSelectRow,
}: Props) {
  const [view, setView] = useState<ViewPreset>("required");
  const [filter, setFilter] = useState<RowFilter>("all");
  const [search, setSearch] = useState("");

  // Reset filter to "all" whenever a new preview lands so a stale
  // filter doesn't hide every row.
  useEffect(() => {
    setFilter("all");
    setSearch("");
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

  // Apply filter + search to row visibility.
  const visibleIndexes = useMemo<Set<number> | null>(() => {
    if (!preview) return null;
    const q = search.trim().toLowerCase();
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
      indexes.add(i);
    });
    return indexes;
  }, [preview, edits, filter, search]);

  return (
    <div className="template-workspace">
      <div className="template-summary-bar">
        <SummaryStat label="Files" value={summary.files} />
        <SummaryStat label="Invoices" value={summary.invoices} />
        <SummaryStat label="Rows" value={summary.rows} />
        <SummaryStat
          label="Flagged"
          value={summary.flagged}
          tone={summary.flagged > 0 ? "warn" : "neutral"}
        />
        <SummaryStat
          label="Edited"
          value={summary.edited}
          tone={summary.edited > 0 ? "info" : "neutral"}
        />
        <SummaryStat
          label="Missing link"
          value={summary.urlsMissing}
          tone={summary.urlsMissing > 0 ? "warn" : "neutral"}
        />
        <SummaryStat
          label="Total"
          value={`$${summary.total.toFixed(2)}`}
          tone="strong"
        />
      </div>

      <div className="template-controls">
        <div className="view-presets" role="tablist" aria-label="View preset">
          <button
            role="tab"
            aria-selected={view === "required"}
            className={`view-preset-btn ${view === "required" ? "active" : ""}`}
            onClick={() => setView("required")}
          >
            Required
          </button>
          <button
            role="tab"
            aria-selected={view === "review"}
            className={`view-preset-btn ${view === "review" ? "active" : ""}`}
            onClick={() => setView("review")}
          >
            Review
          </button>
          <button
            role="tab"
            aria-selected={view === "full"}
            className={`view-preset-btn ${view === "full" ? "active" : ""}`}
            onClick={() => setView("full")}
          >
            Full template
          </button>
        </div>

        <div className="template-controls-spacer" />

        <input
          type="search"
          className="template-search"
          placeholder="Search invoice / property / location…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          aria-label="Search rows"
        />

        <select
          className="template-filter-select"
          value={filter}
          onChange={(e) => setFilter(e.target.value as RowFilter)}
          aria-label="Row filter"
        >
          {FILTERS.map((f) => (
            <option key={f.key} value={f.key}>
              {f.label}
            </option>
          ))}
        </select>
      </div>

      <ResManTemplatePreview
        preview={curatedPreview}
        edits={edits}
        onCellEdit={onCellEdit}
        visibleRowIndexes={visibleIndexes}
        selectedRowIndex={selectedRowIndex}
        onSelectRow={onSelectRow}
      />
    </div>
  );
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
