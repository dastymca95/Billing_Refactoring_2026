import { expect, test, type Page } from "@playwright/test";

const readiness = {
  contract_version: "accounting-readiness/1.0",
  snapshot_id: "human-ready",
  status: "ready",
  export_allowed: true,
  blockers: [],
  non_blocking_issues: [],
  validated_fields: { "GL Account": true, "Property Abbreviation": true, Amount: true },
  reconciliation_status: "passed",
  duplicate_status: "not_detected",
  evaluated_at: "2026-07-18T12:00:00Z",
};

async function mockInvoiceProcessor(page: Page) {
  let saved = false;
  let saveBody: Record<string, unknown> | null = null;
  const row: Record<string, any> = {
    "Invoice Number": "HUMAN-1",
    Vendor: "Fixture Vendor",
    "Property Abbreviation": "PROP",
    Location: "A",
    "GL Account": "6100",
    "Line Item Description": "Observed repair service",
    Amount: 25,
    _meta: {
      invoice_group_id: "human-group",
      line_item_id: "line-1",
      source_file: "invoice.pdf",
      source_page: 1,
      trace_ids: ["trace-location"],
      readiness_status: "ready",
    },
  };
  const preview = () => ({
    batch_id: "human-ui",
    summary: {},
    by_vendor_summaries: {},
    columns: ["Invoice Number", "Vendor", "Property Abbreviation", "Location", "GL Account", "Line Item Description", "Amount"],
    required_columns: ["Invoice Number", "Vendor", "Property Abbreviation", "GL Account", "Amount"],
    recommended_columns: ["Location", "Line Item Description"],
    optional_columns: [],
    optional_columns_collapsible: true,
    optional_columns_hidden_by_default: true,
    rows: [row],
    invoice_count: 1,
    row_count: 1,
    unsupported_files: [],
    accounting_readiness: readiness,
    invoice_readiness: { "human-group": readiness },
  });

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    let body: unknown = {};
    let status = 200;
    if (path === "/api/batches") body = { batches: [{
      batch_id: "human-ui", batch_name: "Human adjudication", status: "completed",
      files_count: 1, invoices_count: 1, rows_count: 1, manual_review_count: 0,
      export_available: true, created_at: "now",
    }] };
    else if (path === "/api/batches/human-ui") body = {
      batch_id: "human-ui", batch_name: "Human adjudication", created_at: "now",
      files: [{ filename: "invoice.pdf", size_bytes: 10, extension: ".pdf" }],
      files_total: 1, preview_available: true, export_available: true, export_filenames: [], summary: {},
    };
    else if (path.endsWith("/files")) body = { batch_id: "human-ui", files: [] };
    else if (path.endsWith("/progress")) body = { batch_id: "human-ui", status: "completed", percent: 100 };
    else if (path.endsWith("/preview")) body = preview();
    else if (path.endsWith("/manual-review")) body = { batch_id: "human-ui", items: [] };
    else if (path.endsWith("/revisions")) body = { batch_id: "human-ui", current_revision_id: null, revisions: [] };
    else if (path.endsWith("/readiness")) body = readiness;
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
    else if (path === "/api/human-adjudication/context") body = {
      contract_version: "human-invoice-adjudication/1.0",
      reviewer_id: "controller@example.invalid",
      role: "accounting_manager_controller",
      tenant_id: "tenant-a",
      permissions: {
        invoice_correction: true, benchmark_submission: true, learning_approval: true,
        rule_proposal: true, rule_approval: true, shared_knowledge_promotion: false,
      },
    };
    else if (path.startsWith("/api/knowledge-core/batches/")) body = {
      contract_version: "accounting-knowledge-core/1.0", tenant_id: "tenant-a",
      line_item_id: "line-1", canonical_concept: "repair_service",
      document_evidence: { immutable: true },
      historical_vendor_priors: [{ dimension: "vendor", gl_code: "6100", count: 8, amount: "200.00", share: .8, snapshot_id: "cis-test", authoritative: false }],
      historical_property_priors: [], vendor_property_joint_priors: [],
      historical_profile_state: "ready",
      similar_approved_learning_examples: [{ learning_example_id: "learn-1", revision_id: "har-old", canonical_concept: "repair_service", document_family: "invoice", line_family: "service", trade_family: "general", work_mode: "service", gl_code: "6100", evidence_fingerprint: "evidence", candidate_only: true }],
      active_governed_rules: [{ rule_id: "rule-1", version: 1, title: "Approved repair policy", status: "active", allowed_gl_codes: ["6100"], scope: {}, candidate_constraint_only: true }],
      contradictions: [], confidence: .84, provenance: [],
      benchmark_examples_visible_to_production: 0, selection_authority: false, export_authority: false,
    };
    else if (path === "/api/knowledge-core/impact") {
      const payload = request.postDataJSON() as Record<string, boolean>;
      body = { contract_version: "accounting-knowledge-core/1.0", invoice_corrections: 1,
        benchmark_examples: payload.add_to_benchmark ? 1 : 0,
        learning_examples: payload.approve_learning_example ? 1 : 0,
        learning_duplicates_avoided: 0, rule_proposals: 0, affected_rows: 1,
        requires_bulk_scope_confirmation: false, statements: [] };
    }
    else if (path.endsWith("/save-edits")) {
      saveBody = request.postDataJSON() as Record<string, unknown>;
      saved = true;
      row.Location = "B";
      row._meta.human_adjudication_badges = { Location: ["manually_corrected", "learning_approved"] };
      body = {
        batch_id: "human-ui", applied: 1, skipped: 0, current_revision_id: null,
        adjudication: {
          recorded: 1, applied: 1, unresolved: 0, revision_ids: ["har-test"],
          benchmark_submissions: 1, learning_approvals: 1, rule_proposals: 0,
        },
      };
    }
    else if (path.endsWith("/activity")) body = {
      contract_version: "operator-activity/1.0",
      items: saved ? [{
        contract_version: "operator-activity/1.0", event_id: "activity-human",
        batch_id: "human-ui", invoice_group_id: "human-group",
        event_type: "human_adjudication_saved", source: "manual",
        actor: "controller@example.invalid", summary: "Saved 1 evidence-backed human adjudication.",
        details: { revision_id: "har-test" }, created_at: "2026-07-18T12:01:00Z",
      }] : [],
    };
    else if (path.includes("/adjudications/evidence/")) {
      status = 404;
      body = { detail: "No evidence crop is available for this cell." };
    }
    else if (path === "/api/billing-v2/audit") body = { generated_at: "now", count: 0, available_count: 0, processors: [], ai_fallback_module: { module: "", available: false } };
    else if (path === "/api/processing/queue") body = { running: null, queued: [] };
    else if (path === "/api/ai/status") body = { enabled: false, configured: false, provider: null, model: null, supports_vision: false, vision_enabled: false };
    else if (path === "/api/health") body = { ok: true };
    await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
  });
  return { getSaveBody: () => saveBody };
}

test("table edit opens scoped adjudication panel and persists badges/history", async ({ page }) => {
  const mock = await mockInvoiceProcessor(page);
  await page.goto("/");
  const row = page.getByTestId("template-row");
  const headers = page.getByTestId("template-grid-card").locator("thead th");
  const locationHeader = headers.filter({ hasText: "Location" });
  const headerIndex = await locationHeader.evaluate((node) => Array.from(node.parentElement!.children).indexOf(node));
  const locationCell = row.locator("td").nth(headerIndex);
  await locationCell.dblclick();
  await locationCell.locator("input").fill("B");
  await locationCell.locator("input").press("Enter");
  await page.getByTestId("template-save-button").click();

  const panel = page.getByTestId("human-adjudication-panel");
  await expect(panel).toBeVisible();
  await expect(panel.getByText("Previous AI")).toBeVisible();
  await expect(panel.getByText("Human correction")).toBeVisible();
  await expect(panel.getByText("Invoice HUMAN-1", { exact: false })).toBeVisible();
  await expect(panel.getByText("row line-1", { exact: false })).toBeVisible();
  await expect(panel.getByTestId("human-adjudication-knowledge")).toContainText("Historical context: GL 6100");
  await expect(panel.getByTestId("human-adjudication-knowledge")).toContainText("Approved repair policy");
  await panel.getByTestId("human-adjudication-rationale").fill("The source document identifies location B.");
  await panel.getByTestId("human-adjudication-benchmark").check();
  await panel.getByTestId("human-adjudication-learning").check();
  await expect(panel.getByTestId("human-adjudication-impact")).toContainText("1 benchmark example");
  await panel.getByTestId("human-adjudication-confirm").click();
  await expect(panel).toBeHidden();

  await expect.poll(() => mock.getSaveBody()).not.toBeNull();
  expect(mock.getSaveBody()).toMatchObject({
    edits: { "0": { Location: "B" } },
    adjudication: {
      rationale: "The source document identifies location B.",
      add_to_benchmark: true,
      approve_learning_example: true,
      propose_reusable_rule: false,
    },
  });
  await expect(page.getByTitle("Manually corrected")).toBeVisible();
  await expect(page.getByTitle("Learning-approved")).toBeVisible();

  await page.getByTestId("template-revisions-btn").click();
  await expect(page.getByText("Saved 1 evidence-backed human adjudication.")).toBeVisible();
});
