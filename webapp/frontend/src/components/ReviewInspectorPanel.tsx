// Phase 1J — Review / Inspector panel.
//
// Replaces the older bottom-of-screen ManualReviewPanel table. Two tabs:
//
//   1. Issues          — grouped issue cards. Click an issue to select
//                        the related template row + select its source
//                        file.
//   2. Selected row    — a property-list view of the currently selected
//                        template row, including provenance hints.
//
// The panel doesn't need backend changes for Phase 1J — it works off
// the existing `manualReviewItems`, `previewRows`, and `selectedRowIndex`
// state already in App.tsx.

import { useMemo } from "react";

import type { ManualReviewItem, PreviewRow } from "../types";

type Props = {
  items: ManualReviewItem[];
  rows: PreviewRow[];
  selectedRowIndex: number | null;
  onSelectRow: (rowIndex: number) => void;
  onSelectFile: (filename: string) => void;
  activeTab: "issues" | "row";
  onTabChange: (tab: "issues" | "row") => void;
  /** Phase 1K — set of issue keys the operator has marked reviewed.
   *  Browser-session state in App.tsx; pure UI signal — does NOT
   *  remove the underlying manual_review reason. */
  reviewedKeys?: Set<string>;
  onToggleReviewed?: (key: string) => void;
  collapsed?: boolean;
  onToggleCollapsed?: () => void;
};

/** Build a stable key for a (review item, reason) pair so the
 *  "reviewed" set survives re-orderings / re-runs of the same set
 *  of items. */
export function issueKey(it: ManualReviewItem, reason: string): string {
  return `${it.invoice_number || it.account_number || it.source_file}::${reason}`;
}

const REASON_HELP: Record<string, string> = {
  unknown_unit_number_for_location:
    "Service address matched a building only — pick the specific unit manually.",
  missing_unit_number_for_description:
    "Unit number not resolved; descriptions fall back to address-only.",
  missing_service_address_for_description:
    "Service address not resolved; description uses unit-only or fallback text.",
  ambiguous_gl_mapping:
    "GL code has medium confidence (e.g. gas → 6930 vs 6935). Confirm before exporting.",
  account_number_unit_mapping_not_found:
    "No GL history for this account; service address could not be inferred.",
  account_number_unit_mapping_ambiguous:
    "GL history shows multiple addresses; the most-common one was picked.",
  dropbox_credentials_missing:
    "DROPBOX_* env vars not set; the support file was not uploaded.",
  dropbox_upload_failed:
    "Dropbox API rejected the upload; the support link column is blank.",
  missing_explicit_service_or_reading_dates:
    "No explicit dates on the bill — set up batch override or YAML default.",
  extracted_total_mismatch:
    "Line items don't sum to total due. OCR likely misread a charge — edit the affected Amount cell.",
  amount_total_mismatch: "Line items don't sum to total due — edit the affected Amount cell.",
  bill_total_does_not_match_generated_lines:
    "Generated lines don't reconcile to the bill total. Review or edit the lines.",
  support_pdf_split_failed:
    "Per-bill PDF split failed; the row falls back to the full PDF link.",
  support_pdf_account_unknown:
    "Couldn't read the account number on this PDF page; split filename used the page number instead.",
  support_pdf_upload_failed:
    "Dropbox rejected the upload of the per-bill split PDF; Document Url is blank.",
  support_pdf_link_missing:
    "Dropbox upload reported success but did not return a shareable URL.",
  late_notice_detected: "Page is a disconnect / late notice rather than a normal bill.",
  service_period_inferred:
    "No explicit service period on the page; processor used the calendar month.",
  service_period_missing: "No explicit service period on the page.",
  disconnection_notice_service_breakdown_missing:
    "No service balances breakdown extracted from the notice; the catch-all single-line is used.",
  disconnection_notice_breakdown_incomplete:
    "Partial service balances breakdown was found but did not reconcile; the catch-all single-line is used instead.",
  ai_filled_field:
    "An AI suggestion was used for this field. Confirm before exporting.",
};

const SEVERITY_ORDER: Record<string, number> = {
  high: 0,
  medium: 1,
  low: 2,
};

function severityFor(reason: string): "high" | "medium" | "low" {
  if (/fail|error|missing.*total|total_mismatch|not_found|invalid/i.test(reason))
    return "high";
  if (/inferred|missing|incomplete|ambiguous|unknown|not_configured/i.test(reason))
    return "medium";
  return "low";
}

export function ReviewInspectorPanel({
  items,
  rows,
  selectedRowIndex,
  onSelectRow,
  onSelectFile,
  activeTab,
  onTabChange,
  reviewedKeys,
  onToggleReviewed,
  collapsed,
  onToggleCollapsed,
}: Props) {
  // Group items by source_file so the operator sees clearly which
  // documents need attention.
  const grouped = useMemo(() => {
    const out: Record<string, ManualReviewItem[]> = {};
    for (const it of items) {
      const key = it.source_file || "(unknown source)";
      (out[key] ||= []).push(it);
    }
    return out;
  }, [items]);

  // Build a quick look-up from review item → row index in the preview
  // by matching invoice_number (most reliable). Falls back to source_file.
  const itemToRowIndex = useMemo(() => {
    const map = new Map<ManualReviewItem, number>();
    rows.forEach((row, idx) => {
      const inv = (row as PreviewRow)["Invoice Number"];
      if (!inv) return;
      const matching = items.find((it) => it.invoice_number === inv);
      if (matching && !map.has(matching)) map.set(matching, idx);
    });
    return map;
  }, [items, rows]);

  return (
    <div className={`inspector-card ${collapsed ? "collapsed" : ""}`}>
      <div className="card-header inspector-header">
        <div className="inspector-tabs" role="tablist">
          <button
            role="tab"
            aria-selected={activeTab === "issues"}
            className={`inspector-tab ${activeTab === "issues" ? "active" : ""}`}
            onClick={() => onTabChange("issues")}
          >
            Issues
            {items.length > 0 && (
              <span className="inspector-tab-count">{items.length}</span>
            )}
          </button>
          <button
            role="tab"
            aria-selected={activeTab === "row"}
            className={`inspector-tab ${activeTab === "row" ? "active" : ""}`}
            onClick={() => onTabChange("row")}
          >
            Selected row
          </button>
        </div>
        {onToggleCollapsed && (
          <button
            onClick={onToggleCollapsed}
            className="icon-btn"
            title={collapsed ? "Expand panel" : "Collapse panel"}
            aria-label={collapsed ? "Expand panel" : "Collapse panel"}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <polyline points={collapsed ? "9 18 15 12 9 6" : "15 18 9 12 15 6"} />
            </svg>
          </button>
        )}
      </div>

      {!collapsed && (
        <div className="inspector-body">
          {activeTab === "issues" ? (
            <IssuesView
              grouped={grouped}
              itemToRowIndex={itemToRowIndex}
              onSelectRow={onSelectRow}
              onSelectFile={onSelectFile}
              reviewedKeys={reviewedKeys}
              onToggleReviewed={onToggleReviewed}
            />
          ) : (
            <SelectedRowView
              row={
                selectedRowIndex != null ? rows[selectedRowIndex] ?? null : null
              }
            />
          )}
        </div>
      )}
    </div>
  );
}

function IssuesView({
  grouped,
  itemToRowIndex,
  onSelectRow,
  onSelectFile,
  reviewedKeys,
  onToggleReviewed,
}: {
  grouped: Record<string, ManualReviewItem[]>;
  itemToRowIndex: Map<ManualReviewItem, number>;
  onSelectRow: (rowIndex: number) => void;
  onSelectFile: (filename: string) => void;
  reviewedKeys?: Set<string>;
  onToggleReviewed?: (key: string) => void;
}) {
  const groupKeys = Object.keys(grouped).sort();
  if (groupKeys.length === 0) {
    return (
      <div className="inspector-empty">
        <span className="inspector-empty-emoji" aria-hidden>
          ✅
        </span>
        <div className="inspector-empty-title">No issues to review</div>
        <div className="inspector-empty-desc">
          Every row reconciled. You can export when you're ready.
        </div>
      </div>
    );
  }

  return (
    <div className="issues-list">
      {groupKeys.map((sourceFile) => {
        const group = grouped[sourceFile];
        return (
          <section key={sourceFile} className="issues-group">
            <header className="issues-group-header">
              <button
                type="button"
                className="issues-group-file"
                title={`Open ${sourceFile} in the document workspace`}
                onClick={() => onSelectFile(sourceFile)}
              >
                {sourceFile}
              </button>
              <span className="issues-group-count">
                {group.length} issue{group.length === 1 ? "" : "s"}
              </span>
            </header>
            <ul className="issues-cards">
              {group.flatMap((it) => {
                const rowIdx = itemToRowIndex.get(it);
                return it.reasons.map((reason) => {
                  const sev = severityFor(reason);
                  const key = issueKey(it, reason);
                  const isReviewed = reviewedKeys?.has(key) ?? false;
                  return (
                    <li
                      key={`${it.invoice_number || it.account_number}-${reason}`}
                      className={`issue-card severity-${sev} ${isReviewed ? "is-reviewed" : ""}`}
                    >
                      <div className="issue-card-head">
                        <span className={`issue-sev-dot sev-${sev}`} aria-hidden />
                        <span className="issue-code">{prettyCode(reason)}</span>
                        <span className="issue-target">
                          {it.invoice_number || it.account_number || ""}
                        </span>
                      </div>
                      <div className="issue-explain">
                        {REASON_HELP[reason] ||
                          "An automated rule flagged this row. Inspect before exporting."}
                      </div>
                      <div className="issue-meta">
                        {it.property_abbreviation && (
                          <span className="issue-meta-pill">
                            {it.property_abbreviation}
                            {it.location ? ` · ${it.location}` : ""}
                          </span>
                        )}
                        {Number.isFinite(it.total_amount) && (
                          <span className="issue-meta-pill">
                            ${it.total_amount.toFixed(2)}
                          </span>
                        )}
                      </div>
                      <div className="issue-actions">
                        {rowIdx != null && (
                          <button
                            type="button"
                            className="btn btn-mini"
                            onClick={() => onSelectRow(rowIdx)}
                          >
                            Show row
                          </button>
                        )}
                        <button
                          type="button"
                          className="btn btn-mini btn-ghost"
                          onClick={() => onSelectFile(sourceFile)}
                        >
                          Open document
                        </button>
                        {onToggleReviewed && (
                          <button
                            type="button"
                            className={`btn btn-mini ${isReviewed ? "btn-accent" : "btn-ghost"}`}
                            onClick={() => onToggleReviewed(key)}
                            title={
                              isReviewed
                                ? "Marked reviewed in this session"
                                : "Mark this issue as reviewed (session only)"
                            }
                          >
                            {isReviewed ? "✓ Reviewed" : "Mark reviewed"}
                          </button>
                        )}
                      </div>
                    </li>
                  );
                });
              })}
            </ul>
          </section>
        );
      })}
    </div>
  );
}

function SelectedRowView({ row }: { row: PreviewRow | null }) {
  if (!row) {
    return (
      <div className="inspector-empty">
        <div className="inspector-empty-title">No row selected</div>
        <div className="inspector-empty-desc">
          Click any row in the template, or pick an issue, to see its details
          here.
        </div>
      </div>
    );
  }
  const reasons = row._meta?.manual_review_reasons ?? [];
  const url = (row as PreviewRow)["Document Url"];
  const fields: { label: string; value: unknown }[] = [
    { label: "Invoice number", value: row["Invoice Number"] },
    { label: "Vendor", value: row["Vendor"] },
    { label: "Invoice date", value: row["Invoice Date"] },
    { label: "Due date", value: row["Due Date"] },
    { label: "Property", value: row["Property Abbreviation"] },
    { label: "Location", value: row["Location"] },
    { label: "GL account", value: row["GL Account"] },
    { label: "Amount", value: row["Amount"] },
    { label: "Description", value: row["Invoice Description"] },
  ];

  return (
    <div className="row-inspector">
      <dl className="row-fields">
        {fields.map((f) => (
          <div key={f.label} className="row-field">
            <dt>{f.label}</dt>
            <dd>{formatField(f.value)}</dd>
          </div>
        ))}
      </dl>

      {url && typeof url === "string" && (
        <div className="row-doc-url">
          <a
            href={url}
            target="_blank"
            rel="noreferrer"
            className="btn btn-mini btn-accent"
          >
            ↗ Open support document
          </a>
        </div>
      )}

      <div className="row-section-title">Provenance</div>
      <ul className="row-provenance">
        <li>
          <span className="row-prov-key">Match strategy</span>
          <span className="row-prov-val">
            {row._meta?.match_strategy || "—"}
          </span>
        </li>
        <li>
          <span className="row-prov-key">Match confidence</span>
          <span className="row-prov-val">
            {row._meta?.match_confidence || "—"}
          </span>
        </li>
        <li>
          <span className="row-prov-key">Service period source</span>
          <span className="row-prov-val">
            {row._meta?.service_period_source ||
              (row._meta?.service_period_inferred ? "inferred" : "—")}
          </span>
        </li>
        <li>
          <span className="row-prov-key">Support document status</span>
          <span className="row-prov-val">
            {row._meta?.support_document_status || "—"}
          </span>
        </li>
      </ul>

      {reasons.length > 0 && (
        <>
          <div className="row-section-title">Manual review</div>
          <ul className="row-reasons">
            {reasons.map((r) => (
              <li key={r}>
                <span className="row-reason-code">{prettyCode(r)}</span>
                <div className="row-reason-help">
                  {REASON_HELP[r] || "Inspect before exporting."}
                </div>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

function formatField(v: unknown): string {
  if (v == null || v === "") return "—";
  if (typeof v === "number") return v.toFixed(2);
  return String(v);
}

function prettyCode(code: string): string {
  return code.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}
