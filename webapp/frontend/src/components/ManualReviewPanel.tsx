import type { ManualReviewItem } from "../types";

const REASON_HELP: Record<string, string> = {
  unknown_unit_number_for_location:
    "GL service address matched a building only — pick the specific unit manually.",
  missing_unit_number_for_description:
    "Unit number not resolved; descriptions fall back to address-only.",
  missing_service_address_for_description:
    "Service address not resolved; description uses unit-only or fallback text.",
  ambiguous_gl_mapping:
    "GL code has medium confidence (gas → 6930 vs 6935 needs verification).",
  account_number_unit_mapping_not_found:
    "No GL history for this account; service address could not be inferred.",
  account_number_unit_mapping_ambiguous:
    "GL history shows multiple addresses; the most-common one was picked.",
  dropbox_credentials_missing:
    "DROPBOX_* env vars not set; the support file was not uploaded.",
  dropbox_upload_failed:
    "Dropbox API rejected the upload; the support link column is blank.",
  missing_explicit_service_or_reading_dates:
    "No explicit dates and no fallback enabled — set up batch override or YAML default.",
  extracted_total_mismatch:
    "PDF line items don't sum to TOTAL DUE NOW; OCR likely misread a charge — edit the affected Amount cell.",
  support_pdf_split_failed:
    "Per-bill PDF split failed; the row falls back to the full PDF link.",
  support_pdf_account_unknown:
    "Couldn't read the account number on this PDF page; split filename used the page number instead.",
  support_pdf_upload_failed:
    "Dropbox rejected the upload of the per-bill split PDF; Document Url is blank.",
  support_pdf_link_missing:
    "Dropbox upload reported success but did not return a shareable URL.",
};

type Props = {
  items: ManualReviewItem[];
  collapsed?: boolean;
  onToggleCollapsed?: () => void;
};

export function ManualReviewPanel({
  items,
  collapsed,
  onToggleCollapsed,
}: Props) {
  const header = (
    <div className="card-header">
      <span>
        Manual review
        <span className="muted" style={{ fontWeight: 400 }}>
          {items.length > 0
            ? ` · ${items.length} flagged`
            : " · no issues"}
        </span>
      </span>
      {onToggleCollapsed && (
        <button onClick={onToggleCollapsed} className="icon-button">
          {collapsed ? "Expand" : "Collapse"}
        </button>
      )}
    </div>
  );

  if (collapsed) {
    return <div className="card manual-review-card collapsed">{header}</div>;
  }

  if (items.length === 0) {
    return (
      <div className="card manual-review-card">
        {header}
        <div className="empty-state small">
          No issues to review for this batch.
        </div>
      </div>
    );
  }

  return (
    <div className="card manual-review-card">
      {header}
      <div className="card-body tight manual-review-body">
        <table className="data-table">
          <thead>
            <tr>
              <th>Source File</th>
              <th>Account</th>
              <th>Invoice #</th>
              <th>Date</th>
              <th>Property</th>
              <th>Location</th>
              <th>Service Address</th>
              <th className="num">Total</th>
              <th>Reasons</th>
            </tr>
          </thead>
          <tbody>
            {items.map((it, i) => (
              <tr key={i}>
                <td>{it.source_file}</td>
                <td>{it.account_number}</td>
                <td>{it.invoice_number}</td>
                <td>{it.invoice_date}</td>
                <td>{it.property_abbreviation}</td>
                <td>{it.location}</td>
                <td>{it.service_address}</td>
                <td className="num">{it.total_amount.toFixed(2)}</td>
                <td>
                  {it.reasons.map((r) => (
                    <div key={r} title={REASON_HELP[r] ?? ""}>
                      <span
                        className="badge yellow"
                        style={{ marginRight: 4 }}
                      >
                        {r}
                      </span>
                    </div>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
