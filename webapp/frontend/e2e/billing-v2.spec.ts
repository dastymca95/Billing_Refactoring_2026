import { expect, test } from "@playwright/test";

const API_BASE = process.env.PLAYWRIGHT_API_BASE_URL ?? "http://localhost:8001";

test("Billing V2 creates an empty batch and uploads a document", async ({
  page,
  request,
}) => {
  test.setTimeout(45_000);

  await page.goto("/");
  await expect(page.getByTestId("billing-v2")).toBeVisible();

  await page.getByRole("button", { name: "New Batch" }).click();
  const batchSelect = page.getByTestId("billing-v2-batch-select");
  await expect(batchSelect).not.toHaveValue("");
  const batchId = await batchSelect.inputValue();

  try {
    await expect(page.getByText("No documents uploaded.")).toBeVisible();

    await page.locator(".billing-v2-file-input").setInputFiles({
      name: "billing_v2_upload.csv",
      mimeType: "text/csv",
      buffer: Buffer.from("invoice,total\nV2-1,12.34\n", "utf8"),
    });

    await expect(page.getByTestId("billing-v2-documents")).toContainText(
      "billing_v2_upload.csv",
      { timeout: 15_000 },
    );
  } finally {
    await request.delete(`${API_BASE}/api/batches/${batchId}`);
  }
});
