import { useCallback, useEffect, useMemo, useState } from "react";

import { api, getFriendlyErrorMessage } from "../api";
import type {
  CanonicalCategoryEditable,
  CanonicalCategoryPayload,
  CanonicalFixtureSummary,
  CanonicalRulesImportPreview,
  CanonicalRulesPayload,
  CanonicalRulesRunAllResponse,
  CanonicalRulesTestBenchResponse,
} from "../types";
import type { Toast } from "./Toasts";

type Props = {
  pushToast: (toast: Omit<Toast, "id"> & { id?: string }) => void;
};

const LOCATION_POLICIES = [
  { value: "valid_unit_if_present", label: "Use valid unit when present" },
  { value: "optional_valid_unit_only", label: "Location optional, valid unit only" },
  { value: "property_level_blank_location_allowed", label: "Property-level service may stay blank" },
];

function cloneEditable(editable: CanonicalCategoryEditable): CanonicalCategoryEditable {
  return JSON.parse(JSON.stringify(editable));
}

function listToText(values: string[] | undefined): string {
  return (values || []).join("\n");
}

function textToList(value: string): string[] {
  return value
    .split(/\n|,/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function mapToText(values: Record<string, string> | undefined): string {
  return Object.entries(values || {})
    .map(([key, value]) => `${key}: ${value}`)
    .join("\n");
}

function textToMap(value: string): Record<string, string> {
  const result: Record<string, string> = {};
  for (const rawLine of value.split(/\n/)) {
    const line = rawLine.trim();
    if (!line) continue;
    const match = line.match(/^([^:=]+)[:=](.+)$/);
    if (!match) continue;
    result[match[1].trim()] = match[2].trim();
  }
  return result;
}

function pretty(value: unknown): string {
  if (Array.isArray(value)) return value.join(", ");
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (value && typeof value === "object") return JSON.stringify(value);
  return String(value ?? "");
}

export function CanonicalRulesStudio({ pushToast }: Props) {
  const [payload, setPayload] = useState<CanonicalRulesPayload | null>(null);
  const [categoryKey, setCategoryKey] = useState("trash_collection_services");
  const [category, setCategory] = useState<CanonicalCategoryPayload | null>(null);
  const [draft, setDraft] = useState<CanonicalCategoryEditable | null>(null);
  const [bench, setBench] = useState<CanonicalRulesTestBenchResponse | null>(null);
  const [fixtures, setFixtures] = useState<CanonicalFixtureSummary[]>([]);
  const [fixtureKey, setFixtureKey] = useState("capital_waste");
  const [allBench, setAllBench] = useState<CanonicalRulesRunAllResponse | null>(null);
  const [importPreview, setImportPreview] = useState<CanonicalRulesImportPreview | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadList = useCallback(async () => {
    setBusy("loading");
    setError(null);
    try {
      const res = await api.canonicalRules();
      setPayload(res);
      if (!res.categories.some((item) => item.key === categoryKey)) {
        setCategoryKey(res.categories[0]?.key || "utilities");
      }
    } catch (e) {
      const message = getFriendlyErrorMessage(e, "Load canonical rules");
      setError(message);
      pushToast({ tone: "error", message });
    } finally {
      setBusy(null);
    }
  }, [categoryKey, pushToast]);

  const loadCategory = useCallback(
    async (key: string) => {
      if (!key) return;
      setBusy((current) => current || "category");
      setError(null);
      try {
        const res = await api.canonicalRuleCategory(key);
        setCategory(res);
        setDraft(cloneEditable(res.editable));
        setBench(null);
        setAllBench(null);
        setImportPreview(null);
      } catch (e) {
        const message = getFriendlyErrorMessage(e, "Load category rules");
        setError(message);
        pushToast({ tone: "error", message });
      } finally {
        setBusy(null);
      }
    },
    [pushToast],
  );

  useEffect(() => {
    void loadList();
  }, [loadList]);

  useEffect(() => {
    let cancelled = false;
    api
      .canonicalRulesTestFixtures()
      .then((res) => {
        if (cancelled) return;
        setFixtures(res.fixtures);
        if (!res.fixtures.some((fixture) => fixture.key === fixtureKey)) {
          setFixtureKey(res.fixtures[0]?.key || "capital_waste");
        }
      })
      .catch((e) => {
        const message = getFriendlyErrorMessage(e, "Load canonical fixtures");
        pushToast({ tone: "warning", message, ttl: 3500 });
      });
    return () => {
      cancelled = true;
    };
  }, [fixtureKey, pushToast]);

  useEffect(() => {
    if (!payload) return;
    void loadCategory(categoryKey);
  }, [categoryKey, loadCategory, payload]);

  const dirty = useMemo(() => {
    if (!category || !draft) return false;
    return JSON.stringify(category.editable) !== JSON.stringify(draft);
  }, [category, draft]);

  const selectedFixture = useMemo(
    () => fixtures.find((fixture) => fixture.key === fixtureKey) || null,
    [fixtureKey, fixtures],
  );

  const patch = useMemo<Record<string, unknown>>(() => {
    if (!draft) return {};
    return { ...draft };
  }, [draft]);

  const updateDraft = useCallback((updater: (draft: CanonicalCategoryEditable) => void) => {
    setDraft((prev) => {
      if (!prev) return prev;
      const next = cloneEditable(prev);
      updater(next);
      return next;
    });
  }, []);

  const validate = useCallback(async () => {
    if (!category || !draft) return;
    setBusy("validate");
    setError(null);
    try {
      const res = await api.validateCanonicalRules({ category: category.category.key, patch });
      if (res.ok) {
        pushToast({ tone: "success", message: "Canonical rules are valid.", ttl: 3000 });
      } else {
        pushToast({ tone: "warning", message: res.issues[0]?.message || "Rules need review." });
      }
      setCategory({ ...category, validation: res });
    } catch (e) {
      const message = getFriendlyErrorMessage(e, "Validate canonical rules");
      setError(message);
      pushToast({ tone: "error", message });
    } finally {
      setBusy(null);
    }
  }, [category, draft, patch, pushToast]);

  const save = useCallback(async () => {
    if (!category || !draft) return;
    setBusy("save");
    setError(null);
    try {
      const res = await api.patchCanonicalRuleCategory(category.category.key, patch);
      setCategory(res.category);
      setDraft(cloneEditable(res.category.editable));
      await loadList();
      pushToast({ tone: "success", message: "Canonical rules saved with backup.", ttl: 3500 });
    } catch (e) {
      const message = getFriendlyErrorMessage(e, "Save canonical rules");
      setError(message);
      pushToast({ tone: "error", message });
    } finally {
      setBusy(null);
    }
  }, [category, draft, loadList, patch, pushToast]);

  const restore = useCallback(async () => {
    setBusy("restore");
    setError(null);
    try {
      await api.restoreCanonicalRules();
      await loadList();
      await loadCategory(categoryKey);
      pushToast({ tone: "success", message: "Restored latest Canonical Rules backup." });
    } catch (e) {
      const message = getFriendlyErrorMessage(e, "Restore canonical rules");
      setError(message);
      pushToast({ tone: "error", message });
    } finally {
      setBusy(null);
    }
  }, [categoryKey, loadCategory, loadList, pushToast]);

  const runBench = useCallback(async () => {
    if (!category) return;
    setBusy("bench");
    setError(null);
    try {
      const res = await api.runCanonicalRulesTestBench({
        fixture_key: fixtureKey,
        category: category.category.key,
        draft_patch: dirty ? patch : undefined,
      });
      setBench(res);
      setAllBench(null);
      pushToast({
        tone: res.ok ? "success" : "warning",
        message: res.skipped
          ? `Fixture skipped: ${res.skip_reason || "Fixture is registered but incomplete."}`
          : res.ok
            ? `${res.title} passed.`
            : `${res.title} has mismatches.`,
        ttl: 3500,
      });
    } catch (e) {
      const message = getFriendlyErrorMessage(e, "Run canonical test bench");
      setError(message);
      pushToast({ tone: "error", message });
    } finally {
      setBusy(null);
    }
  }, [category, dirty, fixtureKey, patch, pushToast]);

  const runAllBench = useCallback(async () => {
    if (!category) return;
    setBusy("bench-all");
    setError(null);
    try {
      const res = await api.runAllCanonicalRulesFixtures({
        category: category.category.key,
        draft_patch: dirty ? patch : undefined,
      });
      setAllBench(res);
      const failed = res.summary.filter((item) => item.status === "FAIL").length;
      const skipped = res.summary.filter((item) => item.status === "SKIPPED").length;
      pushToast({
        tone: failed ? "warning" : "success",
        message: failed
          ? `${failed} canonical fixture(s) failed.`
          : `Canonical fixture suite passed. ${skipped} incomplete fixture(s) skipped.`,
        ttl: 4500,
      });
    } catch (e) {
      const message = getFriendlyErrorMessage(e, "Run canonical fixture suite");
      setError(message);
      pushToast({ tone: "error", message });
    } finally {
      setBusy(null);
    }
  }, [category, dirty, patch, pushToast]);

  const previewImport = useCallback(async () => {
    setBusy("import-preview");
    setError(null);
    try {
      const res = await api.previewCanonicalRulesImport();
      setImportPreview(res);
      pushToast({ tone: res.validation.ok ? "success" : "warning", message: "Excel import preview ready." });
    } catch (e) {
      const message = getFriendlyErrorMessage(e, "Preview Excel import");
      setError(message);
      pushToast({ tone: "error", message });
    } finally {
      setBusy(null);
    }
  }, [pushToast]);

  const applyImport = useCallback(async () => {
    setBusy("import-apply");
    setError(null);
    try {
      await api.applyCanonicalRulesImport();
      await loadList();
      await loadCategory(categoryKey);
      pushToast({ tone: "success", message: "Imported Canonica rules.xlsx into runtime YAML." });
    } catch (e) {
      const message = getFriendlyErrorMessage(e, "Apply Excel import");
      setError(message);
      pushToast({ tone: "error", message });
    } finally {
      setBusy(null);
    }
  }, [categoryKey, loadCategory, loadList, pushToast]);

  if (!payload || !category || !draft) {
    return <div className="settings-loading">Loading Canonical Rules Studio...</div>;
  }

  return (
    <div className="canonical-studio">
      <aside className="canonical-category-list">
        <div className="canonical-pane-header">
          <div>
            <strong>Canonical Rules</strong>
            <span>Category-level source of truth</span>
          </div>
        </div>
        <div className="canonical-category-scroll">
          {payload.categories.map((item) => (
            <button
              key={item.key}
              type="button"
              className={`canonical-category-item ${item.key === categoryKey ? "active" : ""}`}
              onClick={() => setCategoryKey(item.key)}
            >
              <strong>{item.label}</strong>
              <span>{item.key}</span>
            </button>
          ))}
        </div>
      </aside>

      <main className="canonical-main">
        <header className="canonical-header">
          <div>
            <h3>{category.category.label}</h3>
            <p>
              Human-readable rules drive AI fallback, reference validation,
              Bulk Mode, Single Invoice Mode, and export readiness.
            </p>
          </div>
          <div className="canonical-actions">
            {dirty && <span className="settings-unsaved">Unsaved</span>}
            <button className="btn btn-compact" type="button" onClick={restore} disabled={busy !== null}>
              Restore
            </button>
            <button className="btn btn-compact" type="button" onClick={validate} disabled={busy !== null}>
              Validate
            </button>
            <button className="btn btn-compact btn-primary" type="button" onClick={save} disabled={busy !== null || !dirty}>
              Save
            </button>
          </div>
        </header>

        {error && <div className="settings-error inline">{error}</div>}

        <section className="canonical-grid">
          <div className="canonical-rule-groups">
            {category.groups.map((group) => (
              <section key={group.key} className="canonical-rule-card">
                <h4>{group.title}</h4>
                <ul>
                  {group.items.map((item, index) => (
                    <li key={`${group.key}-${index}`}>{item}</li>
                  ))}
                </ul>
              </section>
            ))}
          </div>

          <aside className="canonical-editor">
            <section className="settings-compact-card">
              <div className="settings-section-caption">Editable controls</div>
              <label className="canonical-field">
                Vendor keywords
                <textarea
                  className="settings-field canonical-textarea"
                  value={listToText(draft.vendor_keywords)}
                  onChange={(event) =>
                    updateDraft((next) => {
                      next.vendor_keywords = textToList(event.target.value);
                    })
                  }
                />
              </label>
              <label className="canonical-field">
                Service keywords
                <textarea
                  className="settings-field canonical-textarea"
                  value={listToText(draft.service_keywords)}
                  onChange={(event) =>
                    updateDraft((next) => {
                      next.service_keywords = textToList(event.target.value);
                    })
                  }
                />
              </label>
              <label className="canonical-field">
                Location policy
                <select
                  className="settings-field"
                  value={draft.location_policy}
                  onChange={(event) =>
                    updateDraft((next) => {
                      next.location_policy = event.target.value;
                    })
                  }
                >
                  {LOCATION_POLICIES.map((policy) => (
                    <option key={policy.value} value={policy.value}>
                      {policy.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="canonical-field">
                Default GL candidates
                <textarea
                  className="settings-field canonical-textarea small"
                  value={mapToText(draft.default_gl_candidates)}
                  onChange={(event) =>
                    updateDraft((next) => {
                      next.default_gl_candidates = textToMap(event.target.value);
                    })
                  }
                />
              </label>
              <label className="canonical-field">
                Ignored line keywords
                <textarea
                  className="settings-field canonical-textarea small"
                  value={listToText(draft.ignore_line_keywords)}
                  onChange={(event) =>
                    updateDraft((next) => {
                      next.ignore_line_keywords = textToList(event.target.value);
                    })
                  }
                />
              </label>
            </section>

            <section className="settings-compact-card">
              <div className="settings-section-caption">Description formats</div>
              <label className="canonical-field">
                Invoice description
                <textarea
                  className="settings-field canonical-format"
                  value={draft.invoice_description_format}
                  onChange={(event) =>
                    updateDraft((next) => {
                      next.invoice_description_format = event.target.value;
                    })
                  }
                />
              </label>
              <label className="canonical-field">
                Line item description
                <textarea
                  className="settings-field canonical-format"
                  value={draft.line_item_description_format}
                  onChange={(event) =>
                    updateDraft((next) => {
                      next.line_item_description_format = event.target.value;
                    })
                  }
                />
              </label>
              <div className="settings-variable-strip">
                {payload.variables.map((variable) => (
                  <button
                    key={variable.key}
                    type="button"
                    onClick={() => {
                      void navigator.clipboard?.writeText(variable.label);
                      pushToast({ tone: "success", message: `${variable.label} copied.`, ttl: 1800 });
                    }}
                  >
                    {variable.label}
                  </button>
                ))}
              </div>
            </section>
          </aside>
        </section>

        <section className="canonical-testbench">
          <div className="canonical-testbench-header">
            <div>
              <h4>Rule Test Bench</h4>
              <p>Run expected-vs-actual fixture tests without writing batches, exports, Dropbox files, YAML, or AI calls.</p>
            </div>
            <div className="canonical-actions">
              <label className="canonical-fixture-picker">
                Test fixture
                <select
                  className="settings-field"
                  value={fixtureKey}
                  onChange={(event) => {
                    setFixtureKey(event.target.value);
                    setBench(null);
                    setAllBench(null);
                  }}
                >
                  {fixtures.map((fixture) => (
                    <option key={fixture.key} value={fixture.key}>
                      {fixture.vendor} {fixture.status !== "complete" ? "(Incomplete)" : ""}
                    </option>
                  ))}
                </select>
                {selectedFixture && selectedFixture.status !== "complete" && (
                  <span className="canonical-fixture-note">
                    {selectedFixture.skip_reason || "This fixture is registered but incomplete."}
                  </span>
                )}
              </label>
              <button className="btn btn-compact" type="button" onClick={previewImport} disabled={busy !== null}>
                Preview Excel import
              </button>
              <button
                className="btn btn-compact"
                type="button"
                onClick={applyImport}
                disabled={busy !== null || !importPreview?.validation.ok}
              >
                Apply import
              </button>
              <button className="btn btn-compact" type="button" onClick={runAllBench} disabled={busy !== null}>
                Run all fixtures
              </button>
              <button className="btn btn-compact btn-primary" type="button" onClick={runBench} disabled={busy !== null}>
                Run selected
              </button>
            </div>
          </div>

          {importPreview && (
            <div className="canonical-import-note">
              <strong>Excel preview</strong>
              <span>
                {importPreview.imported_rows} rows read. Changed categories:{" "}
                {importPreview.changed_categories.length ? importPreview.changed_categories.join(", ") : "none"}.
              </span>
            </div>
          )}

          {bench && (
            <div className="canonical-bench-result">
              <div className={`canonical-bench-status ${bench.ok ? "pass" : "fail"}`}>
                {bench.skipped ? "Skipped" : bench.ok ? "Pass" : "Needs review"}
                {bench.dry_run && <span>Dry run with unsaved edits</span>}
                <small>{bench.title}</small>
                <button
                  type="button"
                  className="btn btn-compact"
                  onClick={() => downloadJson(`${bench.fixture_key || bench.test_case}_canonical_result.json`, bench)}
                >
                  Export JSON
                </button>
              </div>
              <div className="canonical-check-table">
                {Object.entries(groupChecks(bench.checks)).map(([group, checks]) => (
                  <section key={group} className="canonical-check-group">
                    <h5>{group}</h5>
                    {checks.map((check) => (
                      <div key={`${group}-${check.field}`} className={check.pass ? "pass" : "fail"}>
                        <span>{check.field.replace(/_/g, " ")}</span>
                        <strong>{pretty(check.actual)}</strong>
                        <em>{check.pass ? "matches" : `expected ${pretty(check.expected)}`}</em>
                      </div>
                    ))}
                  </section>
                ))}
              </div>
              <div className="canonical-timeline">
                {bench.reasoning_timeline.map((item) => (
                  <div key={item.step}>
                    <strong>{item.step}</strong>
                    <span>{item.detail}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {allBench && (
            <div className="canonical-suite-result">
              <div className={`canonical-bench-status ${allBench.ok ? "pass" : "fail"}`}>
                {allBench.ok ? "Fixture suite passed" : "Fixture suite has failures"}
                <button
                  type="button"
                  className="btn btn-compact"
                  onClick={() => downloadJson("canonical_fixture_suite_result.json", allBench)}
                >
                  Export JSON
                </button>
              </div>
              <div className="canonical-suite-grid">
                {allBench.summary.map((item) => (
                  <div key={item.fixture_key} className={item.status.toLowerCase()}>
                    <strong>{item.fixture_key.replace(/_/g, " ")}</strong>
                    <span>{item.status}</span>
                    {item.failed_checks.length > 0 && <em>{item.failed_checks.join(", ")}</em>}
                    {item.status === "SKIPPED" && item.skip_reason && <em>{item.skip_reason}</em>}
                  </div>
                ))}
              </div>
            </div>
          )}
        </section>
      </main>
    </div>
  );
}

function downloadJson(filename: string, payload: unknown): void {
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function groupChecks(
  checks: CanonicalRulesTestBenchResponse["checks"],
): Record<string, CanonicalRulesTestBenchResponse["checks"]> {
  return checks.reduce<Record<string, CanonicalRulesTestBenchResponse["checks"]>>((acc, check) => {
    const group = check.group || "Other";
    acc[group] = acc[group] || [];
    acc[group].push(check);
    return acc;
  }, {});
}
