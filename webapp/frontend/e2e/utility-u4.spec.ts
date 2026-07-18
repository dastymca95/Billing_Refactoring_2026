import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const ACTIVE_BATCH_KEY = "billing_refactoring_active_batch_id";
const API_BASE = process.env.PLAYWRIGHT_API_BASE_URL ?? "http://localhost:8001";
const DISCOVERY_MANIFEST_PATH = path.resolve(
  process.cwd(),
  "e2e/fixtures/legacy-u4/fixture_manifest.json",
);
const RUNTIME_MANIFEST_PATH = path.resolve(
  process.cwd(),
  process.env.INNER_VIEW_U4_RUNTIME_MANIFEST
    ?? "../../docs/reports/phases/screenshots/phase_u4_utility_e2e_qa/fixture_manifest.json",
);
const SCREENSHOT_DIR = path.resolve(
  process.cwd(),
  "../../docs/reports/phases/screenshots/phase_u4_utility_e2e_qa",
);

type DiscoveryCase = {
  key: string;
  label: string;
};

type RuntimeCaseBinding = {
  batch_id: string;
  row_count: number;
  invoice_count: number;
};

type ExecutableCase = DiscoveryCase & RuntimeCaseBinding & {
  screenshot_prefix: string;
};

function loadDiscoveryCases(): DiscoveryCase[] {
  if (!fs.existsSync(DISCOVERY_MANIFEST_PATH)) {
    throw new Error("Tracked legacy U4 discovery fixture is missing.");
  }
  let manifest: unknown;
  try {
    manifest = JSON.parse(fs.readFileSync(DISCOVERY_MANIFEST_PATH, "utf8"));
  } catch {
    throw new Error("Tracked legacy U4 discovery fixture is invalid JSON.");
  }
  if (!manifest || typeof manifest !== "object") {
    throw new Error("Tracked legacy U4 discovery fixture must be an object.");
  }
  const payload = manifest as { schema_version?: unknown; cases?: unknown };
  if (payload.schema_version !== "legacy-u4-discovery/1.0") {
    throw new Error("Tracked legacy U4 discovery fixture has an unsupported schema version.");
  }
  if (!Array.isArray(payload.cases) || payload.cases.length !== 10) {
    throw new Error("Tracked legacy U4 discovery fixture must contain exactly ten cases.");
  }
  const seen = new Set<string>();
  return payload.cases.map((value, index) => {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      throw new Error(`Tracked legacy U4 case ${index + 1} is invalid.`);
    }
    const item = value as Record<string, unknown>;
    const fields = Object.keys(item).sort();
    if (fields.join(",") !== "key,label") {
      throw new Error(`Tracked legacy U4 case ${index + 1} contains unsupported fields.`);
    }
    const key = typeof item.key === "string" ? item.key.trim() : "";
    const label = typeof item.label === "string" ? item.label.trim() : "";
    if (!/^[a-z0-9_]+$/.test(key) || !label || label.length > 80 || /[\\/]/.test(label)) {
      throw new Error(`Tracked legacy U4 case ${index + 1} contains invalid public metadata.`);
    }
    if (seen.has(key)) {
      throw new Error(`Tracked legacy U4 discovery fixture contains duplicate key ${key}.`);
    }
    seen.add(key);
    return { key, label };
  });
}

function loadRuntimeBinding(key: string): RuntimeCaseBinding | null {
  if (!fs.existsSync(RUNTIME_MANIFEST_PATH)) return null;
  let manifest: unknown;
  try {
    manifest = JSON.parse(fs.readFileSync(RUNTIME_MANIFEST_PATH, "utf8"));
  } catch {
    throw new Error("Private legacy U4 runtime manifest is invalid JSON.");
  }
  const cases = (
    manifest && typeof manifest === "object" && Array.isArray((manifest as { cases?: unknown }).cases)
      ? (manifest as { cases: unknown[] }).cases
      : []
  );
  const match = cases.find(
    (value) => value && typeof value === "object" && (value as { key?: unknown }).key === key,
  );
  if (!match || typeof match !== "object") return null;
  const item = match as Record<string, unknown>;
  const batchId = typeof item.batch_id === "string" ? item.batch_id.trim() : "";
  const rowCount = Number(item.row_count);
  const invoiceCount = Number(item.invoice_count);
  if (!batchId || !Number.isInteger(rowCount) || rowCount < 0 || !Number.isInteger(invoiceCount) || invoiceCount < 0) {
    throw new Error(`Private legacy U4 runtime binding for ${key} is invalid.`);
  }
  return { batch_id: batchId, row_count: rowCount, invoice_count: invoiceCount };
}

const u4Cases = loadDiscoveryCases();

function batchRow(page: Page, batchId: string) {
  return page.locator(
    `[data-testid="explorer-batch-drop-target"][data-batch-id="${batchId}"]`,
  );
}

async function openBatchSelector(page: Page) {
  const popover = page.getByTestId("batch-selector-popover");
  if ((await popover.count()) > 0 && (await popover.isVisible())) return;
  await page.getByTestId("template-batch-selector").click();
  await expect(page.getByTestId("batch-explorer")).toBeVisible();
}

async function expectPreviewReady(
  request: APIRequestContext,
  fixture: ExecutableCase,
) {
  const response = await request.get(`${API_BASE}/api/batches/${fixture.batch_id}/preview`);
  expect(response.ok()).toBeTruthy();
  const data = (await response.json()) as {
    row_count?: number;
    invoice_count?: number;
    summary?: { manual_review_total?: number };
  };
  expect(data.row_count ?? 0).toBeGreaterThanOrEqual(fixture.row_count);
  expect(data.invoice_count ?? 0).toBeGreaterThanOrEqual(fixture.invoice_count);
}

async function waitForDocumentPreview(page: Page) {
  await expect(page.getByTestId("document-preview-body")).toBeVisible();
  await Promise.race([
    page.getByTestId("pdf-page-shell").first().waitFor({
      state: "visible",
      timeout: 15_000,
    }),
    page.getByTestId("image-preview-workspace").waitFor({
      state: "visible",
      timeout: 15_000,
    }),
  ]);
}

async function dismissToasts(page: Page) {
  const dismiss = page.getByLabel("Dismiss");
  for (let i = 0; i < 8; i += 1) {
    if ((await dismiss.count()) === 0) break;
    await dismiss.first().click({ force: true, timeout: 1000 }).catch(() => undefined);
    await page.waitForTimeout(80);
  }
}

test.describe("U4 utility end-to-end browser QA", () => {
  for (const definition of u4Cases) {
    test(`bulk and single invoice render for ${definition.label}`, async ({ page, request }) => {
      const runtime = loadRuntimeBinding(definition.key);
      if (!runtime) {
        test.skip(true, "Run the private U4 fixture preparation before executing this historical visual test.");
        return;
      }
      const fixture: ExecutableCase = {
        ...definition,
        ...runtime,
        screenshot_prefix: definition.key,
      };
      await expectPreviewReady(request, fixture);
      await page.setViewportSize({ width: 1600, height: 900 });
      await page.addInitScript(
        ([key, value]) => window.localStorage.setItem(key, value),
        [ACTIVE_BATCH_KEY, fixture.batch_id],
      );

      await page.goto("/");
      await expect(page.getByTestId("template-batch-selector")).toBeVisible();
      await openBatchSelector(page);
      await expect(batchRow(page, fixture.batch_id)).toBeVisible({ timeout: 10_000 });
      await batchRow(page, fixture.batch_id).click();
      await waitForDocumentPreview(page);
      await dismissToasts(page);
      await expect(page.getByTestId("template-window-chrome")).toBeVisible();
      await expect(page.getByTestId("template-grid-card")).toBeVisible();

      await page.screenshot({
        path: path.join(SCREENSHOT_DIR, `${fixture.screenshot_prefix}_bulk.png`),
        fullPage: false,
      });

      if ((await page.getByTestId("template-row").count()) > 0) {
        await expect(page.getByTestId("template-row").first()).toBeVisible();
        await page.getByTestId("template-row").first().click();
        await page.getByTestId("template-mode-single").click();
        await expect(page.getByTestId("single-invoice-mode")).toBeVisible();
        await expect(page.getByTestId("single-invoice-line-items")).toBeVisible();
        await expect(page.getByTestId("single-ready-export")).toBeVisible();

        await page.screenshot({
          path: path.join(SCREENSHOT_DIR, `${fixture.screenshot_prefix}_single.png`),
          fullPage: false,
        });
      } else {
        await page.getByTestId("template-mode-single").click();
        await expect(page.getByText("No invoices are available for single invoice review.")).toBeVisible();
        await page.screenshot({
          path: path.join(SCREENSHOT_DIR, `${fixture.screenshot_prefix}_manual_review.png`),
          fullPage: false,
        });
      }
    });
  }
});
