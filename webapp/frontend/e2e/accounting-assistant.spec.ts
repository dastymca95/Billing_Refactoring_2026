import { expect, test, type Page } from "@playwright/test";

const draftRule = {
  contract_version: "operator-accounting-rule/1.0",
  rule_id: "oar-test",
  title: "Legal expense constraint",
  description: "Restrict legal lines to approved legal expense accounts.",
  scope: { document_family: null, line_family: "legal", trade_family: null, work_mode: null, description_terms: ["legal"], term_match: "any" },
  constraint: { allowed_gl_codes: ["6205"], minimum_gl_code: null, maximum_gl_code: null },
  status: "draft",
  created_at: "2026-07-15T12:00:00Z",
  updated_at: "2026-07-15T12:00:00Z",
  approved_by: null,
  approved_at: null,
  source_interaction_id: "aai-test",
  audit: [{ event: "draft_created", actor: "accounting_assistant", at: "2026-07-15T12:00:00Z", details: {} }],
};

const tenantPolicy = {
  contract_version: "tenant-accounting-policy/1.0", tenant_id: "local-default", policy_id: "tap-test", version: 1,
  title: "Approved internet vendor policy", description: "Constrain matching internet lines to GL 6139.",
  policy_type: "vendor_service_gl",
  scope: { vendor_entity_id: "tve-test", property_ids: [], document_family: null, line_family: null, trade_family: "internet", work_mode: null, description_terms: ["internet"], term_match: "any" },
  action: { allowed_gl_codes: ["6139"], expected_amount: null, amount_tolerance: "0.01", amount_mismatch_behavior: "review" },
  status: "draft", created_at: "2026-07-15T12:00:00Z", updated_at: "2026-07-15T12:00:00Z", approved_by: null, approved_at: null,
  source_interaction_id: "aai-test", latest_simulation: null,
  audit: [{ event: "policy_draft_created", actor: "accounting_assistant", at: "2026-07-15T12:00:00Z", details: {} }],
};

const preview = {
  batch_id: "assistant-ui",
  summary: {}, by_vendor_summaries: {},
  columns: ["Invoice Number", "Vendor", "Property Abbreviation", "GL Account", "Line Item Description", "Amount"],
  required_columns: ["Invoice Number", "Vendor", "Property Abbreviation", "GL Account", "Amount"],
  recommended_columns: [], optional_columns: ["Line Item Description"], optional_columns_collapsible: true,
  optional_columns_hidden_by_default: true,
  rows: [{
    "Invoice Number": "LEGAL-1", Vendor: "Example Firm", "Property Abbreviation": "PROP",
    "GL Account": "6669", "Line Item Description": "Legal filing service", Amount: 25,
    _meta: { invoice_group_id: "legal-group", readiness_status: "ready" },
  }],
  invoice_count: 1, row_count: 1, unsupported_files: [],
  accounting_readiness: {
    contract_version: "accounting-readiness/1.0", snapshot_id: "ready", status: "ready", export_allowed: true,
    blockers: [], non_blocking_issues: [], validated_fields: {}, reconciliation_status: "passed",
    duplicate_status: "not_detected", evaluated_at: "2026-07-15T12:00:00Z",
  },
};

async function mockApp(page: Page, counters: { saves: number; decisions: number; updates: number; toggles: number; simulations: number; tenantDecisions: number }) {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    let body: unknown = {};
    if (path === "/api/batches") body = { batches: [{ batch_id: "assistant-ui", batch_name: "Assistant UI", status: "completed", files_count: 1, invoices_count: 1, rows_count: 1, manual_review_count: 0, export_available: true, created_at: "now" }] };
    else if (path.endsWith("/files")) body = { files: [] };
    else if (path.endsWith("/progress")) body = { status: "completed", current: 1, total: 1 };
    else if (path.endsWith("/preview")) body = preview;
    else if (path.endsWith("/manual-review")) body = { items: [] };
    else if (path.endsWith("/processing-routes")) body = {
      contract_version: "processing-route-policy/1.0",
      batch: {
        resolution: {
          requested_mode: "auto_cost_safe",
          inherited_from: "batch",
          effective_route: "deterministic",
          ai_fallback_authorized: false,
        },
      },
      documents: [],
      pages: [],
    };
    else if (path === "/api/batches/assistant-ui") body = {
      batch_id: "assistant-ui", batch_name: "Assistant UI", status: "completed",
      files: [], preview_available: true, export_available: true,
    };
    else if (path === "/api/tenant-accounting/context") body = { tenant_id: "local-default", context_source: "environment_adapter", production_auth_required: true };
    else if (path === "/api/tenant-accounting/vendors" && request.method() === "GET") body = { tenant_id: "local-default", items: [{ contract_version: "tenant-vendor-entity/1.0", tenant_id: "local-default", vendor_entity_id: "tve-test", canonical_name: "Example Utility", erp_vendor_id: "erp-test", aliases: ["Example Utility"], created_at: "now", updated_at: "now", audit: [] }] };
    else if (path === "/api/tenant-accounting/policies" && request.method() === "GET") body = { tenant_id: "local-default", items: [tenantPolicy], active_count: 0 };
    else if (path.endsWith("/api/tenant-accounting/policies/tap-test/simulate")) {
      counters.simulations += 1;
      body = { ...tenantPolicy, status: "simulated", latest_simulation: { contract_version: "tenant-policy-simulation/1.0", simulation_id: "tps-test", tenant_id: "local-default", policy_id: "tap-test", policy_version: 1, snapshot_id: "snapshot-test", evaluated_lines: 1, matched_lines: 1, would_constrain_lines: 1, unchanged_lines: 0, amount_mismatches: 0, blocking_conflicts: 0, missing_vendor_identity: 0, examples: [], simulated_at: "now", simulated_by: "local_operator" } };
    } else if (path.endsWith("/api/tenant-accounting/policies/tap-test/decision")) {
      counters.tenantDecisions += 1;
      body = { ...tenantPolicy, status: "active", approved_by: "local_operator", approved_at: "now", latest_simulation: { contract_version: "tenant-policy-simulation/1.0", simulation_id: "tps-test", tenant_id: "local-default", policy_id: "tap-test", policy_version: 1, snapshot_id: "snapshot-test", evaluated_lines: 1, matched_lines: 1, would_constrain_lines: 1, unchanged_lines: 0, amount_mismatches: 0, blocking_conflicts: 0, missing_vendor_identity: 0, examples: [], simulated_at: "now", simulated_by: "local_operator" } };
    }
    else if (path === "/api/accounting-assistant/chat") body = {
      contract_version: "accounting-assistant/1.0", interaction_id: "aai-test", batch_id: "assistant-ui",
      invoice_group_id: "legal-group", assistant_message: "The legal line should use the payable legal account.",
      corrections: [{ row_index: 0, field: "GL Account", new_value: "6205", rationale: "Legal filing evidence supports legal expense.", evidence: ["Legal filing service"] }],
      proposed_rule: draftRule, requires_correction_confirmation: true, requires_rule_confirmation: true,
      accounting_readiness_changed: false, export_authorized: false, provider_profile_id: "deepseek-accounting",
      estimated_cost_usd: 0.0003, created_at: "2026-07-15T12:00:00Z", correction_status: "pending",
    };
    else if (path === "/api/accounting-assistant/interactions" && request.method() === "GET") body = { contract_version: "accounting-assistant/1.0", items: [] };
    else if (path === "/api/accounting-assistant/corrections" && request.method() === "GET") body = { contract_version: "approved-invoice-correction/1.0", active_count: 1, items: [{
      contract_version: "approved-invoice-correction/1.0", correction_id: "aic-test", interaction_id: "aai-test",
      batch_id: "assistant-ui", invoice_group_id: "legal-group", local_row_index: 0, line_fingerprint: "safe-hash",
      field: "GL Account", new_value: "6205", rationale: "Observed legal filing evidence supports the legal expense account.",
      evidence: ["Legal filing service"], approved_by: "reviewer", approved_at: "2026-07-15T12:01:00Z", status: "active",
    }] };
    else if (path.includes("/api/accounting-assistant/interactions/") && path.endsWith("/corrections/decision")) {
      counters.saves += 1;
      body = {
        result: {
          contract_version: "accounting-assistant/1.0", interaction_id: "aai-test", batch_id: "assistant-ui",
          invoice_group_id: "legal-group", assistant_message: "The legal line should use the payable legal account.",
          corrections: [{ row_index: 0, field: "GL Account", new_value: "6205", rationale: "Legal filing evidence supports legal expense.", evidence: ["Legal filing service"] }],
          proposed_rule: draftRule, requires_correction_confirmation: false, requires_rule_confirmation: true,
          accounting_readiness_changed: false, export_authorized: false, provider_profile_id: "deepseek-accounting",
          estimated_cost_usd: 0.0003, created_at: "2026-07-15T12:00:00Z", correction_status: "applied",
          corrections_decided_at: "2026-07-15T12:01:00Z", corrections_decided_by: "local_operator",
        },
        applied: 1,
        replayed: true,
      };
    }
    else if (path === "/api/accounting-assistant/rules" && request.method() === "GET") body = { contract_version: "operator-accounting-rule/1.0", items: [{ ...draftRule, status: "active", approved_by: "reviewer" }], active_count: 1 };
    else if (path.includes("/api/accounting-assistant/rules/") && path.endsWith("/decision")) {
      counters.decisions += 1;
      body = { ...draftRule, status: "active", approved_by: "local_operator", approved_at: "now", audit: [...draftRule.audit, { event: "rule_approved_and_activated", actor: "local_operator", at: "now", details: {} }] };
    } else if (path.includes("/api/accounting-assistant/rules/") && path.endsWith("/status")) {
      counters.toggles += 1;
      body = { ...draftRule, status: "disabled", approved_by: "reviewer" };
    } else if (path.includes("/api/accounting-assistant/rules/") && request.method() === "PUT") {
      counters.updates += 1;
      const payload = request.postDataJSON();
      body = { ...draftRule, ...payload.draft, status: "active", approved_by: "reviewer", updated_at: "now" };
    } else if (path.includes("/activity")) {
      body = { contract_version: "operator-activity/1.0", items: [{
        contract_version: "operator-activity/1.0", event_id: "oae-test", batch_id: "assistant-ui",
        invoice_group_id: "legal-group", event_type: "manual_edits_saved", source: "manual", actor: "reviewer",
        summary: "Saved 1 manual cell change.", details: {}, created_at: "2026-07-15T12:02:00Z",
      }] };
    } else if (path.endsWith("/revisions")) {
      body = { batch_id: "assistant-ui", current_revision_id: "rev-test", revisions: [{ revision_id: "rev-test", created_at: "2026-07-15T12:00:00Z", status: "completed", export_name: null, files_count: 1, invoices_count: 1, rows_count: 1, manual_review_count: 0, source_batch_id: "assistant-ui", snapshot_filename: "rev-test.json" }] };
    } else if (path === "/api/billing-v2/audit") body = { generated_at: "now", count: 0, available_count: 0, processors: [], ai_fallback_module: { module: "", available: false } };
    else if (path === "/api/processing/queue") body = { running: null, queued: [] };
    else if (path === "/api/ai/status") body = { enabled: true, configured: true, provider: "configured", model: "configured", supports_vision: true, vision_enabled: true };
    else if (path === "/api/health") body = { ok: true };
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
}

test("assistant never applies corrections or activates a rule before explicit clicks", async ({ page }) => {
  const counters = { saves: 0, decisions: 0, updates: 0, toggles: 0, simulations: 0, tenantDecisions: 0 };
  await mockApp(page, counters);
  await page.goto("/");
  await page.getByRole("button", { name: "Legacy Billing" }).click();
  await page.getByRole("button", { name: "Abrir asistente contable" }).click();
  await expect(page.getByRole("main", { name: "Accounting AI assistant" })).toBeVisible();
  await page.getByPlaceholder(/Estas líneas son servicios legales/).fill("Use approved legal GL codes for legal invoices.");
  await page.getByRole("button", { name: "Enviar" }).click();
  await expect(page.getByText("The legal line should use the payable legal account.")).toBeVisible();
  await expect(page.getByText("¿Quieres hacer de esto una regla determinística?")).toBeVisible();
  expect(counters.saves).toBe(0);
  expect(counters.decisions).toBe(0);

  await page.getByRole("button", { name: "Aprobar y aplicar correcciones al invoice" }).click();
  await expect.poll(() => counters.saves).toBe(1);
  await expect(page.getByText(/persistentes al reprocesar/)).toBeVisible();
  await page.getByRole("button", { name: "Sí, aprobar" }).click();
  await expect.poll(() => counters.decisions).toBe(1);
  await expect(page.getByText(/aprobada y activa/)).toBeVisible();
  await page.getByRole("button", { name: "Minimizar chat" }).click();
  await expect(page.getByRole("button", { name: "Abrir asistente contable" })).toBeVisible();
  await expect(page.getByRole("button", { name: "AI Assistant" })).toHaveCount(0);
});

test("accounting rules tab supports audited editing and disable", async ({ page }) => {
  const counters = { saves: 0, decisions: 0, updates: 0, toggles: 0, simulations: 0, tenantDecisions: 0 };
  await mockApp(page, counters);
  await page.goto("/");
  await page.getByRole("button", { name: "Accounting Rules" }).click();
  await expect(page.getByRole("main", { name: "Accounting rules library" })).toBeVisible();
  await page.getByRole("button", { name: /Reusable rules/ }).click();
  const title = page.getByLabel("Rule title");
  await title.fill("Updated legal rule");
  await page.getByRole("button", { name: "Save validated rule" }).click();
  await expect.poll(() => counters.updates).toBe(1);
  await page.getByRole("button", { name: "Disable" }).click();
  await expect.poll(() => counters.toggles).toBe(1);
});

test("governance library exposes approved corrections and batch history", async ({ page }) => {
  const counters = { saves: 0, decisions: 0, updates: 0, toggles: 0, simulations: 0, tenantDecisions: 0 };
  await mockApp(page, counters);
  await page.goto("/");
  await page.getByRole("button", { name: "Accounting Rules" }).click();
  await page.getByRole("button", { name: /Approved corrections/ }).click();
  await expect(page.getByText("Observed legal filing evidence supports the legal expense account.")).toBeVisible();
  await page.getByRole("button", { name: "Batch & file history" }).click();
  await expect(page.getByText("Saved 1 manual cell change.")).toBeVisible();
  await expect(page.getByText("v1")).toBeVisible();
});

test("template clock combines change activity and processing revisions", async ({ page }) => {
  const counters = { saves: 0, decisions: 0, updates: 0, toggles: 0, simulations: 0, tenantDecisions: 0 };
  await mockApp(page, counters);
  await page.goto("/");
  await page.getByRole("button", { name: "Legacy Billing" }).click();
  await page.getByTestId("template-batch-selector").click();
  await page.getByTestId("explorer-batch-row").click();
  await expect(page.getByTestId("template-batch-selector")).toContainText("Assistant UI");
  await page.getByTestId("template-revisions-btn").click();
  await expect(page.getByText("Change history")).toBeVisible();
  await expect(page.getByText("Saved 1 manual cell change.")).toBeVisible();
  await expect(page.getByText("Revisions")).toBeVisible();
});

test("tenant policy requires simulation before explicit activation", async ({ page }) => {
  const counters = { saves: 0, decisions: 0, updates: 0, toggles: 0, simulations: 0, tenantDecisions: 0 };
  await mockApp(page, counters);
  await page.goto("/");
  await page.getByRole("button", { name: "Accounting Rules" }).click();
  await expect(page.getByRole("heading", { name: "Approved internet vendor policy" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Approve and activate" })).toHaveCount(0);
  await page.getByRole("button", { name: "Simulate current version" }).click();
  await expect.poll(() => counters.simulations).toBe(1);
  await expect(page.getByText(/1 matched/)).toBeVisible();
  await page.getByRole("button", { name: "Approve and activate" }).click();
  await expect.poll(() => counters.tenantDecisions).toBe(1);
  await expect(page.getByRole("button", { name: /Approved internet vendor policy active/ })).toBeVisible();
});
