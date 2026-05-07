// Vendor Rules Studio — Phase 1Z.
//
// Three-pane editor for vendor YAML rules:
//   left   — vendor list + status badges
//   center — editable rule sections (cards) with save / validate / reset
//   right  — help / preview panel: what the selected rule does + examples
//
// The UI never edits Python. All edits flow through PATCH /api/vendor-rules
// which atomically rewrites config/vendors/<key>.yaml after creating a
// backup. CLI processors automatically pick up the new YAML on their next
// run (they always read fresh).

import { useCallback, useEffect, useMemo, useState } from "react";

import { api, getFriendlyErrorMessage } from "../api";
import type { ConfirmDialogOptions } from "./ConfirmDialog";
import type { BatchListEntry } from "../types";
import {
  vendorRulesApi,
  type ImpactPayload,
  type RuleField,
  type RuleGroup,
  type ValidationIssue,
  type VendorListEntry,
  type VendorRulesPayload,
} from "../vendorRulesApi";

type Props = {
  pushToast: (t: { tone: "success" | "info" | "error"; message: string; ttl?: number }) => void;
  // Phase 2E — app-native confirm. Hosts pass App.tsx's requestConfirm.
  requestConfirm?: (opts: ConfirmDialogOptions) => Promise<boolean>;
};

export function VendorRulesStudio({ pushToast, requestConfirm }: Props) {
  const [vendors, setVendors] = useState<VendorListEntry[]>([]);
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const [payload, setPayload] = useState<VendorRulesPayload | null>(null);
  const [edits, setEdits] = useState<Record<string, unknown>>({});
  const [issues, setIssues] = useState<ValidationIssue[]>([]);
  const [busy, setBusy] = useState<"loading" | "saving" | "validating" | "restoring" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [helpField, setHelpField] = useState<{ group: RuleGroup; field: RuleField } | null>(null);
  const [openGroupKeys, setOpenGroupKeys] = useState<Set<string>>(new Set());

  // Phase 2A — "Test against batch" state.
  const [batches, setBatches] = useState<BatchListEntry[]>([]);
  const [testBatchId, setTestBatchId] = useState<string>("");
  const [impact, setImpact] = useState<ImpactPayload | null>(null);
  const [impactBusy, setImpactBusy] = useState(false);
  const [impactError, setImpactError] = useState<string | null>(null);

  // Load vendor list once.
  useEffect(() => {
    let cancelled = false;
    setBusy("loading");
    vendorRulesApi
      .list()
      .then((res) => {
        if (cancelled) return;
        setVendors(res.vendors);
        if (res.vendors.length > 0 && !activeKey) {
          setActiveKey(res.vendors[0].vendor_key);
        }
      })
      .catch((e) => {
        if (cancelled) return;
        setError(getFriendlyErrorMessage(e, "Load vendors"));
      })
      .finally(() => {
        if (!cancelled) setBusy(null);
      });
    return () => {
      cancelled = true;
    };
    // We intentionally only run once on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Load active vendor rules when key changes.
  useEffect(() => {
    if (!activeKey) return;
    let cancelled = false;
    setBusy("loading");
    setEdits({});
    setIssues([]);
    setError(null);
    vendorRulesApi
      .get(activeKey)
      .then((p) => {
        if (cancelled) return;
        setPayload(p);
        // Default: open the first 3 groups so operators see the most common
        // edits without clicking anything.
        setOpenGroupKeys(new Set(p.groups.slice(0, 3).map((g) => g.key)));
      })
      .catch((e) => {
        if (cancelled) return;
        setError(getFriendlyErrorMessage(e, "Load rules"));
      })
      .finally(() => {
        if (!cancelled) setBusy(null);
      });
    return () => {
      cancelled = true;
    };
  }, [activeKey]);

  // Phase 2A — load the user's batches for the "Test against" dropdown.
  // Read-only fetch; we don't mutate any batch.
  useEffect(() => {
    let cancelled = false;
    api
      .listBatches()
      .then((res) => {
        if (cancelled) return;
        setBatches(res.batches);
      })
      .catch(() => {
        // Silent — the impact panel will explain why no batches are pickable.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Reset impact panel when the active vendor changes.
  useEffect(() => {
    setImpact(null);
    setImpactError(null);
  }, [activeKey]);

  const setEdit = useCallback((path: string, value: unknown) => {
    setEdits((prev) => ({ ...prev, [path]: value }));
  }, []);

  const resetEdits = useCallback(() => {
    setEdits({});
    setIssues([]);
  }, []);

  const validate = useCallback(async () => {
    if (!activeKey) return;
    if (Object.keys(edits).length === 0) {
      setIssues([]);
      pushToast({ tone: "info", message: "Nothing to validate — no unsaved changes." });
      return;
    }
    setBusy("validating");
    try {
      const res = await vendorRulesApi.validate(activeKey, edits);
      setIssues(res.issues);
      pushToast({
        tone: res.ok ? "success" : "error",
        message: res.ok ? "All changes look valid." : `${res.issues.length} validation issue(s).`,
      });
    } catch (e) {
      setError(getFriendlyErrorMessage(e, "Validate"));
    } finally {
      setBusy(null);
    }
  }, [activeKey, edits, pushToast]);

  const save = useCallback(async () => {
    if (!activeKey) return;
    if (Object.keys(edits).length === 0) {
      pushToast({ tone: "info", message: "Nothing to save." });
      return;
    }
    setBusy("saving");
    try {
      const res = await vendorRulesApi.patch(activeKey, edits);
      setPayload(res);
      setEdits({});
      setIssues([]);
      pushToast({
        tone: "success",
        message: `Saved. Backup: ${res.result.backup_filename}`,
        ttl: 4500,
      });
    } catch (e) {
      const friendly = getFriendlyErrorMessage(e, "Save rules");
      setError(friendly);
      pushToast({ tone: "error", message: friendly });
    } finally {
      setBusy(null);
    }
  }, [activeKey, edits, pushToast]);

  const testAgainstBatch = useCallback(async () => {
    if (!activeKey) return;
    if (!testBatchId) {
      pushToast({ tone: "info", message: "Pick a batch to test against first." });
      return;
    }
    setImpactBusy(true);
    setImpactError(null);
    try {
      const res = await vendorRulesApi.previewImpact(activeKey, testBatchId, edits);
      setImpact(res);
      const cells = res.summary.cells_changed;
      const linkOnly = res.summary.dry_run_only_link_changes;
      const tone = cells > 0 ? "success" : "info";
      let message: string;
      if (cells === 0 && linkOnly === 0) {
        message = "Draft rules produced no changes for this batch.";
      } else if (cells === 0 && linkOnly > 0) {
        // Phase 2B — surface the dry-run-only nature so the operator
        // doesn't read "0 meaningful changes" as a failed test.
        message =
          "No meaningful rule impact. Only support-document links differ (dry-run skips Dropbox).";
      } else {
        message = `Draft rules changed ${cells} cell${cells === 1 ? "" : "s"} across ${
          res.summary.rows_modified
        } row${res.summary.rows_modified === 1 ? "" : "s"}.`;
      }
      pushToast({ tone, message, ttl: 4500 });
    } catch (e) {
      const friendly = getFriendlyErrorMessage(e, "Test against batch");
      setImpactError(friendly);
      setImpact(null);
    } finally {
      setImpactBusy(false);
    }
  }, [activeKey, edits, pushToast, testBatchId]);

  const restore = useCallback(async () => {
    if (!activeKey) return;
    // Phase 2E — app-native confirm via the host's requestConfirm. Falls
    // back to a single-step restore if no host is wired (covers the
    // popout case which doesn't host the studio in production).
    if (requestConfirm) {
      const ok = await requestConfirm({
        title: "Restore from latest backup?",
        message:
          "Unsaved edits will be lost. The most recent backup will overwrite the current YAML.",
        confirmLabel: "Restore",
        cancelLabel: "Cancel",
        tone: "warning",
      });
      if (!ok) return;
    }
    setBusy("restoring");
    try {
      const res = await vendorRulesApi.restore(activeKey);
      setPayload(res);
      setEdits({});
      setIssues([]);
      pushToast({
        tone: "success",
        message: `Restored from ${res.result.restored_from}.`,
      });
    } catch (e) {
      pushToast({ tone: "error", message: getFriendlyErrorMessage(e, "Restore") });
    } finally {
      setBusy(null);
    }
  }, [activeKey, pushToast]);

  const toggleGroup = useCallback((key: string) => {
    setOpenGroupKeys((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  const issuesByPath = useMemo(() => {
    const m = new Map<string, string>();
    issues.forEach((it) => m.set(it.path, it.message));
    return m;
  }, [issues]);

  const dirtyCount = Object.keys(edits).length;
  const activeVendor = vendors.find((v) => v.vendor_key === activeKey) || null;

  return (
    <div className="rules-studio">
      {/* Left: vendor list */}
      <aside className="rules-vendor-list">
        <div className="rules-pane-header">
          <span>Vendors</span>
        </div>
        <ul>
          {vendors.map((v) => (
            <li
              key={v.vendor_key}
              className={`rules-vendor-item ${v.vendor_key === activeKey ? "active" : ""}`}
              onClick={() => setActiveKey(v.vendor_key)}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") setActiveKey(v.vendor_key);
              }}
            >
              <div className="rules-vendor-name">{v.display_name}</div>
              <div className="rules-vendor-meta">
                <span className="rules-vendor-cat">{v.category}</span>
                <span className={`rules-vendor-status status-${v.status}`}>{v.status}</span>
              </div>
              {v.last_updated && (
                <div className="rules-vendor-stamp">
                  {formatStamp(v.last_updated)}
                </div>
              )}
            </li>
          ))}
        </ul>
      </aside>

      {/* Center: editor */}
      <section className="rules-editor">
        <header className="rules-editor-header">
          <div className="rules-editor-titles">
            <h2>{activeVendor?.display_name || "Vendor Rules"}</h2>
            {activeVendor?.category && (
              <span className="rules-editor-subtitle">{activeVendor.category}</span>
            )}
          </div>
          <div className="rules-editor-actions">
            {dirtyCount > 0 && (
              <span className="rules-dirty-pill" title="Unsaved changes">
                {dirtyCount} unsaved
              </span>
            )}
            <button
              type="button"
              className="btn"
              onClick={resetEdits}
              disabled={dirtyCount === 0 || busy !== null}
            >
              Reset
            </button>
            <button
              type="button"
              className="btn"
              onClick={validate}
              disabled={busy !== null}
            >
              {busy === "validating" ? "Validating…" : "Validate"}
            </button>
            <button
              type="button"
              className="btn"
              onClick={restore}
              disabled={busy !== null}
              title="Restore from latest backup"
            >
              Restore
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={save}
              disabled={dirtyCount === 0 || busy !== null}
            >
              {busy === "saving" ? "Saving…" : "Save"}
            </button>
          </div>
        </header>

        {error && <div className="rules-error-banner">{error}</div>}

        {/* Phase 2A — Test against batch */}
        <div className="rules-impact-panel">
          <div className="rules-impact-row">
            <label className="rules-impact-label">
              Test these rules against a batch before saving.
            </label>
            <select
              className="rules-input rules-impact-select"
              value={testBatchId}
              onChange={(e) => setTestBatchId(e.target.value)}
              disabled={impactBusy}
            >
              <option value="">— pick a batch —</option>
              {batches.map((b) => (
                <option key={b.batch_id} value={b.batch_id}>
                  {(b.batch_name || "Untitled batch") +
                    " · " +
                    b.invoices_count +
                    " inv"}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="btn"
              onClick={testAgainstBatch}
              disabled={impactBusy || !testBatchId || !activeKey}
            >
              {impactBusy ? "Testing rules…" : "Test against batch"}
            </button>
          </div>
          <div className="rules-impact-disclaimer">
            Preview only. This will not upload to Dropbox, write export files,
            or change any source documents.
          </div>
          {impactError && (
            <div className="rules-error-banner rules-error-inline">
              {impactError}
            </div>
          )}
          {impact && <ImpactSummaryView impact={impact} />}
        </div>

        {!payload && busy === "loading" && (
          <div className="rules-loading">Loading rules…</div>
        )}

        {payload &&
          payload.groups.map((g) => {
            const isOpen = openGroupKeys.has(g.key);
            const groupIssues = g.fields
              .map((f) => issuesByPath.get(f.path))
              .filter(Boolean) as string[];
            return (
              <div key={g.key} className={`rules-group ${isOpen ? "open" : ""}`}>
                <button
                  type="button"
                  className="rules-group-toggle"
                  onClick={() => toggleGroup(g.key)}
                  aria-expanded={isOpen}
                >
                  <span className={`rules-chevron ${isOpen ? "open" : ""}`}>›</span>
                  <span className="rules-group-label">{g.label}</span>
                  {groupIssues.length > 0 && (
                    <span className="rules-group-issues">{groupIssues.length}</span>
                  )}
                </button>
                {isOpen && (
                  <div className="rules-group-body">
                    {g.description && (
                      <p className="rules-group-desc">{g.description}</p>
                    )}
                    {g.read_only_summary && (
                      <ReadOnlySummary group={g} />
                    )}
                    {g.fields.map((f) => (
                      <FieldRow
                        key={f.path}
                        field={f}
                        currentEdit={edits[f.path]}
                        issue={issuesByPath.get(f.path)}
                        onEdit={(value) => setEdit(f.path, value)}
                        onFocus={() => setHelpField({ group: g, field: f })}
                      />
                    ))}
                  </div>
                )}
              </div>
            );
          })}
      </section>

      {/* Right: help / preview */}
      <aside className="rules-help">
        <div className="rules-pane-header">
          <span>Help</span>
        </div>
        {helpField ? (
          <HelpPanel group={helpField.group} field={helpField.field} />
        ) : (
          <div className="rules-help-empty">
            Click any field to see what it controls and where it lives in the
            YAML.
          </div>
        )}
      </aside>
    </div>
  );
}

// =============================================================================
// Field row
// =============================================================================
function FieldRow({
  field,
  currentEdit,
  issue,
  onEdit,
  onFocus,
}: {
  field: RuleField;
  currentEdit: unknown;
  issue?: string;
  onEdit: (value: unknown) => void;
  onFocus: () => void;
}) {
  const value = currentEdit !== undefined ? currentEdit : field.value;
  const editable = field.editable;

  return (
    <div
      className={`rules-field ${editable ? "" : "read-only"} ${issue ? "has-error" : ""} ${
        currentEdit !== undefined ? "is-dirty" : ""
      }`}
      onClick={onFocus}
    >
      <div className="rules-field-header">
        <label className="rules-field-label">{field.label}</label>
        {!editable && (
          <span
            className="rules-field-readonly"
            title="Currently controlled by processor code. Not editable yet."
          >
            read-only
          </span>
        )}
        {currentEdit !== undefined && (
          <span className="rules-field-changed" title="Unsaved change">
            ●
          </span>
        )}
      </div>
      {field.description && (
        <div className="rules-field-desc">{field.description}</div>
      )}
      <FieldInput field={field} value={value} editable={editable} onEdit={onEdit} />
      {issue && <div className="rules-field-error">{issue}</div>}
      {field.example && !issue && (
        <div className="rules-field-example">Example: {field.example}</div>
      )}
    </div>
  );
}

function FieldInput({
  field,
  value,
  editable,
  onEdit,
}: {
  field: RuleField;
  value: unknown;
  editable: boolean;
  onEdit: (value: unknown) => void;
}) {
  if (field.type === "boolean") {
    return (
      <label className="rules-bool">
        <input
          type="checkbox"
          checked={value === true}
          disabled={!editable}
          onChange={(e) => onEdit(e.target.checked)}
        />
        <span>{value === true ? "On" : "Off"}</span>
      </label>
    );
  }
  if (field.type === "enum" && field.options) {
    return (
      <select
        className="rules-input"
        disabled={!editable}
        value={typeof value === "string" ? value : ""}
        onChange={(e) => onEdit(e.target.value)}
      >
        <option value="">— select —</option>
        {field.options.map((opt) => (
          <option key={opt} value={opt}>
            {opt.replace(/_/g, " ")}
          </option>
        ))}
      </select>
    );
  }
  if (field.type === "integer" || field.type === "number") {
    return (
      <input
        type="number"
        className="rules-input"
        disabled={!editable}
        step={field.type === "integer" ? 1 : "any"}
        value={typeof value === "number" ? value : value === "" || value == null ? "" : String(value)}
        placeholder={field.placeholder}
        onChange={(e) => {
          const v = e.target.value;
          if (v === "") {
            onEdit(null);
          } else {
            const parsed = field.type === "integer" ? parseInt(v, 10) : parseFloat(v);
            onEdit(Number.isFinite(parsed) ? parsed : null);
          }
        }}
      />
    );
  }
  if (field.type === "string_list") {
    const list = Array.isArray(value) ? (value as string[]) : [];
    return (
      <textarea
        className="rules-input rules-textarea"
        disabled={!editable}
        rows={Math.min(6, Math.max(2, list.length + 1))}
        value={list.join("\n")}
        placeholder="One per line"
        onChange={(e) => {
          const lines = e.target.value
            .split("\n")
            .map((s) => s.trim())
            .filter((s) => s.length > 0);
          onEdit(lines);
        }}
      />
    );
  }
  // string
  return (
    <input
      type="text"
      className="rules-input"
      disabled={!editable}
      value={typeof value === "string" ? value : value == null ? "" : String(value)}
      placeholder={field.placeholder}
      onChange={(e) => onEdit(e.target.value)}
    />
  );
}

// =============================================================================
// Read-only summary (for sections we surface but don't let operators edit yet)
// =============================================================================
function ReadOnlySummary({ group }: { group: RuleGroup }) {
  const s = group.read_only_summary;
  if (!s) return null;
  return (
    <div className="rules-readonly-summary">
      <div className="rules-readonly-title">Currently controlled by processor code.</div>
      {s.kind === "list" && (
        <div className="rules-readonly-meta">{s.count} item(s).</div>
      )}
      {s.kind === "object" && s.keys && (
        <div className="rules-readonly-meta">
          Top keys: {s.keys.join(", ")}
          {s.keys.length === 20 ? "…" : ""}
        </div>
      )}
      {s.kind === "scalar" && (
        <div className="rules-readonly-meta">{s.preview}</div>
      )}
    </div>
  );
}

// =============================================================================
// Help panel (right column)
// =============================================================================
function HelpPanel({ group, field }: { group: RuleGroup; field: RuleField }) {
  return (
    <div className="rules-help-body">
      <div className="rules-help-section">
        <div className="rules-help-eyebrow">{group.label}</div>
        <h3>{field.label}</h3>
      </div>
      {field.description && (
        <div className="rules-help-section">
          <div className="rules-help-heading">What this rule does</div>
          <p>{field.description}</p>
        </div>
      )}
      {field.example && (
        <div className="rules-help-section">
          <div className="rules-help-heading">Example</div>
          <code className="rules-help-code">{field.example}</code>
        </div>
      )}
      <div className="rules-help-section">
        <div className="rules-help-heading">YAML path</div>
        <code className="rules-help-code">{field.path}</code>
      </div>
      {!field.editable && (
        <div className="rules-help-warning">
          This field is documented here but currently controlled by processor
          code. Editing it has no effect yet.
        </div>
      )}
    </div>
  );
}

// =============================================================================
// Impact summary + per-row diff (Phase 2A; Phase 2B noise filter)
// =============================================================================
function ImpactSummaryView({ impact }: { impact: ImpactPayload }) {
  const s = impact.summary;
  const [showLinkDiffs, setShowLinkDiffs] = useState(false);

  // Phase 2B — primary tiles use the *meaningful* counts; the dry-run
  // technical metric gets its own muted tile.
  const stats: { label: string; value: number; tone?: "ok" | "warn" | "muted" }[] = [
    { label: "Meaningful cells changed", value: s.cells_changed },
    { label: "Amount changes", value: s.amounts_changed },
    {
      label: "GL changes",
      value: s.gl_accounts_changed,
      tone: s.gl_accounts_changed > 0 ? "warn" : undefined,
    },
    { label: "Description changes", value: s.descriptions_changed },
    { label: "Date changes", value: s.dates_changed },
    { label: "Issues before", value: s.issues_before },
    {
      label: "Issues after",
      value: s.issues_after,
      tone: s.issues_after < s.issues_before ? "ok" : undefined,
    },
    {
      label: "Dry-run-only link changes",
      value: s.dry_run_only_link_changes,
      tone: "muted",
    },
  ];

  return (
    <div className="rules-impact-result">
      {impact.no_meaningful_impact && impact.no_meaningful_impact_message && (
        <div className="rules-impact-no-impact">
          {impact.no_meaningful_impact_message}
        </div>
      )}

      <div className="rules-impact-stats">
        {stats.map((st) => (
          <div key={st.label} className={`rules-impact-stat tone-${st.tone || "neutral"}`}>
            <div className="rules-impact-stat-value">{st.value}</div>
            <div className="rules-impact-stat-label">{st.label}</div>
          </div>
        ))}
      </div>

      {impact.warnings.length > 0 && (
        <ul className="rules-impact-warnings">
          {impact.warnings.map((w, i) => (
            <li key={i}>{w}</li>
          ))}
        </ul>
      )}
      {(s.rows_added > 0 || s.rows_removed > 0) && (
        <div className="rules-impact-extras">
          {s.rows_added > 0 && (
            <span className="rules-impact-chip rules-impact-chip-added">
              +{s.rows_added} new row(s)
            </span>
          )}
          {s.rows_removed > 0 && (
            <span className="rules-impact-chip rules-impact-chip-removed">
              −{s.rows_removed} row(s) removed
            </span>
          )}
        </div>
      )}

      <ImpactDiffTable rows={impact.row_diffs} showLinkDiffs={showLinkDiffs} />

      {s.dry_run_only_link_changes > 0 && (
        <label className="rules-impact-toggle">
          <input
            type="checkbox"
            checked={showLinkDiffs}
            onChange={(e) => setShowLinkDiffs(e.target.checked)}
          />
          <span>
            Show dry-run technical differences ({s.dry_run_only_link_changes}{" "}
            link change{s.dry_run_only_link_changes === 1 ? "" : "s"})
          </span>
        </label>
      )}

      {impact.row_diffs_truncated && (
        <div className="rules-impact-truncation">
          Showing first {impact.row_diffs.length} affected rows.
        </div>
      )}
    </div>
  );
}

function ImpactDiffTable({
  rows,
  showLinkDiffs,
}: {
  rows: ImpactPayload["row_diffs"];
  showLinkDiffs: boolean;
}) {
  // Pick rows to show. Default view = meaningful-only; toggle adds the
  // rows that contain only dry-run link flips.
  const visible = rows
    .filter((r) => r.kind === "modified" && r.changes.length > 0)
    .filter((r) => (showLinkDiffs ? true : r.has_meaningful_changes !== false))
    // Each row's per-change list is filtered too: the default view
    // hides the technical link rows even on rows that *do* have
    // meaningful changes, so the table reads as pure rule impact.
    .map((r) => {
      const changes = showLinkDiffs
        ? r.changes
        : r.changes.filter((c) => c.category !== "dry_run_link");
      return { ...r, changes };
    })
    .filter((r) => r.changes.length > 0);

  if (visible.length === 0) {
    return (
      <div className="rules-impact-empty">
        No row-level differences. Try a wider edit and run "Test against batch" again.
      </div>
    );
  }
  return (
    <div className="rules-impact-table-wrap">
      <table className="rules-impact-table">
        <thead>
          <tr>
            <th>Source</th>
            <th>Invoice</th>
            <th>Column</th>
            <th>Before</th>
            <th>After</th>
          </tr>
        </thead>
        <tbody>
          {visible.flatMap((r) =>
            r.changes.map((c, ci) => (
              <tr
                key={`${r.row_key}::${c.column}::${ci}`}
                className={c.category === "dry_run_link" ? "is-dry-run-link" : ""}
              >
                {ci === 0 ? (
                  <>
                    <td rowSpan={r.changes.length} className="rules-impact-source">
                      {r.source_file ? (
                        <>
                          <div className="rules-impact-source-name" title={r.source_file}>
                            {r.source_file}
                          </div>
                          {r.source_page && (
                            <div className="rules-impact-source-page">page {r.source_page}</div>
                          )}
                        </>
                      ) : (
                        <span className="rules-impact-source-name muted">—</span>
                      )}
                    </td>
                    <td rowSpan={r.changes.length}>{r.invoice_number || "—"}</td>
                  </>
                ) : null}
                <td className="rules-impact-col">
                  {c.column}
                  {c.category === "dry_run_link" && (
                    <span className="rules-impact-tag" title="Dry-run only — Dropbox skipped">
                      dry-run
                    </span>
                  )}
                </td>
                <td className="rules-impact-before">{renderCell(c.before)}</td>
                <td className="rules-impact-after">{renderCell(c.after)}</td>
              </tr>
            )),
          )}
        </tbody>
      </table>
    </div>
  );
}

function renderCell(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

function formatStamp(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleString();
  } catch {
    return iso;
  }
}
