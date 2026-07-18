import { useEffect, useMemo, useState } from "react";

import { api, getFriendlyErrorMessage } from "../api";
import type {
  ApprovedInvoiceCorrection,
  BatchListEntry,
  OperatorAccountingRule,
  OperatorActivityEvent,
  RevisionEntry,
} from "../types";
import { TenantPolicyGovernancePanel } from "./TenantPolicyGovernancePanel";

type EditableRule = Pick<OperatorAccountingRule, "title" | "description" | "scope" | "constraint">;

export function AccountingRulesWorkspace() {
  const [rules, setRules] = useState<OperatorAccountingRule[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [draft, setDraft] = useState<EditableRule | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [section, setSection] = useState<"tenant" | "rules" | "corrections" | "history">("tenant");
  const [corrections, setCorrections] = useState<ApprovedInvoiceCorrection[]>([]);
  const [batches, setBatches] = useState<BatchListEntry[]>([]);
  const [correctionBatchId, setCorrectionBatchId] = useState("");
  const [historyBatchId, setHistoryBatchId] = useState("");
  const [activity, setActivity] = useState<OperatorActivityEvent[]>([]);
  const [historyRevisions, setHistoryRevisions] = useState<RevisionEntry[]>([]);
  const selected = useMemo(() => rules.find((rule) => rule.rule_id === selectedId) || null, [rules, selectedId]);

  const load = async () => {
    setBusy("load");
    setError("");
    try {
      const response = await api.listAccountingRules();
      setRules(response.items);
      setSelectedId((current) => response.items.some((rule) => rule.rule_id === current)
        ? current : (response.items[0]?.rule_id || ""));
    } catch (reason) {
      setError(getFriendlyErrorMessage(reason, "Load accounting rules"));
    } finally {
      setBusy("");
    }
  };

  const loadGovernanceHistory = async () => {
    try {
      const [correctionResponse, batchResponse] = await Promise.all([
        api.listApprovedAccountingCorrections(),
        api.listBatches(),
      ]);
      setCorrections(correctionResponse.items);
      setBatches(batchResponse.batches);
      setHistoryBatchId((current) => current || batchResponse.batches[0]?.batch_id || "");
    } catch (reason) {
      setError(getFriendlyErrorMessage(reason, "Load accounting history"));
    }
  };

  useEffect(() => { void load(); }, []);
  useEffect(() => { void loadGovernanceHistory(); }, []);
  useEffect(() => {
    if (!historyBatchId) {
      setActivity([]);
      setHistoryRevisions([]);
      return;
    }
    void Promise.all([
      api.listBatchActivity(historyBatchId),
      api.listRevisions(historyBatchId),
    ]).then(([activityResponse, revisionResponse]) => {
      setActivity(activityResponse.items);
      setHistoryRevisions(revisionResponse.revisions);
    }).catch((reason) => setError(getFriendlyErrorMessage(reason, "Load batch history")));
  }, [historyBatchId]);
  useEffect(() => {
    if (!selected) {
      setDraft(null);
      return;
    }
    setDraft(JSON.parse(JSON.stringify({
      title: selected.title,
      description: selected.description,
      scope: selected.scope,
      constraint: selected.constraint,
    })));
  }, [selectedId, selected?.updated_at]);

  const replace = (updated: OperatorAccountingRule) => {
    setRules((current) => current.map((rule) => rule.rule_id === updated.rule_id ? updated : rule));
  };

  const save = async () => {
    if (!selected || !draft) return;
    setBusy("save");
    setError("");
    try {
      replace(await api.updateAccountingRule(selected.rule_id, draft));
    } catch (reason) {
      setError(getFriendlyErrorMessage(reason, "Save accounting rule"));
    } finally {
      setBusy("");
    }
  };

  const decide = async (approve: boolean) => {
    if (!selected) return;
    setBusy("decision");
    setError("");
    try {
      replace(await api.decideAccountingRule(selected.rule_id, approve));
    } catch (reason) {
      setError(getFriendlyErrorMessage(reason, "Decide accounting rule"));
    } finally {
      setBusy("");
    }
  };

  const toggle = async () => {
    if (!selected || !["active", "disabled"].includes(selected.status)) return;
    setBusy("toggle");
    setError("");
    try {
      replace(await api.setAccountingRuleEnabled(selected.rule_id, selected.status !== "active"));
    } catch (reason) {
      setError(getFriendlyErrorMessage(reason, "Update accounting rule status"));
    } finally {
      setBusy("");
    }
  };

  const correctionGroups = useMemo(() => groupCorrections(corrections), [corrections]);

  return <main className="assistant-module-shell rules-library" aria-label="Accounting rules library">
    <header className="assistant-module-header">
      <div>
        <span className="assistant-eyebrow">Human-approved policy</span>
        <h1>Accounting Rules</h1>
        <p>Edit, audit, enable or disable reusable semantic GL constraints.</p>
      </div>
      <button type="button" onClick={() => void Promise.all([load(), loadGovernanceHistory()])} disabled={!!busy}>Refresh</button>
    </header>
    {error && <div className="assistant-error" role="alert">{error}</div>}
    <nav className="rules-library-tabs" aria-label="Accounting governance sections">
      <button type="button" className={section === "tenant" ? "is-active" : ""} onClick={() => setSection("tenant")}>Tenant policies</button>
      <button type="button" className={section === "rules" ? "is-active" : ""} onClick={() => setSection("rules")}>Reusable rules <span>{rules.length}</span></button>
      <button type="button" className={section === "corrections" ? "is-active" : ""} onClick={() => setSection("corrections")}>Approved corrections <span>{correctionGroups.length}</span></button>
      <button type="button" className={section === "history" ? "is-active" : ""} onClick={() => setSection("history")}>Batch & file history</button>
    </nav>
    {section === "tenant" && <TenantPolicyGovernancePanel />}
    {section === "rules" && <div className="rules-library-grid">
      <aside className="rules-library-list">
        <div className="rules-library-count">{rules.filter((rule) => rule.status === "active").length} active · {rules.length} total</div>
        {rules.length === 0 && <p>No rule proposals yet. Create one from Invoice Assistant.</p>}
        {rules.map((rule) => <button type="button" key={rule.rule_id}
          className={rule.rule_id === selectedId ? "is-selected" : ""}
          onClick={() => setSelectedId(rule.rule_id)}>
          <strong>{rule.title}</strong>
          <span className={`rule-status-pill is-${rule.status}`}>{rule.status}</span>
          <small>{rule.description}</small>
        </button>)}
      </aside>
      <section className="rules-library-editor">
        {!selected || !draft ? <div className="rules-library-empty">Select a rule to inspect it.</div> : <>
          <div className="rules-editor-heading">
            <div><code>{selected.rule_id}</code><span className={`rule-status-pill is-${selected.status}`}>{selected.status}</span></div>
            <div className="assistant-rule-actions">
              {selected.status === "draft" && <>
                <button className="assistant-primary" type="button" disabled={!!busy} onClick={() => void decide(true)}>Approve</button>
                <button type="button" disabled={!!busy} onClick={() => void decide(false)}>Reject</button>
              </>}
              {["active", "disabled"].includes(selected.status) && <button type="button" disabled={!!busy} onClick={() => void toggle()}>
                {selected.status === "active" ? "Disable" : "Enable"}
              </button>}
            </div>
          </div>
          <label>Rule title<input value={draft.title} onChange={(event) => setDraft({ ...draft, title: event.target.value })} /></label>
          <label>Description<textarea value={draft.description} onChange={(event) => setDraft({ ...draft, description: event.target.value })} /></label>
          <div className="rules-form-grid">
            <TextField label="Document family" value={draft.scope.document_family || ""}
              onChange={(value) => setDraft({ ...draft, scope: { ...draft.scope, document_family: value || null } })} />
            <TextField label="Line family" value={draft.scope.line_family || ""}
              onChange={(value) => setDraft({ ...draft, scope: { ...draft.scope, line_family: value || null } })} />
            <TextField label="Trade family" value={draft.scope.trade_family || ""}
              onChange={(value) => setDraft({ ...draft, scope: { ...draft.scope, trade_family: value || null } })} />
            <TextField label="Work mode" value={draft.scope.work_mode || ""}
              onChange={(value) => setDraft({ ...draft, scope: { ...draft.scope, work_mode: value || null } })} />
          </div>
          <label>Description terms
            <input value={draft.scope.description_terms.join(", ")} onChange={(event) => setDraft({
              ...draft,
              scope: { ...draft.scope, description_terms: split(event.target.value) },
            })} />
          </label>
          <label>Term matching
            <select value={draft.scope.term_match} onChange={(event) => setDraft({
              ...draft,
              scope: { ...draft.scope, term_match: event.target.value as "any" | "all" },
            })}><option value="any">Any term</option><option value="all">All terms</option></select>
          </label>
          <label>Allowed GL codes
            <input value={draft.constraint.allowed_gl_codes.join(", ")} onChange={(event) => setDraft({
              ...draft,
              constraint: { ...draft.constraint, allowed_gl_codes: split(event.target.value) },
            })} />
          </label>
          <div className="rules-form-grid">
            <TextField label="Minimum GL code" value={draft.constraint.minimum_gl_code || ""}
              onChange={(value) => setDraft({ ...draft, constraint: { ...draft.constraint, minimum_gl_code: value || null } })} />
            <TextField label="Maximum GL code" value={draft.constraint.maximum_gl_code || ""}
              onChange={(value) => setDraft({ ...draft, constraint: { ...draft.constraint, maximum_gl_code: value || null } })} />
          </div>
          {selected.status !== "rejected" && <button className="assistant-primary rules-save" type="button" disabled={!!busy} onClick={() => void save()}>Save validated rule</button>}
        </>}
      </section>
      <aside className="rules-audit-panel">
        <h2>Audit trail</h2>
        {!selected?.audit.length && <p>No events.</p>}
        {selected?.audit.slice().reverse().map((event, index) => <div className="rules-audit-event" key={`${event.at}-${index}`}>
          <strong>{event.event.replaceAll("_", " ")}</strong>
          <span>{event.actor}</span>
          <time>{new Date(event.at).toLocaleString()}</time>
        </div>)}
        <div className="rules-audit-boundary">
          <strong>Enforcement boundary</strong>
          <p>Rules constrain candidates only. AccountingDecisionEngine selects GL; AccountingReadiness controls export.</p>
        </div>
      </aside>
    </div>}
    {section === "corrections" && <section className="governance-history-panel">
      <header>
        <div><h2>Approved invoice corrections</h2><p>Human-approved, invoice-scoped changes. These are not reusable global rules.</p></div>
        <select value={correctionBatchId} onChange={(event) => setCorrectionBatchId(event.target.value)} aria-label="Correction batch filter">
          <option value="">All batches</option>
          {batches.map((batch) => <option key={batch.batch_id} value={batch.batch_id}>{batch.batch_name}</option>)}
        </select>
      </header>
      <div className="governance-card-list">
        {correctionGroups.filter((group) => !correctionBatchId || group.batchId === correctionBatchId).map((group) => <article key={group.key} className="governance-correction-card">
          <div><strong>{group.items.length} correction{group.items.length === 1 ? "" : "s"}</strong><span className="rule-status-pill is-active">approved</span></div>
          <p>Invoice: {group.invoiceGroupId}</p>
          <small>{batchName(batches, group.batchId)} · {new Date(group.approvedAt).toLocaleString()} · {group.approvedBy}</small>
          <ul>{group.items.map((item) => <li key={item.correction_id}><b>Line {item.local_row_index + 1} · {item.field}</b><span>→ {item.new_value}</span><small>{item.rationale}</small></li>)}</ul>
        </article>)}
        {!correctionGroups.some((group) => !correctionBatchId || group.batchId === correctionBatchId) && <div className="rules-library-empty">No approved corrections for this filter.</div>}
      </div>
    </section>}
    {section === "history" && <section className="governance-history-panel">
      <header>
        <div><h2>Batch and file history</h2><p>Processing revisions, manual edits, AI proposals, correction approvals and rule decisions.</p></div>
        <select value={historyBatchId} onChange={(event) => setHistoryBatchId(event.target.value)} aria-label="History batch filter">
          <option value="">Select batch</option>
          {batches.map((batch) => <option key={batch.batch_id} value={batch.batch_id}>{batch.batch_name}</option>)}
        </select>
      </header>
      <div className="governance-history-columns">
        <div><h3>Change activity</h3>{activity.map((event) => <ActivityRow key={event.event_id} event={event} />)}{!activity.length && <p>No recorded change activity.</p>}</div>
        <div><h3>Processing revisions</h3>{historyRevisions.map((revision, index) => <article className="governance-revision-row" key={revision.revision_id}><strong>v{historyRevisions.length - index}</strong><span>{revision.invoices_count} invoices · {revision.rows_count} rows</span><time>{new Date(revision.created_at).toLocaleString()}</time></article>)}{!historyRevisions.length && <p>No saved revisions.</p>}</div>
      </div>
    </section>}
  </main>;
}

function groupCorrections(items: ApprovedInvoiceCorrection[]) {
  const groups = new Map<string, {
    key: string; batchId: string; invoiceGroupId: string; approvedAt: string;
    approvedBy: string; items: ApprovedInvoiceCorrection[];
  }>();
  items.forEach((item) => {
    const key = `${item.batch_id}|${item.invoice_group_id}|${item.interaction_id}`;
    const group = groups.get(key) || {
      key, batchId: item.batch_id, invoiceGroupId: item.invoice_group_id,
      approvedAt: item.approved_at, approvedBy: item.approved_by, items: [],
    };
    group.items.push(item);
    groups.set(key, group);
  });
  return [...groups.values()].sort((a, b) => b.approvedAt.localeCompare(a.approvedAt));
}

function batchName(batches: BatchListEntry[], batchId: string) {
  return batches.find((batch) => batch.batch_id === batchId)?.batch_name || batchId;
}

function ActivityRow({ event }: { event: OperatorActivityEvent }) {
  return <article className={`governance-activity-row is-${event.source}`}>
    <div><strong>{event.summary}</strong><span>{event.source}</span></div>
    <small>{event.actor}{event.invoice_group_id ? ` · ${event.invoice_group_id}` : ""}</small>
    <time>{new Date(event.created_at).toLocaleString()}</time>
  </article>;
}

function TextField({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) {
  return <label>{label}<input value={value} onChange={(event) => onChange(event.target.value)} /></label>;
}

function split(value: string): string[] {
  return value.split(/,|\n/).map((item) => item.trim()).filter(Boolean);
}
