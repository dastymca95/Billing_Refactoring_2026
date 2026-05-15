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

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { api, getFriendlyErrorMessage } from "../api";
import type {
  AiGlCandidatesResponse,
  AiPropertyCandidatesResponse,
  AiVendorCandidatesResponse,
  BatchProgress,
  PreviewResponse,
  PreviewRow,
} from "../types";
import { ColumnFilterMenu } from "./ColumnFilterMenu";
import { GroupedTotalsTable } from "./GroupedTotalsTable";
import { ProcessingTimeline } from "./ProcessingTimeline";
import {
  ResManTemplatePreview,
  type CellEdits,
  type GridCellSuggestionConfig,
} from "./ResManTemplatePreview";
import { TemplateLoadingState } from "./TemplateLoadingState";

type ViewPreset = "required" | "review" | "full";
type TemplateViewMode = "bulk" | "single";
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
  onAddPreviewRow?: (row: PreviewRow, afterRowIndex?: number) => void;
  batchId?: string | null;
  onAiMappingApplied?: () => Promise<void> | void;
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
  onAddPreviewRow,
  batchId,
  onAiMappingApplied,
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
  const [templateMode, setTemplateMode] = useState<TemplateViewMode>("bulk");
  const [singleInvoiceIndex, setSingleInvoiceIndex] = useState(0);
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
  const [aiReviewError, setAiReviewError] = useState("");
  const [aiReviewSaving, setAiReviewSaving] = useState("");
  const [vendorSearch, setVendorSearch] = useState("");
  const [vendorCandidates, setVendorCandidates] =
    useState<AiVendorCandidatesResponse | null>(null);
  const [glCandidates, setGlCandidates] = useState<
    Record<number, AiGlCandidatesResponse>
  >({});
  const [saveVendorForFuture, setSaveVendorForFuture] = useState(true);
  const [saveGlForFuture, setSaveGlForFuture] = useState(true);
  const [applySimilarGl, setApplySimilarGl] = useState(false);
  const [bulkSuggestionGeneration, setBulkSuggestionGeneration] = useState(0);
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

  const invoiceGroups = useMemo(() => buildInvoiceGroups(preview), [preview]);
  const activeInvoiceGroup = invoiceGroups[singleInvoiceIndex] ?? invoiceGroups[0] ?? null;

  useEffect(() => {
    if (singleInvoiceIndex <= invoiceGroups.length - 1) return;
    setSingleInvoiceIndex(Math.max(0, invoiceGroups.length - 1));
  }, [invoiceGroups.length, singleInvoiceIndex]);

  useEffect(() => {
    if (selectedRowIndex == null || templateMode !== "single") return;
    const nextIndex = invoiceGroups.findIndex((group) =>
      group.rowIndexes.includes(selectedRowIndex),
    );
    if (nextIndex >= 0 && nextIndex !== singleInvoiceIndex) {
      setSingleInvoiceIndex(nextIndex);
    }
  }, [invoiceGroups, selectedRowIndex, singleInvoiceIndex, templateMode]);

  const openSingleModeForSelection = () => {
    if (selectedRowIndex != null) {
      const nextIndex = invoiceGroups.findIndex((group) =>
        group.rowIndexes.includes(selectedRowIndex),
      );
      if (nextIndex >= 0) setSingleInvoiceIndex(nextIndex);
    }
    setTemplateMode("single");
  };

  const commitInvoiceField = (
    group: InvoiceGroup,
    columnKey: string,
    newValue: unknown,
  ) => {
    group.rowIndexes.forEach((rowIndex) => onCellEdit(rowIndex, columnKey, newValue));
  };

  const aiReviewTargets = useMemo(() => {
    const rows = preview?.rows ?? [];
    const vendorRow = rows.find((row) =>
      row._meta?.ai_generated &&
      ((row._meta.ai_validation_flags ?? []).includes("vendor_mapping_not_found") ||
        (row._meta.ai_validation_flags ?? []).includes("vendor_mapping_required")),
    );
    const glRows = rows
      .map((row, index) => ({ row, index }))
      .filter(({ row }) =>
        row._meta?.ai_generated &&
        ((row._meta.ai_validation_flags ?? []).includes("ambiguous_gl_mapping") ||
          (row._meta.ai_validation_flags ?? []).includes("gl_mapping_required")),
      )
      .slice(0, 4);
    return {
      vendorRow: vendorRow || null,
      glRows,
    };
  }, [preview]);

  useEffect(() => {
    let cancelled = false;
    const detected = String(
      aiReviewTargets.vendorRow?._meta?.ai_detected_vendor ||
        aiReviewTargets.vendorRow?.Vendor ||
        "",
    ).trim();
    setVendorSearch(detected);
    setAiReviewError("");
    if (!detected) {
      setVendorCandidates(null);
      return;
    }
    void api
      .aiVendorCandidates(detected)
      .then((res) => {
        if (!cancelled) setVendorCandidates(res);
      })
      .catch((e) => {
        if (!cancelled) setAiReviewError(getFriendlyErrorMessage(e, "Load vendor candidates"));
      });
    return () => {
      cancelled = true;
    };
  }, [aiReviewTargets.vendorRow]);

  useEffect(() => {
    let cancelled = false;
    setGlCandidates({});
    const targets = aiReviewTargets.glRows;
    if (!targets.length) return;
    void Promise.all(
      targets.map(async ({ row, index }) => {
        const res = await api.aiGlCandidates({
          line_item_description: String(row["Line Item Description"] || row["Invoice Description"] || ""),
          vendor_name: String(row.Vendor || row._meta?.ai_detected_vendor || ""),
          ai_suggested_gl: String(row["GL Account"] || row._meta?.ai_source_gl_candidate || ""),
        });
        return [index, res] as const;
      }),
    )
      .then((items) => {
        if (cancelled) return;
        setGlCandidates(Object.fromEntries(items));
      })
      .catch((e) => {
        if (!cancelled) setAiReviewError(getFriendlyErrorMessage(e, "Load GL candidates"));
      });
    return () => {
      cancelled = true;
    };
  }, [aiReviewTargets.glRows]);

  useEffect(() => {
    if (!preview || !vendorCandidates?.candidates.length) return;
    const top = vendorCandidates.candidates[0];
    if (!top || top.score < 0.9) return;
    preview.rows.forEach((row, rowIndex) => {
      if (!row._meta?.ai_generated) return;
      const current = String(cellValue(row, edits, rowIndex, "Vendor") ?? "").trim();
      if (current) return;
      const detected = String(row._meta?.ai_detected_vendor || "").trim();
      if (!detected) return;
      onCellEdit(rowIndex, "Vendor", top.vendor_name);
    });
  }, [preview, vendorCandidates, edits, onCellEdit]);

  useEffect(() => {
    if (!preview) return;
    Object.entries(glCandidates).forEach(([rowIndexText, result]) => {
      const rowIndex = Number(rowIndexText);
      const row = preview.rows[rowIndex];
      if (!row?._meta?.ai_generated) return;
      const current = String(cellValue(row, edits, rowIndex, "GL Account") ?? "").trim();
      if (current) return;
      const candidate = bestValidatedGlCandidate(result?.candidates ?? []);
      const code = String(candidate?.gl_code || candidate?.gl_account || "").trim();
      if (!code) return;
      onCellEdit(rowIndex, "GL Account", code);
    });
  }, [preview, glCandidates, edits, onCellEdit]);

  const acceptVendorCandidate = async (
    candidateName: string,
    vendorId = "",
  ) => {
    if (!batchId || !aiReviewTargets.vendorRow || !preview) return;
    const rowIndex = preview.rows.indexOf(aiReviewTargets.vendorRow);
    setAiReviewSaving(`vendor:${candidateName}`);
    setAiReviewError("");
    try {
      await api.applyAiVendorMapping(batchId, {
        detected_vendor: String(
          aiReviewTargets.vendorRow._meta?.ai_detected_vendor ||
            aiReviewTargets.vendorRow.Vendor ||
            "",
        ),
        selected_vendor_name: candidateName,
        vendor_id: vendorId,
        row_index: rowIndex,
        save_for_future: saveVendorForFuture,
        apply_scope: "current_invoice",
      });
      await onAiMappingApplied?.();
    } catch (e) {
      setAiReviewError(getFriendlyErrorMessage(e, "Save vendor mapping"));
    } finally {
      setAiReviewSaving("");
    }
  };

  const searchVendorCandidates = async () => {
    if (!vendorSearch.trim()) return;
    setAiReviewSaving("vendor-search");
    setAiReviewError("");
    try {
      setVendorCandidates(await api.aiVendorCandidates(vendorSearch.trim()));
    } catch (e) {
      setAiReviewError(getFriendlyErrorMessage(e, "Search vendors"));
    } finally {
      setAiReviewSaving("");
    }
  };

  const acceptGlCandidate = async (
    rowIndex: number,
    candidate: { gl_account: string; gl_name?: string },
  ) => {
    if (!batchId) return;
    const row = preview?.rows[rowIndex];
    setAiReviewSaving(`gl:${rowIndex}:${candidate.gl_account}`);
    setAiReviewError("");
    try {
      await api.applyAiGlMapping(batchId, {
        row_index: rowIndex,
        gl_account: candidate.gl_account,
        gl_name: candidate.gl_name,
        save_for_future: saveGlForFuture,
        apply_to_similar: applySimilarGl,
        pattern: String(row?.["Line Item Description"] || ""),
      });
      await onAiMappingApplied?.();
    } catch (e) {
      setAiReviewError(getFriendlyErrorMessage(e, "Save GL mapping"));
    } finally {
      setAiReviewSaving("");
    }
  };

  const getBulkCellAiSuggestions = (
    rowIndex: number,
    column: string,
    row: PreviewRow,
  ): GridCellSuggestionConfig | null => {
    if (!row._meta?.ai_generated) return null;
    const flags = row._meta.ai_validation_flags ?? [];
    const regenerate = () => setBulkSuggestionGeneration((value) => value + 1);

    if (column === "Vendor") {
      const detectedVendor = String(row._meta?.ai_detected_vendor || row.Vendor || "").trim();
      const items =
        vendorCandidates?.candidates.map((candidate) => ({
          label: candidate.vendor_name,
          value: candidate.vendor_name,
          detail: `${Math.round(candidate.score * 100)}% · ${candidate.reason}`,
        })) ?? (detectedVendor
          ? [
              {
                label: detectedVendor,
                value: detectedVendor,
                detail: "Detected on invoice · confirm before export",
              },
            ]
          : []);
      if (!items.length && !flags.some((flag) => flag.includes("vendor"))) return null;
      return {
        title: "Vendor suggestions",
        items,
        emptyText: "No vendor matches yet",
        onApply: (value) => onCellEdit(rowIndex, "Vendor", value),
        onRegenerate: searchVendorCandidates,
      };
    }

    if (column === "GL Account") {
      const items = toGlSuggestionItems(
        rotateItems(glCandidates[rowIndex]?.candidates ?? [], bulkSuggestionGeneration),
      );
      if (!items.length && !flags.some((flag) => flag.includes("gl"))) return null;
      return {
        title: "GL suggestions",
        items,
        emptyText: "No GL suggestions yet",
        onApply: (value) => onCellEdit(rowIndex, "GL Account", value),
        onRegenerate: regenerate,
      };
    }

    if (column === "Invoice Description") {
      const suggestions = buildInvoiceDescriptionSuggestions({
        invoiceDate: String(row["Invoice Date"] || ""),
        vendor: String(row.Vendor || row._meta?.ai_detected_vendor || ""),
        property: String(row["Property Abbreviation"] || row._meta?.ai_property_candidate || ""),
        itemDescription: sourceLineDescription(row),
        invoiceNumber: String(row["Invoice Number"] || ""),
        variant: bulkSuggestionGeneration,
      });
      return {
        title: "Invoice description suggestions",
        items: toSuggestionItems(suggestions),
        onApply: (value) => onCellEdit(rowIndex, "Invoice Description", value),
        onRegenerate: regenerate,
      };
    }

    if (column === "Line Item Description") {
      const suggestions = buildLineDescriptionSuggestions(
        sourceLineDescription(row) || String(row["Line Item Description"] || ""),
        bulkSuggestionGeneration,
      );
      return {
        title: "Line description suggestions",
        items: toSuggestionItems(suggestions),
        onApply: (value) => onCellEdit(rowIndex, "Line Item Description", value),
        onRegenerate: regenerate,
      };
    }

    return null;
  };

  const getBulkCellDisplayValue = (
    rowIndex: number,
    column: string,
    row: PreviewRow,
    value: unknown,
  ): unknown => {
    if (!row._meta?.ai_generated) return value;
    const text = String(value ?? "").trim();

    if (column === "Vendor" && !text) {
      const top = vendorCandidates?.candidates?.[0];
      return top?.vendor_name || row._meta.ai_detected_vendor || value;
    }

    if (column === "GL Account") {
      const candidates = glCandidates[rowIndex]?.candidates ?? [];
      const code = text || String(
        bestValidatedGlCandidate(candidates)?.gl_code ||
          bestValidatedGlCandidate(candidates)?.gl_account ||
          "",
      ).trim();
      const name = glNameForCode(code, candidates);
      if (code && name) return `${code} - ${name}`;
      return code || value;
    }

    return value;
  };

  const getBulkDocumentUrl = (row: PreviewRow) =>
    resolveDocumentUrlForRow(batchId, row);

  const editedCount = summary.edited;
  const canExport = !!onExport && !!preview && summary.rows > 0;
  // Phase 2C — titleMeta retired in favour of the breadcrumb header.

  return (
    <div
      className={`template-workspace ${isSwitchingBatch ? "is-switching" : ""}`}
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
          aria-label="Template view mode"
          data-testid="template-view-tabs"
        >
          <span className="template-controls-label" aria-hidden>
            View:
          </span>
          <button
            role="tab"
            aria-selected={templateMode === "bulk"}
            className={`view-preset-btn ${templateMode === "bulk" ? "active" : ""}`}
            onClick={() => setTemplateMode("bulk")}
            title="Bulk template grid optimized for export review."
            data-testid="template-mode-bulk"
          >
            Bulk
          </button>
          <button
            role="tab"
            aria-selected={templateMode === "single"}
            className={`view-preset-btn ${templateMode === "single" ? "active" : ""}`}
            onClick={openSingleModeForSelection}
            title="Single invoice review screen."
            data-testid="template-mode-single"
          >
            Single invoice
          </button>
        </div>

        {templateMode === "bulk" && (
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
        )}

        <div className="template-filter-tools">
          {templateMode === "single" && activeInvoiceGroup && (
            <div className="single-invoice-nav" data-testid="single-invoice-nav">
              <button
                type="button"
                className="btn btn-compact"
                disabled={singleInvoiceIndex <= 0}
                onClick={() => setSingleInvoiceIndex((v) => Math.max(0, v - 1))}
              >
                Previous
              </button>
              <span>
                Invoice {singleInvoiceIndex + 1} of {invoiceGroups.length}
              </span>
              <button
                type="button"
                className="btn btn-compact"
                disabled={singleInvoiceIndex >= invoiceGroups.length - 1}
                onClick={() =>
                  setSingleInvoiceIndex((v) => Math.min(invoiceGroups.length - 1, v + 1))
                }
              >
                Next
              </button>
            </div>
          )}
          {templateMode === "bulk" && (
          <>
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
          </>
          )}
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

      {templateMode === "single" ? (
        activeInvoiceGroup ? (
          <SingleInvoiceMode
            batchId={batchId}
            readOnly={readOnly === true}
            onRefresh={onAiMappingApplied}
            group={activeInvoiceGroup}
            edits={edits}
            selectedRowIndex={selectedRowIndex}
            onSelectRow={onSelectRow}
            onCellEdit={onCellEdit}
            onAddLineItem={onAddPreviewRow}
            onGroupFieldEdit={commitInvoiceField}
            onReturnToBulk={() => setTemplateMode("bulk")}
          />
        ) : (
          <div className="card template-grid-card" data-testid="single-invoice-mode">
            <div className="empty-state">No invoices are available for single invoice review.</div>
          </div>
        )
      ) : groupBy && curatedPreview ? (
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
          getCellAiSuggestions={getBulkCellAiSuggestions}
          getCellDisplayValue={getBulkCellDisplayValue}
          getDocumentUrl={getBulkDocumentUrl}
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

type InvoiceGroup = {
  key: string;
  label: string;
  rowIndexes: number[];
  rows: PreviewRow[];
  firstRow: PreviewRow;
};

type FieldSuggestion = {
  label: string;
  value: string;
  detail?: string;
  disabled?: boolean;
};

function buildInvoiceGroups(preview: PreviewResponse | null): InvoiceGroup[] {
  if (!preview) return [];
  const groups = new Map<string, InvoiceGroup>();
  preview.rows.forEach((row, index) => {
    const key = String(
      row._meta?.invoice_group_id ||
        `${row._meta?.source_file || ""}|${row["Invoice Number"] || ""}|${row.Vendor || row._meta?.ai_detected_vendor || ""}`,
    );
    const label = String(row["Invoice Number"] || `Invoice ${groups.size + 1}`);
    const existing = groups.get(key);
    if (existing) {
      existing.rowIndexes.push(index);
      existing.rows.push(row);
    } else {
      groups.set(key, {
        key,
        label,
        rowIndexes: [index],
        rows: [row],
        firstRow: row,
      });
    }
  });
  return Array.from(groups.values());
}

function SingleInvoiceMode({
  batchId,
  readOnly,
  onRefresh,
  group,
  edits,
  selectedRowIndex,
  onSelectRow,
  onCellEdit,
  onAddLineItem,
  onGroupFieldEdit,
  onReturnToBulk,
}: {
  batchId?: string | null;
  readOnly?: boolean;
  onRefresh?: () => Promise<void> | void;
  group: InvoiceGroup;
  edits: CellEdits;
  selectedRowIndex: number | null;
  onSelectRow: (rowIndex: number | null) => void;
  onCellEdit: (rowIndex: number, columnKey: string, newValue: unknown) => void;
  onAddLineItem?: (row: PreviewRow, afterRowIndex?: number) => void;
  onGroupFieldEdit: (
    group: InvoiceGroup,
    columnKey: string,
    newValue: unknown,
  ) => void;
  onReturnToBulk: () => void;
}) {
  const firstIndex = group.rowIndexes[0] ?? 0;
  const first = group.firstRow;
  const [propertyQuery, setPropertyQuery] = useState("");
  const [propertyCandidates, setPropertyCandidates] =
    useState<AiPropertyCandidatesResponse | null>(null);
  const [showPropertyCandidates, setShowPropertyCandidates] = useState(false);
  const [vendorCandidates, setVendorCandidates] =
    useState<AiVendorCandidatesResponse | null>(null);
  const [lineGlCandidates, setLineGlCandidates] = useState<
    Record<number, AiGlCandidatesResponse>
  >({});
  const [saving, setSaving] = useState("");
  const [error, setError] = useState("");
  const [visionNotice, setVisionNotice] = useState("");
  const [resolved, setResolved] = useState<Set<string>>(() => new Set());
  const [suggestionGeneration, setSuggestionGeneration] = useState(0);
  const [reviewExpanded, setReviewExpanded] = useState(false);
  const [saveGlForFuture, setSaveGlForFuture] = useState(true);
  const [applySimilarGl, setApplySimilarGl] = useState(false);
  const [taxPolicy, setTaxPolicy] = useState<
    "distribute_proportionally" | "separate_tax_line" | "exclude_tax" | "manual_review"
  >("distribute_proportionally");
  const reasons = uniqueStrings(
    group.rows.flatMap((row) => row._meta?.manual_review_reasons ?? []),
  );
  const rawFlags = uniqueStrings(
    group.rows.flatMap((row) => row._meta?.ai_validation_flags ?? []),
  );
  const flags = uniqueStrings([
    ...rawFlags,
    ...deriveRequiredReviewFlags(group, edits, reasons),
  ]);
  const lineTotal = group.rowIndexes.reduce((sum, rowIndex, idx) => {
    const value = cellValue(group.rows[idx], edits, rowIndex, "Amount");
    const n = typeof value === "number" ? value : Number(value);
    return sum + (Number.isFinite(n) ? n : 0);
  }, 0);
  const baseLineAmounts = useMemo(
    () => group.rows.map((row) => readAiProvenanceNumber(row, "base_amount") ?? parseMoneyLike(row.Amount, 0)),
    [group.key, group.rows],
  );
  const baseLineTotal = baseLineAmounts.reduce((sum, amount) => sum + amount, 0);
  const merchandiseSubtotal = inferMerchandiseSubtotal(first, lineTotal);
  const taxAmount = inferTaxAmount(first, merchandiseSubtotal);
  const baseInvoiceTotal = inferInvoiceTotal(first, merchandiseSubtotal + taxAmount);
  const editedInvoiceTotal = edits[firstIndex]?.["Invoice Total"];
  const rowInvoiceTotal = parseMoneyLike(first["Invoice Total"], Number.NaN);
  const invoiceTotal =
    editedInvoiceTotal !== undefined
      ? parseMoneyLike(editedInvoiceTotal, baseInvoiceTotal)
      : Number.isFinite(rowInvoiceTotal) && rowInvoiceTotal > 0
        ? rowInvoiceTotal
        : baseInvoiceTotal;
  const resmanLineTotal = lineTotal;
  const difference = invoiceTotal - resmanLineTotal;
  const taxReviewPending =
    flags.includes("tax_handling_requires_review") ||
    flags.includes("tax_gl_mapping_required") ||
    first._meta?.ai_tax_handling === "manual_review";
  const tasks = buildReviewTasks(flags, reasons);
  const blockingCount = tasks.filter((task) => task.blocking && !resolved.has(task.code)).length;
  const visibleTasks = tasks.filter((task) => !resolved.has(task.code));
  const reviewSummary =
    visibleTasks.length > 0
      ? visibleTasks.slice(0, 3).map((task) => task.title).join(" · ")
      : "No review blockers";
  const readyBlockerTitle =
    blockingCount > 0
      ? `Blocked by: ${visibleTasks.filter((task) => task.blocking).map((task) => task.title).join(", ")}`
      : "Invoice ready to export.";
  const statusLabel = blockingCount > 0 ? "Needs review" : "Ready";
  const vendorText = String(first.Vendor || first._meta?.ai_detected_vendor || "Not set");
  const billOrCredit = String(cellValue(first, edits, firstIndex, "Bill or Credit") || "Bill");
  const invoiceDate = formatDateForDisplay(cellValue(first, edits, firstIndex, "Invoice Date"));
  const dueDate = formatDateForDisplay(cellValue(first, edits, firstIndex, "Due Date"));
  const accountingDate = formatDateForDisplay(cellValue(first, edits, firstIndex, "Accounting Date"));
  const receivedDate = accountingDate || invoiceDate;
  const description = cellValue(first, edits, firstIndex, "Invoice Description");
  const expenseType = String(cellValue(first, edits, firstIndex, "Expense Type") || "General");
  const sourceFiles = uniqueStrings(group.rows.map((row) => row._meta?.source_file || ""));
  const historyDate = invoiceDate || accountingDate || dueDate || "";
  const enteredActivity = first._meta?.ai_generated
    ? `Entered by AI-assisted extraction${first._meta.ai_confidence != null ? ` (${formatConfidence(first._meta.ai_confidence)})` : ""}`
    : "Entered by web console";
  const selectedLineIndex = group.rowIndexes.includes(selectedRowIndex ?? -1)
    ? selectedRowIndex ?? firstIndex
    : firstIndex;
  const selectedLinePosition = Math.max(0, group.rowIndexes.indexOf(selectedLineIndex));
  const selectedLine = group.rows[selectedLinePosition] ?? first;
  const selectedLineSourceDescription = sourceLineDescription(selectedLine);
  const selectedLineDescription = String(
    cellValue(selectedLine, edits, selectedLineIndex, "Line Item Description") || "",
  );
  const selectedLineCategory = inferItemCategory(
    selectedLineSourceDescription || selectedLineDescription,
  );
  const invoiceDescriptionSuggestions = buildInvoiceDescriptionSuggestions({
    invoiceDate,
    vendor: vendorText,
    property: String(cellValue(first, edits, firstIndex, "Property Abbreviation") || ""),
    itemDescription: selectedLineSourceDescription || selectedLineDescription,
    invoiceNumber: String(cellValue(first, edits, firstIndex, "Invoice Number") || ""),
    variant: suggestionGeneration,
  });
  const invoiceDescriptionSuggestionItems = toSuggestionItems(invoiceDescriptionSuggestions);
  const aiNarrative = buildAiInvoiceNarrative({
    vendor: vendorText,
    category: selectedLineCategory,
    itemDescription: selectedLineSourceDescription || selectedLineDescription,
    property: String(cellValue(first, edits, firstIndex, "Property Abbreviation") || ""),
    serviceAddress: String(first._meta?.ai_service_address || ""),
    confidence: first._meta?.ai_confidence,
  });
  const sourceFile = String(first._meta?.source_file || "");
  const sourceDocumentUrl = resolveDocumentUrlForRow(batchId, first, edits, firstIndex);
  const sourcePage = Number(first._meta?.source_page || 1);
  const topPropertyCandidate = propertyCandidates?.candidates?.[0] || null;
  const currentProperty = String(cellValue(first, edits, firstIndex, "Property Abbreviation") ?? "").trim();

  const addLineItem = () => {
    if (!onAddLineItem) return;
    const lastRowIndex = group.rowIndexes[group.rowIndexes.length - 1] ?? firstIndex;
    onAddLineItem(buildAddedLineItemRow(group, edits), lastRowIndex);
  };
  const regenerateSuggestions = () => setSuggestionGeneration((value) => value + 1);

  const applyDistributedAmounts = () => {
    const adjustment = Number((invoiceTotal - baseLineTotal).toFixed(2));
    if (Math.abs(adjustment) <= 0.009 || baseLineTotal <= 0) return;
    const distributed = distributeAmountAcrossLines(baseLineAmounts, adjustment);
    group.rowIndexes.forEach((rowIndex, idx) => {
      const amount = distributed[idx];
      onCellEdit(rowIndex, "Amount", amount);
      const qty = parseMoneyLike(cellValue(group.rows[idx], edits, rowIndex, "Quantity"), 1);
      if (qty > 0) {
        onCellEdit(rowIndex, "Unit Price", Number((amount / qty).toFixed(2)));
      }
    });
  };

  const restoreBaseAmounts = () => {
    group.rowIndexes.forEach((rowIndex, idx) => {
      const amount = Number((baseLineAmounts[idx] || 0).toFixed(2));
      onCellEdit(rowIndex, "Amount", amount);
      const qty = parseMoneyLike(cellValue(group.rows[idx], edits, rowIndex, "Quantity"), 1);
      if (qty > 0) {
        onCellEdit(rowIndex, "Unit Price", Number((amount / qty).toFixed(2)));
      }
    });
  };

  useEffect(() => {
    setPropertyQuery(String(first["Property Abbreviation"] || first._meta?.ai_property_candidate || ""));
    setPropertyCandidates(null);
    setShowPropertyCandidates(false);
    setVendorCandidates(null);
    setLineGlCandidates({});
    setResolved(new Set());
    setError("");
    setVisionNotice("");
    setReviewExpanded(false);
    setTaxPolicy("distribute_proportionally");
  }, [group.key, first]);

  useEffect(() => {
    if (taxPolicy !== "distribute_proportionally") return;
    const adjustment = invoiceTotal - lineTotal;
    if (Math.abs(adjustment) <= 0.009) return;
    if (baseLineTotal <= 0) return;
    applyDistributedAmounts();
  }, [taxPolicy, invoiceTotal, lineTotal, baseLineTotal, group.key]);

  useEffect(() => {
    let cancelled = false;
    const targets = group.rows.map((row, idx) => ({ row, rowIndex: group.rowIndexes[idx] }));
    if (!targets.length) return;
    void Promise.all(
      targets.map(async ({ row, rowIndex }) => {
        const res = await api.aiGlCandidates({
          line_item_description: String(row["Line Item Description"] || ""),
          vendor_name: String(row.Vendor || row._meta?.ai_detected_vendor || ""),
          ai_suggested_gl: String(row["GL Account"] || row._meta?.ai_source_gl_candidate || ""),
        });
        return [rowIndex, res] as const;
      }),
    )
      .then((items) => {
        if (!cancelled) setLineGlCandidates(Object.fromEntries(items));
      })
      .catch((e) => {
        if (!cancelled) setError(getFriendlyErrorMessage(e, "Load GL choices"));
      });
    return () => {
      cancelled = true;
    };
  }, [group]);

  useEffect(() => {
    let cancelled = false;
    const query = String(
      first["Property Abbreviation"] ||
        first._meta?.ai_property_candidate ||
        first._meta?.ai_service_address ||
        "",
    ).trim();
    const serviceAddress = String(first._meta?.ai_service_address || "").trim();
    if (!query && !serviceAddress) return;
    void api
      .aiPropertyCandidates({
        query,
        service_address: serviceAddress,
      })
      .then((res) => {
        if (!cancelled) setPropertyCandidates(res);
      })
      .catch(() => {
        if (!cancelled) {
          // The explicit Resolve button still surfaces errors; the automatic
          // prefill should stay quiet so detached review opens cleanly.
          setPropertyCandidates(null);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [group.key, first]);

  useEffect(() => {
    const candidates = propertyCandidates?.candidates ?? [];
    if (!candidates.length) return;
    const currentProperty = String(cellValue(first, edits, firstIndex, "Property Abbreviation") ?? "").trim();
    if (currentProperty) return;
    const strong = candidates.filter((candidate) => candidate.score >= 0.9 && candidate.property_abbreviation);
    if (!strong.length) return;
    const abbreviations = uniqueStrings(strong.map((candidate) => candidate.property_abbreviation));
    if (abbreviations.length !== 1) return;
    const locations = uniqueStrings(strong.map((candidate) => candidate.location).filter(Boolean));
    group.rowIndexes.forEach((rowIndex) => {
      onCellEdit(rowIndex, "Property Abbreviation", abbreviations[0]);
      if (locations.length === 1) {
        onCellEdit(rowIndex, "Location", locations[0]);
      }
    });
    setResolved((prev) => new Set([...prev, "property_mapping_required"]));
  }, [propertyCandidates, group.key]);

  useEffect(() => {
    const updates: Array<{ rowIndex: number; gl: string }> = [];
    group.rowIndexes.forEach((rowIndex, idx) => {
      const currentGl = String(cellValue(group.rows[idx], edits, rowIndex, "GL Account") ?? "").trim();
      if (currentGl) return;
      const candidate = (lineGlCandidates[rowIndex]?.candidates ?? []).find(
        (item) =>
          item.valid !== false &&
          item.score >= 0.65 &&
          String(item.gl_code || item.gl_account || "").trim(),
      );
      if (candidate) {
        updates.push({ rowIndex, gl: String(candidate.gl_code || candidate.gl_account).trim() });
      }
    });
    updates.forEach(({ rowIndex, gl }) => onCellEdit(rowIndex, "GL Account", gl));
    if (updates.length > 0) {
      const updatedRows = new Map(updates.map((item) => [item.rowIndex, item.gl]));
      const allRowsHaveGl = group.rowIndexes.every((idx, rowIdx) => {
        const value = updatedRows.get(idx) ?? cellValue(group.rows[rowIdx], edits, idx, "GL Account");
        return String(value ?? "").trim() !== "";
      });
      if (allRowsHaveGl) {
        setResolved((prev) => new Set([...prev, "gl_mapping_required", "ambiguous_gl_mapping"]));
      }
    }
  }, [lineGlCandidates, group.key]);

  const searchProperties = async () => {
    setSaving("property-search");
    setError("");
    try {
      const res = await api.aiPropertyCandidates({
        query: propertyQuery,
        service_address: String(first._meta?.ai_service_address || ""),
      });
      setPropertyCandidates(res);
      setShowPropertyCandidates(true);
    } catch (e) {
      setError(getFriendlyErrorMessage(e, "Search properties"));
    } finally {
      setSaving("");
    }
  };

  const searchVendors = async () => {
    const detected = String(first._meta?.ai_detected_vendor || first.Vendor || "").trim();
    if (!detected) {
      setError("No detected vendor is available for this invoice.");
      return;
    }
    setSaving("vendor-search");
    setError("");
    try {
      const res = await api.aiVendorCandidates(detected);
      setVendorCandidates(res);
    } catch (e) {
      setError(getFriendlyErrorMessage(e, "Search vendors"));
    } finally {
      setSaving("");
    }
  };

  const applyVendor = async (vendorName: string, vendorId = "") => {
    group.rowIndexes.forEach((rowIndex) => {
      onCellEdit(rowIndex, "Vendor", vendorName);
    });
    setResolved((prev) => new Set([...prev, "vendor_mapping_required", "vendor_mapping_not_found"]));
    if (!batchId || readOnly) return;
    setSaving(`vendor:${vendorName}`);
    setError("");
    try {
      await api.applyAiVendorMapping(batchId, {
        detected_vendor: String(first._meta?.ai_detected_vendor || first.Vendor || vendorName),
        selected_vendor_name: vendorName,
        vendor_id: vendorId,
        row_index: firstIndex,
        save_for_future: true,
        apply_scope: "current_invoice",
      });
      await onRefresh?.();
    } catch (e) {
      setError(getFriendlyErrorMessage(e, "Apply vendor"));
    } finally {
      setSaving("");
    }
  };

  const applyProperty = async (
    propertyAbbreviation: string,
    location = "",
    leaveLocationBlank = false,
  ) => {
    group.rowIndexes.forEach((rowIndex) => {
      onCellEdit(rowIndex, "Property Abbreviation", propertyAbbreviation);
      onCellEdit(rowIndex, "Location", leaveLocationBlank ? "" : location);
    });
    setResolved((prev) => new Set([...prev, "property_mapping_required", "location_unresolved"]));
    if (!batchId || readOnly) return;
    setSaving(`property:${propertyAbbreviation}:${location}`);
    setError("");
    try {
      await api.applyAiPropertyLocation(batchId, {
        row_index: firstIndex,
        property_abbreviation: propertyAbbreviation,
        location,
        service_address: String(first._meta?.ai_service_address || ""),
        leave_location_blank: leaveLocationBlank,
        apply_scope: "current_invoice",
      });
      await onRefresh?.();
    } catch (e) {
      setError(getFriendlyErrorMessage(e, "Apply property"));
    } finally {
      setSaving("");
    }
  };

  const applyGl = async (rowIndex: number, glAccount: string, glName = "") => {
    onCellEdit(rowIndex, "GL Account", glAccount);
    const allRowsWillHaveGl = group.rowIndexes.every((idx, rowIdx) => {
      const value = idx === rowIndex
        ? glAccount
        : cellValue(group.rows[rowIdx], edits, idx, "GL Account");
      return String(value ?? "").trim() !== "";
    });
    if (allRowsWillHaveGl) {
      setResolved((prev) => new Set([...prev, "gl_mapping_required", "ambiguous_gl_mapping"]));
    }
    if (!batchId || readOnly) return;
    setSaving(`gl:${rowIndex}`);
    setError("");
    try {
      await api.applyAiGlMapping(batchId, {
        row_index: rowIndex,
        gl_account: glAccount,
        gl_name: glName,
        save_for_future: saveGlForFuture,
        apply_to_similar: applySimilarGl,
        pattern: String(group.rows[group.rowIndexes.indexOf(rowIndex)]?.["Line Item Description"] || ""),
      });
      await onRefresh?.();
    } catch (e) {
      setError(getFriendlyErrorMessage(e, "Apply GL"));
    } finally {
      setSaving("");
    }
  };

  const applyTaxPolicy = async (
    policy: "manual_review" | "distribute_proportionally" | "separate_tax_line" | "exclude_tax",
  ) => {
    setTaxPolicy(policy);
    if (policy === "distribute_proportionally") {
      applyDistributedAmounts();
    } else if (policy === "exclude_tax" || policy === "manual_review") {
      restoreBaseAmounts();
    } else if (policy === "separate_tax_line") {
      restoreBaseAmounts();
      const adjustment = Number((invoiceTotal - baseLineTotal).toFixed(2));
      const hasSeparateLine = group.rows.some((row) =>
        String(row["Line Item Description"] || "").toLowerCase().includes("invoice tax / adjustment"),
      );
      if (adjustment > 0.009 && onAddLineItem && !hasSeparateLine) {
        const baseTaxRow = buildAddedLineItemRow(group, edits);
        const taxRow = {
          ...baseTaxRow,
          "Line Item Description": "Invoice tax / adjustment",
          Amount: adjustment,
          "Unit Price": adjustment,
          Tax: true,
          _meta: {
            ...(baseTaxRow._meta ?? {}),
            ...(first._meta ?? {}),
            match_strategy: first._meta?.match_strategy || baseTaxRow._meta?.match_strategy || "manual_line_item",
            match_confidence: first._meta?.match_confidence || baseTaxRow._meta?.match_confidence || "manual",
            service_period_source: first._meta?.service_period_source || baseTaxRow._meta?.service_period_source || "",
            service_period_inferred: Boolean(first._meta?.service_period_inferred ?? baseTaxRow._meta?.service_period_inferred),
            support_document_status: first._meta?.support_document_status || baseTaxRow._meta?.support_document_status || "",
            ai_generated: false,
            ai_tax_handling: "separate_tax_line",
            ai_validation_flags: ["tax_gl_mapping_required", "gl_mapping_required"],
            manual_review_reasons: ["Separate tax/adjustment line added. Select a valid GL before export."],
          },
        };
        onAddLineItem(taxRow, group.rowIndexes[group.rowIndexes.length - 1] ?? firstIndex);
      }
    }
    setResolved((prev) => new Set([...prev, "tax_handling_requires_review", "tax_gl_mapping_required"]));
    if (!batchId || readOnly) return;
    setSaving(`tax:${policy}`);
    setError("");
    try {
      await api.applyAiTaxPolicy(batchId, { row_index: firstIndex, policy });
      await onRefresh?.();
    } catch (e) {
      setError(getFriendlyErrorMessage(e, "Apply tax policy"));
    } finally {
      setSaving("");
    }
  };

  const runVisionAssist = async () => {
    if (!batchId || !sourceFile) {
      setError("Select a source PDF before using vision assist.");
      return;
    }
    setSaving("vision");
    setError("");
    setVisionNotice("");
    try {
      const result = await api.aiVisionAssist(batchId, {
        filename: sourceFile,
        page_numbers: [Number.isFinite(sourcePage) && sourcePage > 0 ? sourcePage : 1],
        vendor_hint: vendorText,
      });
      const traces = result.trace_regions?.length ?? 0;
      const conflicts = result.validation.text_vision_conflict_fields?.length ?? 0;
      setVisionNotice(
        conflicts > 0
          ? `Vision assist found ${conflicts} text/vision conflict${conflicts === 1 ? "" : "s"}. Review before export.`
          : `Vision assist added ${traces} candidate trace${traces === 1 ? "" : "s"} for this invoice.`,
      );
    } catch (e) {
      setError(getFriendlyErrorMessage(e, "Use vision assist"));
    } finally {
      setSaving("");
    }
  };

  return (
    <section className="single-invoice-mode card resman-invoice-mode" data-testid="single-invoice-mode">
      <div className="resman-invoice-titlebar">
        <span>Invoice</span>
        <div className="single-invoice-actions">
          <span className={`single-status-pill ${blockingCount > 0 ? "needs-review" : "ready"}`} data-testid="single-invoice-status">
            {statusLabel}
          </span>
          <button type="button" className="btn btn-compact" onClick={() => onSelectRow(firstIndex)} data-testid="single-invoice-open-source">
            Source
          </button>
          <button type="button" className="btn btn-compact" onClick={() => onSelectRow(firstIndex)}>
            Trace
          </button>
          <button
            type="button"
            className="btn btn-compact"
            onClick={() => void runVisionAssist()}
            disabled={saving === "vision" || readOnly}
            title="Use vision assist for this invoice page"
            data-testid="single-use-vision-assist"
          >
            {saving === "vision" ? "Scanning..." : "Use vision assist"}
          </button>
          <button type="button" className="btn btn-compact" onClick={onReturnToBulk}>
            Bulk
          </button>
        </div>
      </div>

      <div className="single-invoice-body resman-invoice-body">
        <div className="resman-invoice-top">
          <div className="resman-invoice-form">
            <ResManReadonlyField label="Vendor" required value={vendorText} />
            <ResManReadonlyField label="Expense type" required value={expenseType} />
            <ResManEditableField
              label="Number"
              required
              value={cellValue(first, edits, firstIndex, "Invoice Number")}
              onCommit={(value) => onGroupFieldEdit(group, "Invoice Number", value)}
            />
            <div className="resman-form-split">
              <ResManEditableField
                label="Invoice date"
                required
                value={invoiceDate}
                onCommit={(value) => onGroupFieldEdit(group, "Invoice Date", value)}
              />
              <ResManEditableField
                label="Received date"
                required
                value={receivedDate}
                onCommit={(value) => onGroupFieldEdit(group, "Accounting Date", value)}
              />
            </div>
            <div className="resman-form-split">
              <ResManEditableField
                label="Due date"
                required
                value={dueDate}
                onCommit={(value) => onGroupFieldEdit(group, "Due Date", value)}
              />
              <ResManEditableField
                label="Accounting date"
                required
                value={accountingDate}
                onCommit={(value) => onGroupFieldEdit(group, "Accounting Date", value)}
              />
            </div>
            <ResManField label="Hold date">
              <div className="resman-hold-row">
                <input className="resman-input" value="" readOnly aria-label="Hold date" />
                <label>
                  <input type="checkbox" disabled />
                  Indefinite
                </label>
              </div>
            </ResManField>
            <ResManField label="Total" required>
              <div className="resman-total-row" data-testid="single-invoice-primary-total">
                <ResManMoneyInput
                  value={invoiceTotal}
                  ariaLabel="Invoice total"
                  onCommit={(value) => onGroupFieldEdit(group, "Invoice Total", value)}
                />
                <label>
                  <input
                    type="radio"
                    checked={billOrCredit.toLowerCase() !== "credit"}
                    onChange={() => onGroupFieldEdit(group, "Bill or Credit", "Bill")}
                  />
                  Bill
                </label>
                <label>
                  <input
                    type="radio"
                    checked={billOrCredit.toLowerCase() === "credit"}
                    onChange={() => onGroupFieldEdit(group, "Bill or Credit", "Credit")}
                  />
                  Credit
                </label>
              </div>
            </ResManField>
            <ResManReadonlyField label="Status" value={statusLabel} />
            <ResManEditableField
              label="Description"
              required
              value={description}
              onCommit={(value) => onGroupFieldEdit(group, "Invoice Description", value)}
              aiSuggestions={{
                title: "Invoice description suggestions",
                items: invoiceDescriptionSuggestionItems,
                onApply: (value) => onGroupFieldEdit(group, "Invoice Description", value),
                onRegenerate: regenerateSuggestions,
              }}
            />
          </div>

          <aside className="single-context-panel resman-projects-panel">
            <div className="resman-projects-title">
              <span>Projects</span>
              <button type="button">Add</button>
            </div>
            <dl>
              <dt>Property</dt>
              <dd>{displayValue(cellValue(first, edits, firstIndex, "Property Abbreviation"))}</dd>
              <dt>Location</dt>
              <dd>{displayValue(cellValue(first, edits, firstIndex, "Location"))}</dd>
              <dt>Service address</dt>
              <dd>{displayValue(first._meta?.ai_service_address)}</dd>
              <dt>AI confidence</dt>
              <dd>{formatConfidence(first._meta?.ai_confidence)}</dd>
              <dt>Source document</dt>
              <dd>
                {sourceDocumentUrl ? (
                  <a
                    className="single-source-link"
                    href={sourceDocumentUrl}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {sourceFile || "Open source document"}
                  </a>
                ) : (
                  displayValue(first._meta?.source_file)
                )}
              </dd>
            </dl>
            <div className="single-property-resolver resman-property-resolver" data-testid="single-property-resolver">
              <div className="single-resolver-row">
                <input value={propertyQuery} onChange={(e) => setPropertyQuery(e.target.value)} placeholder="Search property or use service address" aria-label="Search property" />
                {!currentProperty && topPropertyCandidate && (
                  <button
                    type="button"
                    className="btn btn-compact btn-accent-light"
                    onClick={() => void applyProperty(topPropertyCandidate.property_abbreviation, topPropertyCandidate.location, false)}
                    title={topPropertyCandidate.reason}
                  >
                    Accept property
                  </button>
                )}
                <button type="button" className="btn btn-compact" onClick={() => void searchProperties()} disabled={saving === "property-search"}>
                  Search
                </button>
              </div>
              {showPropertyCandidates && propertyCandidates && (
                <div className="single-candidate-list">
                  {propertyCandidates.candidates.slice(0, 4).map((candidate) => (
                    <button
                      type="button"
                      key={`${candidate.property_abbreviation}-${candidate.location}-${candidate.address}`}
                      onClick={() => void applyProperty(candidate.property_abbreviation, candidate.location, false)}
                      data-testid="single-property-candidate"
                    >
                      <strong>{candidate.property_abbreviation}</strong>
                      <span>{candidate.location || "Property only"} - {candidate.reason}</span>
                    </button>
                  ))}
                  {propertyCandidates.candidates.length === 0 && <span className="single-empty-choice">No property match found.</span>}
                </div>
              )}
              <button
                type="button"
                className="single-link-button"
                onClick={() => void applyProperty(String(cellValue(first, edits, firstIndex, "Property Abbreviation") || ""), "", true)}
                disabled={!cellValue(first, edits, firstIndex, "Property Abbreviation")}
              >
                Leave location blank
              </button>
            </div>

            <div className="resman-ai-brief">
              <strong>AI summary</strong>
              <p>{aiNarrative}</p>
            </div>
            {visionNotice && <div className="resman-vision-notice">{visionNotice}</div>}

          </aside>
        </div>

        <section className="resman-line-section">
          <div className="resman-line-heading">
            <h4>Line Items</h4>
            <div className="resman-line-options" aria-label="GL mapping options">
              <label><input type="checkbox" checked={saveGlForFuture} onChange={(e) => setSaveGlForFuture(e.target.checked)} /> Save mapping for future</label>
              <label><input type="checkbox" checked={applySimilarGl} onChange={(e) => setApplySimilarGl(e.target.checked)} /> Apply to similar items</label>
            </div>
            <button
              type="button"
              className="btn btn-compact"
              onClick={addLineItem}
              disabled={!onAddLineItem}
            >
              Add line item
            </button>
          </div>
          <div className="single-line-items resman-line-items" data-testid="single-invoice-line-items">
            <table>
              <colgroup>
                <col className="resman-col-property" />
                <col className="resman-col-location" />
                <col className="resman-col-gl" />
                <col className="resman-col-description" />
                <col className="resman-col-unit" />
                <col className="resman-col-quantity" />
                <col className="resman-col-total" />
                <col className="resman-col-tax" />
                <col className="resman-col-add" />
              </colgroup>
              <thead>
                <tr>
                  <th>Property</th>
                  <th>Location</th>
                  <th>GL Account</th>
                  <th>Description</th>
                  <th>Unit price</th>
                  <th>Quantity</th>
                  <th>Total</th>
                  <th>Tax</th>
                  <th aria-label="Add line item" />
                </tr>
              </thead>
              <tbody>
                {group.rows.map((row, idx) => {
                  const rowIndex = group.rowIndexes[idx];
                  const isSelected = selectedRowIndex === rowIndex;
                  const amount = Number(cellValue(row, edits, rowIndex, "Amount") || 0);
                  const currentGl = String(cellValue(row, edits, rowIndex, "GL Account") ?? "").trim();
                  const currentGlName = glNameForCode(currentGl, lineGlCandidates[rowIndex]?.candidates ?? []);
                  const rowSourceDescription = sourceLineDescription(row);
                  const rowGlSuggestions = toGlSuggestionItems(
                    rotateItems(lineGlCandidates[rowIndex]?.candidates ?? [], suggestionGeneration),
                  );
                  const rowLineDescriptionSuggestions = toSuggestionItems(
                    buildLineDescriptionSuggestions(
                      rowSourceDescription ||
                        String(cellValue(row, edits, rowIndex, "Line Item Description") || ""),
                      suggestionGeneration,
                    ),
                  );
                  return (
                    <tr key={rowIndex} className={isSelected ? "selected-row" : ""} onClick={() => onSelectRow(rowIndex)} data-testid="single-invoice-line-item">
                      <SingleLineCell row={row} edits={edits} rowIndex={rowIndex} column="Property Abbreviation" onCellEdit={onCellEdit} />
                      <SingleLineCell row={row} edits={edits} rowIndex={rowIndex} column="Location" onCellEdit={onCellEdit} />
                      <td className="resman-gl-cell">
                        <div className="resman-gl-field">
                          <SingleLineInput
                            row={row}
                            edits={edits}
                            rowIndex={rowIndex}
                            column="GL Account"
                            onCellEdit={onCellEdit}
                            aiSuggestions={{
                              title: "GL account suggestions",
                              items: rowGlSuggestions,
                              emptyText: "Loading GL choices...",
                              onApply: (value, item) =>
                                void applyGl(rowIndex, value, item.detail || ""),
                              onRegenerate: regenerateSuggestions,
                            }}
                          />
                          {currentGlName && <span className="resman-gl-name">{currentGlName}</span>}
                        </div>
                      </td>
                      <SingleLineCell
                        row={row}
                        edits={edits}
                        rowIndex={rowIndex}
                        column="Line Item Description"
                        onCellEdit={onCellEdit}
                        aiSuggestions={{
                          title: "Line description suggestions",
                          items: rowLineDescriptionSuggestions,
                          onApply: (value) => onCellEdit(rowIndex, "Line Item Description", value),
                          onRegenerate: regenerateSuggestions,
                        }}
                      />
                      <SingleLineCell row={row} edits={edits} rowIndex={rowIndex} column="Unit Price" onCellEdit={onCellEdit} numeric fallbackValue={amount} />
                      <SingleLineCell row={row} edits={edits} rowIndex={rowIndex} column="Quantity" onCellEdit={onCellEdit} numeric fallbackValue={1} />
                      <SingleLineCell row={row} edits={edits} rowIndex={rowIndex} column="Amount" onCellEdit={onCellEdit} numeric />
                      <td className="resman-tax-cell"><input type="checkbox" readOnly checked={taxAmount > 0 && idx === 0} /></td>
                      <td className="resman-plus-cell">
                        <button type="button" onClick={(e) => { e.stopPropagation(); addLineItem(); }} disabled={!onAddLineItem}>+</button>
                      </td>
                    </tr>
                  );
                })}
                <tr className="resman-line-total-row">
                  <td colSpan={6} />
                  <td>Total</td>
                  <td>{money(resmanLineTotal)}</td>
                  <td />
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <div className="single-totals-card resman-totals-card" data-testid="single-invoice-totals">
          <div className="single-total-primary" data-testid="single-total-invoice">
            <span>Invoice total</span>
            <ResManMoneyInput
              value={invoiceTotal}
              ariaLabel="Invoice total summary"
              onCommit={(value) => onGroupFieldEdit(group, "Invoice Total", value)}
            />
          </div>
          <div data-testid="single-total-merchandise"><span>Merchandise</span><strong>{money(merchandiseSubtotal)}</strong></div>
          <div data-testid="single-total-tax"><span>Tax</span><strong>{money(taxAmount)}</strong></div>
          <div><span>ResMan line total</span><strong>{money(resmanLineTotal)}</strong></div>
          <div className={Math.abs(difference) > 0.009 ? "single-total-difference has-difference" : "single-total-difference"}>
            <span>{taxReviewPending ? "Pending tax" : "Difference"}</span>
            <strong>{money(difference)}</strong>
          </div>
        </div>
        {taxAmount > 0 && (
          <div className="resman-tax-actions" data-testid="single-tax-actions">
            <span>Tax / difference</span>
            <button
              type="button"
              className={taxPolicy === "distribute_proportionally" ? "is-active" : ""}
              onClick={() => void applyTaxPolicy("distribute_proportionally")}
            >
              Distributed by default
            </button>
            <button
              type="button"
              className={taxPolicy === "separate_tax_line" ? "is-active" : ""}
              onClick={() => void applyTaxPolicy("separate_tax_line")}
            >
              Separate line
            </button>
            <button
              type="button"
              className={taxPolicy === "exclude_tax" ? "is-active" : ""}
              onClick={() => void applyTaxPolicy("exclude_tax")}
            >
              Leave out
            </button>
          </div>
        )}

        <div className="single-review-tasks resman-review-tasks" data-testid="single-review-tasks">
          <div className="single-section-heading compact-review-heading">
            <strong>{visibleTasks.length} review task{visibleTasks.length === 1 ? "" : "s"}</strong>
            <span>{reviewSummary}</span>
            {visibleTasks.length > 0 && (
              <button type="button" className="single-link-button" onClick={() => setReviewExpanded((v) => !v)}>
                {reviewExpanded ? "Hide" : "Review"}
              </button>
            )}
          </div>
          {error && <div className="ai-mapping-error">{error}</div>}
          {!reviewExpanded && visibleTasks.length > 0 ? (
            <div className="single-task compact-summary">
              <span>{blockingCount > 0 ? `${blockingCount} blocking issue${blockingCount === 1 ? "" : "s"} remain.` : "Only non-blocking review notes remain."}</span>
            </div>
          ) : null}
          {visibleTasks.length === 0 ? (
            <div className="single-task resolved">No review tasks remain.</div>
          ) : reviewExpanded ? (
            visibleTasks.map((task) => (
              <div className={`single-task ${resolved.has(task.code) ? "resolved" : ""}`} key={task.code}>
                <div>
                  <strong>{task.title}</strong>
                  <span>{task.explanation}</span>
                </div>
                <div className="single-task-actions">
                  {task.code === "property_mapping_required" || task.code === "location_unresolved" ? (
                    <button type="button" className="btn btn-compact" onClick={() => void searchProperties()}>Resolve</button>
                  ) : task.code === "vendor_mapping_required" || task.code === "vendor_mapping_not_found" ? (
                    <>
                      <button type="button" className="btn btn-compact" onClick={() => void searchVendors()} disabled={saving === "vendor-search"}>
                        Resolve
                      </button>
                      {vendorCandidates && (
                        <div className="single-candidate-list compact">
                          {vendorCandidates.candidates.slice(0, 3).map((candidate) => (
                            <button
                              type="button"
                              key={`${candidate.vendor_name}-${candidate.vendor_id}`}
                              onClick={() => void applyVendor(candidate.vendor_name, candidate.vendor_id)}
                              data-testid="single-vendor-candidate"
                            >
                              <strong>{candidate.vendor_name}</strong>
                              <span>{Math.round(candidate.score * 100)}% - {candidate.reason}</span>
                            </button>
                          ))}
                          {vendorCandidates.candidates.length === 0 && <span className="single-empty-choice">No vendor match found.</span>}
                        </div>
                      )}
                    </>
                  ) : task.code === "tax_handling_requires_review" || task.code === "tax_gl_mapping_required" ? (
                    <select
                      className="template-filter-select"
                      defaultValue=""
                      onChange={(e) => {
                        const value = e.target.value as "manual_review" | "distribute_proportionally" | "separate_tax_line" | "exclude_tax";
                        if (value) void applyTaxPolicy(value);
                      }}
                      data-testid="single-tax-policy"
                    >
                      <option value="">Tax policy</option>
                      <option value="separate_tax_line">Separate tax line</option>
                      <option value="distribute_proportionally">Distribute tax</option>
                      <option value="exclude_tax">Leave for review</option>
                    </select>
                  ) : (
                    <button type="button" className="btn btn-compact" onClick={() => setResolved((prev) => new Set([...prev, task.code]))}>
                      {task.code.includes("gl") ? "GL below" : "Reviewed"}
                    </button>
                  )}
                </div>
              </div>
            ))
          ) : null}
        </div>

        <section className="resman-notes-section">
          <label>Notes</label>
          <textarea />
          <div className="single-review-footer resman-action-footer">
            <button type="button" className="resman-primary-action" onClick={() => setResolved(new Set(tasks.map((task) => task.code)))} data-testid="single-mark-reviewed">
              Save
            </button>
            <button type="button" className="resman-primary-action" disabled>
              Save & New
            </button>
            <button type="button" className="resman-primary-action" onClick={onReturnToBulk}>
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-compact btn-accent"
              disabled={blockingCount > 0}
              title={readyBlockerTitle}
              data-testid="single-ready-export"
            >
              Ready to export
            </button>
          </div>
        </section>

        <section className="resman-history-section">
          <h4>Invoice History</h4>
          <table>
            <thead>
              <tr>
                <th>Date</th>
                <th>Activity</th>
                <th>Timestamp</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>{historyDate}</td>
                <td>{enteredActivity}</td>
                <td>{new Date().toLocaleString()}</td>
              </tr>
              <tr>
                <td>{historyDate}</td>
                <td>{blockingCount > 0 ? `${blockingCount} review task(s) pending` : "Invoice ready to export"}</td>
                <td>{new Date().toLocaleString()}</td>
              </tr>
            </tbody>
          </table>
        </section>

        <div className="resman-documents-bar">
          <span>Documents ({Math.max(1, sourceFiles.length)})</span>
          {sourceDocumentUrl && (
            <a href={sourceDocumentUrl} target="_blank" rel="noreferrer">
              Open source
            </a>
          )}
        </div>
      </div>
    </section>
  );
}

type ReviewTask = {
  code: string;
  title: string;
  explanation: string;
  blocking: boolean;
};

function buildReviewTasks(flags: string[], reasons: string[]): ReviewTask[] {
  const flagSet = new Set(flags);
  const normalizedReasons = reasons.map(normalizeReviewReasonText);
  const tasks: ReviewTask[] = [];
  const add = (code: string, title: string, explanation: string, blocking = true) => {
    const codeWords = code.replace(/_/g, " ");
    const hasLegacyReason = normalizedReasons.some((r) =>
      r.includes(codeWords.toLowerCase()),
    );
    if (!flagSet.has(code) && !hasLegacyReason) {
      return;
    }
    if (!tasks.some((task) => task.code === code || task.title === title)) {
      tasks.push({ code, title, explanation, blocking });
    }
  };
  add("invoice_number_missing", "Invoice number required", "Enter or generate a unique invoice number before export.");
  add("invoice_date_missing", "Invoice date required", "Confirm the invoice date before export.");
  add("vendor_mapping_required", "Vendor mapping required", "Confirm the AI vendor against the ResMan Vendor List.");
  add("vendor_mapping_not_found", "Vendor mapping required", "Confirm the AI vendor against the ResMan Vendor List.");
  add("property_mapping_required", "Property mapping required", "Choose a known property before export.");
  add("property_mapping_not_found", "Property mapping required", "Choose a known property before export.");
  add("property_abbreviation_missing", "Property mapping required", "Choose a known property before export.");
  add("property_or_service_address_missing", "Property mapping required", "Choose a known property before export.");
  add("location_unresolved", "Location unresolved", "Select a known unit/location or explicitly leave it blank.", false);
  add("unit_mapping_not_found", "Location unresolved", "Select a known unit/location or explicitly leave it blank.", false);
  add("gl_mapping_required", "GL mapping required", "Select a numeric GL account from the Chart of Accounts.");
  add("ambiguous_gl_mapping", "GL mapping required", "Select a numeric GL account from the Chart of Accounts.");
  add("tax_handling_requires_review", "Tax handling requires review", "Choose how tax should be represented in ResMan.", true);
  add("tax_gl_mapping_required", "Tax GL required", "Separate tax lines require a valid GL mapping.", true);
  add("zero_amount_line_excluded", "Zero-dollar line excluded", "A source line was excluded because it has no payable amount.", false);
  flags
    .filter((flag) => /^invoice_date_inferred/.test(flag))
    .forEach((flag) => {
      tasks.push({
        code: flag,
        title: "Invoice date inferred",
        explanation: "The invoice date was inferred from a purchase/ship/received date. Edit if needed.",
        blocking: false,
      });
    });
  return tasks;
}

function normalizeReviewReasonText(value: unknown): string {
  return String(value ?? "")
    .toLowerCase()
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function deriveRequiredReviewFlags(
  group: InvoiceGroup,
  edits: CellEdits,
  reasons: string[],
): string[] {
  const flags: string[] = [];
  const firstIndex = group.rowIndexes[0] ?? 0;
  const first = group.firstRow;
  const invoiceNumber = String(
    cellValue(first, edits, firstIndex, "Invoice Number") ?? "",
  ).trim();
  const vendor = String(
    cellValue(first, edits, firstIndex, "Vendor") || first._meta?.ai_detected_vendor || "",
  ).trim();

  if (!invoiceNumber || /^unknown\b/i.test(invoiceNumber)) {
    flags.push("invoice_number_missing");
  }
  if (!vendor || /^unknown\b/i.test(vendor)) {
    flags.push("vendor_mapping_required");
  }

  const missingProperty = group.rows.some((row, idx) => {
    const rowIndex = group.rowIndexes[idx];
    return !String(cellValue(row, edits, rowIndex, "Property Abbreviation") ?? "").trim();
  });
  if (missingProperty) {
    flags.push("property_mapping_required");
  }

  const missingOrInvalidGl = group.rows.some((row, idx) => {
    const rowIndex = group.rowIndexes[idx];
    const gl = String(cellValue(row, edits, rowIndex, "GL Account") ?? "").trim();
    return !/^\d{3,}(?:\.\d+)?/.test(gl);
  });
  if (missingOrInvalidGl) {
    flags.push("gl_mapping_required");
  }

  const legacy = reasons.map(normalizeReviewReasonText).join(" | ");
  if (legacy.includes("property mapping not found") || legacy.includes("property abbreviation missing")) {
    flags.push("property_mapping_not_found");
  }
  if (legacy.includes("unit mapping not found")) {
    flags.push("unit_mapping_not_found");
  }
  if (legacy.includes("invoice date missing")) {
    flags.push("invoice_date_missing");
  }
  return flags;
}

function buildAddedLineItemRow(group: InvoiceGroup, edits: CellEdits): PreviewRow {
  const firstIndex = group.rowIndexes[0] ?? 0;
  const first = group.firstRow;
  const meta = first._meta;
  const maxLine = group.rows.reduce((max, row, idx) => {
    const rowIndex = group.rowIndexes[idx];
    const raw = Number(cellValue(row, edits, rowIndex, "Line Item Number"));
    return Number.isFinite(raw) ? Math.max(max, raw) : max;
  }, group.rows.length);
  return {
    "Invoice Number": String(cellValue(first, edits, firstIndex, "Invoice Number") || ""),
    "Bill or Credit": String(cellValue(first, edits, firstIndex, "Bill or Credit") || "Bill"),
    "Invoice Date": String(cellValue(first, edits, firstIndex, "Invoice Date") || ""),
    "Accounting Date": String(cellValue(first, edits, firstIndex, "Accounting Date") || ""),
    Vendor: String(cellValue(first, edits, firstIndex, "Vendor") || first.Vendor || ""),
    "Invoice Description": String(cellValue(first, edits, firstIndex, "Invoice Description") || ""),
    "Invoice Total": cellValue(first, edits, firstIndex, "Invoice Total") || readAiProvenanceNumber(first, "invoice_total") || "",
    "Line Item Number": maxLine + 1,
    "Property Abbreviation": String(cellValue(first, edits, firstIndex, "Property Abbreviation") || ""),
    Location: String(cellValue(first, edits, firstIndex, "Location") || ""),
    "GL Account": "",
    "Line Item Description": "",
    Amount: 0,
    "Expense Type": String(cellValue(first, edits, firstIndex, "Expense Type") || "General"),
    "Is Replacement Reserve": false,
    "Due Date": String(cellValue(first, edits, firstIndex, "Due Date") || ""),
    Quantity: 1,
    "Unit Price": 0,
    Tax: false,
    _meta: {
      ...(meta ?? {}),
      match_strategy: meta?.match_strategy || "manual_line_item",
      match_confidence: meta?.match_confidence || "manual",
      service_period_source: meta?.service_period_source || "",
      service_period_inferred: Boolean(meta?.service_period_inferred),
      support_document_status: meta?.support_document_status || "",
      manual_review_reasons: ["Manual line item added. Review GL, description, and amount before export."],
      ai_generated: false,
      ai_validation_flags: ["manual_line_item_added", "gl_mapping_required"],
      invoice_group_id: meta?.invoice_group_id,
      invoice_row_index: maxLine,
    },
  };
}

function formatGlLabel(code: unknown, name: unknown): string {
  const c = String(code ?? "").trim();
  const n = String(name ?? "").trim();
  if (c && n) return `${c} - ${n}`;
  return c || n || "GL not set";
}

function toSuggestionItems(values: string[]): FieldSuggestion[] {
  return values.map((value) => ({
    label: value,
    value,
  }));
}

function toGlSuggestionItems(
  candidates: Array<{ gl_code?: string; gl_account?: string; gl_name?: string; reason?: string; disabled?: boolean; valid?: boolean }>,
): FieldSuggestion[] {
  return candidates.slice(0, 8).map((candidate) => {
    const code = String(candidate.gl_code || candidate.gl_account || "").trim();
    const name = String(candidate.gl_name || "").trim();
    return {
      label: formatGlLabel(code, name),
      value: code,
      detail: name,
      disabled: candidate.disabled || candidate.valid === false || !code,
    };
  });
}

function bestValidatedGlCandidate(
  candidates: Array<{
    gl_code?: string;
    gl_account?: string;
    gl_name?: string;
    score?: number;
    valid?: boolean;
  }>,
) {
  return candidates.find((candidate) =>
    candidate.valid !== false &&
    Number(candidate.score ?? 0) >= 0.65 &&
    String(candidate.gl_code || candidate.gl_account || "").trim(),
  );
}

function rotateItems<T>(items: T[], generation: number): T[] {
  if (items.length <= 1) return items;
  const offset = Math.abs(generation) % items.length;
  return [...items.slice(offset), ...items.slice(0, offset)];
}

function sourceLineDescription(row: PreviewRow): string {
  const value = String(row["Line Item Description"] || "").trim();
  const parts = value.split(/\s+-\s+/).filter(Boolean);
  return parts[parts.length - 1] || value;
}

function glNameForCode(code: string, candidates: { gl_code?: string; gl_account?: string; gl_name?: string }[]): string {
  const needle = code.trim();
  if (!needle) return "";
  const found = candidates.find((candidate) =>
    String(candidate.gl_code || candidate.gl_account || "").trim() === needle,
  );
  return String(found?.gl_name || "").trim();
}

function resolveDocumentUrlForRow(
  batchId: string | null | undefined,
  row: PreviewRow,
  edits?: CellEdits,
  rowIndex?: number,
): string {
  const edited =
    edits && typeof rowIndex === "number" && edits[rowIndex]
      ? edits[rowIndex]["Document Url"]
      : undefined;
  const explicit = String(
    edited ?? row["Document Url"] ?? row._meta?.ai_provenance?.document_url ?? "",
  ).trim();
  if (explicit) return explicit;
  const sourceFile = String(row._meta?.source_file || "").trim();
  if (!batchId || !sourceFile) return "";
  return api.fileContentUrl(batchId, sourceFile);
}

function parseMoneyLike(value: unknown, fallback = 0): number {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  const cleaned = String(value ?? "").replace(/[$,]/g, "").trim();
  const parsed = Number(cleaned);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function inferItemCategory(description: string): string {
  const text = description.toLowerCase();
  if (/bar pull|pull|knob|handle|hinge|cabinet/.test(text)) return "cabinet hardware";
  if (/paint|primer|brush|roller/.test(text)) return "paint supplies";
  if (/plumb|faucet|valve|pipe|drain/.test(text)) return "plumbing materials";
  if (/wire|breaker|outlet|switch|electrical/.test(text)) return "electrical materials";
  if (/lock|key|deadbolt/.test(text)) return "locksmith hardware";
  return "maintenance supplies";
}

function buildInvoiceDescriptionSuggestions({
  invoiceDate,
  vendor,
  property,
  itemDescription,
  invoiceNumber,
  variant,
}: {
  invoiceDate: string;
  vendor: string;
  property: string;
  itemDescription: string;
  invoiceNumber: string;
  variant?: number;
}): string[] {
  const date = invoiceDate ? invoiceDate.replace(/\/20(\d{2})$/, "/$1") : "";
  const category = titleCase(inferItemCategory(itemDescription));
  const item = sentenceCase(itemDescription);
  const parts = [date, vendor, property, category].filter(Boolean);
  const suggestions = [
    parts.join(" - "),
    [date, vendor, property, item].filter(Boolean).join(" - "),
    [vendor, invoiceNumber ? `Invoice ${invoiceNumber}` : "", category].filter(Boolean).join(" - "),
    [date, vendor, category].filter(Boolean).join(" - "),
    [property, category, item].filter(Boolean).join(" - "),
    [vendor, item].filter(Boolean).join(" - "),
    [invoiceNumber ? `Invoice ${invoiceNumber}` : "", item].filter(Boolean).join(" - "),
  ];
  return rotateItems(uniqueStrings(suggestions), variant ?? 0).slice(0, 3);
}

function buildLineDescriptionSuggestions(description: string, variant = 0): string[] {
  const item = sentenceCase(sourceDescriptionText(description));
  const category = titleCase(inferItemCategory(description));
  const suggestions = uniqueStrings([
    item,
    `${category} - ${item}`,
    `${category} material - ${item}`,
    `${item} (${category})`,
    `${category} supplies - ${item}`,
    `${item} - ${category}`,
  ]);
  return rotateItems(suggestions, variant).slice(0, 3);
}

function buildAiInvoiceNarrative({
  vendor,
  category,
  itemDescription,
  property,
  serviceAddress,
  confidence,
}: {
  vendor: string;
  category: string;
  itemDescription: string;
  property: string;
  serviceAddress: string;
  confidence: unknown;
}): string {
  const confidenceText = typeof confidence === "number" ? `${Math.round(confidence * 100)}%` : "review";
  const propertyText = property ? ` for ${property}` : "";
  const addressText = serviceAddress ? ` The source address is ${serviceAddress}.` : "";
  const focus = sentenceCase(sourceDescriptionText(itemDescription));
  return `AI reads this as a ${category} invoice from ${vendor}${propertyText}, centered on ${focus || "maintenance materials"}. Confidence is ${confidenceText}.${addressText}`;
}

function sourceDescriptionText(value: string): string {
  const text = String(value || "").trim();
  const parts = text.split(/\s+-\s+/).filter(Boolean);
  return parts[parts.length - 1] || text;
}

function sentenceCase(value: string): string {
  const text = String(value || "").trim();
  if (!text) return "";
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function titleCase(value: string): string {
  return String(value || "")
    .split(/\s+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function readAiProvenanceNumber(row: PreviewRow, key: string): number | null {
  if (!row._meta?.ai_provenance || typeof row._meta.ai_provenance !== "object") {
    return null;
  }
  const value = Number((row._meta.ai_provenance as Record<string, unknown>)[key]);
  return Number.isFinite(value) ? value : null;
}

function inferMerchandiseSubtotal(row: PreviewRow, lineTotal: number): number {
  const subtotal = readAiProvenanceNumber(row, "subtotal");
  if (subtotal != null && subtotal >= 0) return Number(subtotal.toFixed(2));
  return Number(lineTotal.toFixed(2));
}

function inferTaxAmount(row: PreviewRow, merchandiseSubtotal: number): number {
  const metaTax = readAiProvenanceNumber(row, "tax_amount");
  if (metaTax != null && metaTax > 0) {
    return Number(metaTax.toFixed(2));
  }
  const flags = row._meta?.ai_validation_flags ?? [];
  const invoiceTotal = inferInvoiceTotal(row, merchandiseSubtotal);
  const diff = invoiceTotal - merchandiseSubtotal;
  if (row._meta?.ai_generated === true && diff > 0) {
    return Number(diff.toFixed(2));
  }
  if (!flags.includes("tax_handling_requires_review") && !flags.includes("tax_gl_mapping_required")) {
    return 0;
  }
  return diff > 0 ? Number(diff.toFixed(2)) : 0;
}

function distributeAmountAcrossLines(baseAmounts: number[], adjustment: number): number[] {
  const total = baseAmounts.reduce((sum, amount) => sum + Math.max(0, amount), 0);
  if (total <= 0) return baseAmounts.map((amount) => Number(amount.toFixed(2)));
  let running = 0;
  return baseAmounts.map((amount, idx) => {
    const isLast = idx === baseAmounts.length - 1;
    const share = isLast
      ? Number((adjustment - running).toFixed(2))
      : Number((adjustment * (Math.max(0, amount) / total)).toFixed(2));
    if (!isLast) running += share;
    return Number((amount + share).toFixed(2));
  });
}

function inferInvoiceTotal(row: PreviewRow, fallback: number): number {
  const metaTotal = readAiProvenanceNumber(row, "invoice_total");
  if (metaTotal != null && metaTotal > 0) return Number(metaTotal.toFixed(2));
  return Number(fallback.toFixed(2));
}

function ResManField({
  label,
  required,
  children,
}: {
  label: string;
  required?: boolean;
  children: ReactNode;
}) {
  return (
    <div className="resman-form-row">
      <span className="resman-field-label">
        {label}
        {required && <span className="resman-required">*</span>}
      </span>
      <div className="resman-field-control">{children}</div>
    </div>
  );
}

function ResManReadonlyField({
  label,
  required,
  value,
}: {
  label: string;
  required?: boolean;
  value: unknown;
}) {
  return (
    <ResManField label={label} required={required}>
      <span className="resman-static-value">{displayValue(value)}</span>
    </ResManField>
  );
}

function ResManEditableField({
  label,
  required,
  value,
  onCommit,
  aiSuggestions,
}: {
  label: string;
  required?: boolean;
  value: unknown;
  onCommit: (value: string) => void;
  aiSuggestions?: {
    title: string;
    items: FieldSuggestion[];
    emptyText?: string;
    onApply: (value: string, item: FieldSuggestion) => void;
    onRegenerate: () => void;
  };
}) {
  const display = String(value ?? "");
  const [draft, setDraft] = useState(display);
  useEffect(() => {
    setDraft(display);
  }, [display]);
  const commit = () => {
    if (draft !== display) onCommit(draft);
  };
  return (
    <ResManField label={label} required={required}>
      <div className={aiSuggestions ? "resman-input-with-ai" : undefined}>
        <input
          className="resman-input"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              commit();
              (e.currentTarget as HTMLInputElement).blur();
            }
          }}
          data-testid={`single-invoice-field-${fieldTestId(label)}`}
        />
        {aiSuggestions && (
          <AiSuggestionControl
            title={aiSuggestions.title}
            items={aiSuggestions.items}
            emptyText={aiSuggestions.emptyText}
            onApply={(item) => {
              setDraft(item.value);
              aiSuggestions.onApply(item.value, item);
            }}
            onRegenerate={aiSuggestions.onRegenerate}
          />
        )}
      </div>
    </ResManField>
  );
}

function AiSuggestionControl({
  title,
  items,
  emptyText = "No suggestions available.",
  onApply,
  onRegenerate,
}: {
  title: string;
  items: FieldSuggestion[];
  emptyText?: string;
  onApply: (item: FieldSuggestion) => void;
  onRegenerate: () => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <span className="field-ai-suggest" onClick={(e) => e.stopPropagation()}>
      <button
        type="button"
        className={`field-ai-trigger ${open ? "is-open" : ""}`}
        aria-label={title}
        title={title}
        onClick={(e) => {
          e.stopPropagation();
          setOpen((value) => !value);
        }}
      >
        <AiSparkIcon />
      </button>
      {open && (
        <div className="field-ai-menu" role="menu">
          <div className="field-ai-menu-head">
            <span>{title}</span>
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onRegenerate();
              }}
              title="Generate a new suggestion set"
              aria-label="Generate a new suggestion set"
            >
              <AiSparkIcon />
              New set
            </button>
          </div>
          <div className="field-ai-menu-list">
            {items.length === 0 ? (
              <em>{emptyText}</em>
            ) : (
              items.map((item) => (
                <button
                  type="button"
                  key={`${item.value}-${item.label}`}
                  role="menuitem"
                  disabled={item.disabled}
                  onClick={(e) => {
                    e.stopPropagation();
                    onApply(item);
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

function AiSparkIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M8 1.8l.9 2.7 2.7.9-2.7.9L8 9l-.9-2.7-2.7-.9 2.7-.9L8 1.8Z" stroke="currentColor" strokeWidth="1.35" strokeLinejoin="round" />
      <path d="M12.2 9.2l.5 1.4 1.4.5-1.4.5-.5 1.4-.5-1.4-1.4-.5 1.4-.5.5-1.4Z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
      <path d="M3.8 9.8l.4 1.1 1.1.4-1.1.4-.4 1.1-.4-1.1-1.1-.4 1.1-.4.4-1.1Z" stroke="currentColor" strokeWidth="1.1" strokeLinejoin="round" />
    </svg>
  );
}

function ResManMoneyInput({
  value,
  ariaLabel,
  onCommit,
}: {
  value: number;
  ariaLabel: string;
  onCommit: (value: number) => void;
}) {
  const display = money(value).replace("$", "");
  const [draft, setDraft] = useState(display);
  useEffect(() => {
    setDraft(display);
  }, [display]);
  const commit = () => {
    const next = parseMoneyLike(draft, value);
    onCommit(Number(next.toFixed(2)));
  };
  return (
    <input
      className="resman-input resman-money-input"
      value={draft}
      aria-label={ariaLabel}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onFocus={(e) => e.currentTarget.select()}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          commit();
          (e.currentTarget as HTMLInputElement).blur();
        }
      }}
    />
  );
}

function SingleInvoiceField({
  label,
  value,
  onCommit,
}: {
  label: string;
  value: unknown;
  onCommit: (value: string) => void;
}) {
  const [draft, setDraft] = useState(String(value ?? ""));
  useEffect(() => {
    setDraft(String(value ?? ""));
  }, [value]);
  return (
    <label className="single-field">
      <span>{label}</span>
      <input
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => onCommit(draft)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            onCommit(draft);
            (e.currentTarget as HTMLInputElement).blur();
          }
        }}
        data-testid={`single-invoice-field-${fieldTestId(label)}`}
      />
    </label>
  );
}

function SingleLineCell({
  row,
  edits,
  rowIndex,
  column,
  onCellEdit,
  numeric,
  fallbackValue,
  aiSuggestions,
}: {
  row: PreviewRow;
  edits: CellEdits;
  rowIndex: number;
  column: string;
  onCellEdit: (rowIndex: number, columnKey: string, newValue: unknown) => void;
  numeric?: boolean;
  fallbackValue?: unknown;
  aiSuggestions?: {
    title: string;
    items: FieldSuggestion[];
    emptyText?: string;
    onApply: (value: string, item: FieldSuggestion) => void;
    onRegenerate: () => void;
  };
}) {
  return (
    <td>
      <SingleLineInput
        row={row}
        edits={edits}
        rowIndex={rowIndex}
        column={column}
        onCellEdit={onCellEdit}
        numeric={numeric}
        fallbackValue={fallbackValue}
        aiSuggestions={aiSuggestions}
      />
    </td>
  );
}

function SingleLineInput({
  row,
  edits,
  rowIndex,
  column,
  onCellEdit,
  numeric,
  fallbackValue,
  aiSuggestions,
}: {
  row: PreviewRow;
  edits: CellEdits;
  rowIndex: number;
  column: string;
  onCellEdit: (rowIndex: number, columnKey: string, newValue: unknown) => void;
  numeric?: boolean;
  fallbackValue?: unknown;
  aiSuggestions?: {
    title: string;
    items: FieldSuggestion[];
    emptyText?: string;
    onApply: (value: string, item: FieldSuggestion) => void;
    onRegenerate: () => void;
  };
}) {
  const rawValue = cellValue(row, edits, rowIndex, column);
  const value =
    (rawValue == null || rawValue === "") && fallbackValue != null
      ? fallbackValue
      : rawValue;
  const [draft, setDraft] = useState(String(value ?? ""));
  useEffect(() => {
    setDraft(String(value ?? ""));
  }, [value]);
  const commit = (nextDraft = draft) => {
    if (numeric) {
      const n = Number(nextDraft);
      onCellEdit(rowIndex, column, Number.isFinite(n) ? n : nextDraft);
    } else {
      onCellEdit(rowIndex, column, nextDraft);
    }
  };
  const input = (
    <input
      className={numeric ? "num" : ""}
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => commit()}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          (e.currentTarget as HTMLInputElement).blur();
        }
      }}
      data-testid={`single-line-cell-${rowIndex}-${fieldTestId(column)}`}
    />
  );
  if (!aiSuggestions) return input;
  return (
    <div className="resman-line-input-with-ai">
      {input}
      <AiSuggestionControl
        title={aiSuggestions.title}
        items={aiSuggestions.items}
        emptyText={aiSuggestions.emptyText}
        onApply={(item) => {
          setDraft(item.value);
          aiSuggestions.onApply(item.value, item);
        }}
        onRegenerate={aiSuggestions.onRegenerate}
      />
    </div>
  );
}

function cellValue(
  row: PreviewRow,
  edits: CellEdits,
  rowIndex: number,
  column: string,
): unknown {
  if (edits[rowIndex] && column in edits[rowIndex]) {
    return edits[rowIndex][column];
  }
  return (row as Record<string, unknown>)[column];
}

function displayValue(value: unknown): string {
  const text = String(value ?? "").trim();
  return text || "Not set";
}

function money(value: number): string {
  const safe = Number.isFinite(value) ? value : 0;
  return `$${safe.toFixed(2)}`;
}

function formatDateForDisplay(value: unknown): string {
  const text = String(value ?? "").trim();
  const iso = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
  if (iso) return `${iso[2]}/${iso[3]}/${iso[1]}`;
  return text;
}

function formatConfidence(value: unknown): string {
  return typeof value === "number" ? `${Math.round(value * 100)}%` : "Not available";
}

function fieldTestId(value: string): string {
  return value.replace(/[^a-z0-9]+/gi, "-").replace(/^-|-$/g, "");
}

function uniqueStrings(values: unknown[]): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  values.forEach((value) => {
    const text = String(value ?? "").trim();
    if (!text || seen.has(text)) return;
    seen.add(text);
    out.push(text);
  });
  return out;
}

function humanFlag(flag: string): string {
  return flag
    .replace(/^ai_/, "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (m) => m.toUpperCase());
}

function AiMappingReviewPanel({
  error,
  savingKey,
  vendorRow,
  vendorSearch,
  onVendorSearchChange,
  onSearchVendor,
  saveVendorForFuture,
  onSaveVendorForFutureChange,
  vendorCandidates,
  onAcceptVendor,
  glRows,
  glCandidates,
  saveGlForFuture,
  onSaveGlForFutureChange,
  applySimilarGl,
  onApplySimilarGlChange,
  onAcceptGl,
}: {
  error: string;
  savingKey: string;
  vendorRow: PreviewRow | null;
  vendorSearch: string;
  onVendorSearchChange: (value: string) => void;
  onSearchVendor: () => void;
  saveVendorForFuture: boolean;
  onSaveVendorForFutureChange: (value: boolean) => void;
  vendorCandidates: AiVendorCandidatesResponse | null;
  onAcceptVendor: (vendorName: string, vendorId?: string) => Promise<void>;
  glRows: { row: PreviewRow; index: number }[];
  glCandidates: Record<number, AiGlCandidatesResponse>;
  saveGlForFuture: boolean;
  onSaveGlForFutureChange: (value: boolean) => void;
  applySimilarGl: boolean;
  onApplySimilarGlChange: (value: boolean) => void;
  onAcceptGl: (
    rowIndex: number,
    candidate: { gl_account: string; gl_name?: string },
  ) => Promise<void>;
}) {
  return (
    <section className="ai-mapping-review" data-testid="ai-mapping-review">
      <div className="ai-mapping-review-header">
        <div>
          <div className="ai-mapping-kicker">AI review</div>
          <div className="ai-mapping-title">Confirm vendor and GL mapping</div>
        </div>
        <div className="ai-mapping-subtle">Suggestions are validated before saving.</div>
      </div>
      {error && <div className="ai-mapping-error">{error}</div>}

      {vendorRow && (
        <div className="ai-mapping-section" data-testid="ai-vendor-review">
          <div className="ai-mapping-section-title">
            <span>Detected vendor</span>
            <strong>
              {String(
                vendorRow._meta?.ai_detected_vendor ||
                  vendorRow.Vendor ||
                  "Unknown vendor",
              )}
            </strong>
          </div>
          <div className="ai-mapping-search-row">
            <input
              type="search"
              className="ai-mapping-search"
              value={vendorSearch}
              onChange={(e) => onVendorSearchChange(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  void onSearchVendor();
                }
              }}
              placeholder="Search Vendor List"
              aria-label="Search Vendor List"
            />
            <button
              type="button"
              className="btn btn-compact"
              onClick={() => void onSearchVendor()}
              disabled={savingKey === "vendor-search"}
            >
              Search
            </button>
            <label className="ai-mapping-check">
              <input
                type="checkbox"
                checked={saveVendorForFuture}
                onChange={(e) => onSaveVendorForFutureChange(e.target.checked)}
              />
              Save for future
            </label>
          </div>
          <div className="ai-mapping-candidates">
            {(vendorCandidates?.candidates ?? []).slice(0, 4).map((candidate) => (
              <button
                type="button"
                className="ai-mapping-candidate"
                key={`${candidate.vendor_name}-${candidate.vendor_id}`}
                onClick={() =>
                  void onAcceptVendor(candidate.vendor_name, candidate.vendor_id)
                }
                disabled={savingKey.startsWith("vendor:")}
                data-testid="ai-vendor-candidate"
              >
                <span className="ai-mapping-main">{candidate.vendor_name}</span>
                <span className="ai-mapping-meta">
                  {Math.round(candidate.score * 100)}% · {candidate.reason}
                </span>
              </button>
            ))}
          </div>
        </div>
      )}

      {glRows.length > 0 && (
        <div className="ai-mapping-section" data-testid="ai-gl-review">
          <div className="ai-mapping-section-title">
            <span>GL mapping</span>
            <strong>{glRows.length} line item{glRows.length === 1 ? "" : "s"} need review</strong>
          </div>
          <div className="ai-mapping-options-row">
            <label className="ai-mapping-check">
              <input
                type="checkbox"
                checked={saveGlForFuture}
                onChange={(e) => onSaveGlForFutureChange(e.target.checked)}
              />
              Save mapping
            </label>
            <label className="ai-mapping-check">
              <input
                type="checkbox"
                checked={applySimilarGl}
                onChange={(e) => onApplySimilarGlChange(e.target.checked)}
              />
              Apply to similar items
            </label>
          </div>
          <div className="ai-gl-items">
            {glRows.map(({ row, index }) => {
              const candidates = glCandidates[index]?.candidates ?? [];
              return (
                <div className="ai-gl-item" key={index}>
                  <div className="ai-gl-item-title">
                    {String(row["Line Item Description"] || row["Invoice Description"] || "Line item")}
                  </div>
                  <div className="ai-mapping-candidates compact">
                    {candidates.slice(0, 3).map((candidate) => (
                      <button
                        type="button"
                        className="ai-mapping-candidate"
                        key={`${index}-${candidate.gl_account}-${candidate.gl_name}`}
                        onClick={() => void onAcceptGl(index, candidate)}
                        disabled={savingKey.startsWith(`gl:${index}:`) || candidate.valid === false}
                        data-testid="ai-gl-candidate"
                      >
                        <span className="ai-mapping-main">
                          {candidate.gl_code || candidate.gl_account}
                          {candidate.gl_name ? ` · ${candidate.gl_name}` : ""}
                        </span>
                        <span className="ai-mapping-meta">
                          {Math.round(candidate.score * 100)}% · {candidate.reason}
                        </span>
                      </button>
                    ))}
                    {!candidates.length && (
                      <span className="ai-mapping-empty">Loading GL choices...</span>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </section>
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
