import { expect, test } from "@playwright/test";

test.describe("Phase 3.9A private assisted workspace", () => {
  test("three panels scroll independently and raw JSON is diagnostics-only", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByTestId("pilot-queue")).toBeVisible();
    await expect(page.getByTestId("preview-panel")).toBeVisible();
    await expect(page.getByTestId("label-form")).toBeVisible();
    for (const id of ["pilot-queue", "preview-panel", "label-form"]) {
      const overflow = await page.getByTestId(id).evaluate((node) => getComputedStyle(node).overflowY);
      expect(["auto", "scroll"]).toContain(overflow);
    }
    await expect(page.locator("textarea")).toHaveCount(1);
    await expect(page.getByText("Diagnostics — raw proposal JSON")).toBeVisible();
  });

  test("responsive controls and sticky actions remain inside the viewport", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 720 });
    await page.goto("/");
    const box = await page.getByRole("button", { name: "Approve verified document" }).boundingBox();
    expect(box).not.toBeNull();
    expect(box!.x + box!.width).toBeLessThanOrEqual(1280);
    await expect(page.getByRole("button", { name: "Resume" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Pause" })).toBeVisible();
  });

  test("keyboard approval cannot bypass server validation", async ({ page }) => {
    await page.goto("/");
    await page.getByPlaceholder("reviewer id").fill("e2e-reviewer");
    await page.getByTestId("queue-item").first().click();
    await page.getByLabel("I inspected this document").check();
    await page.keyboard.press("Control+Shift+Enter");
    await expect(page.getByText(/blocking validation errors|proposed fields require/)).toBeVisible();
  });

  test("document switching is guarded when a field has unsaved edits", async ({ page }) => {
    await page.goto("/");
    await page.getByPlaceholder("reviewer id").fill("e2e-reviewer");
    await page.getByTestId("queue-item").first().click();
    const proposed = page.locator("[data-field-path] input").first();
    if (await proposed.count()) {
      await proposed.fill("unsaved human edit");
      await page.getByRole("button", { name: "Next →" }).click();
      await expect(page.getByText("Resolve or reject the edited field before switching documents.")).toBeVisible();
    }
  });
});
