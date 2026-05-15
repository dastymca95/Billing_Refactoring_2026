import { expect, test, type APIRequestContext } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const API_BASE = process.env.PLAYWRIGHT_API_BASE_URL ?? "http://localhost:8001";
const SCREENSHOT_DIR = "../../docs/reports/phases/screenshots/phase_ai9_universal_ingestion";

function pdfEscape(text: string): string {
  return text.replace(/\\/g, "\\\\").replace(/\(/g, "\\(").replace(/\)/g, "\\)");
}

function minimalPdf(text: string): Buffer {
  const content = Buffer.from(`BT /F1 12 Tf 72 720 Td (${pdfEscape(text)}) Tj ET`, "latin1");
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
    `<< /Length ${content.length} >>\nstream\n${content.toString("latin1")}\nendstream`,
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
  ];
  const chunks: Buffer[] = [Buffer.from("%PDF-1.4\n", "ascii")];
  const offsets = [0];
  for (let i = 0; i < objects.length; i += 1) {
    offsets.push(Buffer.concat(chunks).length);
    chunks.push(Buffer.from(`${i + 1} 0 obj\n${objects[i]}\nendobj\n`, "latin1"));
  }
  const xrefOffset = Buffer.concat(chunks).length;
  chunks.push(Buffer.from(`xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`, "ascii"));
  for (const offset of offsets.slice(1)) {
    chunks.push(Buffer.from(`${offset.toString().padStart(10, "0")} 00000 n \n`, "ascii"));
  }
  chunks.push(
    Buffer.from(
      `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xrefOffset}\n%%EOF\n`,
      "ascii",
    ),
  );
  return Buffer.concat(chunks);
}

async function upload(
  request: APIRequestContext,
  batchId: string,
  name: string,
  mimeType: string,
  buffer: Buffer,
) {
  const response = await request.post(`${API_BASE}/api/batches/${batchId}/upload`, {
    multipart: {
      file: {
        name,
        mimeType,
        buffer,
      },
    },
  });
  expect(response.ok()).toBeTruthy();
}

test("AI-9 file rows show normalized ingestion support badges and preview metadata", async ({
  page,
  request,
}) => {
  test.setTimeout(60_000);

  const created = await request.post(`${API_BASE}/api/batches`, {
    data: { batch_name: `QA AI9 Ingestion ${Date.now()}` },
  });
  expect(created.ok()).toBeTruthy();
  const createdJson = (await created.json()) as { batch_id: string };
  const batchId = createdJson.batch_id;

  try {
    await upload(
      request,
      batchId,
      "ai9_digital_invoice.pdf",
      "application/pdf",
      minimalPdf("Invoice Number AI9-100 Account 555 Total 12.34 ".repeat(6)),
    );
    await upload(
      request,
      batchId,
      "ai9_rows.csv",
      "text/csv",
      Buffer.from("invoice,total\nCSV-1,88.10\n", "utf8"),
    );
    await upload(
      request,
      batchId,
      "screenshot_ai9.png",
      "image/png",
      Buffer.from(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=",
        "base64",
      ),
    );
    await upload(
      request,
      batchId,
      "legacy_invoice.doc",
      "application/msword",
      Buffer.from("legacy word placeholder", "utf8"),
    );

    const preview = await request.get(
      `${API_BASE}/api/batches/${batchId}/files/ai9_digital_invoice.pdf/ingestion-preview`,
    );
    expect(preview.ok()).toBeTruthy();
    const previewJson = await preview.json();
    expect(previewJson.source_type).toBe("pdf_digital");
    expect(previewJson.text_quality_score).toBeGreaterThan(0);
    expect(previewJson.text_preview).toContain("AI9-100");

    const filesResponse = await request.get(`${API_BASE}/api/batches/${batchId}/files`);
    expect(filesResponse.ok()).toBeTruthy();
    const fileLabels = ((await filesResponse.json()) as { files: { file_support_label?: string }[] }).files.map(
      (file) => file.file_support_label,
    );
    expect(fileLabels).toEqual(
      expect.arrayContaining(["PDF digital", "CSV", "Screenshot", "Unsupported"]),
    );

    await page.goto("/");
    await page.evaluate((id) => localStorage.setItem("billing_refactoring_active_batch_id", id), batchId);
    await page.reload();
    await expect(page.getByText("QA AI9 Ingestion")).toBeVisible();
    const toggle = page.locator(`[data-batch-id="${batchId}"] [data-testid="explorer-batch-toggle"]`).first();
    if ((await toggle.getAttribute("aria-expanded")) !== "true") {
      await toggle.click();
    }

    const batchNode = page.locator(
      `[data-testid="explorer-batch-drop-target"][data-batch-id="${batchId}"]`,
    );
    await expect(batchNode).toBeAttached();

    fs.mkdirSync(path.resolve(SCREENSHOT_DIR), { recursive: true });
    await page.getByTestId("batch-explorer").screenshot({
      path: `${SCREENSHOT_DIR}/file_type_badges.png`,
    });
  } finally {
    await request.delete(`${API_BASE}/api/batches/${batchId}`);
  }
});
