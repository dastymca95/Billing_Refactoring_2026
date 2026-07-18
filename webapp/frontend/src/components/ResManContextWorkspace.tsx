import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api, getFriendlyErrorMessage } from "../api";
import type {
  ResManContextRecord,
  ResManDatasetKind,
  ResManDatasetStatus,
  ResManImportPreview,
  ResManSnapshot,
} from "../types";


type Field = {
  key: string;
  label: string;
  type?: "text" | "boolean" | "select";
  options?: string[];
  required?: boolean;
};

type DatasetConfig = {
  eyebrow: string;
  title: string;
  description: string;
  expectedReport: string;
  columns: Field[];
  editFields: Field[];
  newDefaults: Record<string, unknown>;
};

const CONFIG: Record<ResManDatasetKind, DatasetConfig> = {
  vendors: {
    eyebrow: "RESMAN MASTER DATA",
    title: "Vendors",
    description: "Canonical vendor identities available to invoice processing.",
    expectedReport: "Vendor List CSV",
    columns: [
      { key: "company", label: "Company" },
      { key: "abbreviation", label: "Abbreviation" },
      { key: "status", label: "Status" },
      { key: "default_gl", label: "Default GL" },
      { key: "active", label: "Active", type: "boolean" },
    ],
    editFields: [
      { key: "company", label: "Company", required: true },
      { key: "abbreviation", label: "Abbreviation" },
      { key: "customer_number", label: "Customer #" },
      { key: "status", label: "Status" },
      { key: "general_contact", label: "General contact" },
      { key: "general_address", label: "Address" },
      { key: "general_city", label: "City" },
      { key: "general_state", label: "State" },
      { key: "general_zip", label: "ZIP" },
      { key: "general_phone", label: "Phone" },
      { key: "general_email", label: "Email" },
      { key: "workflow", label: "Workflow" },
      { key: "default_gl", label: "Default GL" },
      { key: "active", label: "Active", type: "boolean" },
      { key: "notes", label: "Notes" },
    ],
    newDefaults: { company: "", active: true },
  },
  properties_units: {
    eyebrow: "RESMAN MASTER DATA",
    title: "Properties & Units",
    description: "Property identities and the units that may receive invoice allocations.",
    expectedReport: "All Units CSV",
    columns: [
      { key: "entity_type", label: "Kind" },
      { key: "property_name", label: "Property" },
      { key: "property_code", label: "Code" },
      { key: "unit_number", label: "Unit" },
      { key: "unit_status", label: "Status" },
    ],
    editFields: [
      { key: "entity_type", label: "Kind", type: "select", options: ["property", "unit"], required: true },
      { key: "property_name", label: "Property name", required: true },
      { key: "property_code", label: "Property code" },
      { key: "unit_number", label: "Unit number" },
      { key: "unit_type", label: "Unit type" },
      { key: "unit_status", label: "Unit status" },
      { key: "square_feet", label: "Square feet" },
      { key: "lease_status", label: "Lease status" },
      { key: "market_rent", label: "Market rent" },
      { key: "active", label: "Active", type: "boolean" },
      { key: "notes", label: "Notes" },
    ],
    newDefaults: { entity_type: "property", property_name: "", active: true },
  },
  gl_accounts: {
    eyebrow: "ACCOUNTING REFERENCE",
    title: "Chart of GL Accounts",
    description: "The published chart defines which GL codes exist and which are payable.",
    expectedReport: "Chart Of Accounts CSV",
    columns: [
      { key: "gl_code", label: "GL code" },
      { key: "gl_name", label: "Account name" },
      { key: "account_type", label: "Type" },
      { key: "payable", label: "Payable", type: "boolean" },
      { key: "active", label: "Active", type: "boolean" },
    ],
    editFields: [
      { key: "gl_code", label: "GL code", required: true },
      { key: "gl_name", label: "Account name", required: true },
      { key: "account_type", label: "Account type" },
      { key: "description", label: "Description" },
      { key: "payable", label: "Payable", type: "boolean" },
      { key: "active", label: "Active", type: "boolean" },
      { key: "notes", label: "Notes" },
    ],
    newDefaults: { gl_code: "", gl_name: "", payable: false, active: true },
  },
  general_ledger: {
    eyebrow: "ACCOUNTING EVIDENCE",
    title: "General Ledger",
    description: "Historical transactions for review and candidate evidence; history never selects a GL by itself.",
    expectedReport: "General Ledger CSV",
    columns: [
      { key: "transaction_date", label: "Date" },
      { key: "account_code", label: "GL" },
      { key: "property_code", label: "Property" },
      { key: "resolved_vendor_name", label: "Resolved Vendor" },
      { key: "counterparty_name", label: "Source Name" },
      { key: "vendor_resolution_status", label: "Resolution" },
      { key: "invoice_history_reconciliation_status", label: "Invoice match" },
      { key: "debit", label: "Debit" },
      { key: "credit", label: "Credit" },
    ],
    editFields: [
      { key: "transaction_date", label: "Date" },
      { key: "account_code", label: "GL code" },
      { key: "account_name", label: "GL account name" },
      { key: "reference", label: "Reference" },
      { key: "property_code", label: "Property" },
      { key: "counterparty_name", label: "Name" },
      { key: "description", label: "Description" },
      { key: "debit", label: "Debit" },
      { key: "credit", label: "Credit" },
      { key: "balance", label: "Balance" },
      { key: "notes", label: "Correction / annotation" },
    ],
    newDefaults: {},
  },
  invoice_history: {
    eyebrow: "AP HISTORY & RECONCILIATION",
    title: "Invoice History",
    description: "Invoice allocations reconciled against vendor, property, GL and posting evidence. History never selects a GL by itself.",
    expectedReport: "Invoice Detail CSV",
    columns: [
      { key: "invoice_date", label: "Invoice date" },
      { key: "vendor_name", label: "Vendor" },
      { key: "invoice_number", label: "Invoice #" },
      { key: "property_code", label: "Property" },
      { key: "gl_code", label: "GL" },
      { key: "allocation_amount", label: "Allocation" },
      { key: "ledger_reconciliation_status", label: "Ledger match" },
    ],
    editFields: [
      { key: "invoice_occurrence_id", label: "Invoice occurrence", required: true },
      { key: "allocation_index", label: "Allocation index", required: true },
      { key: "vendor_name", label: "Vendor", required: true },
      { key: "invoice_number", label: "Invoice #", required: true },
      { key: "invoice_date", label: "Invoice date", required: true },
      { key: "accounting_date", label: "Accounting date" },
      { key: "due_date", label: "Due date" },
      { key: "invoice_description", label: "Invoice description" },
      { key: "invoice_total", label: "Invoice total", required: true },
      { key: "po_number", label: "PO" },
      { key: "batch", label: "Batch" },
      { key: "property_code", label: "Property", required: true },
      { key: "gl_code", label: "GL", required: true },
      { key: "allocation_description", label: "Allocation description" },
      { key: "allocation_amount", label: "Allocation amount", required: true },
      { key: "allocation_count", label: "Allocation count", required: true },
      { key: "invoice_reconciliation_status", label: "Invoice total status", type: "select", options: ["reconciled", "total_mismatch"], required: true },
      { key: "notes", label: "Correction / annotation" },
    ],
    newDefaults: {
      invoice_occurrence_id: "manual",
      allocation_index: 1,
      vendor_name: "",
      invoice_number: "",
      invoice_date: "",
      invoice_total: "0.00",
      property_code: "",
      gl_code: "",
      allocation_amount: "0.00",
      allocation_count: 1,
      invoice_reconciliation_status: "reconciled",
    },
  },
};


export function ResManContextWorkspace({ dataset }: { dataset: ResManDatasetKind }) {
  const config = CONFIG[dataset];
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [status, setStatus] = useState<ResManDatasetStatus | null>(null);
  const [snapshots, setSnapshots] = useState<ResManSnapshot[]>([]);
  const [items, setItems] = useState<ResManContextRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [searchDraft, setSearchDraft] = useState("");
  const [search, setSearch] = useState("");
  const [preview, setPreview] = useState<ResManImportPreview | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [editor, setEditor] = useState<{ key?: string; payload: Record<string, unknown> } | null>(null);

  const load = useCallback(async () => {
    setError("");
    try {
      const [statusResponse, pageResponse, snapshotResponse] = await Promise.all([
        api.getResManContextStatus(),
        api.listResManRecords(dataset, { page, pageSize: 50, search }),
        api.listResManSnapshots(dataset),
      ]);
      setStatus(statusResponse.datasets.find((item) => item.dataset === dataset) || null);
      setItems(pageResponse.items);
      setTotal(pageResponse.total);
      setSnapshots(snapshotResponse.items);
    } catch (reason) {
      setError(getFriendlyErrorMessage(reason));
    }
  }, [dataset, page, search]);

  useEffect(() => {
    setPage(1);
    setSearch("");
    setSearchDraft("");
    setPreview(null);
    setEditor(null);
  }, [dataset]);

  useEffect(() => { void load(); }, [load]);

  const pageCount = Math.max(1, Math.ceil(total / 50));
  const sourceLabel = status?.current_snapshot
    ? `${status.current_snapshot.original_filename} · ${shortHash(status.current_snapshot.sha256)}`
    : "No published ResMan snapshot";

  async function handleFile(file: File | undefined) {
    if (!file) return;
    setBusy("preview");
    setError("");
    try {
      setPreview(await api.previewResManImport(dataset, file));
    } catch (reason) {
      setError(getFriendlyErrorMessage(reason));
    } finally {
      setBusy("");
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  async function publish() {
    if (!preview || preview.status !== "preview_ready") return;
    setBusy("publish");
    setError("");
    try {
      await api.publishResManImport(dataset, preview.import_id);
      setPreview(null);
      setPage(1);
      await load();
    } catch (reason) {
      setError(getFriendlyErrorMessage(reason));
    } finally {
      setBusy("");
    }
  }

  async function saveEditor() {
    if (!editor) return;
    setBusy("save");
    setError("");
    try {
      if (editor.key) {
        await api.updateResManRecord(dataset, editor.key, editor.payload);
      } else {
        await api.createResManRecord(dataset, editor.payload);
      }
      setEditor(null);
      await load();
    } catch (reason) {
      setError(getFriendlyErrorMessage(reason));
    } finally {
      setBusy("");
    }
  }

  async function remove(item: ResManContextRecord) {
    const key = item._record.natural_key;
    if (!window.confirm("Remove this effective record? The source snapshot and audit history will be preserved.")) return;
    setBusy(`delete:${key}`);
    setError("");
    try {
      await api.deleteResManRecord(dataset, key);
      await load();
    } catch (reason) {
      setError(getFriendlyErrorMessage(reason));
    } finally {
      setBusy("");
    }
  }

  async function rollback(snapshot: ResManSnapshot) {
    if (snapshot.active) return;
    if (!window.confirm(`Activate snapshot ${shortHash(snapshot.sha256)}? Manual overlays will remain applied.`)) return;
    setBusy(`snapshot:${snapshot.snapshot_id}`);
    try {
      await api.activateResManSnapshot(dataset, snapshot.snapshot_id);
      await load();
    } catch (reason) {
      setError(getFriendlyErrorMessage(reason));
    } finally {
      setBusy("");
    }
  }

  return (
    <section className="resman-data-workspace" data-testid={`resman-workspace-${dataset}`}>
      <header className="resman-data-header">
        <div>
          <span>{config.eyebrow}</span>
          <h1>{config.title}</h1>
          <p>{config.description}</p>
        </div>
        <button type="button" className="resman-primary" onClick={() => setEditor({ payload: { ...config.newDefaults } })}>
          + Add record
        </button>
      </header>

      {error && <div className="resman-error" role="alert">{error}</div>}

      <div className="resman-data-summary">
        <article>
          <span>Published source</span>
          <strong>{sourceLabel}</strong>
          <small>{status?.current_snapshot ? new Date(status.current_snapshot.activated_at).toLocaleString() : "Upload the first report"}</small>
        </article>
        <article>
          <span>Effective records</span>
          <strong>{(status?.effective_record_count || 0).toLocaleString()}</strong>
          <small>{status?.manual_overlay_count || 0} manual overlays</small>
        </article>
        <article className="resman-upload-card">
          <span>Replace from ResMan</span>
          <strong>{config.expectedReport}</strong>
          <input ref={fileRef} type="file" accept=".csv,text/csv" onChange={(event) => void handleFile(event.target.files?.[0])} />
          <button type="button" onClick={() => fileRef.current?.click()} disabled={Boolean(busy)}>
            {busy === "preview" ? "Reading report…" : "Upload CSV & preview"}
          </button>
        </article>
      </div>

      {preview && (
        <section className={`resman-import-preview ${preview.status}`}>
          <div>
            <span>IMPORT PREVIEW · RAW PRESERVED</span>
            <h2>{preview.original_filename}</h2>
            <p>SHA-256 {shortHash(preview.sha256)} · {preview.parsed_records.toLocaleString()} canonical records</p>
          </div>
          <div className="resman-diff-grid">
            <Diff value={preview.added_records} label="Added" />
            <Diff value={preview.changed_records} label="Changed" />
            <Diff value={preview.removed_records} label="Removed" />
            <Diff value={preview.unchanged_records} label="Unchanged" />
          </div>
          {preview.issues.length > 0 && (
            <ul>{preview.issues.map((issue, index) => <li key={`${issue.code}-${index}`} className={issue.severity}>{issue.message}</li>)}</ul>
          )}
          {preview.excluded_sensitive_columns.length > 0 && (
            <p className="resman-privacy-note">Not copied into normalized data: {preview.excluded_sensitive_columns.join(", ")}.</p>
          )}
          <div className="resman-preview-actions">
            <button type="button" onClick={() => setPreview(null)}>Cancel</button>
            <button type="button" className="resman-primary" disabled={preview.status !== "preview_ready" || Boolean(busy)} onClick={() => void publish()}>
              {busy === "publish" ? "Publishing…" : "Publish snapshot"}
            </button>
          </div>
        </section>
      )}

      <div className="resman-record-toolbar">
        <form onSubmit={(event) => { event.preventDefault(); setPage(1); setSearch(searchDraft.trim()); }}>
          <input value={searchDraft} onChange={(event) => setSearchDraft(event.target.value)} placeholder={`Search ${config.title.toLowerCase()}…`} />
          <button type="submit">Search</button>
        </form>
        <span>{total.toLocaleString()} records · page {page} of {pageCount}</span>
      </div>

      <div className="resman-table-wrap">
        <table className="resman-data-table">
          <thead><tr>{config.columns.map((field) => <th key={field.key}>{field.label}</th>)}<th>Source</th><th /></tr></thead>
          <tbody>
            {items.map((item) => (
              <tr key={item._record.natural_key}>
                {config.columns.map((field) => <td key={field.key}>{displayValue(item[field.key], field.type)}</td>)}
                <td><span className={`resman-source ${item._record.source_kind}`}>{item._record.source_kind === "manual_overlay" ? "Manual" : "ResMan"}</span></td>
                <td className="resman-row-actions">
                  <button type="button" onClick={() => setEditor({ key: item._record.natural_key, payload: editablePayload(item, config.editFields) })}>Edit</button>
                  <button type="button" onClick={() => void remove(item)} disabled={busy === `delete:${item._record.natural_key}`}>Remove</button>
                </td>
              </tr>
            ))}
            {items.length === 0 && <tr><td colSpan={config.columns.length + 2} className="resman-empty">No records match this view.</td></tr>}
          </tbody>
        </table>
      </div>

      <footer className="resman-data-footer">
        <div>
          <button type="button" disabled={page <= 1} onClick={() => setPage((value) => Math.max(1, value - 1))}>Previous</button>
          <button type="button" disabled={page >= pageCount} onClick={() => setPage((value) => Math.min(pageCount, value + 1))}>Next</button>
        </div>
        {snapshots.length > 0 && (
          <details>
            <summary>Snapshot history ({snapshots.length})</summary>
            <ul>{snapshots.map((snapshot) => (
              <li key={snapshot.snapshot_id}>
                <span>{new Date(snapshot.created_at).toLocaleString()} · {snapshot.record_count.toLocaleString()} · {shortHash(snapshot.sha256)}</span>
                {snapshot.active ? <strong>Active</strong> : <button type="button" onClick={() => void rollback(snapshot)}>Activate</button>}
              </li>
            ))}</ul>
          </details>
        )}
      </footer>

      {editor && (
        <div className="resman-editor-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setEditor(null); }}>
          <section className="resman-record-editor" role="dialog" aria-modal="true" aria-label={editor.key ? "Edit record" : "Add record"}>
            <header><div><span>{editor.key ? "AUDITABLE OVERLAY" : "MANUAL MASTER DATA"}</span><h2>{editor.key ? "Edit record" : "Add record"}</h2></div><button type="button" onClick={() => setEditor(null)}>×</button></header>
            <div className="resman-editor-grid">
              {config.editFields.map((field) => <EditorField key={field.key} field={field} value={editor.payload[field.key]} onChange={(value) => setEditor((current) => current ? ({ ...current, payload: { ...current.payload, [field.key]: value } }) : current)} />)}
            </div>
            <p>Saving creates a tenant-scoped overlay. The imported raw report and snapshot remain unchanged.</p>
            <footer><button type="button" onClick={() => setEditor(null)}>Cancel</button><button type="button" className="resman-primary" disabled={busy === "save"} onClick={() => void saveEditor()}>{busy === "save" ? "Saving…" : "Save"}</button></footer>
          </section>
        </div>
      )}
    </section>
  );
}


function Diff({ value, label }: { value: number; label: string }) {
  return <div><strong>{value.toLocaleString()}</strong><span>{label}</span></div>;
}

function EditorField({ field, value, onChange }: { field: Field; value: unknown; onChange: (value: unknown) => void }) {
  if (field.type === "boolean") {
    return <label><span>{field.label}</span><select value={String(value ?? false)} onChange={(event) => onChange(event.target.value === "true")}><option value="true">Yes</option><option value="false">No</option></select></label>;
  }
  if (field.type === "select") {
    return <label><span>{field.label}</span><select value={String(value ?? "")} required={field.required} onChange={(event) => onChange(event.target.value)}>{field.options?.map((option) => <option key={option} value={option}>{option}</option>)}</select></label>;
  }
  return <label><span>{field.label}{field.required ? " *" : ""}</span><input value={String(value ?? "")} required={field.required} onChange={(event) => onChange(event.target.value)} /></label>;
}

function editablePayload(item: ResManContextRecord, fields: Field[]): Record<string, unknown> {
  return Object.fromEntries(fields.map((field) => [field.key, item[field.key] ?? (field.type === "boolean" ? false : "")]));
}

function displayValue(value: unknown, type?: Field["type"]): string {
  if (type === "boolean") return value ? "Yes" : "No";
  if (value == null || value === "") return "—";
  if (value === "exact") return "Exact vendor master";
  if (value === "ambiguous") return "Ambiguous";
  if (value === "unresolved") return "Not resolved";
  if (value === "missing_source_name") return "Source name missing";
  if (value === "matched_to_ledger") return "Matched to ledger";
  if (value === "posting_date_difference") return "Posting date differs";
  if (value === "amount_mismatch") return "Amount mismatch";
  if (value === "gl_mismatch") return "GL mismatch";
  if (value === "property_mismatch") return "Property mismatch";
  if (value === "invoice_only") return "Invoice only";
  if (value === "matched_to_invoice_history") return "Matched to invoice history";
  if (value === "ledger_only") return "Ledger only";
  if (value === "invoice_history_unavailable") return "Invoice history unavailable";
  return String(value);
}

function shortHash(value: string): string {
  return value ? `${value.slice(0, 10)}…${value.slice(-6)}` : "—";
}
