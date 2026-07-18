import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const ACTIVE_BATCH_KEY = "billing_refactoring_active_batch_id";
const API_BASE = process.env.PLAYWRIGHT_API_BASE_URL ?? "http://localhost:8001";
const MANIFEST_PATH = path.resolve(
  process.cwd(),
  "../../docs/reports/phases/screenshots/phase_u4_utility_e2e_qa/fixture_manifest.json",
);
const SCREENSHOT_DIR = path.resolve(
  process.cwd(),
  "../../docs/reports/phases/screenshots/phase_u4_utility_e2e_qa",
);

type ManifestCase = {
  key: string;
  label: string;
  batch_id: string;
  expected_vendor_key: string;
  row_count: number;
  invoice_count: number;
  manual_review_count: number;
  manual_review_reasons?: string[];
  community_master?: boolean;
  screenshot_prefix: string;
  note?: string;
};

function loadManifestCases(): ManifestCase[] {
  if (!fs.existsSync(MANIFEST_PATH)) return [];
  const manifest = JSON.parse(fs.readFileSync(MANIFEST_PATH, "utf8")) as {
    cases?: ManifestCase[];
  };
  return manifest.cases ?? [];
}

const u4Cases = loadManifestCases();

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
  fixture: ManifestCase,
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
  test.skip(
    u4Cases.length === 0,
    "Run `python scripts\\smoke_utility_e2e_outputs.py --prepare-browser-fixtures` before U4 browser screenshots.",
  );

  for (const fixture of u4Cases) {
    test(`bulk and single invoice render for ${fixture.label}`, async ({ page, request }) => {
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
