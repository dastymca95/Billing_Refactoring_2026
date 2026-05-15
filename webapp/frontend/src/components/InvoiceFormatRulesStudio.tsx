// Invoice Format Rules Studio.
//
// This is the operator-facing control surface for output formatting rules:
// required invoice numbers, invoice descriptions, and line-item descriptions.
// The backend applies these rules after extraction/validation, so managers can
// change formatting policy without changing vendor processors.

import { useCallback, useEffect, useMemo, useState } from "react";

import { api, getFriendlyErrorMessage } from "../api";
import type {
  InvoiceFormatRule,
  InvoiceFormatRuleScopeType,
  InvoiceFormatRulesConfig,
  InvoiceFormatRulesPayload,
} from "../types";

type ToastFn = (t: {
  tone: "success" | "info" | "warning" | "error";
  message: string;
  ttl?: number;
}) => void;

type Props = {
  pushToast: ToastFn;
};

type ReferenceTab = "vendors" | "gl" | "properties";
type GroupTab = "vendor_groups" | "gl_groups" | "property_groups";
type TemplateField =
  | "invoice_number"
  | "invoice_description"
  | "line_item_description";

const FIELD_LABELS: Record<TemplateField, string> = {
  invoice_number: "Invoice number",
  invoice_description: "Invoice description",
  line_item_description: "Line item description",
};

const DEFAULT_SAMPLE = {
  vendor_name: "City of Chattanooga Wastewater Department",
  account_number: "040582701-01",
  invoice_date: "05/11/2026",
  service_period_start: "03/26/2026",
  service_period_end: "04/27/2026",
  service_address: "1400 N Chamberlain AVE",
  property_abbreviation: "TFF",
  gl_account: "6955",
  gl_name: "Water & Sewer",
  line_item_description: "Rate 1 minimum (0-2,054 gals)",
  amount: 52.97,
  bill_or_credit: "Bill",
};

const DEFAULT_REQUIRED_COLUMNS = [
  "Bill or Credit",
  "Invoice Number",
  "Invoice Date",
  "Vendor",
  "Invoice Description",
  "Line Item Number",
  "Property Abbreviation",
  "GL Account",
  "Amount",
  "Expense Type",
  "Is Replacement Reserve",
];

function createRule(): InvoiceFormatRule {
  return {
    id: `rule_${Date.now()}`,
    name: "New output rule",
    enabled: true,
    priority: 25,
    scope: { type: "vendor", value: "" },
    document_type: "bill",
    templates: {
      invoice_number: "{account_number}-{service_period_start_month3_upper}{service_period_end_year2}",
      invoice_description:
        "{service_period_range} - {service_address_or_property} - {line_item_description_short}",
      line_item_description:
        "{service_period_range} - {service_address_or_property} - {line_item_description}",
    },
  };
}

function cloneConfig(config: InvoiceFormatRulesConfig): InvoiceFormatRulesConfig {
  return JSON.parse(JSON.stringify(config));
}

function stableJson(value: unknown): string {
  return JSON.stringify(value);
}

function humanScope(rule: InvoiceFormatRule): string {
  const scope = rule.scope;
  if (scope.type === "general") return "All invoices";
  const label = scope.type.replace(/_/g, " ");
  return `${label}: ${scope.value || "not set"}`;
}

function scopeHint(type: InvoiceFormatRuleScopeType): string {
  if (type === "general") return "Applies when no more specific rule matches.";
  if (type === "vendor") return "Pick an exact ResMan/vendor list name.";
  if (type === "vendor_group") return "Use a maintained vendor group.";
  if (type === "gl_account") return "Use a numeric GL code.";
  if (type === "gl_group") return "Use a maintained GL group.";
  if (type === "property") return "Use a property abbreviation.";
  return "Use a maintained property group.";
}

export function InvoiceFormatRulesStudio({ pushToast }: Props) {
  const [payload, setPayload] = useState<InvoiceFormatRulesPayload | null>(null);
  const [config, setConfig] = useState<InvoiceFormatRulesConfig | null>(null);
  const [savedConfig, setSavedConfig] = useState<InvoiceFormatRulesConfig | null>(
    null,
  );
  const [activeRuleId, setActiveRuleId] = useState<string>("");
  const [busy, setBusy] = useState<"loading" | "saving" | "preview" | null>(
    "loading",
  );
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<Record<string, string> | null>(null);
  const [referenceTab, setReferenceTab] = useState<ReferenceTab>("vendors");
  const [referenceSearch, setReferenceSearch] = useState("");
  const [groupTab, setGroupTab] = useState<GroupTab>("vendor_groups");
  const [sample, setSample] = useState<Record<string, unknown>>(DEFAULT_SAMPLE);

  const load = useCallback(() => {
    setBusy("loading");
    setError(null);
    api
      .invoiceFormatRules()
      .then((res) => {
        setPayload(res);
        setConfig(cloneConfig(res.config));
        setSavedConfig(cloneConfig(res.config));
        setActiveRuleId(res.config.rules[0]?.id ?? "");
        setPreview(null);
      })
      .catch((e) => setError(getFriendlyErrorMessage(e, "Load output rules")))
      .finally(() => setBusy(null));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const activeRule = useMemo(() => {
    if (!config) return null;
    return config.rules.find((r) => r.id === activeRuleId) ?? config.rules[0] ?? null;
  }, [activeRuleId, config]);

  const dirty = useMemo(() => {
    if (!config || !savedConfig) return false;
    return stableJson(config) !== stableJson(savedConfig);
  }, [config, savedConfig]);

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
      updateConfig((draft) => {
        const rule = draft.rules.find((r) => r.id === activeRuleId);
        if (rule) updater(rule);
      });
    },
    [activeRuleId, updateConfig],
  );

  const requiredColumns = useMemo(
    () =>
      Array.isArray(config?.template_requirements?.required_columns)
        ? config.template_requirements.required_columns
        : DEFAULT_REQUIRED_COLUMNS,
    [config],
  );

  const toggleRequiredColumn = useCallback(
    (column: string) => {
      updateConfig((draft) => {
        const existing =
          Array.isArray(draft.template_requirements?.required_columns)
            ? draft.template_requirements.required_columns
            : DEFAULT_REQUIRED_COLUMNS;
        const set = new Set(existing);
        if (set.has(column)) {
          set.delete(column);
        } else {
          set.add(column);
        }
        const ordered = (payload?.template_columns ?? []).filter((col) => set.has(col));
        draft.template_requirements = {
          required_columns: ordered.length ? ordered : Array.from(set),
        };
      });
    },
    [payload?.template_columns, updateConfig],
  );

  const save = useCallback(async () => {
    if (!config) return;
    setBusy("saving");
    setError(null);
    try {
      const res = await api.saveInvoiceFormatRules(config);
      setConfig(cloneConfig(res.config));
      setSavedConfig(cloneConfig(res.config));
      pushToast({
        tone: "success",
        message: "Output rules saved. New AI-assisted invoices will use them.",
        ttl: 4500,
      });
    } catch (e) {
      const friendly = getFriendlyErrorMessage(e, "Save output rules");
      setError(friendly);
      pushToast({ tone: "error", message: friendly });
    } finally {
      setBusy(null);
    }
  }, [config, pushToast]);

  const runPreview = useCallback(async () => {
    if (!config) return;
    setBusy("preview");
    setError(null);
    try {
      const res = await api.previewInvoiceFormatRules({ config, sample });
      setPreview(res.preview);
    } catch (e) {
      const friendly = getFriendlyErrorMessage(e, "Preview output rules");
      setError(friendly);
      pushToast({ tone: "error", message: friendly });
    } finally {
      setBusy(null);
    }
  }, [config, pushToast, sample]);

  const addRule = useCallback(() => {
    const next = createRule();
    updateConfig((draft) => {
      draft.rules = [next, ...(draft.rules ?? [])];
    });
    setActiveRuleId(next.id);
  }, [updateConfig]);

  const duplicateRule = useCallback(() => {
    if (!activeRule) return;
    const next = cloneConfig({ version: 1, groups: { vendor_groups: {}, gl_groups: {}, property_groups: {} }, rules: [activeRule] }).rules[0];
    next.id = `rule_${Date.now()}`;
    next.name = `${activeRule.name} copy`;
    updateConfig((draft) => {
      const index = Math.max(0, draft.rules.findIndex((r) => r.id === activeRule.id));
      draft.rules.splice(index + 1, 0, next);
    });
    setActiveRuleId(next.id);
  }, [activeRule, updateConfig]);

  const deleteRule = useCallback(() => {
    if (!activeRule || !config) return;
    if (config.rules.length <= 1) {
      pushToast({ tone: "warning", message: "At least one output rule is required." });
      return;
    }
    updateConfig((draft) => {
      draft.rules = draft.rules.filter((r) => r.id !== activeRule.id);
    });
    const fallback = config.rules.find((r) => r.id !== activeRule.id)?.id ?? "";
    setActiveRuleId(fallback);
  }, [activeRule, config, pushToast, updateConfig]);

  const references = payload?.references;
  const scopeOptions = useMemo(() => {
    if (!config || !references) return [];
    if (!activeRule) return [];
    const type = activeRule.scope.type;
    if (type === "vendor") {
      return references.vendors.map((v) => ({
        value: v.vendor_name,
        label: `${v.vendor_name}${v.vendor_id ? ` (${v.vendor_id})` : ""}`,
      }));
    }
    if (type === "gl_account") {
      return references.gl_accounts.map((g) => ({
        value: g.gl_code,
        label: `${g.gl_code} - ${g.gl_name}`,
      }));
    }
    if (type === "property") {
      return references.properties.map((p) => ({
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
  }, [activeRule, config, references]);

  const referenceRows = useMemo(() => {
    if (!references) return [];
    const query = referenceSearch.trim().toLowerCase();
    if (referenceTab === "vendors") {
      return references.vendors
        .filter((v) =>
          `${v.vendor_name} ${v.vendor_id} ${v.default_gl || ""}`
            .toLowerCase()
            .includes(query),
        )
        .slice(0, 180)
        .map((v) => ({
          key: v.vendor_name,
          title: v.vendor_name,
          meta: [v.vendor_id, v.default_gl ? `Default GL ${v.default_gl}` : ""]
            .filter(Boolean)
            .join(" - "),
        }));
    }
    if (referenceTab === "gl") {
      return references.gl_accounts
        .filter((g) => `${g.gl_code} ${g.gl_name} ${g.type}`.toLowerCase().includes(query))
        .slice(0, 220)
        .map((g) => ({
          key: g.gl_code,
          title: `${g.gl_code} - ${g.gl_name}`,
          meta: g.type,
        }));
    }
    return references.properties
      .filter((p) =>
        `${p.property_abbreviation} ${p.property_name}`.toLowerCase().includes(query),
      )
      .slice(0, 220)
      .map((p) => ({
        key: p.property_abbreviation,
        title: p.property_abbreviation,
        meta: p.property_name,
      }));
  }, [referenceSearch, referenceTab, references]);

  const groupEntries = useMemo(() => {
    if (!config) return [];
    const groups = config.groups[groupTab] as Record<
      string,
      { label: string; vendors?: string[]; gl_accounts?: string[]; properties?: string[] }
    >;
    return Object.entries(groups).sort(([a], [b]) => a.localeCompare(b));
  }, [config, groupTab]);

  const groupMemberKey =
    groupTab === "vendor_groups"
      ? "vendors"
      : groupTab === "gl_groups"
        ? "gl_accounts"
        : "properties";

  const updateGroup = useCallback(
    (key: string, field: "label" | "members", value: string) => {
      updateConfig((draft) => {
        const groups = draft.groups[groupTab] as Record<string, any>;
        const current = groups[key] ?? { label: key, [groupMemberKey]: [] };
        if (field === "label") {
          current.label = value;
        } else {
          current[groupMemberKey] = value
            .split(/\r?\n|,/)
            .map((part) => part.trim())
            .filter(Boolean);
        }
        groups[key] = current;
      });
    },
    [groupMemberKey, groupTab, updateConfig],
  );

  const addGroup = useCallback(() => {
    const key = `${groupTab.replace(/s$/, "")}_${Date.now().toString().slice(-5)}`;
    updateConfig((draft) => {
      const groups = draft.groups[groupTab] as Record<string, any>;
      groups[key] = { label: "New group", [groupMemberKey]: [] };
    });
  }, [groupMemberKey, groupTab, updateConfig]);

  const deleteGroup = useCallback(
    (key: string) => {
      updateConfig((draft) => {
        const groups = draft.groups[groupTab] as Record<string, any>;
        delete groups[key];
      });
    },
    [groupTab, updateConfig],
  );

  if (busy === "loading" && !config) {
    return (
      <section className="format-rules-studio">
        <div className="format-rules-loading">Loading output rules...</div>
      </section>
    );
  }

  if (!config || !payload) {
    return (
      <section className="format-rules-studio">
        <div className="format-rules-loading">
          {error || "Output rules could not be loaded."}
        </div>
      </section>
    );
  }

  return (
    <section className="format-rules-studio" data-testid="format-rules-studio">
      <aside className="format-rules-list">
        <div className="format-rules-pane-header">
          <div>
            <div className="format-rules-title">Output rules</div>
            <div className="format-rules-subtitle">
              Output rules for invoice numbers and descriptions
            </div>
          </div>
          <button className="btn btn-mini btn-primary" type="button" onClick={addRule}>
            New
          </button>
        </div>
        <div className="format-rules-list-scroll">
          {config.rules.map((rule) => (
            <button
              key={rule.id}
              type="button"
              className={`format-rule-item ${rule.id === activeRule?.id ? "active" : ""}`}
              onClick={() => setActiveRuleId(rule.id)}
            >
              <span className="format-rule-item-title">
                {rule.enabled ? "" : "Paused - "}
                {rule.name}
              </span>
              <span className="format-rule-item-meta">
                {rule.document_type} - {humanScope(rule)}
              </span>
            </button>
          ))}
        </div>
      </aside>

      <main className="format-rules-editor">
        <div className="format-rules-editor-header">
          <div>
            <h2>Output policy</h2>
            <p>
              Rules are evaluated by priority, scope, and document type after AI
              extraction is validated.
            </p>
          </div>
          <div className="format-rules-actions">
            {dirty && <span className="format-dirty-pill">Unsaved</span>}
            <button className="btn btn-compact" type="button" onClick={load} disabled={busy !== null}>
              Reset
            </button>
            <button
              className="btn btn-compact btn-primary"
              type="button"
              onClick={save}
              disabled={busy !== null || !dirty}
            >
              {busy === "saving" ? "Saving..." : "Save rules"}
            </button>
          </div>
        </div>

        {error && <div className="format-error-banner">{error}</div>}

        {activeRule && (
          <div className="format-rule-form">
            <section className="format-rule-card">
              <div className="format-section-heading">Rule targeting</div>
              <div className="format-grid">
                <label>
                  Rule name
                  <input
                    className="rules-input"
                    value={activeRule.name}
                    onChange={(e) => updateRule((r) => (r.name = e.target.value))}
                  />
                </label>
                <label>
                  Priority
                  <input
                    className="rules-input"
                    type="number"
                    value={activeRule.priority}
                    onChange={(e) =>
                      updateRule((r) => (r.priority = Number(e.target.value || 0)))
                    }
                  />
                </label>
                <label>
                  Document type
                  <select
                    className="rules-input"
                    value={activeRule.document_type}
                    onChange={(e) =>
                      updateRule(
                        (r) =>
                          (r.document_type = e.target.value as
                            | "any"
                            | "bill"
                            | "invoice"),
                      )
                    }
                  >
                    <option value="any">Any</option>
                    <option value="bill">Bill</option>
                    <option value="invoice">Invoice</option>
                  </select>
                </label>
                <label className="format-toggle-row">
                  <input
                    type="checkbox"
                    checked={activeRule.enabled}
                    onChange={(e) => updateRule((r) => (r.enabled = e.target.checked))}
                  />
                  Enabled
                </label>
              </div>
              <div className="format-grid two">
                <label>
                  Applies to
                  <select
                    className="rules-input"
                    value={activeRule.scope.type}
                    onChange={(e) =>
                      updateRule((r) => {
                        r.scope.type = e.target.value as InvoiceFormatRuleScopeType;
                        r.scope.value = "";
                      })
                    }
                  >
                    {payload.scope_types.map((type) => (
                      <option key={type.value} value={type.value}>
                        {type.label}
                      </option>
                    ))}
                  </select>
                  <span className="format-field-help">{scopeHint(activeRule.scope.type)}</span>
                </label>
                {activeRule.scope.type !== "general" && (
                  <label>
                    Scope value
                    <input
                      className="rules-input"
                      list="format-scope-values"
                      value={activeRule.scope.value}
                      onChange={(e) => updateRule((r) => (r.scope.value = e.target.value))}
                      placeholder="Select or type a value"
                    />
                    <datalist id="format-scope-values">
                      {scopeOptions.map((option) => (
                        <option key={option.value} value={option.value} label={option.label} />
                      ))}
                    </datalist>
                  </label>
                )}
              </div>
            </section>

            <section className="format-rule-card">
              <div className="format-section-heading">Templates</div>
              {(["invoice_number", "invoice_description", "line_item_description"] as TemplateField[]).map(
                (field) => (
                  <TemplateEditor
                    key={field}
                    field={field}
                    value={activeRule.templates[field] || ""}
                    payload={payload}
                    onChange={(value) =>
                      updateRule((r) => {
                        r.templates[field] = value;
                      })
                    }
                  />
                ),
              )}
              <div className="format-rule-ops">
                <button className="btn btn-compact" type="button" onClick={duplicateRule}>
                  Duplicate
                </button>
                <button className="btn btn-compact btn-ghost" type="button" onClick={deleteRule}>
                  Delete rule
                </button>
              </div>
            </section>

            <section className="format-rule-card">
              <div className="format-preview-header">
                <div>
                  <div className="format-section-heading">Required template fields</div>
                  <p>
                    These columns must be filled before export. Location can stay
                    optional when the invoice has no exact unit.
                  </p>
                </div>
              </div>
              <div className="format-required-grid">
                {(payload.template_columns ?? []).map((column) => (
                  <label key={column} className="format-required-toggle">
                    <input
                      type="checkbox"
                      checked={requiredColumns.includes(column)}
                      onChange={() => toggleRequiredColumn(column)}
                    />
                    <span>{column}</span>
                  </label>
                ))}
              </div>
            </section>

            <section className="format-rule-card">
              <div className="format-preview-header">
                <div>
                  <div className="format-section-heading">Live preview</div>
                  <p>
                    Uses sample bill data so managers can test tomorrow's format
                    before it touches production invoices.
                  </p>
                </div>
                <button
                  className="btn btn-compact btn-accent"
                  type="button"
                  onClick={runPreview}
                  disabled={busy !== null}
                >
                  {busy === "preview" ? "Rendering..." : "Preview"}
                </button>
              </div>
              <div className="format-sample-grid">
                {Object.entries(sample).slice(0, 10).map(([key, value]) => (
                  <label key={key}>
                    {key.replace(/_/g, " ")}
                    <input
                      className="rules-input"
                      value={String(value ?? "")}
                      onChange={(e) =>
                        setSample((prev) => ({ ...prev, [key]: e.target.value }))
                      }
                    />
                  </label>
                ))}
              </div>
              <div className="format-preview-output">
                {(["invoice_number", "invoice_description", "line_item_description"] as TemplateField[]).map(
                  (field) => (
                    <div key={field} className="format-preview-line">
                      <span>{FIELD_LABELS[field]}</span>
                      <strong>{preview?.[field] || "Run preview"}</strong>
                    </div>
                  ),
                )}
              </div>
            </section>
          </div>
        )}
      </main>

      <aside className="format-rules-side">
        <section className="format-side-card">
          <div className="format-section-heading">Variables</div>
          <div className="format-token-list">
            {payload.variables.map((v) => (
              <button
                key={v.key}
                type="button"
                className="format-token"
                title={v.label}
                onClick={() => navigator.clipboard?.writeText(`{${v.key}}`).catch(() => undefined)}
              >
                {"{"}
                {v.key}
                {"}"}
              </button>
            ))}
          </div>
        </section>

        <section className="format-side-card format-groups-card">
          <div className="format-side-card-header">
            <div className="format-section-heading">Groups</div>
            <button className="btn btn-mini" type="button" onClick={addGroup}>
              Add group
            </button>
          </div>
          <div className="format-tabs">
            <button
              type="button"
              className={groupTab === "vendor_groups" ? "active" : ""}
              onClick={() => setGroupTab("vendor_groups")}
            >
              Vendors
            </button>
            <button
              type="button"
              className={groupTab === "gl_groups" ? "active" : ""}
              onClick={() => setGroupTab("gl_groups")}
            >
              GL
            </button>
            <button
              type="button"
              className={groupTab === "property_groups" ? "active" : ""}
              onClick={() => setGroupTab("property_groups")}
            >
              Properties
            </button>
          </div>
          <div className="format-group-list">
            {groupEntries.map(([key, group]) => {
              const members = (group as any)[groupMemberKey] || [];
              return (
                <div key={key} className="format-group-editor">
                  <div className="format-group-editor-top">
                    <code>{key}</code>
                    <button
                      type="button"
                      className="btn btn-mini btn-ghost"
                      onClick={() => deleteGroup(key)}
                    >
                      Remove
                    </button>
                  </div>
                  <input
                    className="rules-input"
                    value={group.label}
                    onChange={(e) => updateGroup(key, "label", e.target.value)}
                    placeholder="Group label"
                  />
                  <textarea
                    className="rules-input rules-textarea"
                    value={members.join("\n")}
                    onChange={(e) => updateGroup(key, "members", e.target.value)}
                    rows={3}
                    placeholder="One vendor / GL / property per line"
                  />
                </div>
              );
            })}
          </div>
        </section>

        <section className="format-side-card">
          <div className="format-section-heading">Reference library</div>
          <div className="format-tabs">
            <button
              type="button"
              className={referenceTab === "vendors" ? "active" : ""}
              onClick={() => setReferenceTab("vendors")}
            >
              Vendors
            </button>
            <button
              type="button"
              className={referenceTab === "gl" ? "active" : ""}
              onClick={() => setReferenceTab("gl")}
            >
              GL
            </button>
            <button
              type="button"
              className={referenceTab === "properties" ? "active" : ""}
              onClick={() => setReferenceTab("properties")}
            >
              Properties
            </button>
          </div>
          <input
            className="rules-input"
            value={referenceSearch}
            onChange={(e) => setReferenceSearch(e.target.value)}
            placeholder="Search references"
          />
          <div className="format-reference-list">
            {referenceRows.map((row) => (
              <div key={row.key} className="format-reference-row">
                <strong>{row.title}</strong>
                {row.meta && <span>{row.meta}</span>}
              </div>
            ))}
          </div>
        </section>
      </aside>
    </section>
  );
}

function TemplateEditor({
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
    <div className="format-template-editor">
      <label>
        <span>{FIELD_LABELS[field]}</span>
        <textarea
          className="rules-input rules-textarea"
          rows={2}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          spellCheck={false}
        />
      </label>
      <div className="format-preset-row">
        {presets.map((preset) => (
          <button
            key={preset.label}
            type="button"
            className="format-preset-chip"
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
