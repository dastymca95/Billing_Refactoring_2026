import { expect, test } from "@playwright/test";


test("operator explicitly scans ResMan and opens compact vendor detail", async ({ page }) => {
  let generated = false;
  let governanceUpdates = 0;
  let patternUpdates = 0;
  let builderApprovals = 0;
  let builderSelectedColumn: string | null = null;
  const snapshot = {
    contract_version: "context-intelligence/1.1",
    analytics_version: "vendor-property-gl-matrix/1.0",
    snapshot_id: "cis-test",
    tenant_id: "local-default",
    generated_at: "2026-07-16T18:00:00Z",
    generated_by: "local_operator",
    source_hashes: {
      vendors: "a".repeat(64), properties_units: "b".repeat(64),
      gl_accounts: "c".repeat(64), general_ledger: "d".repeat(64),
      invoice_history: "e".repeat(64),
    },
    vendor_count: 1, property_count: 1, invoice_count: 12, allocation_count: 12,
    gl_account_count: 2, ledger_record_count: 12,
    deterministic_candidate_count: 1, review_candidate_count: 0,
  };
  const vendor = {
    vendor_key: "vendor:example", vendor_name: "Example Utility", vendor_abbreviation: "EU", active: true,
    invoice_count: 12, allocation_count: 12, active_months: 12, history_span_months: 12,
    ledger_posting_count: 12, ledger_total_amount: "1200.00",
    total_amount: "1200.00", average_invoice_amount: "100.00", top_gl_share: 1, top_property_share: 1,
    gl_usage: [{ key: "6100", label: "6100", count: 12, amount: "1200.00", share: 1 }],
    property_usage: [{ key: "ep", label: "EP", count: 12, amount: "1200.00", share: 1 }],
    property_gl_usage: {}, first_accounting_date: "2025-08-01", last_accounting_date: "2026-07-01",
    statistical_score: .98, recommended_mode: "deterministic_candidate",
    recommendation_reasons: ["12 historical invoices across 12 active months."],
    governance_status: "unreviewed", reviewer_notes: null,
    deterministic_coverage: {
      contract_version: "deterministic-coverage/1.0", vendor_key: "example_utility",
      display_name: "Example Utility", aliases: ["EU"], status: "active",
      implementation_kind: "hybrid", processor_module: "processors.example_utility",
      processor_entrypoint: "process_example_utility_batch", processor_available: true,
      config_present: true, config_name: "example_utility.yaml", editable: true,
      pattern_count: 1, patterns: [{ path: "pdf_extraction_rules.vendor_patterns", label: "Vendor Patterns", values: ["EXAMPLE UTILITY"], editable: true }],
    },
  };
  const patternGroups = [{
    key: "deterministic_patterns", label: "Deterministic matching patterns", fields: [{
      label: "Vendor Patterns", path: "pdf_extraction_rules.vendor_patterns", type: "string_list",
      editable: true, value: ["EXAMPLE UTILITY"],
    }],
  }];
  const builderBase = {
    contract_version: "deterministic-builder/1.0", session_id: "dbs_test", vendor_key: "example_utility",
    vendor_name: "Example Utility", status: "draft", revision: 0, selected_column: null,
    samples: [], messages: [{ message_id: "m0", role: "system", content: "Upload samples.", created_at: "2026-07-17T12:00:00Z", estimated_cost_usd: 0, proposed_paths: [] }],
    draft_patch: {}, draft_rationales: {}, validation_issues: [],
    preview: { status: "not_run", revision: 0, columns: [], rows: [], row_count: 0, warnings: [], generated_at: null },
    created_at: "2026-07-17T12:00:00Z", updated_at: "2026-07-17T12:00:00Z", audit: [],
  };

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    let body: unknown = {};
    if (path === "/api/context-intelligence/status") {
      body = {
        contract_version: "context-intelligence/1.1", tenant_id: "local-default",
        state: generated ? "ready" : "not_generated", required_datasets: Object.keys(snapshot.source_hashes),
        missing_datasets: [], current_source_hashes: snapshot.source_hashes,
        snapshot: generated ? snapshot : null,
      };
    } else if (path === "/api/context-intelligence/scan") {
      generated = true;
      body = { state: "ready", snapshot };
    } else if (path === "/api/context-intelligence/matrix") {
      body = { contract_version: "context-intelligence/1.1", snapshot_id: "cis-test", tenant_id: "local-default", page: 1, page_size: 50, total: 1, items: [vendor] };
    } else if (path.includes("/api/context-intelligence/vendors/") && path.endsWith("/governance")) {
      governanceUpdates += 1;
      body = { ...vendor, governance_status: "approved_candidate" };
    } else if (path === "/api/vendor-rules/example_utility/validate") {
      body = { vendor_key: "example_utility", ok: true, issues: [] };
    } else if (path === "/api/vendor-rules/example_utility" && request.method() === "PATCH") {
      patternUpdates += 1;
      body = { vendor_key: "example_utility", groups: patternGroups, result: { backup_filename: "example_utility_backup.yaml", written_paths: ["pdf_extraction_rules.vendor_patterns"] } };
    } else if (path === "/api/vendor-rules/example_utility") {
      body = { vendor_key: "example_utility", groups: patternGroups };
    } else if (path === "/api/deterministic-builder/sessions") {
      body = builderBase;
    } else if (path.endsWith("/samples")) {
      body = { ...builderBase, samples: [{ sample_id: "s1", original_filename: "sample.csv", source_type: "csv", size_bytes: 30, page_count: 1, sha256: "a".repeat(64), text_available: true, warnings: [], uploaded_at: "2026-07-17T12:01:00Z" }] };
    } else if (path.endsWith("/chat")) {
      const payload = request.postDataJSON() as { selected_column?: string | null };
      builderSelectedColumn = payload.selected_column || null;
      body = { ...builderBase, revision: 1, selected_column: builderSelectedColumn,
        samples: [{ sample_id: "s1", original_filename: "sample.csv", source_type: "csv", size_bytes: 30, page_count: 1, sha256: "a".repeat(64), text_available: true, warnings: [], uploaded_at: "2026-07-17T12:01:00Z" }],
        messages: [...builderBase.messages, { message_id: "m1", role: "assistant", content: "I proposed a validated detection pattern.", created_at: "2026-07-17T12:02:00Z", provider_profile_id: "test-accounting", estimated_cost_usd: .001, proposed_paths: ["vendor_identity.detection_keywords"] }],
        draft_patch: { "vendor_identity.detection_keywords": ["EXAMPLE UTILITY"] }, draft_rationales: { "vendor_identity.detection_keywords": "Observed in all samples." },
        preview: builderSelectedColumn ? { status: "passed", revision: 1, columns: ["Invoice Number", "Amount"], rows: [{ "Invoice Number": "100", Amount: "10.00" }], row_count: 1, warnings: [], generated_at: "2026-07-17T12:03:00Z" } : builderBase.preview,
      };
    } else if (path.endsWith("/preview")) {
      body = { ...builderBase, status: "previewed", revision: 1,
        samples: [{ sample_id: "s1", original_filename: "sample.csv", source_type: "csv", size_bytes: 30, page_count: 1, sha256: "a".repeat(64), text_available: true, warnings: [], uploaded_at: "2026-07-17T12:01:00Z" }],
        draft_patch: { "vendor_identity.detection_keywords": ["EXAMPLE UTILITY"] }, draft_rationales: { "vendor_identity.detection_keywords": "Observed in all samples." },
        preview: { status: "passed", revision: 1, columns: ["Invoice Number", "Amount"], rows: [{ "Invoice Number": "100", Amount: "10.00" }], row_count: 1, warnings: [], generated_at: "2026-07-17T12:03:00Z" },
      };
    } else if (path.endsWith("/approve")) {
      builderApprovals += 1;
      body = { ...builderBase, status: "approved", revision: 1,
        preview: { status: "passed", revision: 1, columns: ["Invoice Number", "Amount"], rows: [{ "Invoice Number": "100", Amount: "10.00" }], row_count: 1, warnings: [], generated_at: "2026-07-17T12:03:00Z" } };
    } else if (path === "/api/batches") body = { batches: [] };
    else if (path === "/api/billing-v2/audit") body = { generated_at: "now", count: 0, available_count: 0, processors: [], ai_fallback_module: { module: "", available: false } };
    else if (path === "/api/processing/queue") body = { running: null, queued: [] };
    else if (path === "/api/ai/status") body = { enabled: false, configured: false };
    else if (path === "/api/health") body = { ok: true };
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });

  await page.goto("/");
  await page.getByRole("button", { name: "Context Matrix" }).click();
  await expect(page.getByText("Context has not been scanned")).toBeVisible();
  await expect(page.getByText("Example Utility")).toHaveCount(0);

  await page.getByRole("button", { name: "Scan ResMan", exact: true }).click();
  await expect(page.getByText("Example Utility")).toBeVisible();
  await expect(page.getByText("Deterministic candidate", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Deterministic parser active")).toBeVisible();

  await page.getByText("Example Utility").dblclick();
  await expect(page.getByRole("dialog", { name: "Example Utility context detail" })).toBeVisible();
  await expect(page.getByText("process_example_utility_batch")).toBeVisible();
  await page.getByLabel("Vendor Patterns").fill("EXAMPLE UTILITY\nEXAMPLE POWER");
  await page.getByRole("button", { name: "Validate & save patterns" }).click();
  await expect.poll(() => patternUpdates).toBe(1);
  await page.getByLabel("Decision").selectOption("approved_candidate");
  await page.getByRole("button", { name: "Save review" }).click();
  await expect.poll(() => governanceUpdates).toBe(1);

  await page.getByRole("button", { name: "Open builder" }).click();
  await page.locator('input[type="file"]').setInputFiles({ name: "sample.csv", mimeType: "text/csv", buffer: Buffer.from("invoice,total\n100,10.00") });
  await expect(page.getByText("sample.csv")).toBeVisible();
  await page.getByPlaceholder("Describe what the deterministic processor should recognize or change…").fill("Learn the vendor detection pattern from this sample.");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("I proposed a validated detection pattern.")).toBeVisible();
  await page.getByRole("button", { name: "Preview against samples" }).click();
  await expect(page.getByText("Dry-run row preview")).toBeVisible();
  await page.getByRole("button", { name: "Invoice Number" }).click();
  await page.getByPlaceholder("Tell the AI what should change in Invoice Number…").fill("Keep the invoice number format consistent.");
  await page.getByRole("button", { name: "Send" }).click();
  await expect.poll(() => builderSelectedColumn).toBe("Invoice Number");
  await page.getByText("I reviewed the current revision and its sample preview.").click();
  await page.getByRole("button", { name: "Approve this revision" }).click();
  await expect.poll(() => builderApprovals).toBe(1);
});


test("backend error never renders a zero-value matrix as a successful scan", async ({ page }) => {
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/context-intelligence/status") {
      await route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ detail: "Not Found" }) });
      return;
    }
    const body = path === "/api/batches" ? { batches: [] }
      : path === "/api/processing/queue" ? { running: null, queued: [] }
      : path === "/api/ai/status" ? { enabled: false, configured: false }
      : {};
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });

  await page.goto("/");
  await page.getByRole("button", { name: "Context Matrix" }).click();
  await expect(page.getByTestId("context-unavailable-state")).toBeVisible();
  await expect(page.getByText("No profiles match this view.")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Scan ResMan again" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Retry connection" }).first()).toBeVisible();
});
