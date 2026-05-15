import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

const ACTIVE_BATCH_KEY = "billing_refactoring_active_batch_id";
const API_BASE = process.env.PLAYWRIGHT_API_BASE_URL ?? "http://localhost:8001";
const SCREENSHOT_DIR =
  "../../docs/reports/phases/screenshots/phase_2f_desktop_workspace_shell/e2e";

type BatchListEntry = {
  batch_id: string;
  batch_name: string;
  files_count: number;
  invoices_count: number;
  rows_count: number;
  manual_review_count: number;
};

type FileEntry = {
  filename: string;
  extension: string;
  page_count?: number | null;
};

async function listBatches(request: APIRequestContext): Promise<BatchListEntry[]> {
  const response = await request.get(`${API_BASE}/api/batches`);
  expect(response.ok()).toBeTruthy();
  const data = (await response.json()) as { batches: BatchListEntry[] };
  return data.batches;
}

function stableUiBatches(batches: BatchListEntry[]): BatchListEntry[] {
  const stable = batches.filter(
    (b) =>
      !/^QA AI/i.test(b.batch_name) &&
      !/^QA AI mapping\b/i.test(b.batch_name),
  );
  return stable.length > 0 ? stable : batches;
}

async function pickPreviewBatch(
  request: APIRequestContext,
): Promise<BatchListEntry | null> {
  const batches = stableUiBatches(await listBatches(request));
  return (
    batches.find((b) => b.batch_name === "QA Visual Fixture" && b.rows_count > 0) ??
    batches.find((b) => b.batch_name === "HWEA" && b.rows_count > 0) ??
    batches.find((b) => b.rows_count > 0) ??
    null
  );
}

async function pickFileBatch(
  request: APIRequestContext,
): Promise<BatchListEntry | null> {
  const batches = stableUiBatches(await listBatches(request));
  return (
    batches.find((b) => b.batch_name === "HWEA" && b.files_count > 0) ??
    batches.find((b) => /HWEA|Richmond Utilities|Alabama Power/i.test(b.batch_name) && b.files_count > 0) ??
    batches.find((b) => b.files_count > 0) ??
    null
  );
}

async function pickPdfPreviewBatch(
  request: APIRequestContext,
): Promise<{ batch: BatchListEntry; file: FileEntry } | null> {
  const batches = stableUiBatches(await listBatches(request)).filter(
    (b) => b.files_count > 0 && b.rows_count > 0,
  );
  for (const batch of batches) {
    const response = await request.get(`${API_BASE}/api/batches/${batch.batch_id}/files`);
    if (!response.ok()) continue;
    const data = (await response.json()) as { files: FileEntry[] };
    const file =
      data.files.find(
        (f) =>
          f.extension === ".pdf" &&
          typeof f.page_count === "number" &&
          f.page_count > 1,
      ) ??
      data.files.find((f) => f.extension === ".pdf" && (f.page_count ?? 1) >= 1);
    if (file) return { batch, file };
  }
  return null;
}

async function pickMultiPagePdfBatch(
  request: APIRequestContext,
): Promise<{ batch: BatchListEntry; file: FileEntry } | null> {
  const batches = stableUiBatches(await listBatches(request)).filter(
    (b) => b.files_count > 0,
  );
  for (const batch of batches) {
    const response = await request.get(`${API_BASE}/api/batches/${batch.batch_id}/files`);
    if (!response.ok()) continue;
    const data = (await response.json()) as { files: FileEntry[] };
    const file = data.files.find(
      (f) =>
        f.extension === ".pdf" &&
        typeof f.page_count === "number" &&
        f.page_count > 1,
    );
    if (file) return { batch, file };
  }
  return null;
}

function batchRow(page: Page, batchId: string) {
  return page.locator(
    `[data-testid="explorer-batch-drop-target"][data-batch-id="${batchId}"]`,
  );
}

async function expandBatch(page: Page, batchId: string) {
  const row = batchRow(page, batchId);
  const toggle = row.getByTestId("explorer-batch-toggle");
  await expect(toggle).toBeVisible();
  if ((await toggle.getAttribute("aria-expanded")) !== "true") {
    await toggle.click();
  }
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
}

async function loadBatch(
  page: Page,
  request: APIRequestContext,
  batch: BatchListEntry,
  viewport?: { width: number; height: number },
) {
  if (viewport) await page.setViewportSize(viewport);
  await page.addInitScript(
    ([key, value]) => window.localStorage.setItem(key, value),
    [ACTIVE_BATCH_KEY, batch.batch_id],
  );
  const batchesResponse = page.waitForResponse(
    (res) =>
      res.url().includes("/api/batches") &&
      res.request().method() === "GET" &&
      res.ok(),
  );
  await page.goto("/");
  await batchesResponse.catch(() => undefined);
  await expect(page.getByTestId("batch-explorer")).toBeVisible();
  await expect(batchRow(page, batch.batch_id)).toBeVisible();
}

async function loadPreviewBatch(
  page: Page,
  request: APIRequestContext,
  viewport?: { width: number; height: number },
) {
  const batch = await pickPreviewBatch(request);
  test.skip(!batch, "No processed batch with preview rows is available.");
  await loadBatch(page, request, batch!, viewport);
  await expect(page.getByTestId("template-window-chrome")).toBeVisible();
  await expect(page.getByTestId("template-grid-card")).toBeVisible();
  return batch!;
}

async function expectTemplateHeaderHealthy(page: Page) {
  const header = page.getByTestId("template-window-chrome");
  const exportButton = page.getByTestId("template-export-button");
  const controls = page.getByTestId("template-controls");
  const gridScroll = page.getByTestId("template-grid-scroll");
  const revisions = page.getByTestId("template-revisions-btn");

  await expect(header).toBeVisible();
  await expect(exportButton).toBeVisible();
  await expect(controls).toBeVisible();
  await expect(gridScroll).toBeVisible();
  await expect(revisions).toBeVisible();
  await expect(revisions).not.toContainText("No runs");

  const headerHasHorizontalOverflow = await header.evaluate(
    (el) => el.scrollWidth > el.clientWidth + 2,
  );
  expect(headerHasHorizontalOverflow).toBe(false);

  const exportBox = await exportButton.boundingBox();
  const headerBox = await header.boundingBox();
  expect(exportBox?.width ?? 0).toBeGreaterThan(60);
  expect(exportBox?.height ?? 0).toBeGreaterThan(20);
  expect(headerBox?.height ?? 0).toBeGreaterThan(24);
}

async function expectDesktopPanelChromeAligned(page: Page) {
  const metrics = await page.evaluate(() => {
    const batches = document.querySelector(".file-sidebar-card")?.getBoundingClientRect();
    const batchHeader = document
      .querySelector(".file-sidebar-header")
      ?.getBoundingClientRect();
    const documentHeader = document
      .querySelector(".doc-preview-header")
      ?.getBoundingClientRect();
    const templateChrome = document
      .querySelector('[data-testid="template-window-chrome"]')
      ?.getBoundingClientRect();
    const template = document
      .querySelector('[data-testid="template-workspace"]')
      ?.getBoundingClientRect();
    if (!batches || !template) return null;
    return {
      topDelta: Math.abs(batches.top - template.top),
      bottomDelta: Math.abs(batches.bottom - template.bottom),
      batchHeaderHeight: batchHeader?.height ?? 0,
      documentHeaderHeight: documentHeader?.height ?? 0,
      templateChromeHeight: templateChrome?.height ?? 0,
    };
  });
  expect(metrics).not.toBeNull();
  expect(metrics!.topDelta).toBeLessThanOrEqual(1);
  expect(metrics!.bottomDelta).toBeLessThanOrEqual(1);
  expect(metrics!.batchHeaderHeight).toBeLessThanOrEqual(32);
  expect(metrics!.documentHeaderHeight).toBeLessThanOrEqual(32);
  expect(metrics!.templateChromeHeight).toBeLessThanOrEqual(32);
}

async function dismissToasts(page: Page) {
  const dismiss = page.getByLabel("Dismiss");
  while ((await dismiss.count()) > 0) {
    await dismiss.first().click();
  }
}

for (const viewport of [
  { width: 1920, height: 1080 },
  { width: 1600, height: 900 },
  { width: 1366, height: 768 },
]) {
  test(`template header is not clipped at ${viewport.width}x${viewport.height}`, async ({
    page,
    request,
  }) => {
    await loadPreviewBatch(page, request, viewport);
    await expectTemplateHeaderHealthy(page);
    await expectDesktopPanelChromeAligned(page);
    await dismissToasts(page);
    await page.screenshot({
      path: `${SCREENSHOT_DIR}/viewport_${viewport.width}x${viewport.height}.png`,
      fullPage: false,
    });
  });
}

test("batch explorer renders and disabled nav items are hidden", async ({
  page,
  request,
}) => {
  const batch = await pickFileBatch(request);
  test.skip(!batch, "No batch with files is available.");
  await loadBatch(page, request, batch!, { width: 1366, height: 768 });
  await expect(page.getByTestId("batch-explorer")).toBeVisible();
  const nav = page.getByTestId("nav-rail");
  await expect(nav.getByText("Batches", { exact: true })).toBeVisible();
  await expect(nav.getByText("Review", { exact: true })).toHaveCount(0);
  await expect(nav.getByText("Vendors", { exact: true })).toHaveCount(0);
  await expect(nav.getByText("Exports", { exact: true })).toHaveCount(0);
  await expect(nav.getByText("Settings", { exact: true })).toHaveCount(0);
});

test("AI mock provider status is visible without external calls", async ({ page }) => {
  await page.route("**/api/ai/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        enabled: true,
        provider: "mock",
        model: "mock-invoice-v1",
        configured: true,
        supports_vision: false,
        vision_enabled: false,
        vision_model: null,
        vision_mode: "fallback_only",
        message: "AI invoice processing is configured with the mock provider.",
        reason: "AI invoice processing is configured with the mock provider.",
        policy: "invoice_extraction_candidates",
        allowed_tasks: ["variable vendor invoice extraction"],
      }),
    });
  });
  await page.goto("/");
  await expect(page.getByTestId("ai-status-pill")).toHaveText("AI: Mock");
});

test("AI assisted processing shows the in-document scan overlay", async ({
  page,
  request,
}) => {
  const batch = await pickFileBatch(request);
  test.skip(!batch, "No batch with files is available.");
  const filesResponse = await request.get(`${API_BASE}/api/batches/${batch!.batch_id}/files`);
  expect(filesResponse.ok()).toBeTruthy();
  const filesData = (await filesResponse.json()) as { files: FileEntry[] };
  const file = filesData.files.find((f) => f.extension === ".pdf") ?? filesData.files[0];
  test.skip(!file, "Selected batch has no files.");

  await page.route(`**/api/batches/${batch!.batch_id}/process**`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ status: "started", batch_id: batch!.batch_id }),
    });
  });
  await page.route(`**/api/batches/${batch!.batch_id}/progress`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        batch_id: batch!.batch_id,
        status: "processing",
        percent: 42,
        current_step: "Reading line items",
        current_file: file.filename,
        processing_mode: "ai_assisted",
        ai_stage: "Reading line items",
        ai_enabled: true,
      }),
    });
  });

  await loadBatch(page, request, batch!, { width: 1366, height: 768 });
  await expandBatch(page, batch!.batch_id);
  await batchRow(page, batch!.batch_id).getByTestId("explorer-batch-process").click();
  const overlay = page.getByTestId("ai-scan-overlay");
  await expect(overlay).toBeVisible();
  await expect(overlay.getByText("Reading line items")).toBeVisible();
  await expect(overlay.getByText(file.filename)).toBeVisible();
});

test("panel minimize uses the bottom dock and restores cleanly", async ({
  page,
  request,
}) => {
  await loadPreviewBatch(page, request, { width: 1366, height: 768 });
  await expect(page.getByTestId("batches-minimize")).toHaveCount(0);
  await expect(page.getByTestId("batches-maximize")).toHaveCount(0);
  await expect(page.getByTestId("batches-close")).toHaveCount(0);
  await page.getByTestId("windows-menu-toggle").click();
  await page.getByTestId("windows-menu-minimize-all").click();
  await expect(page.getByTestId("panel-batches")).toHaveCount(0);
  await expect(page.getByTestId("workspace-dock")).toBeVisible();
  await expect(page.getByTestId("workspace-dock-batches")).toBeVisible();
  const dockMetrics = await page.getByTestId("workspace-dock").evaluate((el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    const item = el.querySelector(".workspace-dock-item");
    const itemStyle = item ? window.getComputedStyle(item) : null;
    const itemRect = item?.getBoundingClientRect();
    return {
      bottomGap: window.innerHeight - rect.bottom,
      topRadius: Number.parseFloat(style.borderTopLeftRadius || "0"),
      bottomRadius: Number.parseFloat(style.borderBottomLeftRadius || "0"),
      itemHeight: itemRect?.height ?? 0,
      itemRadius: itemStyle
        ? Number.parseFloat(itemStyle.borderTopLeftRadius || "0")
        : 0,
    };
  });
  expect(dockMetrics.bottomGap).toBeLessThanOrEqual(1);
  expect(dockMetrics.topRadius).toBeLessThanOrEqual(8);
  expect(dockMetrics.bottomRadius).toBe(0);
  expect(dockMetrics.itemHeight).toBeLessThanOrEqual(30);
  expect(dockMetrics.itemRadius).toBeLessThanOrEqual(5);
  await page.getByTestId("workspace-dock-batches").click();
  await expect(page.getByTestId("panel-batches")).toBeVisible();
});

test("Windows menu closes and restores panels", async ({ page, request }) => {
  await loadPreviewBatch(page, request, { width: 1366, height: 768 });
  await page.getByTestId("windows-menu-toggle").click();
  await expect(page.getByTestId("windows-menu-popover")).toBeVisible();
  await page.getByTestId("windows-menu-template").click();
  await expect(page.getByTestId("panel-template")).toHaveCount(0);

  await page.getByTestId("windows-menu-toggle").click();
  await page.getByTestId("windows-menu-template").click();
  await expect(page.getByTestId("panel-template")).toBeVisible();
});

test("template exposes separate-window detach control", async ({
  page,
  request,
}) => {
  await loadPreviewBatch(page, request, { width: 1366, height: 768 });

  await expect(page.getByTestId("template-detach")).toBeVisible();
  await expect(page.getByTestId("template-detach")).toHaveAttribute(
    "aria-label",
    "Detach Template to separate window",
  );
});

test("expanded batch shows files or a clear empty state without endless skeletons", async ({
  page,
  request,
}) => {
  const batch = await pickFileBatch(request);
  test.skip(!batch, "No batch with files is available.");
  await loadBatch(page, request, batch!, { width: 1366, height: 768 });

  const target = batchRow(page, batch!.batch_id);
  await expect(target).toContainText(batch!.batch_name || "Untitled batch");
  await expect(target.getByTestId("explorer-file-row").first()).toBeVisible();
  await expect(target.locator(".file-row-skeleton")).toHaveCount(0);
  const processButton = target.getByTestId("explorer-batch-process");
  await expect(processButton).toBeVisible();
  await expect(processButton).toHaveAttribute("title", "Process batch");
  const processBox = await processButton.boundingBox();
  expect(processBox?.width ?? 999).toBeLessThan(44);
});

test("batch row click switches the active batch", async ({ page, request }) => {
  const activeBatch = await pickFileBatch(request);
  test.skip(!activeBatch, "No file batch is available.");
  await loadBatch(page, request, activeBatch!, { width: 1366, height: 768 });

  const targetBatch = (await listBatches(request)).find(
    (b) =>
      b.batch_id !== activeBatch!.batch_id &&
      !/^QA AI\b/i.test(b.batch_name) &&
      !/^QA AI mapping\b/i.test(b.batch_name),
  );
  test.skip(!targetBatch, "Only one batch is available.");

  const target = batchRow(page, targetBatch!.batch_id);
  await target.getByTestId("explorer-batch-row").click();
  await expect(target).toHaveClass(/active/);
});

test("inline new batch row opens and validates long names", async ({ page, request }) => {
  const batch = await pickFileBatch(request);
  test.skip(!batch, "No batch is available.");
  await loadBatch(page, request, batch!, { width: 1366, height: 768 });
  await page.getByTestId("explorer-add-batch").click();
  await expect(page.getByTestId("inline-new-batch-panel")).toBeVisible();
  await expect(page.getByTestId("new-batch-modal")).toHaveCount(0);
  await page.getByTestId("inline-new-batch-name-input").fill("A".repeat(85));
  await page.getByTestId("inline-create-batch-submit").click();
  await expect(page.getByText("Batch name is too long")).toBeVisible();
  await page.keyboard.press("Escape");
});

test("inline batch rename opens and cancels cleanly", async ({ page, request }) => {
  const batch = await pickFileBatch(request);
  test.skip(!batch, "No batch is available.");
  await loadBatch(page, request, batch!, { width: 1366, height: 768 });
  const target = batchRow(page, batch!.batch_id);
  await target.locator(".batch-row-name").dblclick();
  await expect(target.getByTestId("explorer-batch-rename-input")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(target.getByTestId("explorer-batch-rename-input")).toHaveCount(0);
});

test("file delete opens app-native confirm and can be cancelled", async ({
  page,
  request,
}) => {
  const batch = await pickFileBatch(request);
  test.skip(!batch, "No batch with files is available.");
  await loadBatch(page, request, batch!, { width: 1366, height: 768 });
  await expandBatch(page, batch!.batch_id);
  await page.getByTestId("explorer-file-menu").first().click();
  await page.getByTestId("explorer-file-delete").first().click();
  await expect(page.getByTestId("confirm-dialog")).toBeVisible();
  await expect(page.getByText("Delete file?")).toBeVisible();
  await page.getByTestId("confirm-cancel").click();
  await expect(page.getByTestId("confirm-dialog")).toHaveCount(0);
});

test("file delete hover stays visually neutral", async ({ page, request }) => {
  const batch = await pickFileBatch(request);
  test.skip(!batch, "No batch with files is available.");
  await loadBatch(page, request, batch!, { width: 1366, height: 768 });
  await expandBatch(page, batch!.batch_id);
  await page.getByTestId("explorer-file-menu").first().click();
  const deleteButton = page.getByTestId("explorer-file-delete").first();
  await deleteButton.hover();
  const styles = await deleteButton.evaluate((el) => {
    const cs = getComputedStyle(el);
    return {
      backgroundColor: cs.backgroundColor,
      borderColor: cs.borderColor,
    };
  });
  expect(styles.backgroundColor).not.toBe("rgb(254, 226, 226)");
  expect(styles.borderColor).not.toBe("rgb(254, 202, 202)");
});

test("drag and drop onto a batch row uploads into that batch", async ({
  page,
  request,
}) => {
  const create = await request.post(`${API_BASE}/api/batches`, {
    data: {
      batch_name: `QA Drop Target ${Date.now()}`,
      document_mode: "auto_detect",
    },
  });
  expect(create.ok()).toBeTruthy();
  const created = (await create.json()) as { batch_id: string; batch_name: string };

  try {
    await page.setViewportSize({ width: 1366, height: 768 });
    await page.goto("/");
    await expect(page.getByTestId("batch-explorer")).toBeVisible();
    await page.reload();

    const row = batchRow(page, created.batch_id);
    await expect(row).toBeVisible();
    const dataTransfer = await page.evaluateHandle(() => {
      const dt = new DataTransfer();
      dt.items.add(new File(["phase 1w"], "phase1w_drop_test.txt", { type: "text/plain" }));
      return dt;
    });
    await row.dispatchEvent("dragenter", { dataTransfer });
    await expect(row).toHaveClass(/drag-over/);
    await row.dispatchEvent("drop", { dataTransfer });
    await expect(row).not.toHaveClass(/drag-over/);
    await expect(row).toContainText("phase1w_drop_test.txt");
  } finally {
    await request.delete(`${API_BASE}/api/batches/${created.batch_id}`);
  }
});

test("column view buttons are visible without duplicate optional controls", async ({
  page,
  request,
}) => {
  await loadPreviewBatch(page, request, { width: 1366, height: 768 });
  const tabs = page.getByTestId("column-view-tabs");
  await expect(tabs.getByRole("tab", { name: "Required" })).toBeVisible();
  await expect(tabs.getByRole("tab", { name: "Issues" })).toHaveCount(0);
  await expect(tabs.getByRole("tab", { name: "All" })).toBeVisible();
  await expect(page.getByText("Show optional cols")).toHaveCount(0);
});

test("single invoice mode renders and edits update the bulk grid", async ({
  page,
  request,
}) => {
  await loadPreviewBatch(page, request, { width: 1366, height: 768 });
  await page.getByTestId("template-row").first().click();
  await page.getByTestId("template-mode-single").click();
  await expect(page.getByTestId("single-invoice-mode")).toBeVisible();
  await expect(page.getByTestId("single-invoice-status")).toBeVisible();
  await expect(page.getByTestId("single-invoice-totals")).toBeVisible();
  await expect(page.getByTestId("single-review-tasks")).toBeVisible();
  await expect(page.getByTestId("single-use-vision-assist")).toBeVisible();
  await expect(page.getByTestId("single-ready-export")).toHaveAttribute("title", /Blocked by|Invoice ready/);
  await expect(page.getByTestId("single-invoice-line-items")).toBeVisible();
  await expect(page.getByText("Invoice History")).toBeVisible();

  const editedDescription = `QA single invoice edit ${Date.now()}`;
  const descriptionField = page.getByTestId(
    "single-invoice-field-Description",
  );
  await descriptionField.fill(editedDescription);
  await descriptionField.press("Enter");

  await page.getByTestId("template-mode-bulk").click();
  await page.getByTestId("column-view-tabs").getByRole("tab", { name: "All" }).click();
  await expect(page.getByTestId("template-row").first()).toContainText(
    editedDescription,
  );
});

test("detached single invoice review renders the polished review layout", async ({
  page,
  request,
}) => {
  const batch = await pickPreviewBatch(request);
  test.skip(!batch, "No processed batch with preview rows is available.");
  await page.setViewportSize({ width: 1366, height: 768 });
  await page.goto(`/#popout/template?batch=${batch!.batch_id}`);
  await expect(page.getByText("Detached review")).toBeVisible();
  await expect(page.getByTestId("template-window-chrome")).toBeVisible();
  await page.getByTestId("template-row").first().click();
  await page.getByTestId("template-mode-single").click();
  await expect(page.getByTestId("single-invoice-mode")).toBeVisible();
  await expect(page.getByTestId("single-property-resolver")).toBeVisible();
  await expect(page.getByTestId("single-invoice-totals")).toBeVisible();
  await expect(page.getByTestId("single-review-tasks")).toBeVisible();
  await expect(page.getByTestId("single-mark-reviewed")).toBeVisible();
  await expect(page.getByTestId("single-ready-export")).toBeVisible();
});

test("AI supplier single invoice shows invoice total separate from merchandise and tax", async ({
  page,
  request,
}) => {
  const lowesBatch = (await listBatches(request)).find(
    (b) => /Lowes Pro Supply/i.test(b.batch_name) && b.rows_count > 0,
  );
  test.skip(!lowesBatch, "No Lowe's AI-assisted preview batch is available.");
  await loadBatch(page, request, lowesBatch!, { width: 1600, height: 900 });
  await page.getByTestId("template-row").first().click();
  await page.getByTestId("template-mode-single").click();
  await expect(page.getByTestId("single-invoice-mode")).toBeVisible();
  await expect(page.getByTestId("single-total-invoice").getByLabel("Invoice total summary")).toHaveValue("6.75");
  await expect(page.getByTestId("single-total-merchandise")).toContainText("$6.16");
  await expect(page.getByTestId("single-total-tax")).toContainText("$0.59");
  await expect(page.getByTestId("single-invoice-primary-total").getByLabel("Invoice total")).toHaveValue("6.75");
  await expect(page.getByTestId("ai-mapping-review")).toHaveCount(0);
});

test("issues drawer opens and closes when issues exist", async ({ page, request }) => {
  await loadPreviewBatch(page, request, { width: 1366, height: 768 });
  const issuePill = page.getByTestId("issues-pill");
  test.skip((await issuePill.count()) === 0, "Selected preview batch has no issue pill.");
  await expect(issuePill).toBeVisible();
  await issuePill.click();
  await expect(page.getByTestId("issues-drawer")).toBeVisible();
  await page.getByTestId("issues-drawer-close").click();
  await expect(page.getByTestId("issues-drawer")).toHaveCount(0);
});

test("continuous document viewer syncs page tree and template rows", async ({
  page,
  request,
}) => {
  const picked = await pickPdfPreviewBatch(request);
  test.skip(!picked, "No processed PDF batch is available for document sync.");
  const { batch, file } = picked!;
  const pageCount = Math.max(1, file.page_count ?? 1);
  test.skip(pageCount < 2, "No multi-page PDF is available for page navigation.");

  await loadBatch(page, request, batch, { width: 1366, height: 768 });
  await expandBatch(page, batch.batch_id);
  const escapedFile = file.filename.replace(/"/g, '\\"');
  const fileNode = page.locator(
    `[data-testid="explorer-file-node"][data-filename="${escapedFile}"]`,
  );
  await expect(fileNode).toBeVisible();
  await fileNode.getByTestId("explorer-file-row").click();
  await expect(page.getByTestId("pdf-continuous-scroll")).toBeVisible({
    timeout: 15000,
  });
  await expect(page.getByTestId("pdf-page-shell")).toHaveCount(pageCount, {
    timeout: 15000,
  });

  if ((await fileNode.getByTestId("explorer-file-page").count()) === 0) {
    await fileNode.getByTestId("explorer-file-page-toggle").click();
  }
  const pageTwo = fileNode.locator(
    '[data-testid="explorer-file-page"][data-page-number="2"]',
  );
  await expect(pageTwo).toBeVisible();
  await pageTwo.click();
  await expect(pageTwo).toHaveAttribute("aria-current", "page", {
    timeout: 5000,
  });

  const pageTwoRows = page.locator('[data-testid="template-row"][data-source-page="2"]');
  if ((await pageTwoRows.count()) > 0) {
    await expect(pageTwoRows.first()).toHaveClass(/document-page-row/);
    await pageTwoRows.first().click();
    await expect(pageTwo).toHaveAttribute("aria-current", "page", {
      timeout: 5000,
    });
  }
});

test("holding Space while panning does not trigger native page-scroll repeat", async ({
  page,
  request,
}) => {
  const picked = await pickMultiPagePdfBatch(request);
  test.skip(!picked, "No multi-page PDF batch is available for pan regression.");
  const { batch, file } = picked!;
  const pageCount = Math.max(1, file.page_count ?? 1);
  test.skip(pageCount < 2, "No multi-page PDF is available for pan regression.");

  await loadBatch(page, request, batch, { width: 1366, height: 768 });
  await expandBatch(page, batch.batch_id);
  const escapedFile = file.filename.replace(/"/g, '\\"');
  const fileNode = page.locator(
    `[data-testid="explorer-file-node"][data-filename="${escapedFile}"]`,
  );
  await expect(fileNode).toBeVisible();
  await fileNode.getByTestId("explorer-file-row").click();

  const scroller = page.getByTestId("pdf-continuous-scroll");
  await expect(scroller).toBeVisible({ timeout: 15000 });
  await expect(page.getByTestId("pdf-page-shell")).toHaveCount(pageCount, {
    timeout: 15000,
  });
  await scroller.evaluate((el) => {
    el.scrollTop = 0;
    el.scrollLeft = 0;
  });

  const box = await scroller.boundingBox();
  expect(box).not.toBeNull();
  await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
  await page.keyboard.down("Space");
  // A held Space key generates repeated keydown events. The viewer must
  // prevent the native default on every repeat, otherwise Chromium
  // scrolls the document down while the operator is trying to pan.
  for (let i = 0; i < 8; i += 1) {
    await page.keyboard.down("Space");
  }
  const repeatedSpaceScrollTop = await scroller.evaluate((el) => el.scrollTop);
  expect(repeatedSpaceScrollTop).toBeLessThanOrEqual(2);

  await page.mouse.down();
  for (let i = 0; i < 8; i += 1) {
    await page.keyboard.down("Space");
  }
  await page.mouse.up();
  await page.keyboard.up("Space");
  const afterPanScrollTop = await scroller.evaluate((el) => el.scrollTop);
  expect(afterPanScrollTop).toBeLessThanOrEqual(2);
});

test("canonical rules test bench returns the Capital Waste expected result", async ({
  request,
}) => {
  test.setTimeout(90_000);
  const list = await request.get(`${API_BASE}/api/canonical-rules`);
  expect(list.ok()).toBeTruthy();
  const rules = (await list.json()) as { categories: { key: string }[] };
  expect(
    rules.categories.some((category) => category.key === "trash_collection_services"),
  ).toBeTruthy();

  const fixtures = await request.get(`${API_BASE}/api/canonical-rules/test-fixtures`);
  expect(fixtures.ok()).toBeTruthy();
  const fixturePayload = (await fixtures.json()) as { fixtures: { key: string; status: string }[] };
  expect(fixturePayload.fixtures.some((fixture) => fixture.key === "spectrum")).toBeTruthy();

  const suite = await request.post(`${API_BASE}/api/canonical-rules/test-bench`, {
    data: { run_all: true },
  });
  expect(suite.ok()).toBeTruthy();
  const suiteResult = (await suite.json()) as {
    ok: boolean;
    results: {
      fixture_key: string;
      ok: boolean;
      actual: { category?: string; gl_accounts?: string[]; property?: string };
    }[];
  };
  expect(suiteResult.ok).toBeTruthy();
  const capital = suiteResult.results.find((item) => item.fixture_key === "capital_waste");
  const spectrum = suiteResult.results.find((item) => item.fixture_key === "spectrum");
  expect(capital?.ok).toBeTruthy();
  expect(capital?.actual.category).toBe("trash_collection_services");
  expect(capital?.actual.property).toBe("RCC");
  expect(capital?.actual.gl_accounts).toEqual(["6940", "6940"]);
  expect(spectrum?.ok).toBeTruthy();
  expect(spectrum?.actual.category).toBe("subscriptions");
  expect(spectrum?.actual.gl_accounts).toEqual(["6905", "6905"]);
});
