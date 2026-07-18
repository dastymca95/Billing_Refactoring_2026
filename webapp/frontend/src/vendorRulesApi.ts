// Vendor Rules Studio — API client.
//
// Talks to /api/vendor-rules/*. Errors propagate through the same
// `ApiError` handling used by the main api.ts.

import { ApiError } from "./api";

export type VendorListEntry = {
  vendor_key: string;
  display_name: string;
  category: string;
  status: string;
  last_updated?: string | null;
  editable?: boolean;
  implementation_kind?: "hybrid" | "code_managed";
};

export type RuleField = {
  label: string;
  path: string;
  type: "string" | "boolean" | "integer" | "number" | "string_list" | "enum";
  editable: boolean;
  description?: string;
  example?: string;
  options?: string[];
  placeholder?: string;
  value?: unknown;
};

export type RuleGroup = {
  key: string;
  label: string;
  description?: string;
  fields: RuleField[];
  read_only_summary?: {
    kind: "list" | "object" | "scalar";
    count?: number;
    keys?: string[];
    preview?: string;
  };
};

export type ValidationIssue = { path: string; message: string };

export type VendorRulesPayload = {
  vendor_key: string;
  groups: RuleGroup[];
};

export type RowChange = {
  column: string;
  before: unknown;
  after: unknown;
  // Phase 2B — "meaningful" = real rule effect, "dry_run_link" = a
  // Dropbox URL flip caused purely by dry-run skipping uploads.
  category?: "meaningful" | "dry_run_link";
};

export type RowDiff = {
  row_key: string;
  kind: "modified" | "added" | "removed";
  // Phase 2B — present on modified rows. Lets the UI hide rows whose
  // only diffs are technical link flips behind the toggle.
  has_meaningful_changes?: boolean;
  has_dry_run_link_changes?: boolean;
  invoice_number?: string | null;
  source_file?: string | null;
  source_page?: number | null;
  changes: RowChange[];
};

export type ImpactSummary = {
  rows_before: number;
  rows_after: number;
  rows_added: number;
  rows_removed: number;
  rows_modified: number;             // Phase 2B: meaningful-only count
  rows_modified_dry_run_only: number; // Phase 2B: rows whose only changes are link flips
  cells_changed: number;             // Phase 2B: meaningful-only count
  cells_changed_total: number;       // every cell change (kept for context)
  dry_run_only_link_changes: number; // Phase 2B
  amounts_changed: number;
  gl_accounts_changed: number;
  descriptions_changed: number;
  dates_changed: number;
  issues_before: number;
  issues_after: number;
};

export type ImpactPayload = {
  vendor_key: string;
  summary: ImpactSummary;
  row_diffs: RowDiff[];
  warnings: string[];
  row_diffs_truncated: boolean;
  no_meaningful_impact: boolean;
  no_meaningful_impact_message: string | null;
};

async function jsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let raw = "";
    try {
      raw = await res.text();
    } catch {
      raw = "";
    }
    let detail: unknown = raw;
    try {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object" && "detail" in parsed) {
        detail = (parsed as { detail: unknown }).detail;
      } else {
        detail = parsed;
      }
    } catch {
      /* keep raw */
    }
    throw new ApiError(
      typeof detail === "string" ? detail : res.statusText || "Request failed.",
      { status: res.status, statusText: res.statusText, detail, rawBody: raw },
    );
  }
  return (await res.json()) as T;
}

export const vendorRulesApi = {
  async list(): Promise<{ vendors: VendorListEntry[] }> {
    return jsonOrThrow(await fetch("/api/vendor-rules"));
  },
  async get(vendorKey: string): Promise<VendorRulesPayload> {
    return jsonOrThrow(await fetch(`/api/vendor-rules/${encodeURIComponent(vendorKey)}`));
  },
  async validate(
    vendorKey: string,
    patch: Record<string, unknown>,
  ): Promise<{ ok: boolean; issues: ValidationIssue[] }> {
    return jsonOrThrow(
      await fetch(`/api/vendor-rules/${encodeURIComponent(vendorKey)}/validate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ patch }),
      }),
    );
  },
  async patch(
    vendorKey: string,
    patch: Record<string, unknown>,
  ): Promise<VendorRulesPayload & { result: { backup_filename: string; written_paths: string[] } }> {
    return jsonOrThrow(
      await fetch(`/api/vendor-rules/${encodeURIComponent(vendorKey)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ patch }),
      }),
    );
  },
  async previewImpact(
    vendorKey: string,
    batchId: string,
    draftRules: Record<string, unknown>,
  ): Promise<ImpactPayload> {
    return jsonOrThrow(
      await fetch(
        `/api/vendor-rules/${encodeURIComponent(vendorKey)}/preview-impact`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            batch_id: batchId,
            draft_rules: draftRules,
            compare_against_saved: true,
          }),
        },
      ),
    );
  },
  async restore(
    vendorKey: string,
  ): Promise<VendorRulesPayload & { result: { restored_from: string } }> {
    return jsonOrThrow(
      await fetch(`/api/vendor-rules/${encodeURIComponent(vendorKey)}/restore`, {
        method: "POST",
      }),
    );
  },
};
