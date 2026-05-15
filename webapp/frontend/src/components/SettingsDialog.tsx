import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";

import { api, getFriendlyErrorMessage } from "../api";
import type {
  AiStatus,
  InvoiceFormatRule,
  InvoiceFormatRuleScopeType,
  InvoiceFormatRulesConfig,
  InvoiceFormatRulesPayload,
} from "../types";
import type { ConfirmDialogOptions } from "./ConfirmDialog";
import type { Toast } from "./Toasts";
import { CanonicalRulesStudio } from "./CanonicalRulesStudio";
import { VendorRulesStudio } from "./VendorRulesStudio";

type SettingsSection =
  | "overview"
  | "processing"
  | "canonical"
  | "formatting"
  | "vendors"
  | "references"
  | "ai";

type Props = {
  open: boolean;
  onClose: () => void;
  pushToast: (toast: Omit<Toast, "id"> & { id?: string }) => void;
  requestConfirm?: (options: ConfirmDialogOptions) => Promise<boolean>;
};

const SAMPLE_INVOICE = {
  vendor_name: "EPB Fiber Optics",
  account_number: "C10181446",
  invoice_date: "05/07/2026",
  service_period_start: "03/26/2026",
  service_period_end: "04/27/2026",
  service_address: "21752 River Canyon Rd",
  property_abbreviation: "TFF",
  gl_account: "6955",
  gl_name: "Water & Sewer",
  line_item_description: "Fi-Speed Internet",
  amount: "68.95",
  source_file_stem: "epb_statement",
  bill_or_credit: "Bill",
};

type TemplateField = keyof InvoiceFormatRule["templates"];

const FIELD_LABELS: Record<TemplateField, string> = {
  invoice_number: "Invoice number",
  invoice_description: "Invoice description",
  line_item_description: "Line item description",
};

function cloneConfig(config: InvoiceFormatRulesConfig): InvoiceFormatRulesConfig {
  return JSON.parse(JSON.stringify(config));
}

function createOutputRule(): InvoiceFormatRule {
  return {
    id: `rule_${Date.now()}`,
    name: "New output rule",
    enabled: true,
    priority: 20,
    scope: { type: "general", value: "" },
    document_type: "any",
    templates: {
      invoice_number: "{account_number} {service_period_start_month3} {service_period_end_year2}",
      invoice_description:
        "{service_period_range} - {service_address_or_property} - {line_item_description_short}",
      line_item_description:
        "{service_period_range} - {service_address_or_property} - {line_item_description}",
    },
  };
}

function humanScope(rule: InvoiceFormatRule): string {
  if (rule.scope.type === "general") return "All invoices";
  return `${rule.scope.type.replace(/_/g, " ")} - ${rule.scope.value || "not set"}`;
}

function scopeHint(type: InvoiceFormatRuleScopeType): string {
  if (type === "general") return "Applies when no more specific rule matches.";
  if (type === "vendor") return "Exact vendor name from the ResMan/vendor reference.";
  if (type === "vendor_group") return "Named vendor group from Settings references.";
  if (type === "gl_account") return "One numeric GL account.";
  if (type === "gl_group") return "Named GL group from Settings references.";
  if (type === "property") return "One property abbreviation.";
  return "Named property group from Settings references.";
}

const SECTIONS: { key: SettingsSection; title: string; meta: string }[] = [
  {
    key: "overview",
    title: "General",
    meta: "Universal behavior map",
  },
  {
    key: "processing",
    title: "Processing fallback",
    meta: "How unknown documents are handled",
  },
  {
    key: "canonical",
    title: "Canonical rules",
    meta: "Category logic and test bench",
  },
  {
    key: "formatting",
    title: "Output rules",
    meta: "Numbers, descriptions, required fields",
  },
  {
    key: "vendors",
    title: "Vendor behavior",
    meta: "Deterministic and AI routing rules",
  },
  {
    key: "references",
    title: "Reference library",
    meta: "Vendors, GL codes, properties",
  },
  {
    key: "ai",
    title: "AI & vision",
    meta: "Provider status and safeguards",
  },
];

export function SettingsDialog({
  open,
  onClose,
  pushToast,
  requestConfirm,
}: Props) {
  const [section, setSection] = useState<SettingsSection>("overview");
  const [payload, setPayload] = useState<InvoiceFormatRulesPayload | null>(null);
  const [aiStatus, setAiStatus] = useState<AiStatus | null>(null);
  const [preview, setPreview] = useState<Record<string, string> | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [referenceTab, setReferenceTab] =
    useState<"vendors" | "gl" | "properties">("vendors");
  const [referenceSearch, setReferenceSearch] = useState("");
  const [position, setPosition] = useState<{ x: number; y: number } | null>(null);
  const windowRef = useRef<HTMLElement | null>(null);
  const dragRef = useRef<{
    pointerId: number;
    startX: number;
    startY: number;
    originX: number;
    originY: number;
  } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [rules, status] = await Promise.all([
        api.invoiceFormatRules(),
        api.getAiStatus(),
      ]);
      setPayload(rules);
      setAiStatus(status);
      const rendered = await api.previewInvoiceFormatRules({
        config: rules.config,
        sample: SAMPLE_INVOICE,
      });
      setPreview(rendered.preview);
    } catch (e) {
      const message = getFriendlyErrorMessage(e, "Load settings");
      setError(message);
      pushToast({ tone: "error", message });
    } finally {
      setLoading(false);
    }
  }, [pushToast]);

  useEffect(() => {
    if (!open) return;
    void load();
  }, [load, open]);

  useEffect(() => {
    if (!open || position) return;
    const width = Math.min(1220, Math.max(760, window.innerWidth - 80));
    const height = Math.min(760, Math.max(560, window.innerHeight - 80));
    setPosition({
      x: Math.max(16, Math.round((window.innerWidth - width) / 2)),
      y: Math.max(16, Math.round((window.innerHeight - height) / 2)),
    });
  }, [open, position]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose, open]);

  useEffect(() => {
    if (!open) return;
    const onPointerMove = (event: PointerEvent) => {
      const drag = dragRef.current;
      if (!drag) return;
      const width = windowRef.current?.offsetWidth ?? 980;
      const height = windowRef.current?.offsetHeight ?? 640;
      const nextX = drag.originX + event.clientX - drag.startX;
      const nextY = drag.originY + event.clientY - drag.startY;
      setPosition({
        x: Math.min(Math.max(8, nextX), Math.max(8, window.innerWidth - width - 8)),
        y: Math.min(Math.max(8, nextY), Math.max(8, window.innerHeight - height - 8)),
      });
    };
    const onPointerUp = () => {
      dragRef.current = null;
    };
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp);
    window.addEventListener("pointercancel", onPointerUp);
    return () => {
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", onPointerUp);
      window.removeEventListener("pointercancel", onPointerUp);
    };
  }, [open]);

  const beginDrag = useCallback(
    (event: ReactPointerEvent<HTMLElement>) => {
      if (event.button !== 0) return;
      const target = event.target as HTMLElement;
      if (target.closest("button,input,select,textarea,a")) return;
      const current = position ?? { x: 40, y: 40 };
      dragRef.current = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        originX: current.x,
        originY: current.y,
      };
      event.currentTarget.setPointerCapture?.(event.pointerId);
    },
    [position],
  );

  const summary = useMemo(() => {
    const required = payload?.config.template_requirements?.required_columns ?? [];
    return {
      rules: payload?.config.rules.length ?? 0,
      required: required.length,
      vendors: payload?.references.vendors.length ?? 0,
      gl: payload?.references.gl_accounts.length ?? 0,
      properties: payload?.references.properties.length ?? 0,
    };
  }, [payload]);

  const referenceRows = useMemo(() => {
    if (!payload) return [];
    const query = referenceSearch.trim().toLowerCase();
    if (referenceTab === "vendors") {
      return payload.references.vendors
        .filter((v) =>
          `${v.vendor_name} ${v.vendor_id} ${v.default_gl || ""}`
            .toLowerCase()
            .includes(query),
        )
        .slice(0, 80)
        .map((v) => ({
          key: v.vendor_name,
          title: v.vendor_name,
          meta: [v.vendor_id, v.default_gl ? `Default GL ${v.default_gl}` : ""]
            .filter(Boolean)
            .join(" - "),
        }));
    }
    if (referenceTab === "gl") {
      return payload.references.gl_accounts
        .filter((g) => `${g.gl_code} ${g.gl_name} ${g.type}`.toLowerCase().includes(query))
        .slice(0, 120)
        .map((g) => ({
          key: g.gl_code,
          title: `${g.gl_code} - ${g.gl_name}`,
          meta: g.type,
        }));
    }
    return payload.references.properties
      .filter((p) =>
        `${p.property_abbreviation} ${p.property_name}`.toLowerCase().includes(query),
      )
      .slice(0, 120)
      .map((p) => ({
        key: p.property_abbreviation,
        title: p.property_abbreviation,
        meta: p.property_name,
      }));
  }, [payload, referenceSearch, referenceTab]);

  if (!open) return null;

  return (
    <div className="settings-backdrop" role="presentation">
      <section
        ref={windowRef}
        className="settings-window"
        role="dialog"
        aria-modal="true"
        aria-label="Settings"
        style={position ? { left: position.x, top: position.y } : undefined}
      >
        <header className="settings-titlebar" onPointerDown={beginDrag}>
          <div>
            <h2>Settings</h2>
            <p>Universal processing rules, AI fallback, and ResMan output behavior.</p>
          </div>
          <button type="button" className="settings-close" onClick={onClose} aria-label="Close settings">
            x
          </button>
        </header>

        <div className="settings-shell">
          <aside className="settings-sidebar">
            <input
              className="settings-search"
              placeholder="Search settings"
              aria-label="Search settings"
              readOnly
            />
            <nav className="settings-nav" aria-label="Settings sections">
              {SECTIONS.map((item) => (
                <button
                  key={item.key}
                  type="button"
                  className={`settings-nav-item ${section === item.key ? "active" : ""}`}
                  onClick={() => setSection(item.key)}
                >
                  <span>{item.title}</span>
                  <small>{item.meta}</small>
                </button>
              ))}
            </nav>
          </aside>

          <main className="settings-content">
            {loading && !payload && (
              <div className="settings-loading">Loading settings...</div>
            )}
            {error && <div className="settings-error">{error}</div>}
            {section === "overview" && (
              <OverviewPanel
                summary={summary}
                aiStatus={aiStatus}
                preview={preview}
                onOpenFormatting={() => setSection("formatting")}
                onOpenProcessing={() => setSection("processing")}
              />
            )}
            {section === "processing" && (
              <ProcessingPanel aiStatus={aiStatus} onOpenFormatting={() => setSection("formatting")} />
            )}
            {section === "canonical" && (
              <div className="settings-embedded-studio">
                <CanonicalRulesStudio pushToast={pushToast} />
              </div>
            )}
            {section === "formatting" && (
              <OutputRulesPanel
                payload={payload}
                onPayloadChange={setPayload}
                pushToast={pushToast}
              />
            )}
            {section === "vendors" && (
              <div className="settings-embedded-studio">
                <VendorRulesStudio pushToast={pushToast} requestConfirm={requestConfirm} />
              </div>
            )}
            {section === "references" && (
              <ReferencePanel
                tab={referenceTab}
                onTab={setReferenceTab}
                search={referenceSearch}
                onSearch={setReferenceSearch}
                rows={referenceRows}
                summary={summary}
              />
            )}
            {section === "ai" && <AiPanel aiStatus={aiStatus} />}
          </main>
        </div>
      </section>
    </div>
  );
}

function OverviewPanel({
  summary,
  aiStatus,
  preview,
  onOpenFormatting,
  onOpenProcessing,
}: {
  summary: { rules: number; required: number; vendors: number; gl: number; properties: number };
  aiStatus: AiStatus | null;
  preview: Record<string, string> | null;
  onOpenFormatting: () => void;
  onOpenProcessing: () => void;
}) {
  return (
    <div className="settings-page">
      <div className="settings-page-header">
        <div>
          <h3>Behavior engine</h3>
          <p>
            One place controls deterministic vendors, AI-assisted fallback,
            required ResMan fields, and manager-facing output text.
          </p>
        </div>
        <button className="btn btn-primary btn-compact" type="button" onClick={onOpenFormatting}>
          Edit output rules
        </button>
      </div>

      <div className="settings-metric-grid">
        <Metric label="Active rules" value={summary.rules} />
        <Metric label="Required fields" value={summary.required} />
        <Metric label="Vendors" value={summary.vendors} />
        <Metric label="GL codes" value={summary.gl} />
        <Metric label="Properties" value={summary.properties} />
      </div>

      <div className="settings-two-column">
        <section className="settings-card">
          <div className="settings-card-header">
            <span>Universal flow</span>
            <button type="button" className="settings-link-button" onClick={onOpenProcessing}>
              Configure behavior
            </button>
          </div>
          <ol className="settings-flow">
            <li>Dedicated utility processors run first when a trusted vendor match exists.</li>
            <li>Unknown or variable invoices are classified, OCR/vision-read, and normalized.</li>
            <li>Vendor, property, GL, totals, dates, and required fields are validated.</li>
            <li>Settings output rules render invoice numbers and descriptions before export.</li>
          </ol>
          <div className="settings-ai-state">
            <strong>{aiLabel(aiStatus)}</strong>
            <span>{aiStatus?.message || aiStatus?.reason || "AI status unavailable."}</span>
          </div>
        </section>

        <MiniInvoicePreview preview={preview} />
      </div>
    </div>
  );
}

function ProcessingPanel({
  aiStatus,
  onOpenFormatting,
}: {
  aiStatus: AiStatus | null;
  onOpenFormatting: () => void;
}) {
  return (
    <div className="settings-page compact">
      <div className="settings-page-header">
        <div>
          <h3>Processing fallback</h3>
          <p>
            The app should handle known utilities deterministically and route
            unfamiliar bills/invoices through a validated universal fallback.
          </p>
        </div>
      </div>
      <div className="settings-policy-list">
        <PolicyRow
          title="Document classification"
          body="Every upload is treated as bill, invoice, receipt-like invoice, statement, or unsupported document before rows are generated."
          state="Enabled"
        />
        <PolicyRow
          title="AI fallback"
          body="When no dedicated processor owns the vendor, AI extracts candidates; backend validation decides what can become template data."
          state={aiStatus?.enabled ? "Available" : "Off"}
        />
        <PolicyRow
          title="Required fields"
          body="Invoice Number, Vendor, Property Abbreviation, GL Account, Amount, and any selected required fields block export until filled."
          state="Source of truth"
          onClick={onOpenFormatting}
        />
        <PolicyRow
          title="Reference validation"
          body="Vendors, GL codes, and properties are matched against project references. Unknown values become review tasks."
          state="Strict"
        />
        <PolicyRow
          title="Non-invoice documents"
          body="If the classifier cannot identify a payable document, the batch is flagged for review instead of creating fake ResMan rows."
          state="Guarded"
        />
      </div>
    </div>
  );
}

function OutputRulesPanel({
  payload,
  onPayloadChange,
  pushToast,
}: {
  payload: InvoiceFormatRulesPayload | null;
  onPayloadChange: (payload: InvoiceFormatRulesPayload) => void;
  pushToast: (toast: Omit<Toast, "id"> & { id?: string }) => void;
}) {
  const [config, setConfig] = useState<InvoiceFormatRulesConfig | null>(null);
  const [activeRuleId, setActiveRuleId] = useState("");
  const [preview, setPreview] = useState<Record<string, string> | null>(null);
  const [busy, setBusy] = useState<"saving" | "preview" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showAdvanced, setShowAdvanced] = useState(false);

  useEffect(() => {
    if (!payload) return;
    const next = cloneConfig(payload.config);
    setConfig(next);
    setActiveRuleId((current) =>
      next.rules.some((rule) => rule.id === current)
        ? current
        : next.rules[0]?.id ?? "",
    );
    setPreview(null);
  }, [payload]);

  const activeRule = useMemo(
    () => config?.rules.find((rule) => rule.id === activeRuleId) ?? null,
    [activeRuleId, config],
  );

  const dirty = useMemo(() => {
    if (!config || !payload) return false;
    return JSON.stringify(config) !== JSON.stringify(payload.config);
  }, [config, payload]);

  const requiredColumns =
    config?.template_requirements?.required_columns ?? [];

  const updateConfig = useCallback((updater: (draft: InvoiceFormatRulesConfig) => void) => {
    setConfig((prev) => {
      if (!prev) return prev;
      const draft = cloneConfig(prev);
      updater(draft);
      return draft;
    });
  }, []);

  const updateRule = useCallback(
    (updater: (rule: InvoiceFormatRule) => void) => {
      if (!activeRule) return;
      updateConfig((draft) => {
        const rule = draft.rules.find((r) => r.id === activeRule.id);
        if (rule) updater(rule);
      });
    },
    [activeRule, updateConfig],
  );

  const scopeOptions = useMemo(() => {
    if (!payload || !activeRule || !config) return [];
    const type = activeRule.scope.type;
    if (type === "vendor") {
      return payload.references.vendors.map((v) => ({
        value: v.vendor_name,
        label: `${v.vendor_name}${v.vendor_id ? ` (${v.vendor_id})` : ""}`,
      }));
    }
    if (type === "gl_account") {
      return payload.references.gl_accounts.map((g) => ({
        value: g.gl_code,
        label: `${g.gl_code} - ${g.gl_name}`,
      }));
    }
    if (type === "property") {
      return payload.references.properties.map((p) => ({
        value: p.property_abbreviation,
        label: `${p.property_abbreviation} - ${p.property_name}`,
      }));
    }
    if (type === "vendor_group") {
      return Object.entries(config.groups.vendor_groups).map(([key, group]) => ({
        value: key,
        label: `${group.label} (${group.vendors.length})`,
      }));
    }
    if (type === "gl_group") {
      return Object.entries(config.groups.gl_groups).map(([key, group]) => ({
        value: key,
        label: `${group.label} (${group.gl_accounts.length})`,
      }));
    }
    if (type === "property_group") {
      return Object.entries(config.groups.property_groups).map(([key, group]) => ({
        value: key,
        label: `${group.label} (${group.properties.length})`,
      }));
    }
    return [];
  }, [activeRule, config, payload]);

  const addRule = useCallback(() => {
    const rule = createOutputRule();
    updateConfig((draft) => {
      draft.rules.push(rule);
    });
    setActiveRuleId(rule.id);
  }, [updateConfig]);

  const duplicateRule = useCallback(() => {
    if (!activeRule) return;
    const copy: InvoiceFormatRule = JSON.parse(JSON.stringify(activeRule));
    copy.id = `rule_${Date.now()}`;
    copy.name = `${activeRule.name} copy`;
    updateConfig((draft) => {
      const index = Math.max(0, draft.rules.findIndex((rule) => rule.id === activeRule.id));
      draft.rules.splice(index + 1, 0, copy);
    });
    setActiveRuleId(copy.id);
  }, [activeRule, updateConfig]);

  const deleteRule = useCallback(() => {
    if (!activeRule || !config) return;
    if (config.rules.length <= 1) {
      pushToast({ tone: "warning", message: "At least one output rule is required." });
      return;
    }
    updateConfig((draft) => {
      draft.rules = draft.rules.filter((rule) => rule.id !== activeRule.id);
    });
    setActiveRuleId(config.rules.find((rule) => rule.id !== activeRule.id)?.id ?? "");
  }, [activeRule, config, pushToast, updateConfig]);

  const toggleRequiredColumn = useCallback(
    (column: string) => {
      updateConfig((draft) => {
        const current = draft.template_requirements?.required_columns ?? [];
        const exists = current.includes(column);
        draft.template_requirements = {
          required_columns: exists
            ? current.filter((item) => item !== column)
            : [...current, column],
        };
      });
    },
    [updateConfig],
  );

  const reset = useCallback(() => {
    if (!payload) return;
    setConfig(cloneConfig(payload.config));
    setActiveRuleId(payload.config.rules[0]?.id ?? "");
    setPreview(null);
    setError(null);
  }, [payload]);

  const runPreview = useCallback(async () => {
    if (!config) return;
    setBusy("preview");
    setError(null);
    try {
      const res = await api.previewInvoiceFormatRules({
        config,
        sample: SAMPLE_INVOICE,
      });
      setPreview(res.preview);
    } catch (e) {
      const message = getFriendlyErrorMessage(e, "Preview output rules");
      setError(message);
      pushToast({ tone: "error", message });
    } finally {
      setBusy(null);
    }
  }, [config, pushToast]);

  const save = useCallback(async () => {
    if (!config || !payload) return;
    setBusy("saving");
    setError(null);
    try {
      const res = await api.saveInvoiceFormatRules(config);
      const nextPayload = { ...payload, config: res.config };
      onPayloadChange(nextPayload);
      setConfig(cloneConfig(res.config));
      pushToast({
        tone: "success",
        message: "Settings saved. New AI-assisted invoices will use these rules.",
        ttl: 4500,
      });
    } catch (e) {
      const message = getFriendlyErrorMessage(e, "Save output rules");
      setError(message);
      pushToast({ tone: "error", message });
    } finally {
      setBusy(null);
    }
  }, [config, onPayloadChange, payload, pushToast]);

  if (!payload || !config || !activeRule) {
    return <div className="settings-loading">Loading output settings...</div>;
  }

  return (
    <div className="settings-output-page">
      <header className="settings-output-header">
        <div>
          <h3>Output rules</h3>
          <p>
            These rules are the source of truth after extraction and validation:
            invoice number, invoice description, line description, and required fields.
          </p>
        </div>
        <div className="settings-output-actions">
          {dirty && <span className="settings-unsaved">Unsaved</span>}
          <button className="btn btn-compact" type="button" onClick={reset} disabled={busy !== null}>
            Reset
          </button>
          <button
            className="btn btn-compact btn-primary"
            type="button"
            onClick={save}
            disabled={busy !== null || !dirty}
          >
            {busy === "saving" ? "Saving..." : "Save"}
          </button>
        </div>
      </header>

      {error && <div className="settings-error inline">{error}</div>}

      <div className="settings-output-layout">
        <aside className="settings-rule-list">
          <div className="settings-rule-list-header">
            <span>Rules</span>
            <button className="btn btn-mini btn-primary" type="button" onClick={addRule}>
              New
            </button>
          </div>
          <div className="settings-rule-list-scroll">
            {config.rules.map((rule) => (
              <button
                key={rule.id}
                type="button"
                className={`settings-rule-item ${rule.id === activeRule.id ? "active" : ""}`}
                onClick={() => setActiveRuleId(rule.id)}
              >
                <strong>{rule.enabled ? rule.name : `Paused - ${rule.name}`}</strong>
                <span>{rule.document_type} - {humanScope(rule)}</span>
              </button>
            ))}
          </div>
        </aside>

        <section className="settings-rule-editor">
          <div className="settings-compact-card">
            <div className="settings-section-caption">Targeting</div>
            <div className="settings-target-grid">
              <label>
                Name
                <input
                  className="settings-field"
                  value={activeRule.name}
                  onChange={(event) => updateRule((rule) => (rule.name = event.target.value))}
                />
              </label>
              <label>
                Priority
                <input
                  className="settings-field"
                  type="number"
                  value={activeRule.priority}
                  onChange={(event) =>
                    updateRule((rule) => (rule.priority = Number(event.target.value || 0)))
                  }
                />
              </label>
              <label>
                Document type
                <select
                  className="settings-field"
                  value={activeRule.document_type}
                  onChange={(event) =>
                    updateRule(
                      (rule) =>
                        (rule.document_type = event.target.value as "any" | "bill" | "invoice"),
                    )
                  }
                >
                  <option value="any">Any</option>
                  <option value="bill">Bill</option>
                  <option value="invoice">Invoice</option>
                </select>
              </label>
              <label className="settings-check-row">
                <input
                  type="checkbox"
                  checked={activeRule.enabled}
                  onChange={(event) => updateRule((rule) => (rule.enabled = event.target.checked))}
                />
                Enabled
              </label>
              <label>
                Applies to
                <select
                  className="settings-field"
                  value={activeRule.scope.type}
                  onChange={(event) =>
                    updateRule((rule) => {
                      rule.scope.type = event.target.value as InvoiceFormatRuleScopeType;
                      rule.scope.value = "";
                    })
                  }
                >
                  {payload.scope_types.map((type) => (
                    <option key={type.value} value={type.value}>
                      {type.label}
                    </option>
                  ))}
                </select>
                <small>{scopeHint(activeRule.scope.type)}</small>
              </label>
              {activeRule.scope.type !== "general" && (
                <label className="settings-target-wide">
                  Scope value
                  <input
                    className="settings-field"
                    list="settings-scope-values"
                    value={activeRule.scope.value}
                    onChange={(event) => updateRule((rule) => (rule.scope.value = event.target.value))}
                  />
                  <datalist id="settings-scope-values">
                    {scopeOptions.map((option) => (
                      <option key={option.value} value={option.value} label={option.label} />
                    ))}
                  </datalist>
                </label>
              )}
            </div>
          </div>

          <div className="settings-compact-card">
            <div className="settings-section-caption">Text templates</div>
            {(["invoice_number", "invoice_description", "line_item_description"] as TemplateField[]).map(
              (field) => (
                <CompactTemplateEditor
                  key={field}
                  field={field}
                  value={activeRule.templates[field] || ""}
                  payload={payload}
                  onChange={(value) =>
                    updateRule((rule) => {
                      rule.templates[field] = value;
                    })
                  }
                />
              ),
            )}
            <button
              type="button"
              className="settings-link-button"
              onClick={() => setShowAdvanced((value) => !value)}
            >
              {showAdvanced ? "Hide advanced fields" : "Show required fields and variables"}
            </button>
          </div>

          {showAdvanced && (
            <div className="settings-compact-card">
              <div className="settings-section-caption">Required template fields</div>
              <div className="settings-required-compact">
                {(payload.template_columns ?? []).map((column) => (
                  <label key={column}>
                    <input
                      type="checkbox"
                      checked={requiredColumns.includes(column)}
                      onChange={() => toggleRequiredColumn(column)}
                    />
                    <span>{column}</span>
                  </label>
                ))}
              </div>
              <div className="settings-section-caption subtle">Variables</div>
              <div className="settings-variable-strip">
                {payload.variables.map((variable) => (
                  <button
                    key={variable.key}
                    type="button"
                    title={variable.label}
                    onClick={() => navigator.clipboard?.writeText(`{${variable.key}}`).catch(() => undefined)}
                  >
                    {"{"}{variable.key}{"}"}
                  </button>
                ))}
              </div>
            </div>
          )}

          <div className="settings-rule-ops">
            <button className="btn btn-compact" type="button" onClick={duplicateRule}>
              Duplicate rule
            </button>
            <button className="btn btn-compact btn-ghost" type="button" onClick={deleteRule}>
              Delete rule
            </button>
          </div>
        </section>

        <aside className="settings-preview-column">
          <MiniInvoicePreview preview={preview} />
          <button
            className="btn btn-compact btn-accent settings-preview-button"
            type="button"
            onClick={runPreview}
            disabled={busy !== null}
          >
            {busy === "preview" ? "Rendering..." : "Preview draft"}
          </button>
          <div className="settings-preview-note">
            Changes here are applied by the backend after AI/fallback extraction
            is validated. Existing deterministic processors stay untouched.
          </div>
        </aside>
      </div>
    </div>
  );
}

function CompactTemplateEditor({
  field,
  value,
  payload,
  onChange,
}: {
  field: TemplateField;
  value: string;
  payload: InvoiceFormatRulesPayload;
  onChange: (value: string) => void;
}) {
  const presets = payload.presets[field] ?? [];
  return (
    <div className="settings-template-row">
      <label>
        <span>{FIELD_LABELS[field]}</span>
        <textarea
          className="settings-field settings-template-textarea"
          rows={2}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          spellCheck={false}
        />
      </label>
      <div className="settings-preset-strip">
        {presets.map((preset) => (
          <button
            key={preset.label}
            type="button"
            title={preset.description}
            onClick={() => onChange(preset.template)}
          >
            {preset.label}
          </button>
        ))}
      </div>
    </div>
  );
}

function ReferencePanel({
  tab,
  onTab,
  search,
  onSearch,
  rows,
  summary,
}: {
  tab: "vendors" | "gl" | "properties";
  onTab: (tab: "vendors" | "gl" | "properties") => void;
  search: string;
  onSearch: (value: string) => void;
  rows: { key: string; title: string; meta: string }[];
  summary: { vendors: number; gl: number; properties: number };
}) {
  return (
    <div className="settings-page compact">
      <div className="settings-page-header">
        <div>
          <h3>Reference library</h3>
          <p>Review the source references that AI and fallback validation must obey.</p>
        </div>
      </div>
      <div className="settings-reference-toolbar">
        <div className="settings-tabs">
          <button className={tab === "vendors" ? "active" : ""} type="button" onClick={() => onTab("vendors")}>
            Vendors ({summary.vendors})
          </button>
          <button className={tab === "gl" ? "active" : ""} type="button" onClick={() => onTab("gl")}>
            GL ({summary.gl})
          </button>
          <button className={tab === "properties" ? "active" : ""} type="button" onClick={() => onTab("properties")}>
            Properties ({summary.properties})
          </button>
        </div>
        <input
          className="settings-search wide"
          value={search}
          onChange={(event) => onSearch(event.target.value)}
          placeholder="Search references"
        />
      </div>
      <div className="settings-reference-list">
        {rows.map((row) => (
          <div key={row.key} className="settings-reference-row">
            <strong>{row.title}</strong>
            {row.meta && <span>{row.meta}</span>}
          </div>
        ))}
      </div>
    </div>
  );
}

function AiPanel({ aiStatus }: { aiStatus: AiStatus | null }) {
  return (
    <div className="settings-page compact">
      <div className="settings-page-header">
        <div>
          <h3>AI & vision</h3>
          <p>Provider status is visible here, but API keys never leave the backend.</p>
        </div>
      </div>
      <section className="settings-card">
        <dl className="settings-status-grid">
          <div>
            <dt>Status</dt>
            <dd>{aiLabel(aiStatus)}</dd>
          </div>
          <div>
            <dt>Provider</dt>
            <dd>{aiStatus?.provider || "Not configured"}</dd>
          </div>
          <div>
            <dt>Model</dt>
            <dd>{aiStatus?.model || "Not configured"}</dd>
          </div>
          <div>
            <dt>Vision</dt>
            <dd>
              {aiStatus?.vision_enabled
                ? `${aiStatus.vision_model || "Vision model"} (${aiStatus.vision_mode || "default"})`
                : "Off"}
            </dd>
          </div>
        </dl>
        <p className="settings-muted">{aiStatus?.message || aiStatus?.reason}</p>
        {aiStatus?.allowed_tasks?.length ? (
          <div className="settings-chip-row">
            {aiStatus.allowed_tasks.map((task) => (
              <span key={task} className="settings-chip">
                {task}
              </span>
            ))}
          </div>
        ) : null}
      </section>
    </div>
  );
}

function MiniInvoicePreview({ preview }: { preview: Record<string, string> | null }) {
  return (
    <section className="settings-mini-invoice" aria-label="Single invoice draft preview">
      <div className="mini-invoice-bar">Single invoice draft</div>
      <div className="mini-invoice-grid">
        <span>Vendor</span>
        <strong>{SAMPLE_INVOICE.vendor_name}</strong>
        <span>Number</span>
        <strong>{preview?.invoice_number || "Run preview"}</strong>
        <span>Property</span>
        <strong>{SAMPLE_INVOICE.property_abbreviation}</strong>
        <span>GL Account</span>
        <strong>
          {SAMPLE_INVOICE.gl_account} - {SAMPLE_INVOICE.gl_name}
        </strong>
        <span>Description</span>
        <strong>{preview?.invoice_description || "Run preview"}</strong>
      </div>
      <div className="mini-line-row">
        <span>Line item</span>
        <strong>{preview?.line_item_description || "Run preview"}</strong>
        <em>$68.95</em>
      </div>
    </section>
  );
}

function Metric({ label, value }: { label: string; value: number }) {
  return (
    <div className="settings-metric">
      <strong>{value.toLocaleString()}</strong>
      <span>{label}</span>
    </div>
  );
}

function PolicyRow({
  title,
  body,
  state,
  onClick,
}: {
  title: string;
  body: string;
  state: string;
  onClick?: () => void;
}) {
  const inner = (
    <>
      <div>
        <strong>{title}</strong>
        <span>{body}</span>
      </div>
      <em>{state}</em>
    </>
  );
  if (onClick) {
    return (
      <button type="button" className="settings-policy-row" onClick={onClick}>
        {inner}
      </button>
    );
  }
  return (
    <div className="settings-policy-row">
      {inner}
    </div>
  );
}

function aiLabel(aiStatus: AiStatus | null): string {
  if (!aiStatus) return "AI status loading";
  if (!aiStatus.enabled) return "AI Off";
  if (aiStatus.provider === "mock") return "AI Mock";
  if (!aiStatus.configured) return "AI Error";
  if (aiStatus.vision_enabled) return "AI + Vision Configured";
  return "AI Configured";
}
