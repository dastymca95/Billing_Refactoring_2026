import { expect, test, type Page } from "@playwright/test";


const datasets = ["vendors", "properties_units", "gl_accounts", "invoice_history", "general_ledger"];

async function mockContextHub(page: Page, counters: { published: number }) {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    let body: unknown = {};
    if (path === "/api/resman-context/status") {
      body = {
        tenant_id: "local-default",
        datasets: datasets.map((dataset) => ({
          contract_version: "resman-context-data/1.0",
          tenant_id: "local-default",
          dataset,
          current_snapshot: dataset === "vendors" ? {
            contract_version: "resman-context-data/1.0",
            snapshot_id: "rms-vendors",
            import_id: "rmi-vendors",
            tenant_id: "local-default",
            dataset,
            original_filename: "Vendor List.csv",
            sha256: "a".repeat(64),
            record_count: 1,
            created_at: "2026-07-15T12:00:00Z",
            activated_at: "2026-07-15T12:00:00Z",
            active: true,
          } : null,
          effective_record_count: dataset === "vendors" ? 1 : 0,
          manual_overlay_count: 0,
          staged_import_count: 0,
        })),
      };
    } else if (path.endsWith("/records") && request.method() === "GET") {
      const dataset = path.split("/")[3];
      body = {
        contract_version: "resman-context-data/1.0",
        tenant_id: "local-default",
        dataset,
        page: 1,
        page_size: 50,
        total: dataset === "vendors" || dataset === "general_ledger" || dataset === "invoice_history" ? 1 : 0,
        items: dataset === "vendors" ? [{
          company: "Example Vendor",
          abbreviation: "EXV",
          status: "Approved",
          default_gl: "6500",
          active: true,
          _record: { natural_key: "vendor:exv", source_kind: "resman_import", source_snapshot_id: "rms-vendors" },
        }] : dataset === "general_ledger" ? [{
          transaction_date: "2026-01-01",
          account_code: "6500",
          property_code: "PROP",
          counterparty_name: "EXV",
          resolved_vendor_name: "Example Vendor",
          vendor_resolution_status: "exact",
          invoice_history_reconciliation_status: "matched_to_invoice_history",
          debit: "25.00",
          credit: null,
          _record: { natural_key: "ledger:test:1", source_kind: "resman_import", source_snapshot_id: "rms-ledger" },
        }] : dataset === "invoice_history" ? [{
          invoice_date: "2026-01-01",
          vendor_name: "Example Vendor",
          invoice_number: "INV-1",
          property_code: "PROP",
          gl_code: "6500",
          allocation_amount: "25.00",
          ledger_reconciliation_status: "matched_to_ledger",
          _record: { natural_key: "invoice:test:1", source_kind: "resman_import", source_snapshot_id: "rms-invoice" },
        }] : [],
      };
    } else if (path.endsWith("/snapshots")) {
      body = { items: [] };
    } else if (path.endsWith("/imports/preview")) {
      body = {
        contract_version: "resman-context-data/1.0",
        import_id: "rmi-preview",
        tenant_id: "local-default",
        dataset: "vendors",
        original_filename: "Vendor List.csv",
        sha256: "b".repeat(64),
        size_bytes: 100,
        parsed_records: 12,
        added_records: 2,
        changed_records: 1,
        removed_records: 0,
        unchanged_records: 9,
        sample_records: [],
        issues: [{ code: "sensitive_columns_excluded", severity: "info", message: "Sensitive columns remain private." }],
        excluded_sensitive_columns: ["ACH Routing #", "ACH Account #"],
        status: "preview_ready",
        created_at: "2026-07-15T12:00:00Z",
      };
    } else if (path.endsWith("/publish")) {
      counters.published += 1;
      body = { snapshot_id: "rms-new" };
    } else if (path === "/api/batches") body = { batches: [] };
    else if (path === "/api/billing-v2/audit") body = { generated_at: "now", count: 0, available_count: 0, processors: [], ai_fallback_module: { module: "", available: false } };
    else if (path === "/api/processing/queue") body = { running: null, queued: [] };
    else if (path === "/api/ai/status") body = { enabled: false, configured: false };
    else if (path === "/api/health") body = { ok: true };
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
}


test("five ResMan context modules are available and backend records are rendered", async ({ page }) => {
  const counters = { published: 0 };
  await mockContextHub(page, counters);
  await page.goto("/");

  await page.getByRole("button", { name: "Vendors" }).click();
  await expect(page.getByTestId("resman-workspace-vendors")).toBeVisible();
  await expect(page.getByText("Example Vendor")).toBeVisible();
  await expect(page.getByText("Vendor List.csv", { exact: false })).toBeVisible();

  await page.getByRole("button", { name: "Properties & Units" }).click();
  await expect(page.getByTestId("resman-workspace-properties_units")).toBeVisible();
  await page.getByRole("button", { name: "Chart of Accounts" }).click();
  await expect(page.getByTestId("resman-workspace-gl_accounts")).toBeVisible();
  await page.getByRole("button", { name: "Invoice History" }).click();
  await expect(page.getByTestId("resman-workspace-invoice_history")).toBeVisible();
  await expect(page.getByText("Matched to ledger")).toBeVisible();
  await page.getByRole("button", { name: "General Ledger" }).click();
  await expect(page.getByTestId("resman-workspace-general_ledger")).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "Resolved Vendor" })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "Invoice match" })).toBeVisible();
  await expect(page.getByText("Example Vendor")).toBeVisible();
  await expect(page.getByText("Exact vendor master")).toBeVisible();
});


test("CSV upload requires preview before publishing a snapshot", async ({ page }) => {
  const counters = { published: 0 };
  await mockContextHub(page, counters);
  await page.goto("/");
  await page.getByRole("button", { name: "Vendors" }).click();

  await page.locator('input[type="file"]').setInputFiles({
    name: "Vendor List.csv",
    mimeType: "text/csv",
    buffer: Buffer.from("Company,Company Abbreviation\nExample Vendor,EXV\n"),
  });
  await expect(page.getByText("IMPORT PREVIEW · RAW PRESERVED")).toBeVisible();
  await expect(page.getByText("12 canonical records")).toBeVisible();
  expect(counters.published).toBe(0);
  await page.getByRole("button", { name: "Publish snapshot" }).click();
  await expect.poll(() => counters.published).toBe(1);
});
