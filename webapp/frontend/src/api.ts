// Thin typed fetch wrappers around the FastAPI backend.

import type {
  AiFallbackPolicy,
  AiStatus,
  AiVisionAssistResponse,
  BatchListEntry,
  BatchProgress,
  BatchStatus,
  DocumentMode,
  ExportResponse,
  FilePreview,
  FilesResponse,
  IngestionPreviewResponse,
  ManualReviewResponse,
  PreviewResponse,
  ProcessResult,
  QueueStatus,
  RegionHint,
  RegionHintsResponse,
  RevisionListResponse,
} from "./types";

export class ApiError extends Error {
  status: number;
  statusText: string;
  detail: unknown;
  rawBody: string;

  constructor(
    message: string,
    opts: {
      status: number;
      statusText: string;
      detail: unknown;
      rawBody: string;
    },
  ) {
    super(message);
    this.name = "ApiError";
    this.status = opts.status;
    this.statusText = opts.statusText;
    this.detail = opts.detail;
    this.rawBody = opts.rawBody;
  }
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function detailMessage(detail: unknown): string | null {
  if (typeof detail === "string") return detail;
  if (isRecord(detail)) {
    const message = detail.message;
    if (typeof message === "string") return message;
  }
  return null;
}

function parseErrorBody(rawBody: string): { parsed: unknown; detail: unknown } {
  if (!rawBody) return { parsed: null, detail: null };
  try {
    const parsed = JSON.parse(rawBody) as unknown;
    if (isRecord(parsed) && "detail" in parsed) {
      return { parsed, detail: parsed.detail };
    }
    return { parsed, detail: parsed };
  } catch {
    return { parsed: null, detail: rawBody };
  }
}

async function jsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let rawBody = "";
    try {
      rawBody = await res.text();
    } catch {
      rawBody = "";
    }
    const { detail } = parseErrorBody(rawBody);
    const message =
      detailMessage(detail) ||
      (res.status === 422
        ? "Some information is invalid. Please review and try again."
        : res.statusText || "Request failed.");
    throw new ApiError(message, {
      status: res.status,
      statusText: res.statusText,
      detail,
      rawBody,
    });
  }
  return (await res.json()) as T;
}

type UploadProgressEvent = {
  loaded: number;
  total: number;
  percent: number;
};

function uploadWithProgress<T>(
  url: string,
  body: FormData,
  onProgress: (progress: UploadProgressEvent) => void,
  fallbackTotal: number,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.responseType = "text";

    xhr.upload.onprogress = (event) => {
      const total =
        event.lengthComputable && event.total > 0 ? event.total : fallbackTotal;
      const loaded = Math.min(event.loaded || 0, total || event.loaded || 0);
      const percent = total > 0 ? Math.round((loaded / total) * 100) : 0;
      onProgress({ loaded, total, percent });
    };

    xhr.onload = () => {
      const rawBody = typeof xhr.responseText === "string" ? xhr.responseText : "";
      if (xhr.status < 200 || xhr.status >= 300) {
        const { detail } = parseErrorBody(rawBody);
        const message =
          detailMessage(detail) ||
          (xhr.status === 422
            ? "Some information is invalid. Please review and try again."
            : xhr.statusText || "Request failed.");
        reject(
          new ApiError(message, {
            status: xhr.status,
            statusText: xhr.statusText,
            detail,
            rawBody,
          }),
        );
        return;
      }

      try {
        resolve(rawBody ? (JSON.parse(rawBody) as T) : ({} as T));
      } catch {
        reject(
          new ApiError("Backend returned an invalid upload response.", {
            status: xhr.status,
            statusText: xhr.statusText,
            detail: rawBody,
            rawBody,
          }),
        );
      }
    };

    xhr.onerror = () => {
      reject(new TypeError("Could not reach the backend."));
    };

    xhr.onabort = () => {
      reject(new Error("Upload was cancelled."));
    };

    xhr.send(body);
  });
}

export function getFriendlyErrorMessage(error: unknown, context?: string): string {
  if (isApiError(error)) {
    const detailText =
      typeof error.detail === "string" ? error.detail : error.message;

    if (error.status === 400 && /invalid batch id/i.test(detailText)) {
      return "Invalid batch. Please refresh and try again.";
    }
    if (error.status === 404) {
      return "Batch not found. It may have been deleted.";
    }
    if (error.status === 405) {
      return "This action is not available on the running backend. Restart the backend and refresh the app.";
    }
    if (error.status === 422) {
      return "Some information is invalid. Please review and try again.";
    }
    return detailMessage(error.detail) || error.message || "Request failed. Please try again.";
  }

  if (error instanceof TypeError) {
    return "Could not reach the backend. Make sure the backend is running.";
  }
  if (error instanceof Error && error.message) {
    if (/failed to fetch|networkerror|load failed/i.test(error.message)) {
      return "Could not reach the backend. Make sure the backend is running.";
    }
    return error.message;
  }

  return context
    ? `${context} failed. Please try again.`
    : "Request failed. Please try again.";
}

export const api = {
  async health() {
    const res = await fetch("/api/health");
    return jsonOrThrow<{ ok: boolean }>(res);
  },

  async createBatch(
    batchName?: string,
    opts?: {
      documentMode?: DocumentMode;
      aiFallbackEnabled?: boolean;
      aiFallbackPolicy?: AiFallbackPolicy;
    },
  ) {
    const init: RequestInit = { method: "POST" };
    const body: Record<string, unknown> = {};
    if (batchName) body.batch_name = batchName;
    if (opts?.documentMode) body.document_mode = opts.documentMode;
    if (opts?.aiFallbackEnabled !== undefined)
      body.ai_fallback_enabled = opts.aiFallbackEnabled;
    if (opts?.aiFallbackPolicy) body.ai_fallback_policy = opts.aiFallbackPolicy;
    if (Object.keys(body).length > 0) {
      init.headers = { "Content-Type": "application/json" };
      init.body = JSON.stringify(body);
    }
    const res = await fetch("/api/batches", init);
    return jsonOrThrow<{ batch_id: string; batch_name: string; metadata: any }>(res);
  },

  async updateBatch(
    batchId: string,
    updates: {
      batchName?: string;
      documentMode?: DocumentMode;
      aiFallbackEnabled?: boolean;
      aiFallbackPolicy?: AiFallbackPolicy;
      // Phase 2C — display name for the export workbook.
      exportName?: string;
    },
  ) {
    const body: Record<string, unknown> = {};
    if (updates.batchName !== undefined) body.batch_name = updates.batchName;
    if (updates.documentMode !== undefined) body.document_mode = updates.documentMode;
    if (updates.aiFallbackEnabled !== undefined)
      body.ai_fallback_enabled = updates.aiFallbackEnabled;
    if (updates.aiFallbackPolicy !== undefined)
      body.ai_fallback_policy = updates.aiFallbackPolicy;
    if (updates.exportName !== undefined) body.export_name = updates.exportName;
    const res = await fetch(`/api/batches/${batchId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return jsonOrThrow<{ batch_id: string; metadata: any }>(res);
  },

  async listBatches() {
    const res = await fetch("/api/batches");
    return jsonOrThrow<{ batches: BatchListEntry[] }>(res);
  },

  async renameBatch(batchId: string, batchName: string) {
    const res = await fetch(`/api/batches/${batchId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ batch_name: batchName }),
    });
    return jsonOrThrow<{ batch_id: string; metadata: any }>(res);
  },

  // Returns 404 if the batch folder is gone (stale localStorage). The
  // frontend uses that to clear localStorage on app load.
  async getBatch(batchId: string) {
    const res = await fetch(`/api/batches/${batchId}`);
    return jsonOrThrow<BatchStatus>(res);
  },

  // Phase 1F: per-batch progress snapshot (polled by the frontend
  // progress bar while Process Batch is running).
  async getBatchProgress(batchId: string) {
    const res = await fetch(`/api/batches/${batchId}/progress`);
    return jsonOrThrow<BatchProgress>(res);
  },

  async uploadFile(
    batchId: string,
    file: File,
    onProgress?: (progress: UploadProgressEvent) => void,
  ) {
    const fd = new FormData();
    fd.append("file", file);
    if (onProgress) {
      return uploadWithProgress<{ filename: string; size_bytes: number }>(
        `/api/batches/${batchId}/upload`,
        fd,
        onProgress,
        file.size,
      );
    }
    const res = await fetch(`/api/batches/${batchId}/upload`, {
      method: "POST",
      body: fd,
    });
    return jsonOrThrow<{ filename: string; size_bytes: number }>(res);
  },

  async listFiles(batchId: string) {
    const res = await fetch(`/api/batches/${batchId}/files`);
    return jsonOrThrow<FilesResponse>(res);
  },

  // Phase 1X — delete one file from a batch.
  async deleteFile(batchId: string, filename: string) {
    const res = await fetch(
      `/api/batches/${batchId}/files/${encodeURIComponent(filename)}`,
      { method: "DELETE" },
    );
    return jsonOrThrow<{ batch_id: string; filename: string; deleted: boolean }>(res);
  },

  async filePreview(batchId: string, filename: string) {
    const res = await fetch(
      `/api/batches/${batchId}/files/${encodeURIComponent(filename)}/preview`,
    );
    return jsonOrThrow<FilePreview>(res);
  },

  async ingestionPreview(batchId: string, filename: string) {
    const res = await fetch(
      `/api/batches/${batchId}/files/${encodeURIComponent(filename)}/ingestion-preview`,
    );
    return jsonOrThrow<IngestionPreviewResponse>(res);
  },

  fileRawUrl(batchId: string, filename: string) {
    return `/api/batches/${batchId}/files/${encodeURIComponent(filename)}/raw`;
  },

  // Inline content URL — Content-Disposition: inline, correct Content-Type.
  // Used by the document preview panel for PDFs/images so the browser
  // renders them in-page instead of triggering a download.
  fileContentUrl(batchId: string, filename: string) {
    return `/api/batches/${batchId}/files/${encodeURIComponent(filename)}/content`;
  },

  // Phase 1G: process() now returns immediately ({status: "accepted",
  // polling_url}) and the actual work happens in a backend thread. The
  // frontend polls /progress until status=completed|failed, then re-fetches
  // /preview and /manual-review. Pass `sync=true` for the legacy blocking
  // behaviour (used by tests).
  async process(
    batchId: string,
    opts: { sync?: boolean; file?: string; fileMode?: "replace" | "merge" } = {},
  ) {
    // Phase 2M — pass ``file`` to process a single file inside the
    // batch. Single-file runs are always sync on the server.
    const params = new URLSearchParams();
    if (opts.sync || opts.file) params.set("sync", "1");
    if (opts.file) params.set("file", opts.file);
    if (opts.fileMode) params.set("file_mode", opts.fileMode);
    const qs = params.toString();
    const url = qs
      ? `/api/batches/${batchId}/process?${qs}`
      : `/api/batches/${batchId}/process`;
    const res = await fetch(url, { method: "POST" });
    return jsonOrThrow<ProcessResult | { status: string; polling_url: string }>(
      res,
    );
  },

  // Phase 1N — request cooperative cancellation of an active run.
  async cancelBatch(batchId: string) {
    const res = await fetch(`/api/batches/${batchId}/cancel`, {
      method: "POST",
    });
    return jsonOrThrow<{ batch_id: string; status: string; message: string }>(
      res,
    );
  },

  async preview(batchId: string) {
    const res = await fetch(`/api/batches/${batchId}/preview`);
    return jsonOrThrow<PreviewResponse>(res);
  },

  async manualReview(batchId: string) {
    const res = await fetch(`/api/batches/${batchId}/manual-review`);
    return jsonOrThrow<ManualReviewResponse>(res);
  },

  async exportBatch(batchId: string, editedRows?: Record<string, unknown>[]) {
    const init: RequestInit = { method: "POST" };
    if (editedRows && editedRows.length > 0) {
      init.headers = { "Content-Type": "application/json" };
      init.body = JSON.stringify({ edited_rows: editedRows });
    }
    const res = await fetch(`/api/batches/${batchId}/export`, init);
    return jsonOrThrow<ExportResponse>(res);
  },

  downloadUrl(batchId: string) {
    return `/api/batches/${batchId}/download`;
  },

  async deleteBatch(batchId: string) {
    const res = await fetch(`/api/batches/${batchId}`, { method: "DELETE" });
    return jsonOrThrow<{ deleted: boolean }>(res);
  },

  // ---- Phase 1H — AI fallback status ----------------------------------
  async getAiStatus() {
    const res = await fetch("/api/ai/status");
    return jsonOrThrow<AiStatus>(res);
  },

  // ---- Phase 1H — region hints ----------------------------------------
  async aiVisionAssist(
    batchId: string,
    body: {
      filename: string;
      page_numbers?: number[];
      vendor_hint?: string;
      document_text?: string;
      current_extraction?: Record<string, unknown> | null;
      dry_run?: boolean;
    },
  ) {
    const res = await fetch(`/api/batches/${batchId}/ai-invoice/vision-assist`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dry_run: true, ...body }),
    });
    return jsonOrThrow<AiVisionAssistResponse>(res);
  },

  async listRegions(batchId: string) {
    const res = await fetch(`/api/batches/${batchId}/regions`);
    return jsonOrThrow<RegionHintsResponse>(res);
  },

  async replaceRegions(batchId: string, regions: RegionHint[]) {
    const res = await fetch(`/api/batches/${batchId}/regions`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ regions }),
    });
    return jsonOrThrow<RegionHintsResponse>(res);
  },

  async addRegion(batchId: string, region: RegionHint) {
    const res = await fetch(`/api/batches/${batchId}/regions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(region),
    });
    return jsonOrThrow<RegionHintsResponse>(res);
  },

  async deleteRegion(batchId: string, regionId: string) {
    const res = await fetch(
      `/api/batches/${batchId}/regions/${encodeURIComponent(regionId)}`,
      { method: "DELETE" },
    );
    return jsonOrThrow<RegionHintsResponse & { deleted: number }>(res);
  },

  // ---- Phase 2D — template revision history --------------------------
  async listRevisions(batchId: string) {
    const res = await fetch(`/api/batches/${batchId}/revisions`);
    return jsonOrThrow<RevisionListResponse>(res);
  },

  async activateRevision(batchId: string, revisionId: string) {
    const res = await fetch(
      `/api/batches/${batchId}/revisions/${encodeURIComponent(revisionId)}/activate`,
      { method: "POST" },
    );
    return jsonOrThrow<{
      batch_id: string;
      current_revision_id: string;
      activated: import("./types").RevisionEntry;
    }>(res);
  },

  async explainCell(batchId: string, rowIndex: number, column: string) {
    const res = await fetch(
      `/api/batches/${batchId}/cells/${rowIndex}/${encodeURIComponent(column)}/explain`,
    );
    return jsonOrThrow<import("./types").CellExplain>(res);
  },

  async overrideCell(
    batchId: string,
    rowIndex: number,
    column: string,
    body: {
      new_value: unknown;
      scope: "cell" | "vendor";
      note?: string;
      contains_text?: string;
    },
  ) {
    const res = await fetch(
      `/api/batches/${batchId}/cells/${rowIndex}/${encodeURIComponent(column)}/override`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    return jsonOrThrow<{
      batch_id: string;
      row_index: number;
      column: string;
      saved: "edit" | "learned_correction";
      correction_id: string | null;
      new_value: unknown;
    }>(res);
  },

  async remapCellSource(
    batchId: string,
    rowIndex: number,
    column: string,
    body: {
      field_key: string;
      page: number;
      bbox: { x: number; y: number; w: number; h: number };
      scope?: "cell" | "document" | "batch" | "vendor";
      note?: string;
    },
  ) {
    const res = await fetch(
      `/api/batches/${batchId}/cells/${rowIndex}/${encodeURIComponent(column)}/remap-source`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    return jsonOrThrow<{
      batch_id: string;
      correction_id: string;
      saved: string;
    }>(res);
  },

  async listLearnedCorrections(vendorKey?: string) {
    const url = vendorKey
      ? `/api/learned-corrections?vendor_key=${encodeURIComponent(vendorKey)}`
      : "/api/learned-corrections";
    const res = await fetch(url);
    return jsonOrThrow<{
      vendor_key: string;
      items: import("./types").LearnedCorrection[];
    }>(res);
  },

  async deleteLearnedCorrection(vendorKey: string, correctionId: string) {
    const res = await fetch(
      `/api/learned-corrections/${encodeURIComponent(vendorKey)}/${encodeURIComponent(correctionId)}`,
      { method: "DELETE" },
    );
    return jsonOrThrow<{
      vendor_key: string;
      correction_id: string;
      deleted: true;
    }>(res);
  },

  async aiVendorCandidates(detectedVendor: string) {
    const res = await fetch(
      `/api/ai-review/vendor-candidates?detected_vendor=${encodeURIComponent(detectedVendor)}`,
    );
    return jsonOrThrow<import("./types").AiVendorCandidatesResponse>(res);
  },

  async aiGlCandidates(params: {
    line_item_description: string;
    vendor_name?: string;
    ai_suggested_gl?: string;
  }) {
    const qs = new URLSearchParams({
      line_item_description: params.line_item_description,
      vendor_name: params.vendor_name || "",
      ai_suggested_gl: params.ai_suggested_gl || "",
      limit: "8",
    });
    const res = await fetch(`/api/ai-review/gl-candidates?${qs.toString()}`);
    return jsonOrThrow<import("./types").AiGlCandidatesResponse>(res);
  },

  async applyAiVendorMapping(
    batchId: string,
    body: {
      detected_vendor: string;
      selected_vendor_name: string;
      vendor_id?: string;
      row_index?: number | null;
      save_for_future?: boolean;
      apply_scope?: "current_invoice" | "batch";
    },
  ) {
    const res = await fetch(`/api/batches/${batchId}/ai-review/vendor-mapping`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return jsonOrThrow<{
      batch_id: string;
      applied_rows: number;
      selected_vendor_name: string;
      saved_mapping: Record<string, unknown> | null;
    }>(res);
  },

  async applyAiGlMapping(
    batchId: string,
    body: {
      row_index: number;
      gl_account: string;
      gl_name?: string;
      save_for_future?: boolean;
      apply_to_similar?: boolean;
      pattern?: string;
    },
  ) {
    const res = await fetch(`/api/batches/${batchId}/ai-review/gl-mapping`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return jsonOrThrow<{
      batch_id: string;
      applied_rows: number;
      gl_account: string;
      gl_name: string;
      saved_mapping: Record<string, unknown> | null;
    }>(res);
  },

  async aiPropertyCandidates(params: {
    query?: string;
    service_address?: string;
  }) {
    const qs = new URLSearchParams({
      query: params.query || "",
      service_address: params.service_address || "",
    });
    const res = await fetch(`/api/ai-review/property-candidates?${qs.toString()}`);
    return jsonOrThrow<import("./types").AiPropertyCandidatesResponse>(res);
  },

  async aiLocationCandidates(params: {
    property_abbreviation: string;
    query?: string;
  }) {
    const qs = new URLSearchParams({
      property_abbreviation: params.property_abbreviation,
      query: params.query || "",
    });
    const res = await fetch(`/api/ai-review/location-candidates?${qs.toString()}`);
    return jsonOrThrow<import("./types").AiLocationCandidatesResponse>(res);
  },

  async invoiceFormatRules() {
    const res = await fetch("/api/invoice-format-rules");
    return jsonOrThrow<import("./types").InvoiceFormatRulesPayload>(res);
  },

  async saveInvoiceFormatRules(config: import("./types").InvoiceFormatRulesConfig) {
    const res = await fetch("/api/invoice-format-rules", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config }),
    });
    return jsonOrThrow<{ ok: true; config: import("./types").InvoiceFormatRulesConfig }>(res);
  },

  async previewInvoiceFormatRules(body: {
    config?: import("./types").InvoiceFormatRulesConfig;
    sample?: Record<string, unknown>;
  }) {
    const res = await fetch("/api/invoice-format-rules/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return jsonOrThrow<{ preview: Record<string, string> }>(res);
  },

  async canonicalRules() {
    const res = await fetch("/api/canonical-rules");
    return jsonOrThrow<import("./types").CanonicalRulesPayload>(res);
  },

  async canonicalRuleCategory(category: string) {
    const res = await fetch(`/api/canonical-rules/${encodeURIComponent(category)}`);
    return jsonOrThrow<import("./types").CanonicalCategoryPayload>(res);
  },

  async validateCanonicalRules(body: {
    config?: Record<string, unknown>;
    category?: string;
    patch?: Record<string, unknown>;
  }) {
    const res = await fetch("/api/canonical-rules/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return jsonOrThrow<import("./types").CanonicalRulesValidationResponse>(res);
  },

  async patchCanonicalRuleCategory(category: string, patch: Record<string, unknown>) {
    const res = await fetch(`/api/canonical-rules/${encodeURIComponent(category)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ patch }),
    });
    return jsonOrThrow<{
      result: Record<string, unknown>;
      category: import("./types").CanonicalCategoryPayload;
    }>(res);
  },

  async restoreCanonicalRules() {
    const res = await fetch("/api/canonical-rules/restore", { method: "POST" });
    return jsonOrThrow<{ ok: true; restored_from: string }>(res);
  },

  async previewCanonicalRulesImport() {
    const res = await fetch("/api/canonical-rules/import-preview", { method: "POST" });
    return jsonOrThrow<import("./types").CanonicalRulesImportPreview>(res);
  },

  async applyCanonicalRulesImport() {
    const res = await fetch("/api/canonical-rules/import-apply", { method: "POST" });
    return jsonOrThrow<{ ok: true; backup_path: string; preview: import("./types").CanonicalRulesImportPreview }>(res);
  },

  async canonicalRulesTestFixtures() {
    const res = await fetch("/api/canonical-rules/test-fixtures");
    return jsonOrThrow<import("./types").CanonicalRulesFixtureList>(res);
  },

  async runCanonicalRulesTestBench(body: {
    test_case?: string;
    fixture_key?: string;
    category?: string;
    draft_patch?: Record<string, unknown>;
    run_all?: boolean;
  }) {
    const res = await fetch("/api/canonical-rules/test-bench", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return jsonOrThrow<import("./types").CanonicalRulesTestBenchResponse>(res);
  },

  async runAllCanonicalRulesFixtures(body: {
    category?: string;
    draft_patch?: Record<string, unknown>;
  } = {}) {
    const res = await fetch("/api/canonical-rules/test-bench", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...body, run_all: true }),
    });
    return jsonOrThrow<import("./types").CanonicalRulesRunAllResponse>(res);
  },

  async applyAiPropertyLocation(
    batchId: string,
    body: {
      row_index: number;
      property_abbreviation: string;
      location?: string;
      service_address?: string;
      save_for_future?: boolean;
      apply_scope?: "current_invoice" | "batch";
      leave_location_blank?: boolean;
    },
  ) {
    const res = await fetch(`/api/batches/${batchId}/ai-review/property-location`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return jsonOrThrow<{
      batch_id: string;
      applied_rows: number;
      property_abbreviation: string;
      location: string;
      saved_mapping: Record<string, unknown> | null;
    }>(res);
  },

  async applyAiTaxPolicy(
    batchId: string,
    body: {
      row_index: number;
      policy: "manual_review" | "distribute_proportionally" | "separate_tax_line" | "exclude_tax";
    },
  ) {
    const res = await fetch(`/api/batches/${batchId}/ai-review/tax-policy`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return jsonOrThrow<{
      batch_id: string;
      applied_rows: number;
      policy: string;
    }>(res);
  },

  async getDocumentTrace(batchId: string, filename: string) {
    const res = await fetch(
      `/api/batches/${batchId}/documents/${encodeURIComponent(filename)}/trace`,
    );
    return jsonOrThrow<import("./types").DocumentTraceResponse>(res);
  },

  async saveEdits(
    batchId: string,
    edits: Record<number, Record<string, unknown>>,
  ) {
    const res = await fetch(`/api/batches/${batchId}/save-edits`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ edits }),
    });
    return jsonOrThrow<{
      batch_id: string;
      applied: number;
      skipped: number;
      current_revision_id: string | null;
    }>(res);
  },

  async deleteRevision(batchId: string, revisionId: string) {
    const res = await fetch(
      `/api/batches/${batchId}/revisions/${encodeURIComponent(revisionId)}`,
      { method: "DELETE" },
    );
    return jsonOrThrow<{
      batch_id: string;
      deleted: import("./types").RevisionEntry;
      current_revision_id: string | null;
    }>(res);
  },

  // ---- Phase 2D — cross-batch processing queue -----------------------
  async getQueueStatus() {
    const res = await fetch("/api/processing/queue");
    return jsonOrThrow<QueueStatus>(res);
  },
};
