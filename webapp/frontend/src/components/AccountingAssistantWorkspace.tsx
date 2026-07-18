import { useEffect, useMemo, useState } from "react";

import { api, getFriendlyErrorMessage } from "../api";
import type {
  AccountingAssistantChatResult,
  BatchListEntry,
  PreviewResponse,
} from "../types";

const ACTIVE_BATCH_KEY = "billing_refactoring_active_batch_id";

type ChatEntry = {
  id: string;
  role: "user" | "assistant" | "system";
  text: string;
  result?: AccountingAssistantChatResult;
};

const WELCOME_ENTRY: ChatEntry = {
  id: "welcome",
  role: "system",
  text: "Selecciona un batch y un invoice. La IA solo propondrá cambios; nada se aplica ni se convierte en regla sin tu confirmación.",
};

type AssistantWorkspaceProps = {
  variant?: "page" | "floating";
  contextBatchId?: string | null;
  contextInvoiceId?: string | null;
};

export function AccountingAssistantWorkspace({
  variant = "page",
  contextBatchId,
  contextInvoiceId,
}: AssistantWorkspaceProps = {}) {
  const [batches, setBatches] = useState<BatchListEntry[]>([]);
  const [batchId, setBatchId] = useState(() => contextBatchId || localStorage.getItem(ACTIVE_BATCH_KEY) || "");
  const [preview, setPreview] = useState<PreviewResponse | null>(null);
  const [invoiceId, setInvoiceId] = useState("");
  const [message, setMessage] = useState("");
  const [entries, setEntries] = useState<ChatEntry[]>([WELCOME_ENTRY]);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  const groups = useMemo(() => {
    const output = new Map<string, { label: string; rowIndexes: number[] }>();
    (preview?.rows || []).forEach((row, rowIndex) => {
      const id = String(row._meta?.invoice_group_id || row["Invoice Number"] || `invoice-${rowIndex + 1}`);
      const current = output.get(id) || {
        label: `${row["Invoice Number"] || id} · ${row.Vendor || "Unknown vendor"}`,
        rowIndexes: [],
      };
      current.rowIndexes.push(rowIndex);
      output.set(id, current);
    });
    return output;
  }, [preview]);

  useEffect(() => {
    let cancelled = false;
    api.listBatches().then(({ batches: items }) => {
      if (cancelled) return;
      setBatches(items);
      if (!batchId || !items.some((item) => item.batch_id === batchId)) {
        setBatchId(items[0]?.batch_id || "");
      }
    }).catch((reason) => setError(getFriendlyErrorMessage(reason, "Load batches")));
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (contextBatchId && contextBatchId !== batchId) setBatchId(contextBatchId);
  }, [contextBatchId, batchId]);

  useEffect(() => {
    if (!batchId) {
      setPreview(null);
      setInvoiceId("");
      return;
    }
    let cancelled = false;
    setBusy("preview");
    setError("");
    api.preview(batchId).then((value) => {
      if (cancelled) return;
      setPreview(value);
      const ids = value.rows.map((row, index) => String(
        row._meta?.invoice_group_id || row["Invoice Number"] || `invoice-${index + 1}`,
      ));
      setInvoiceId((current) => ids.includes(current) ? current : (ids[0] || ""));
      localStorage.setItem(ACTIVE_BATCH_KEY, batchId);
    }).catch((reason) => {
      if (!cancelled) setError(getFriendlyErrorMessage(reason, "Load batch preview"));
    }).finally(() => { if (!cancelled) setBusy(""); });
    return () => { cancelled = true; };
  }, [batchId]);

  useEffect(() => {
    if (contextInvoiceId && groups.has(contextInvoiceId) && contextInvoiceId !== invoiceId) {
      setInvoiceId(contextInvoiceId);
    }
  }, [contextInvoiceId, groups, invoiceId]);

  useEffect(() => {
    if (!batchId || !invoiceId) {
      setEntries([WELCOME_ENTRY]);
      return;
    }
    let cancelled = false;
    api.listAccountingAssistantInteractions(batchId, invoiceId).then(({ items }) => {
      if (cancelled) return;
      const history: ChatEntry[] = [WELCOME_ENTRY];
      items.forEach((item) => {
        history.push({
          id: `user-${item.result.interaction_id}`,
          role: "user",
          text: item.user_message,
        });
        history.push({
          id: item.result.interaction_id,
          role: "assistant",
          text: item.result.assistant_message,
          result: item.result,
        });
      });
      setEntries(history);
    }).catch((reason) => {
      if (!cancelled) setError(getFriendlyErrorMessage(reason, "Load assistant history"));
    });
    return () => { cancelled = true; };
  }, [batchId, invoiceId]);

  const replaceRule = (interactionId: string, rule: NonNullable<AccountingAssistantChatResult["proposed_rule"]>) => {
    setEntries((current) => current.map((entry) => entry.result?.interaction_id === interactionId
      ? { ...entry, result: { ...entry.result, proposed_rule: rule, requires_rule_confirmation: false } }
      : entry));
  };

  const submit = async () => {
    const text = message.trim();
    if (!text || !batchId || !invoiceId || busy) return;
    setEntries((current) => [...current, { id: `user-${Date.now()}`, role: "user", text }]);
    setMessage("");
    setBusy("chat");
    setError("");
    try {
      const result = await api.accountingAssistantChat({
        batch_id: batchId,
        invoice_group_id: invoiceId,
        message: text,
      });
      setEntries((current) => [...current, {
        id: result.interaction_id,
        role: "assistant",
        text: result.assistant_message,
        result,
      }]);
    } catch (reason) {
      setError(getFriendlyErrorMessage(reason, "Accounting assistant"));
    } finally {
      setBusy("");
    }
  };

  const applyCorrections = async (result: AccountingAssistantChatResult) => {
    if (!result.corrections.length) return;
    setBusy(`apply-${result.interaction_id}`);
    setError("");
    try {
      const decision = await api.decideAccountingAssistantCorrections(result.interaction_id, true);
      const refreshed = await api.preview(result.batch_id);
      setPreview(refreshed);
      setEntries((current) => current.map((entry) => entry.result?.interaction_id === result.interaction_id
        ? { ...entry, result: decision.result }
        : entry));
      setEntries((current) => [...current, {
        id: `applied-${Date.now()}`,
        role: "system",
        text: `${decision.applied} corrección(es) aprobadas y aplicadas. Quedaron guardadas para este invoice y se reaplicarán si vuelves a procesar el batch. Accounting Pipeline V2 y readiness fueron recalculados.`,
      }]);
    } catch (reason) {
      setError(getFriendlyErrorMessage(reason, "Apply assistant corrections"));
    } finally {
      setBusy("");
    }
  };

  const decideRule = async (result: AccountingAssistantChatResult, approve: boolean) => {
    const rule = result.proposed_rule;
    if (!rule) return;
    setBusy(`rule-${rule.rule_id}`);
    setError("");
    try {
      const updated = await api.decideAccountingRule(rule.rule_id, approve);
      replaceRule(result.interaction_id, updated);
      setEntries((current) => [...current, {
        id: `rule-status-${Date.now()}`,
        role: "system",
        text: approve
          ? `Regla “${updated.title}” aprobada y activa. Se aplicará como restricción de candidatos en futuros recálculos.`
          : `Propuesta “${updated.title}” rechazada. No se aplicará.`,
      }]);
    } catch (reason) {
      setError(getFriendlyErrorMessage(reason, "Decide accounting rule"));
    } finally {
      setBusy("");
    }
  };

  return (
    <main className={`assistant-module-shell ${variant === "floating" ? "assistant-is-floating" : ""}`} aria-label="Accounting AI assistant">
      {variant === "page" && <header className="assistant-module-header">
        <div>
          <span className="assistant-eyebrow">Human-controlled accounting AI</span>
          <h1>Invoice Assistant</h1>
          <p>Conversa, revisa propuestas y decide qué cambios o reglas aceptar.</p>
        </div>
        <div className="assistant-safety-badge">No auto-apply · No auto-rule</div>
      </header>}

      <section className="assistant-context-bar" aria-label="Assistant context">
        <label>Batch
          <select value={batchId} onChange={(event) => setBatchId(event.target.value)} disabled={variant === "floating" && !!contextBatchId}>
            <option value="">Select batch</option>
            {batches.map((batch) => <option key={batch.batch_id} value={batch.batch_id}>{batch.batch_name}</option>)}
          </select>
        </label>
        <label>Invoice
          <select value={invoiceId} onChange={(event) => setInvoiceId(event.target.value)} disabled={!groups.size}>
            <option value="">Select invoice</option>
            {[...groups.entries()].map(([id, group]) => <option key={id} value={id}>{group.label}</option>)}
          </select>
        </label>
        <div className="assistant-context-stat">
          <strong>{groups.get(invoiceId)?.rowIndexes.length || 0}</strong>
          <span>líneas en contexto</span>
        </div>
      </section>

      {error && <div className="assistant-error" role="alert">{error}</div>}
      <section className="assistant-chat-log" aria-live="polite">
        {entries.map((entry) => <article key={entry.id} className={`assistant-message is-${entry.role}`}>
          <div className="assistant-message-role">{entry.role === "user" ? "Tú" : entry.role === "assistant" ? "IA contable" : "Sistema"}</div>
          <p>{entry.text}</p>
          {entry.result?.corrections.length ? <div className="assistant-proposal-card">
            <div className="assistant-proposal-title">Correcciones propuestas</div>
            {entry.result.corrections.map((correction, index) => <div className="assistant-correction" key={`${correction.row_index}-${correction.field}-${index}`}>
              <strong>Línea {correction.row_index + 1} · {correction.field}</strong>
              <span className="assistant-change-value">→ {correction.new_value}</span>
              <small>{correction.rationale}</small>
            </div>)}
            {entry.result.correction_status === "applied" ? <div className="assistant-rule-status is-active">
              Correcciones aprobadas y aplicadas · persistentes al reprocesar este invoice
            </div> : entry.result.correction_status === "rejected" ? <div className="assistant-rule-status is-rejected">
              Correcciones rechazadas · no se aplicaron
            </div> : <button type="button" className="assistant-primary" disabled={!!busy}
              onClick={() => void applyCorrections(entry.result!)}>
              Aprobar y aplicar correcciones al invoice
            </button>}
          </div> : null}
          {entry.result?.proposed_rule ? <div className="assistant-rule-offer">
            <div className="assistant-proposal-title">Propuesta de regla reutilizable</div>
            <strong>{entry.result.proposed_rule.title}</strong>
            <p>{entry.result.proposed_rule.description}</p>
            <code>{ruleSummary(entry.result.proposed_rule)}</code>
            {entry.result.requires_rule_confirmation ? <>
              <div className="assistant-rule-question">¿Quieres hacer de esto una regla determinística?</div>
              <div className="assistant-rule-actions">
                <button type="button" className="assistant-primary" disabled={!!busy}
                  onClick={() => void decideRule(entry.result!, true)}>Sí, aprobar</button>
                <button type="button" disabled={!!busy}
                  onClick={() => void decideRule(entry.result!, false)}>No</button>
              </div>
            </> : <div className={`assistant-rule-status is-${entry.result.proposed_rule.status}`}>
              {entry.result.proposed_rule.status}
            </div>}
          </div> : null}
          {entry.result?.proposed_tenant_policy ? <div className="assistant-rule-offer">
            <div className="assistant-proposal-title">Tenant policy draft</div>
            <strong>{entry.result.proposed_tenant_policy.title}</strong>
            <p>{entry.result.proposed_tenant_policy.description}</p>
            <code>{tenantPolicySummary(entry.result.proposed_tenant_policy)}</code>
            <div className="assistant-rule-status is-draft">
              Draft saved · not active. Open Accounting Rules → Tenant policies, simulate it against a batch, review conflicts, then approve explicitly.
            </div>
          </div> : null}
          {entry.result && <small className="assistant-cost-line">
            Perfil {entry.result.provider_profile_id} · costo estimado ${entry.result.estimated_cost_usd.toFixed(4)}
          </small>}
        </article>)}
        {busy === "chat" && <article className="assistant-message is-assistant"><p>Analizando evidencia y chart of accounts…</p></article>}
      </section>

      <footer className="assistant-composer">
        <textarea value={message} onChange={(event) => setMessage(event.target.value)}
          placeholder="Ejemplo: Estas líneas son servicios legales. Revisa los GL y explícame qué cambiarías…"
          disabled={!invoiceId || !!busy} onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              void submit();
            }
          }} />
        <button type="button" className="assistant-primary" onClick={() => void submit()}
          disabled={!message.trim() || !invoiceId || !!busy}>Enviar</button>
      </footer>
    </main>
  );
}

export function FloatingAccountingAssistant({
  batchId,
  invoiceGroupId,
}: {
  batchId?: string | null;
  invoiceGroupId?: string | null;
}) {
  const [open, setOpen] = useState(false);
  if (!open) {
    return <button type="button" className="assistant-chat-bubble" onClick={() => setOpen(true)}
      aria-label="Abrir asistente contable" title="Abrir asistente contable">
      <ChatBubbleIcon />
      <span>IA</span>
    </button>;
  }
  return <section className="assistant-floating-window" aria-label="Chat contable flotante">
    <header className="assistant-floating-header">
      <div>
        <strong>Asistente contable</strong>
        <span>Conversación privada · nada se aplica sin aprobación</span>
      </div>
      <button type="button" onClick={() => setOpen(false)} aria-label="Minimizar chat" title="Minimizar">—</button>
    </header>
    <AccountingAssistantWorkspace
      variant="floating"
      contextBatchId={batchId}
      contextInvoiceId={invoiceGroupId}
    />
  </section>;
}

function ChatBubbleIcon() {
  return <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M4 5.5h16v11H9l-5 3z" />
    <path d="M8 9h8M8 13h5" />
  </svg>;
}

function ruleSummary(rule: NonNullable<AccountingAssistantChatResult["proposed_rule"]>): string {
  const scope = Object.entries(rule.scope)
    .filter(([key, value]) => key !== "term_match" && value != null && (!Array.isArray(value) || value.length))
    .map(([key, value]) => `${key}=${Array.isArray(value) ? value.join("|") : value}`)
    .join(", ");
  const constraint = [
    rule.constraint.allowed_gl_codes.length ? `codes=${rule.constraint.allowed_gl_codes.join("|")}` : "",
    rule.constraint.minimum_gl_code ? `min=${rule.constraint.minimum_gl_code}` : "",
    rule.constraint.maximum_gl_code ? `max=${rule.constraint.maximum_gl_code}` : "",
  ].filter(Boolean).join(", ");
  return `${scope} → ${constraint}`;
}

function tenantPolicySummary(policy: NonNullable<AccountingAssistantChatResult["proposed_tenant_policy"]>): string {
  const scope = [
    policy.scope.vendor_entity_id ? `vendor=${policy.scope.vendor_entity_id}` : "",
    policy.scope.document_family ? `document=${policy.scope.document_family}` : "",
    policy.scope.line_family ? `line=${policy.scope.line_family}` : "",
    policy.scope.trade_family ? `trade=${policy.scope.trade_family}` : "",
    policy.scope.work_mode ? `mode=${policy.scope.work_mode}` : "",
    policy.scope.description_terms.length ? `terms=${policy.scope.description_terms.join("|")}` : "",
  ].filter(Boolean).join(", ");
  return `${scope} → allowed GL ${policy.action.allowed_gl_codes.join("|")}`;
}
