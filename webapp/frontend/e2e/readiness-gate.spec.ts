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
  blockers: [{
    code: "gl_invalid", severity: "blocking", scope: "line_item",
    invoice_id: "UI-1", line_item_id: "0", field: "GL Account",
    message: "GL Account must be a valid chart account.", source: "accounting_readiness",
    evidence: [{ row_index: 0, field: "GL Account" }], resolution_required: true,
    resolved: false,
  }],
  validated_fields: { "GL Account": false, "Property Abbreviation": true, Amount: true },
});

async function mockBilling(
  page: Page,
  initial: Readiness,
  gl: string,
  onReadiness?: () => void,
  onRouteUpdate?: (body: Record<string, unknown>) => void,
) {
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
    else if (path.endsWith("/processing-routes")) {
      const update = route.request().method() === "PATCH"
        ? route.request().postDataJSON() as Record<string, unknown>
        : null;
      if (update) onRouteUpdate?.(update);
      const requestedMode = String(update?.mode || "auto_cost_safe");
      const inheritedFrom = update?.scope === "document" ? "document" : update?.scope === "batch" ? "batch" : "default";
      const aiAllowed = requestedMode === "ai_fallback_allowed";
      body = {
        contract_version: "processing-route-api/1.0",
        policy_version: update ? "prp_sha256_updated" : "prp_sha256_initial",
        batch: {
          resolution: {
            contract_version: "processing-route-policy/1.0",
            batch_id: "readiness-ui",
            requested_mode: update?.scope === "batch" ? requestedMode : "auto_cost_safe",
            inherited_from: update?.scope === "batch" ? "batch" : "default",
          },
        },
        documents: [{
          filename: "invoice.pdf",
          detection: { vendor_key: "fixture_vendor", confidence: 0.99, reason: "registered identity" },
          decision: {
            contract_version: "processing-route-decision/1.0",
            policy_contract_version: "processing-route-policy/1.0",
            batch_id: "readiness-ui",
            filename: "invoice.pdf",
            requested_mode: requestedMode,
            inherited_from: inheritedFrom,
            effective_route: "deterministic",
            deterministic_available: true,
            vendor_key: "fixture_vendor",
            processor_id: "fixture_vendor.process",
            ai_fallback_authorized: aiAllowed,
            reason_code: aiAllowed
              ? "deterministic_first_ai_fallback_authorized"
              : "cost_safe_deterministic_default",
          },
        }],
        pages: [],
        audit: [],
      };
    }
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
  await expect(page.getByRole("main", { name: "ResMan template grid" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Export", exact: true })).toBeEnabled();
});

test("missing GL disables Export and editing GL refreshes backend readiness", async ({ page }) => {
  let readinessCalls = 0;
  await mockBilling(page, blocked(), "", () => { readinessCalls += 1; });
  await page.goto("/");
  await expect(page.getByRole("main", { name: "ResMan template grid" })).toBeVisible();
  const exportButton = page.getByRole("button", { name: "Export", exact: true });
  await expect(exportButton).toBeDisabled();

  const row = page.getByTestId("template-row");
  const headers = page.getByTestId("template-grid-card").locator("thead th");
  const glHeader = headers.filter({ hasText: "GL Account" });
  const headerIndex = await glHeader.evaluate((node) => Array.from(node.parentElement!.children).indexOf(node));
  const glCell = row.locator("td").nth(headerIndex);
  const explanation = glCell.getByRole("button", { name: "Explain missing GL Account" });
  await expect(explanation).toBeVisible();
  await explanation.hover();
  const tooltip = page.getByRole("tooltip");
  await expect(tooltip.getByText("Required field missing", { exact: true })).toBeVisible();
  await expect(tooltip.getByText("GL Account must be a valid chart account.")).toBeVisible();
  await glCell.dblclick();
  await glCell.locator("input").fill("6100");
  await glCell.locator("input").press("Enter");

  await expect.poll(() => readinessCalls).toBeGreaterThan(0);
  await expect(exportButton).toBeEnabled();
});

test("routing control persists document and bulk AI authorization in backend", async ({ page }) => {
  const updates: Record<string, unknown>[] = [];
  await mockBilling(page, ready("routing"), "6100", undefined, (body) => updates.push(body));
  page.on("dialog", (dialog) => void dialog.accept());
  await page.goto("/");

  const control = page.getByTestId("processing-route-control");
  await expect(control.locator("summary")).toContainText("Deterministic locked");
  await control.locator("summary").click();
  await control.locator(".processing-route-options button", { hasText: "Allow AI fallback" }).click();
  await expect.poll(() => updates.length).toBe(1);
  expect(updates[0]).toMatchObject({
    scope: "document",
    filename: "invoice.pdf",
    mode: "ai_fallback_allowed",
    actor: "local_operator",
  });

  await control.locator("summary").click();
  await control.locator("summary").click();
  await control.locator(".processing-route-bulk-actions button", { hasText: "Deterministic only" }).click();
  await expect.poll(() => updates.length).toBe(2);
  expect(updates[1]).toMatchObject({
    scope: "batch",
    mode: "deterministic_only",
    reset_exceptions: true,
    actor: "local_operator",
  });
});
