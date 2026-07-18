import { useEffect, useMemo, useState } from "react";

import { api, getFriendlyErrorMessage } from "../api";
import type { BatchListEntry, PreviewRow, TenantAccountingPolicy, TenantVendorEntity } from "../types";

export function TenantPolicyGovernancePanel() {
  const [tenantId, setTenantId] = useState("");
  const [policies, setPolicies] = useState<TenantAccountingPolicy[]>([]);
  const [vendors, setVendors] = useState<TenantVendorEntity[]>([]);
  const [batches, setBatches] = useState<BatchListEntry[]>([]);
  const [batchId, setBatchId] = useState("");
  const [selectedId, setSelectedId] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [vendorName, setVendorName] = useState("");
  const [vendorErpId, setVendorErpId] = useState("");
  const [vendorAliases, setVendorAliases] = useState("");
  const selected = useMemo(
    () => policies.find((policy) => policy.policy_id === selectedId) || null,
    [policies, selectedId],
  );

  const load = async () => {
    setBusy("load");
    setError("");
    try {
      const context = await api.tenantAccountingContext();
      const [policyResponse, vendorResponse, batchResponse] = await Promise.all([
        api.listTenantPolicies(context.tenant_id),
        api.listTenantVendors(context.tenant_id),
        api.listBatches(),
      ]);
      setTenantId(context.tenant_id);
      setPolicies(policyResponse.items);
      setVendors(vendorResponse.items);
      setBatches(batchResponse.batches);
      setSelectedId((current) => policyResponse.items.some((item) => item.policy_id === current)
        ? current : policyResponse.items[0]?.policy_id || "");
      setBatchId((current) => current || batchResponse.batches[0]?.batch_id || "");
    } catch (reason) {
      setError(getFriendlyErrorMessage(reason, "Load tenant policy governance"));
    } finally {
      setBusy("");
    }
  };

  useEffect(() => { void load(); }, []);

  const replace = (updated: TenantAccountingPolicy) => {
    setPolicies((current) => current.map((item) => item.policy_id === updated.policy_id ? updated : item));
  };

  const simulate = async () => {
    if (!selected || !batchId) return;
    setBusy("simulate");
    setError("");
    try {
      const preview = await api.preview(batchId);
      replace(await api.simulateTenantPolicy(
        selected.policy_id,
        preview.rows.map((row, index) => simulationLine(row, index)),
        tenantId,
      ));
    } catch (reason) {
      setError(getFriendlyErrorMessage(reason, "Simulate tenant policy"));
    } finally {
      setBusy("");
    }
  };

  const decide = async (approve: boolean) => {
    if (!selected) return;
    setBusy("decision");
    setError("");
    try {
      replace(await api.decideTenantPolicy(selected.policy_id, approve, tenantId));
    } catch (reason) {
      setError(getFriendlyErrorMessage(reason, "Decide tenant policy"));
    } finally {
      setBusy("");
    }
  };

  const toggle = async () => {
    if (!selected || !["active", "disabled"].includes(selected.status)) return;
    setBusy("toggle");
    setError("");
    try {
      replace(await api.setTenantPolicyEnabled(selected.policy_id, selected.status !== "active", tenantId));
    } catch (reason) {
      setError(getFriendlyErrorMessage(reason, "Update tenant policy status"));
    } finally {
      setBusy("");
    }
  };

  const createVendor = async () => {
    if (!vendorName.trim()) return;
    setBusy("vendor");
    setError("");
    try {
      const created = await api.createTenantVendor({
        canonical_name: vendorName.trim(),
        erp_vendor_id: vendorErpId.trim() || null,
        aliases: split(vendorAliases),
      }, tenantId);
      setVendors((current) => [...current, created]);
      setVendorName(""); setVendorErpId(""); setVendorAliases("");
    } catch (reason) {
      setError(getFriendlyErrorMessage(reason, "Create tenant vendor identity"));
    } finally {
      setBusy("");
    }
  };

  return <section className="governance-history-panel tenant-policy-panel" aria-label="Tenant policy governance">
    <header>
      <div><h2>Tenant-governed policies</h2><p>Tenant <code>{tenantId || "loading"}</code>. Drafts are inert until simulation and explicit approval.</p></div>
      <button type="button" disabled={!!busy} onClick={() => void load()}>Refresh</button>
    </header>
    {error && <div className="assistant-error" role="alert">{error}</div>}
    <div className="rules-library-grid">
      <aside className="rules-library-list">
        <div className="rules-library-count">{policies.filter((item) => item.status === "active").length} active · {policies.length} total</div>
        {!policies.length && <p>No tenant policy drafts yet. The Invoice Assistant may propose one after a VendorEntity exists.</p>}
        {policies.map((policy) => <button type="button" key={policy.policy_id}
          className={selectedId === policy.policy_id ? "is-selected" : ""}
          onClick={() => setSelectedId(policy.policy_id)}>
          <strong>{policy.title}</strong><span className={`rule-status-pill is-${policy.status}`}>{policy.status}</span>
          <small>{policy.description}</small>
        </button>)}
      </aside>
      <section className="rules-library-editor">
        {!selected ? <div className="rules-library-empty">Select a tenant policy to inspect and simulate it.</div> : <>
          <div className="rules-editor-heading"><div><code>{selected.policy_id} · v{selected.version}</code><span className={`rule-status-pill is-${selected.status}`}>{selected.status}</span></div></div>
          <h3>{selected.title}</h3><p>{selected.description}</p>
          <code>{policySummary(selected, vendors)}</code>
          <label>Historical batch for simulation<select value={batchId} onChange={(event) => setBatchId(event.target.value)}>
            <option value="">Select batch</option>{batches.map((batch) => <option key={batch.batch_id} value={batch.batch_id}>{batch.batch_name}</option>)}
          </select></label>
          <div className="assistant-rule-actions">
            {!["rejected", "superseded"].includes(selected.status) && <button className="assistant-primary" type="button" disabled={!!busy || !batchId} onClick={() => void simulate()}>Simulate current version</button>}
            {selected.status === "simulated" && <><button type="button" disabled={!!busy || !!selected.latest_simulation?.blocking_conflicts} onClick={() => void decide(true)}>Approve and activate</button><button type="button" disabled={!!busy} onClick={() => void decide(false)}>Reject</button></>}
            {["active", "disabled"].includes(selected.status) && <button type="button" disabled={!!busy} onClick={() => void toggle()}>{selected.status === "active" ? "Disable" : "Enable"}</button>}
          </div>
          {selected.latest_simulation && <div className="rules-audit-boundary">
            <strong>Simulation {selected.latest_simulation.simulation_id}</strong>
            <p>{selected.latest_simulation.matched_lines} matched · {selected.latest_simulation.would_constrain_lines} constrained · {selected.latest_simulation.blocking_conflicts} blocking conflicts · {selected.latest_simulation.amount_mismatches} amount mismatches</p>
            <small>Snapshot {selected.latest_simulation.snapshot_id}</small>
          </div>}
          <div className="rules-audit-boundary"><strong>Enforcement boundary</strong><p>This policy only constrains/adds candidates. AccountingDecisionEngine selects GL; AccountingReadiness controls export.</p></div>
        </>}
      </section>
      <aside className="rules-audit-panel">
        <h2>Vendor identities</h2>
        {vendors.map((vendor) => <div className="rules-audit-event" key={vendor.vendor_entity_id}><strong>{vendor.canonical_name}</strong><span>{vendor.erp_vendor_id || "No ERP id"}</span><small>{vendor.aliases.join(", ")}</small></div>)}
        <label>Name<input value={vendorName} onChange={(event) => setVendorName(event.target.value)} /></label>
        <label>ERP vendor id<input value={vendorErpId} onChange={(event) => setVendorErpId(event.target.value)} /></label>
        <label>Aliases<input value={vendorAliases} onChange={(event) => setVendorAliases(event.target.value)} placeholder="comma separated" /></label>
        <button type="button" disabled={!!busy || !vendorName.trim()} onClick={() => void createVendor()}>Create VendorEntity</button>
      </aside>
    </div>
  </section>;
}

function simulationLine(row: PreviewRow, index: number): Record<string, unknown> {
  const meta = (row._meta || {}) as Record<string, unknown>;
  const source = (meta.source_text || {}) as Record<string, unknown>;
  const semantics = (meta.semantic_classification || {}) as Record<string, unknown>;
  const decision = (meta.accounting_decision || {}) as Record<string, unknown>;
  const ranked = Array.isArray(decision.candidates_ranked) ? decision.candidates_ranked as Record<string, unknown>[] : [];
  return {
    line_id: String(meta.invoice_group_id || row["Invoice Number"] || "invoice") + `:${index}`,
    observed_vendor: row.Vendor || null,
    property_id: row["Property Abbreviation"] || null,
    raw_description: source.raw_description || source.raw_activity || row["Line Item Description"] || null,
    document_family: semantics.document_family || null,
    line_family: semantics.line_family || null,
    trade_family: semantics.trade_family || null,
    work_mode: semantics.work_mode || null,
    amount: row.Amount ?? null,
    current_gl: row["GL Account"] || null,
    candidate_gl_codes: ranked.map((item) => String(item.gl_code || "")).filter(Boolean),
  };
}

function policySummary(policy: TenantAccountingPolicy, vendors: TenantVendorEntity[]): string {
  const vendor = vendors.find((item) => item.vendor_entity_id === policy.scope.vendor_entity_id);
  const terms = policy.scope.description_terms.length ? `terms=${policy.scope.description_terms.join("|")}` : "";
  const semantic = [policy.scope.document_family, policy.scope.line_family, policy.scope.trade_family, policy.scope.work_mode].filter(Boolean).join("/");
  return [vendor ? `vendor=${vendor.canonical_name}` : "", terms, semantic ? `semantics=${semantic}` : "", `allowed GL=${policy.action.allowed_gl_codes.join("|")}`].filter(Boolean).join(" · ");
}

function split(value: string): string[] {
  return [...new Set(value.split(",").map((item) => item.trim()).filter(Boolean))];
}
