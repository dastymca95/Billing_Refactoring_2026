// Thin typed fetch wrappers around the FastAPI backend.

import type {
  AiFallbackPolicy,
  AiStatus,
  BatchListEntry,
  BatchProgress,
  BatchStatus,
  DocumentMode,
  ExportResponse,
  FilePreview,
  FilesResponse,
  ManualReviewResponse,
  PreviewResponse,
  ProcessResult,
  RegionHint,
  RegionHintsResponse,
} from "./types";

async function jsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`HTTP ${res.status} ${res.statusText}: ${detail}`);
  }
  return (await res.json()) as T;
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
    },
  ) {
    const body: Record<string, unknown> = {};
    if (updates.batchName !== undefined) body.batch_name = updates.batchName;
    if (updates.documentMode !== undefined) body.document_mode = updates.documentMode;
    if (updates.aiFallbackEnabled !== undefined)
      body.ai_fallback_enabled = updates.aiFallbackEnabled;
    if (updates.aiFallbackPolicy !== undefined)
      body.ai_fallback_policy = updates.aiFallbackPolicy;
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

  async uploadFile(batchId: string, file: File) {
    const fd = new FormData();
    fd.append("file", file);
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

  async filePreview(batchId: string, filename: string) {
    const res = await fetch(
      `/api/batches/${batchId}/files/${encodeURIComponent(filename)}/preview`,
    );
    return jsonOrThrow<FilePreview>(res);
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
  async process(batchId: string, opts: { sync?: boolean } = {}) {
    const url = opts.sync
      ? `/api/batches/${batchId}/process?sync=1`
      : `/api/batches/${batchId}/process`;
    const res = await fetch(url, { method: "POST" });
    return jsonOrThrow<ProcessResult | { status: string; polling_url: string }>(
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
};
