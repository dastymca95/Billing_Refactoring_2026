import { type Dispatch, type SetStateAction, useCallback, useEffect, useMemo, useState } from "react";

import { api, getFriendlyErrorMessage } from "../api";
import type {
  ContextIntelligenceStatus,
  DeterministicBuilderSession,
  DeterministicCoverage,
  PropertyContextProfile,
  VendorContextProfile,
} from "../types";
import { vendorRulesApi, type RuleField, type RuleGroup } from "../vendorRulesApi";


type Dimension = "vendors" | "properties";
type MatrixItem = VendorContextProfile | PropertyContextProfile;


export function ContextIntelligenceWorkspace() {
  const [status, setStatus] = useState<ContextIntelligenceStatus | null>(null);
  const [dimension, setDimension] = useState<Dimension>("vendors");
  const [items, setItems] = useState<MatrixItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [searchDraft, setSearchDraft] = useState("");
  const [search, setSearch] = useState("");
  const [mode, setMode] = useState("");
  const [selected, setSelected] = useState<MatrixItem | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const nextStatus = await api.getContextIntelligenceStatus();
      setStatus(nextStatus);
      if (nextStatus.state === "not_generated") {
        setItems([]);
        setTotal(0);
        return;
      }
      const matrix = await api.listContextMatrix({
        dimension, page, pageSize: 50, search, mode: dimension === "vendors" ? mode : "",
      });
      setItems(matrix.items);
      setTotal(matrix.total);
    } catch (reason) {
      setStatus(null);
      setItems([]);
      setTotal(0);
      setError(getFriendlyErrorMessage(reason));
    } finally {
      setLoading(false);
    }
  }, [dimension, mode, page, search]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => { setPage(1); setSelected(null); }, [dimension, mode, search]);

  const snapshot = status?.snapshot;
  const pageCount = Math.max(1, Math.ceil(total / 50));
  const sourceSummary = useMemo(() => Object.entries(snapshot?.source_hashes || {}), [snapshot]);

  async function scan() {
    setBusy("scan");
    setError("");
    try {
      await api.scanResManContext();
      setPage(1);
      await load();
    } catch (reason) {
      setError(getFriendlyErrorMessage(reason));
    } finally {
      setBusy("");
    }
  }

  async function saveGovernance(profile: VendorContextProfile) {
    setBusy("save");
    setError("");
    try {
      const saved = await api.updateVendorContextGovernance(profile.vendor_key, {
        governance_status: profile.governance_status,
        reviewer_notes: profile.reviewer_notes,
      });
      setSelected(saved);
      setItems((current) => current.map((item) => (
        "vendor_key" in item && item.vendor_key === saved.vendor_key ? saved : item
      )));
    } catch (reason) {
      setError(getFriendlyErrorMessage(reason));
    } finally {
      setBusy("");
    }
  }

  return (
    <section className="context-intelligence" data-testid="context-intelligence-workspace">
      <header className="context-intelligence-header">
        <div>
          <span>RESMAN ONBOARDING INTELLIGENCE</span>
          <h1>Context Matrix</h1>
          <p>Crosses vendor, property, GL, invoice and ledger evidence. Recommendations never become rules without approval.</p>
        </div>
        {status ? <button type="button" className="context-scan-button" disabled={busy === "scan" || loading || Boolean(status.missing_datasets.length)} onClick={() => void scan()}>
          {busy === "scan" ? "Scanning ResMan…" : status.state === "not_generated" ? "Scan ResMan" : "Scan ResMan again"}
        </button> : <button type="button" className="context-scan-button" disabled={loading} onClick={() => void load()}>{loading ? "Connecting…" : "Retry connection"}</button>}
      </header>

      {error && <div className="context-error" role="alert">{error}</div>}
      {status?.missing_datasets.length ? (
        <div className="context-error">Publish these datasets before scanning: {status.missing_datasets.join(", ")}.</div>
      ) : null}

      {loading && !status ? (
        <div className="context-empty-state" data-testid="context-loading-state">
          <div className="context-scan-orbit" aria-hidden>◎</div>
          <h2>Checking ResMan context…</h2>
          <p>Verifying the five published datasets and the current matrix state.</p>
        </div>
      ) : !status ? (
        <div className="context-empty-state" data-testid="context-unavailable-state">
          <h2>Context Intelligence is unavailable</h2>
          <p>The application could not verify the backend state. No scan was started and no zero-value matrix was created.</p>
          <button type="button" className="context-scan-button" onClick={() => void load()}>Retry connection</button>
        </div>
      ) : status.state === "not_generated" ? (
        <div className="context-empty-state">
          <div className="context-scan-orbit" aria-hidden>◎</div>
          <h2>Context has not been scanned</h2>
          <p>Press <strong>Scan ResMan</strong> to calculate the cross-report matrix from the five published datasets.</p>
          <ul>
            <li>No AI provider call is made for statistical facts.</li>
            <li>No deterministic rule is created or activated.</li>
            <li>The generated snapshot records every source hash.</li>
          </ul>
        </div>
      ) : status.snapshot ? (
        <>
          {status?.state === "stale" && <div className="context-stale">Source reports changed after this matrix was generated. Scan again before relying on its evidence.</div>}
          <section className="context-metrics" aria-label="Context scan summary">
            <Metric label="Vendors" value={snapshot?.vendor_count || 0} />
            <Metric label="Properties" value={snapshot?.property_count || 0} />
            <Metric label="Invoices" value={snapshot?.invoice_count || 0} />
            <Metric label="Allocations" value={snapshot?.allocation_count || 0} />
            <Metric label="GL accounts" value={snapshot?.gl_account_count || 0} />
            <Metric label="Ledger postings" value={snapshot?.ledger_record_count || 0} />
            <Metric label="Deterministic candidates" value={snapshot?.deterministic_candidate_count || 0} accent />
            <Metric label="Review candidates" value={snapshot?.review_candidate_count || 0} />
          </section>

          <div className="context-toolbar">
            <div className="context-dimension-tabs">
              <button type="button" className={dimension === "vendors" ? "active" : ""} onClick={() => setDimension("vendors")}>Vendor matrix</button>
              <button type="button" className={dimension === "properties" ? "active" : ""} onClick={() => setDimension("properties")}>Property matrix</button>
            </div>
            <form onSubmit={(event) => { event.preventDefault(); setSearch(searchDraft); }}>
              <input value={searchDraft} onChange={(event) => setSearchDraft(event.target.value)} placeholder="Search matrix…" />
              <button type="submit">Search</button>
            </form>
            {dimension === "vendors" && (
              <select aria-label="Recommendation filter" value={mode} onChange={(event) => setMode(event.target.value)}>
                <option value="">All recommendations</option>
                <option value="deterministic_candidate">Deterministic candidates</option>
                <option value="review_candidate">Review candidates</option>
                <option value="variable">Variable</option>
                <option value="insufficient_history">Insufficient history</option>
              </select>
            )}
          </div>

          <div className="context-table-wrap">
            {dimension === "vendors" ? (
              <VendorMatrix items={items as VendorContextProfile[]} onOpen={setSelected} />
            ) : (
              <PropertyMatrix items={items as PropertyContextProfile[]} onOpen={setSelected} />
            )}
          </div>

          <footer className="context-footer">
            <span>{total.toLocaleString()} profiles · Double-click a row for full detail</span>
            <div><button type="button" disabled={page <= 1} onClick={() => setPage((value) => value - 1)}>Previous</button><span>{page} / {pageCount}</span><button type="button" disabled={page >= pageCount} onClick={() => setPage((value) => value + 1)}>Next</button></div>
          </footer>

          <details className="context-provenance">
            <summary>Snapshot provenance</summary>
            <p>Generated {snapshot ? new Date(snapshot.generated_at).toLocaleString() : "—"} · {snapshot?.analytics_version}</p>
            {sourceSummary.map(([dataset, hash]) => <code key={dataset}>{dataset}: {shortHash(hash)}</code>)}
          </details>
        </>
      ) : (
        <div className="context-empty-state" data-testid="context-invalid-state">
          <h2>Matrix state is incomplete</h2>
          <p>The backend did not provide a valid snapshot. Scan has not been assumed successful.</p>
          <button type="button" className="context-scan-button" onClick={() => void load()}>Refresh status</button>
        </div>
      )}

      {selected && (
        <ContextDetailModal
          item={selected}
          busy={busy === "save"}
          onChange={setSelected}
          onSave={saveGovernance}
          onClose={() => setSelected(null)}
        />
      )}
    </section>
  );
}


function VendorMatrix({ items, onOpen }: { items: VendorContextProfile[]; onOpen: (item: VendorContextProfile) => void }) {
  return <table><thead><tr><th>Vendor</th><th>Parser</th><th>Invoices</th><th>Ledger</th><th>Top GL</th><th>GL concentration</th><th>Top property</th><th>Total</th><th>Pattern score</th><th>Recommendation</th><th>Review</th></tr></thead><tbody>
    {items.map((item) => <tr key={item.vendor_key} tabIndex={0} onDoubleClick={() => onOpen(item)} onKeyDown={(event) => { if (event.key === "Enter") onOpen(item); }}>
      <td><strong>{item.vendor_name}</strong><small>{item.vendor_abbreviation || "No abbreviation"}</small></td>
      <td><DeterministicStatus coverage={item.deterministic_coverage} /></td>
      <td>{item.invoice_count.toLocaleString()}<small>{item.active_months} active months</small></td>
      <td>{item.ledger_posting_count.toLocaleString()}<small>{money(item.ledger_total_amount)}</small></td>
      <td>{frequencyLabel(item.gl_usage[0])}</td>
      <td><ShareBar value={item.top_gl_share} /></td>
      <td>{frequencyLabel(item.property_usage[0])}</td>
      <td>{money(item.total_amount)}</td>
      <td>{Math.round(item.statistical_score * 100)}%</td>
      <td><span className={`context-badge ${item.recommended_mode}`}>{recommendationLabel(item.recommended_mode)}</span></td>
      <td>{governanceLabel(item.governance_status)}</td>
    </tr>)}
    {!items.length && <tr><td colSpan={11} className="context-no-rows">No profiles match this view.</td></tr>}
  </tbody></table>;
}


function PropertyMatrix({ items, onOpen }: { items: PropertyContextProfile[]; onOpen: (item: PropertyContextProfile) => void }) {
  return <table><thead><tr><th>Property</th><th>Invoices</th><th>Allocations</th><th>Ledger</th><th>Top GL</th><th>Top vendor</th><th>Total</th></tr></thead><tbody>
    {items.map((item) => <tr key={item.property_key} tabIndex={0} onDoubleClick={() => onOpen(item)} onKeyDown={(event) => { if (event.key === "Enter") onOpen(item); }}>
      <td><strong>{item.property_code || "—"}</strong><small>{item.property_name}</small></td>
      <td>{item.invoice_count.toLocaleString()}</td><td>{item.allocation_count.toLocaleString()}</td><td>{item.ledger_posting_count.toLocaleString()}</td>
      <td>{frequencyLabel(item.gl_usage[0])}</td><td>{frequencyLabel(item.vendor_usage[0])}</td><td>{money(item.total_amount)}</td>
    </tr>)}
    {!items.length && <tr><td colSpan={7} className="context-no-rows">No profiles match this view.</td></tr>}
  </tbody></table>;
}


function ContextDetailModal({ item, busy, onChange, onSave, onClose }: {
  item: MatrixItem;
  busy: boolean;
  onChange: (item: MatrixItem) => void;
  onSave: (item: VendorContextProfile) => Promise<void>;
  onClose: () => void;
}) {
  const vendor = "vendor_key" in item ? item : null;
  const property = vendor ? null : item as PropertyContextProfile;
  const title = vendor ? vendor.vendor_name : property!.property_name;
  const glUsage = item.gl_usage;
  const secondary = vendor ? vendor.property_usage : property!.vendor_usage;
  return <div className="context-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="context-detail-modal" role="dialog" aria-modal="true" aria-label={`${title} context detail`}>
      <header><div><span>CROSS-REPORT PROFILE</span><h2>{title}</h2><p>{vendor ? `${vendor.invoice_count} invoices · ${vendor.allocation_count} allocations` : `${item.invoice_count} invoices · ${item.allocation_count} allocations`}</p></div><button type="button" onClick={onClose}>×</button></header>
      {vendor && <section className="context-reasoning"><h3>Why this recommendation</h3>{vendor.recommendation_reasons.map((reason) => <p key={reason}>{reason}</p>)}</section>}
      {vendor && <DeterministicCoveragePanel vendor={vendor} />}
      <div className="context-detail-columns">
        <FrequencyList title="GL usage" items={glUsage} />
        <FrequencyList title={vendor ? "Property usage" : "Vendor usage"} items={secondary} />
      </div>
      {vendor && <section className="context-governance-editor">
        <h3>Human governance</h3>
        <label><span>Decision</span><select value={vendor.governance_status} onChange={(event) => onChange({ ...vendor, governance_status: event.target.value as VendorContextProfile["governance_status"] })}><option value="unreviewed">Unreviewed</option><option value="approved_candidate">Approved candidate</option><option value="needs_review">Needs review</option><option value="excluded">Excluded</option></select></label>
        <label><span>Reviewer notes</span><textarea value={vendor.reviewer_notes || ""} onChange={(event) => onChange({ ...vendor, reviewer_notes: event.target.value })} placeholder="Document the accounting context or reason for this decision." /></label>
        <p>Approval keeps this as candidate evidence. It does not activate a deterministic rule.</p>
      </section>}
      <footer><button type="button" onClick={onClose}>Close</button>{vendor && <button type="button" className="context-primary" disabled={busy} onClick={() => void onSave(vendor)}>{busy ? "Saving…" : "Save review"}</button>}</footer>
    </section>
  </div>;
}


function DeterministicStatus({ coverage }: { coverage: VendorContextProfile["deterministic_coverage"] }) {
  if (!coverage) return <span className="context-parser none" title="No registered deterministic processor" aria-label="No deterministic parser">—</span>;
  const healthy = coverage.status === "active" && coverage.processor_available;
  return <span
    className={`context-parser ${healthy ? "active" : "warning"}`}
    title={`${coverage.display_name}: ${coverage.status}`}
    aria-label={healthy ? "Deterministic parser active" : `Deterministic parser ${coverage.status}`}
  >{healthy ? "✓" : "!"}<small>{coverage.implementation_kind === "hybrid" ? "Rules" : "Code"}</small></span>;
}


function DeterministicCoveragePanel({ vendor }: { vendor: VendorContextProfile }) {
  const coverage = vendor.deterministic_coverage;
  const [groups, setGroups] = useState<RuleGroup[]>([]);
  const [edits, setEdits] = useState<Record<string, unknown>>({});
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  useEffect(() => {
    setGroups([]);
    setEdits({});
    setMessage("");
    if (!coverage?.editable) return;
    let cancelled = false;
    setBusy(true);
    vendorRulesApi.get(coverage.vendor_key)
      .then((payload) => {
        if (!cancelled) setGroups(payload.groups);
      })
      .catch((reason) => { if (!cancelled) setMessage(getFriendlyErrorMessage(reason)); })
      .finally(() => { if (!cancelled) setBusy(false); });
    return () => { cancelled = true; };
  }, [coverage?.editable, coverage?.vendor_key]);

  if (!coverage) return <section className="context-parser-detail none"><h3>Deterministic processor</h3><p>No registered deterministic processor matched this vendor identity.</p></section>;

  const save = async () => {
    if (!Object.keys(edits).length) return;
    setBusy(true);
    setMessage("");
    try {
      const validation = await vendorRulesApi.validate(coverage.vendor_key, edits);
      if (!validation.ok) {
        setMessage(validation.issues.map((item) => `${item.path}: ${item.message}`).join(" · "));
        return;
      }
      const saved = await vendorRulesApi.patch(coverage.vendor_key, edits);
      setGroups(saved.groups);
      setEdits({});
      setMessage(`Saved with backup ${saved.result.backup_filename}. The processor will use the configuration on its next run.`);
    } catch (reason) {
      setMessage(getFriendlyErrorMessage(reason));
    } finally {
      setBusy(false);
    }
  };

  return <section className="context-parser-detail">
    <div className="context-parser-heading"><div><h3>Deterministic processor</h3><p><strong>{coverage.status === "active" ? "Active" : coverage.status}</strong> · {coverage.implementation_kind === "hybrid" ? "Python processor + declarative rules" : "Python code managed"}</p></div><DeterministicStatus coverage={coverage} /></div>
    <dl><div><dt>Entrypoint</dt><dd>{coverage.processor_entrypoint}</dd></div><div><dt>Module</dt><dd>{coverage.processor_module}</dd></div><div><dt>Configuration</dt><dd>{coverage.config_name || "No verified declarative configuration"}</dd></div></dl>
    {!coverage.editable && <p className="context-parser-notice">The processing code is inspect-only here. Add a registry-keyed declarative contract before browser editing can be enabled safely.</p>}
    {coverage.editable && <>
      <h4>Editable matching patterns</h4>
      <p className="context-parser-notice">These fields affect detection/extraction on future runs. They do not edit Python, activate learned rules, select GL, or bypass readiness.</p>
      {busy && !groups.length ? <p>Loading declarative patterns…</p> : null}
      <RuleFields fields={(groups.find((item) => item.key === "deterministic_patterns")?.fields || [])} edits={edits} setEdits={setEdits} />
      {!busy && groups.length > 0 && !(groups.find((item) => item.key === "deterministic_patterns")?.fields.length) ? <p>No editable pattern lists are declared in this vendor contract.</p> : null}
      {groups.filter((item) => item.key !== "deterministic_patterns" && item.fields.some((field) => field.editable)).length > 0 && <details className="context-logic-editor"><summary>Edit other declarative processor logic</summary>{groups.filter((item) => item.key !== "deterministic_patterns" && item.fields.some((field) => field.editable)).map((item) => <section key={item.key}><h5>{item.label}</h5>{item.description && <p>{item.description}</p>}<RuleFields fields={item.fields.filter((field) => field.editable)} edits={edits} setEdits={setEdits} /></section>)}</details>}
      <div className="context-pattern-actions"><button type="button" disabled={busy || !Object.keys(edits).length} onClick={() => setEdits({})}>Reset patterns</button><button type="button" className="context-primary" disabled={busy || !Object.keys(edits).length} onClick={() => void save()}>{busy ? "Saving…" : "Validate & save patterns"}</button></div>
    </>}
    {message && <p className="context-parser-message" role="status">{message}</p>}
    {coverage.editable && <DeterministicBuilderPanel coverage={coverage} />}
  </section>;
}


function DeterministicBuilderPanel({ coverage }: { coverage: DeterministicCoverage }) {
  const [session, setSession] = useState<DeterministicBuilderSession | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [selectedColumn, setSelectedColumn] = useState<string | null>(null);
  const [approvalConfirmed, setApprovalConfirmed] = useState(false);

  const start = async () => {
    setBusy("start"); setError("");
    try { setSession(await api.createDeterministicBuilderSession(coverage.vendor_key)); }
    catch (reason) { setError(getFriendlyErrorMessage(reason)); }
    finally { setBusy(""); }
  };

  const upload = async (files: FileList | null) => {
    if (!session || !files?.length) return;
    setBusy("upload"); setError("");
    try {
      let current = session;
      for (const file of Array.from(files)) current = await api.uploadDeterministicBuilderSample(current.session_id, file);
      setSession(current);
    } catch (reason) { setError(getFriendlyErrorMessage(reason)); }
    finally { setBusy(""); }
  };

  const send = async () => {
    if (!session || !message.trim()) return;
    setBusy("chat"); setError("");
    try {
      setSession(await api.chatDeterministicBuilder(session.session_id, message.trim(), selectedColumn));
      setMessage("");
    } catch (reason) { setError(getFriendlyErrorMessage(reason)); }
    finally { setBusy(""); }
  };

  const runPreview = async () => {
    if (!session) return;
    setBusy("preview"); setError(""); setSelectedColumn(null); setApprovalConfirmed(false);
    try { setSession(await api.previewDeterministicBuilder(session.session_id)); }
    catch (reason) { setError(getFriendlyErrorMessage(reason)); }
    finally { setBusy(""); }
  };

  const approve = async () => {
    if (!session || !approvalConfirmed) return;
    setBusy("approve"); setError("");
    try { setSession(await api.approveDeterministicBuilder(session.session_id, session.revision)); }
    catch (reason) { setError(getFriendlyErrorMessage(reason)); }
    finally { setBusy(""); }
  };

  if (!session) return <section className="deterministic-builder-launch">
    <div><h3>AI Deterministic Builder</h3><p>Train improvements from private samples, converse naturally, inspect a row preview and approve only when satisfied.</p></div>
    <button type="button" className="context-primary" disabled={busy === "start"} onClick={() => void start()}>{busy === "start" ? "Opening…" : "Open builder"}</button>
    {error && <p className="context-builder-error">{error}</p>}
  </section>;

  const draftEntries = Object.entries(session.draft_patch);
  const canPreview = session.samples.length > 0 && draftEntries.length > 0 && !session.validation_issues.length;
  const canApprove = session.preview.status === "passed" && session.preview.revision === session.revision && session.status !== "approved";
  return <section className="deterministic-builder" aria-label="AI Deterministic Builder">
    <header><div><span>PRIVATE · HUMAN-GOVERNED</span><h3>AI Deterministic Builder</h3><p>Revision {session.revision} · {session.status}</p></div><strong>{session.samples.length} samples</strong></header>
    <div className="deterministic-builder-samples">
      <div><h4>Training samples</h4><p>PDF, image, CSV or XLSX. Files stay in private runtime storage and are never committed.</p></div>
      <label className="context-upload-samples"><input type="file" multiple accept=".pdf,.png,.jpg,.jpeg,.tif,.tiff,.csv,.xlsx" disabled={Boolean(busy)} onChange={(event) => { void upload(event.target.files); event.currentTarget.value = ""; }} /><span>{busy === "upload" ? "Uploading…" : "Add samples"}</span></label>
      <ul>{session.samples.map((sample) => <li key={sample.sample_id}><strong>{sample.original_filename}</strong><span>{sample.source_type} · {sample.page_count || 1} page(s) · {Math.ceil(sample.size_bytes / 1024)} KB</span></li>)}</ul>
    </div>
    <div className="deterministic-builder-grid">
      <section className="deterministic-builder-chat"><h4>Conversation</h4><div className="deterministic-builder-messages">{session.messages.map((item) => <article key={item.message_id} className={item.role}><strong>{item.role === "user" ? "You" : item.role === "assistant" ? "Accounting AI" : "System"}</strong><p>{item.content}</p>{item.provider_profile_id && <small>{item.provider_profile_id} · est. ${item.estimated_cost_usd.toFixed(4)}</small>}</article>)}</div>{selectedColumn && <p className="context-selected-column">Instruction scope: <strong>{selectedColumn}</strong> <button type="button" onClick={() => setSelectedColumn(null)}>Clear</button></p>}<div className="deterministic-builder-composer"><textarea value={message} onChange={(event) => setMessage(event.target.value)} placeholder={selectedColumn ? `Tell the AI what should change in ${selectedColumn}…` : "Describe what the deterministic processor should recognize or change…"} /><button type="button" disabled={Boolean(busy) || !message.trim()} onClick={() => void send()}>{busy === "chat" ? "Thinking…" : "Send"}</button></div></section>
      <section className="deterministic-builder-draft"><h4>Proposed declarative changes</h4>{draftEntries.map(([path, value]) => <article key={path}><strong>{path}</strong><code>{JSON.stringify(value)}</code><p>{session.draft_rationales[path] || "Validated declarative proposal."}</p></article>)}{!draftEntries.length && <p>No changes proposed yet. Upload samples and explain the behavior you want.</p>}{session.validation_issues.map((issue, index) => <p className="context-builder-error" key={`${issue.path}-${index}`}>{issue.path}: {issue.message}</p>)}<button type="button" disabled={Boolean(busy) || !canPreview} onClick={() => void runPreview()}>{busy === "preview" ? "Running dry-run…" : "Preview against samples"}</button></section>
    </div>
    {session.preview.status !== "not_run" && <section className="deterministic-builder-preview"><header><div><h4>Dry-run row preview</h4><p>{session.preview.row_count} row(s). Click a column header, then describe the correction in chat.</p></div><span className={session.preview.status}>{session.preview.status}</span></header>{session.preview.warnings.map((warning) => <p className="context-builder-error" key={warning}>{warning}</p>)}<div><table><thead><tr>{session.preview.columns.map((column) => <th key={column} className={selectedColumn === column ? "selected" : ""}><button type="button" onClick={() => setSelectedColumn(column)}>{column}</button></th>)}</tr></thead><tbody>{session.preview.rows.slice(0, 100).map((row, rowIndex) => <tr key={rowIndex}>{session.preview.columns.map((column) => <td key={column} className={selectedColumn === column ? "selected" : ""}>{String(row[column] ?? "")}</td>)}</tr>)}</tbody></table></div></section>}
    {canApprove && <section className="deterministic-builder-approval"><label><input type="checkbox" checked={approvalConfirmed} onChange={(event) => setApprovalConfirmed(event.target.checked)} /> I reviewed the current revision and its sample preview.</label><button type="button" className="context-primary" disabled={Boolean(busy) || !approvalConfirmed} onClick={() => void approve()}>{busy === "approve" ? "Applying…" : "Approve this revision"}</button><p>Approval writes only the validated declarative patch, creates a backup and affects future runs. It does not authorize export.</p></section>}
    {session.status === "approved" && <p className="context-builder-success">Approved and versioned. The updated deterministic configuration will be used on future processing runs.</p>}
    {error && <p className="context-builder-error" role="alert">{error}</p>}
  </section>;
}


function RuleFields({ fields, edits, setEdits }: { fields: RuleField[]; edits: Record<string, unknown>; setEdits: Dispatch<SetStateAction<Record<string, unknown>>> }) {
  return <div className="context-pattern-grid">{fields.map((field) => <RuleValueField key={field.path} field={field} value={Object.prototype.hasOwnProperty.call(edits, field.path) ? edits[field.path] : field.value} onChange={(value) => setEdits((current) => ({ ...current, [field.path]: value }))} />)}</div>;
}


function RuleValueField({ field, value, onChange }: { field: RuleField; value: unknown; onChange: (value: unknown) => void }) {
  let control;
  if (field.type === "string_list") {
    const current = Array.isArray(value) ? value.map(String) : [];
    control = <textarea value={current.join("\n")} onChange={(event) => onChange(event.target.value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean))} />;
  } else if (field.type === "boolean") {
    control = <input type="checkbox" checked={Boolean(value)} onChange={(event) => onChange(event.target.checked)} />;
  } else if (field.type === "enum") {
    control = <select value={String(value ?? "")} onChange={(event) => onChange(event.target.value)}>{(field.options || []).map((option) => <option key={option} value={option}>{option}</option>)}</select>;
  } else if (field.type === "integer" || field.type === "number") {
    control = <input type="number" value={String(value ?? "")} onChange={(event) => onChange(field.type === "integer" ? Number.parseInt(event.target.value, 10) : Number(event.target.value))} />;
  } else {
    control = <input value={String(value ?? "")} onChange={(event) => onChange(event.target.value)} />;
  }
  return <label><span>{field.label}</span>{control}<small>{field.path}</small></label>;
}


function FrequencyList({ title, items }: { title: string; items: { key: string; label: string; count: number; amount: string; share: number }[] }) {
  return <section><h3>{title}</h3><div className="context-frequency-list">{items.slice(0, 12).map((item) => <div key={item.key}><div><strong>{item.label}</strong><span>{item.count} uses · {money(item.amount)}</span></div><ShareBar value={item.share} /></div>)}{!items.length && <p>No observed history.</p>}</div></section>;
}


function Metric({ label, value, accent = false }: { label: string; value: number; accent?: boolean }) {
  return <div className={accent ? "accent" : ""}><strong>{value.toLocaleString()}</strong><span>{label}</span></div>;
}

function ShareBar({ value }: { value: number }) {
  return <div className="context-share"><span style={{ width: `${Math.max(0, Math.min(100, value * 100))}%` }} /><strong>{Math.round(value * 100)}%</strong></div>;
}

function frequencyLabel(item?: { label: string; count: number }): string {
  return item ? `${item.label} · ${item.count}` : "—";
}

function money(value: string): string {
  const number = Number(value);
  return Number.isFinite(number) ? number.toLocaleString(undefined, { style: "currency", currency: "USD" }) : value;
}

function recommendationLabel(value: VendorContextProfile["recommended_mode"]): string {
  return ({ deterministic_candidate: "Deterministic candidate", review_candidate: "Review candidate", variable: "Variable", insufficient_history: "Insufficient history" })[value];
}

function governanceLabel(value: VendorContextProfile["governance_status"]): string {
  return ({ unreviewed: "Unreviewed", approved_candidate: "Approved candidate", excluded: "Excluded", needs_review: "Needs review" })[value];
}

function shortHash(value: string): string {
  return `${value.slice(0, 8)}…${value.slice(-6)}`;
}
