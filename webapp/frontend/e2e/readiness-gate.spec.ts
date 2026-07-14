import { expect, test, type Page } from "@playwright/test";

type Readiness = {
  contract_version: string;
  snapshot_id: string;
  status: "ready" | "needs_review" | "blocked";
  export_allowed: boolean;
  blockers: unknown[];
  non_blocking_issues: unknown[];
  validated_fields: Record<string, boolean>;
  reconciliation_status: string;
  duplicate_status: string;
  evaluated_at: string;
};

const ready = (snapshot: string, status: Readiness["status"] = "ready"): Readiness => ({
  contract_version: "accounting-readiness/1.0",
  snapshot_id: snapshot,
  status,
  export_allowed: true,
  blockers: [],
  non_blocking_issues: status === "needs_review" ? [{ code: "extraction_warning" }] : [],
  validated_fields: { "GL Account": true, "Property Abbreviation": true, Amount: true },
  reconciliation_status: "passed",
  duplicate_status: "not_detected",
  evaluated_at: "2026-07-14T12:00:00Z",
});

const blocked = (): Readiness => ({
  ...ready("missing-gl"),
  status: "blocked",
  export_allowed: false,
  blockers: [{ code: "gl_invalid", field: "GL Account" }],
  validated_fields: { "GL Account": false, "Property Abbreviation": true, Amount: true },
});

async function mockBilling(page: Page, initial: Readiness, gl: string, onReadiness?: () => void) {
  const columns = ["Invoice Number", "Vendor", "Property Abbreviation", "GL Account", "Amount"];
  const row = {
    "Invoice Number": "UI-1",
    Vendor: "Fixture Vendor",
    "Property Abbreviation": "PROP",
    "GL Account": gl,
    Amount: 25,
    _meta: {
      invoice_group_id: "invoice-1", readiness_status: initial.status, ai_warnings: ["OCR contrast warning"],
      accounting_decision: gl ? {
        selected_gl_code: gl, selected_gl_name: "Repairs", decision_source: "AccountingDecisionEngine",
        why_selected: "Backend exact explanation", confidence: 0.88, review_required: false, review_blocking: false,
        decision_version: "accounting-decision/1.0", semantic_version: "semantic-classification/1.0", catalog_version: "gl-catalog/1.0",
        evidence: [{ text: "raw repair service" }],
        candidates_ranked: [{ gl_code: gl, gl_name: "Repairs", score_components: { semantic_compatibility: 0.35 } }],
        rejected_alternatives: [{ gl_code: "6500", gl_name: "Contract Services", reason: "Backend rejected reason" }],
      } : null,
    },
  };
  const preview = {
    batch_id: "readiness-ui",
    summary: {}, by_vendor_summaries: {}, columns,
    required_columns: columns, recommended_columns: [], optional_columns: [],
    optional_columns_collapsible: true, optional_columns_hidden_by_default: true,
    rows: [row], invoice_count: 1, row_count: 1, unsupported_files: [],
    accounting_readiness: initial, invoice_readiness: { "invoice-1": initial },
  };
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    let body: unknown;
    if (path === "/api/billing-v2/audit") body = { generated_at: "now", count: 0, available_count: 0, processors: [], ai_fallback_module: { module: "", available: false } };
    else if (path === "/api/processing/queue") body = { running: null, queued: [] };
    else if (path === "/api/ai/status") body = { enabled: false, configured: false, provider: null, model: null, supports_vision: false, vision_enabled: false };
    else if (path === "/api/health") body = { status: "ok" };
    else if (path === "/api/batches") body = { batches: [{ batch_id: "readiness-ui", batch_name: "Readiness UI", status: "completed", files_count: 1, invoices_count: 1, rows_count: 1, manual_review_count: 0, export_available: false, created_at: "now" }] };
    else if (path === "/api/batches/readiness-ui") body = { batch_id: "readiness-ui", batch_name: "Readiness UI", created_at: "now", files: [{ filename: "invoice.pdf", size_bytes: 10, extension: ".pdf" }], files_total: 1, preview_available: true, export_available: false, export_filenames: [], summary: {} };
    else if (path.endsWith("/progress")) body = { batch_id: "readiness-ui", status: "completed", percent: 100, current_step: "Completed", message: "" };
    else if (path.endsWith("/preview")) body = preview;
    else if (path.endsWith("/manual-review")) body = { batch_id: "readiness-ui", items: [] };
    else if (path.endsWith("/revisions")) body = { batch_id: "readiness-ui", current_revision_id: null, revisions: [] };
    else if (path.endsWith("/readiness")) {
      const requestBody = route.request().postDataJSON() as { rows?: Record<string, unknown>[] } | null;
      const editedGl = String(requestBody?.rows?.[0]?.["GL Account"] ?? "").trim();
      if (editedGl) onReadiness?.();
      body = editedGl ? ready("edited-gl") : initial;
    }
    else body = {};
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
}

test("valid batch and OCR-only warning keep Export enabled", async ({ page }) => {
  await mockBilling(page, ready("ocr-warning", "needs_review"), "6100");
  await page.goto("/");
  await page.getByTestId("explorer-batch-row").click();
  await expect(page.getByTestId("panel-template")).toBeVisible();
  await expect(page.getByTestId("template-export-button")).toBeEnabled();
});

test("missing GL disables Export and editing GL refreshes backend readiness", async ({ page }) => {
  let readinessCalls = 0;
  await mockBilling(page, blocked(), "", () => { readinessCalls += 1; });
  await page.goto("/");
  await page.getByTestId("explorer-batch-row").click();
  const exportButton = page.getByTestId("template-export-button");
  await expect(exportButton).toBeDisabled();

  const row = page.getByTestId("template-row");
  const headers = page.getByTestId("template-grid-card").locator("thead th");
  const glHeader = headers.filter({ hasText: "GL Account" });
  const headerIndex = await glHeader.evaluate((node) => Array.from(node.parentElement!.children).indexOf(node));
  const glCell = row.locator("td").nth(headerIndex);
  await glCell.dblclick();
  await glCell.locator("input").fill("6100");
  await glCell.locator("input").press("Enter");

  await expect.poll(() => readinessCalls).toBeGreaterThan(0);
  await expect(exportButton).toBeEnabled();
});
