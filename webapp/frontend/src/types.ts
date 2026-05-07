// Types mirror what the FastAPI backend returns. They intentionally
// stay small — the frontend is a thin viewer; all business logic lives
// in Python.

export type FileEntry = {
  filename: string;
  size_bytes: number;
  extension: string;
  page_count?: number | null;
  vendor_key: string;
  vendor_confidence: number;
  vendor_detection_reason: string;
  supported_in_phase_1: boolean;
};

export type FilesResponse = {
  batch_id: string;
  files: FileEntry[];
};

export type FilePreview =
  | {
      kind: "table";
      filename: string;
      sheet_name?: string;
      all_sheets?: string[];
      headers: string[];
      rows: (string | number | boolean | null)[][];
      truncated_rows: number;
    }
  | {
      kind: "binary";
      filename: string;
      extension: string;
      size_bytes: number;
      page_count?: number | null;
      note: string;
    }
  | {
      kind: "metadata";
      filename: string;
      note: string;
    };

export type ProcessResult = {
  batch_id: string;
  summary: {
    files_total: number;
    files_supported: number;
    files_unsupported: number;
    invoices_total: number;
    manual_review_total: number;
  };
  by_vendor: Record<string, any>;
  detection: Record<string, any>;
  unsupported_files: any[];
  all_invoices: any[];
  all_manual_review: any[];
};

export type PreviewRowMeta = {
  manual_review_reasons: string[];
  match_strategy: string;
  match_confidence: string;
  service_period_source: string;
  service_period_inferred: boolean;
  support_document_status: string;
  source_file?: string | null;
  source_page?: number | null;
  invoice_group_id?: string | null;
  invoice_number?: string | null;
  invoice_index?: number;
  invoice_row_index?: number;
  row_index?: number;
  // Phase 2J — opaque ids of the extraction trace items (regions on
  // the source PDF) that fed this row. Drives the row ↔ overlay
  // highlight in the document viewer.
  trace_ids?: string[];
};

// Phase 2J — Extraction Trace Overlay.
export type TraceBBox = { x: number; y: number; w: number; h: number };
export type TraceItem = {
  trace_id: string;
  source_file: string;
  page: number;
  bbox: TraceBBox;
  field_key: string;
  field_label: string;
  source_type: string;
  rule_id: string;
  match_strategy: string;
  confidence: number;
  feeds_rows: string[];
  feeds_columns: string[];
  detected_text: string;
};
export type DocumentTraceResponse = {
  batch_id: string;
  source_file: string;
  trace_count: number;
  items: TraceItem[];
};

// Phase 2K — Cell Explain / Correct / Learn.
export type CellExplain = {
  batch_id: string;
  row_index: number;
  column: string;
  current_value: unknown;
  summary: string;
  cell_kind: string;
  fallback_used: boolean;
  missing_components: string[];
  trace_ids: string[];
  traces: TraceItem[];
  source_file: string | null;
  source_page: number | null;
  vendor_key: string;
};

export type LearnedCorrection = {
  correction_id: string;
  vendor_key: string;
  kind: "value_override" | "region_remap";
  scope: "cell" | "document" | "batch" | "vendor";
  trigger: Record<string, unknown>;
  action: Record<string, unknown>;
  created_at: string;
  created_from: Record<string, unknown>;
  note: string;
};

// PreviewRow now uses an index signature so the row carries every column
// the backend declares in `PreviewResponse.columns` (the full template).
// Generated columns still have known types via the explicit fields below.
export type PreviewRow = {
  "Invoice Number"?: string | null;
  "Bill or Credit"?: string | null;
  "Invoice Date"?: string | null;
  "Accounting Date"?: string | null;
  Vendor?: string | null;
  "Invoice Description"?: string | null;
  "Line Item Number"?: number | null;
  "Property Abbreviation"?: string | null;
  Location?: string | null;
  "GL Account"?: string | null;
  "Line Item Description"?: string | null;
  Amount?: number | null;
  "Expense Type"?: string | null;
  "Is Replacement Reserve"?: boolean | null;
  "Due Date"?: string | null;
  "Reference Number"?: string | null;
  "Document Url"?: string | null;
  _meta?: PreviewRowMeta;
  [key: string]: unknown;
};

export type PreviewResponse = {
  batch_id: string;
  summary: ProcessResult["summary"];
  by_vendor_summaries: Record<string, any>;
  // Full template column list (canonical order from Output/Template.xlsx).
  columns: string[];
  required_columns: string[];
  recommended_columns: string[];
  optional_columns: string[];
  optional_columns_collapsible: boolean;
  optional_columns_hidden_by_default: boolean;
  rows: PreviewRow[];
  invoice_count: number;
  row_count: number;
  unsupported_files: any[];
};

export type BatchStatus = {
  batch_id: string;
  batch_name?: string;
  created_at: string;
  updated_at?: string;
  files: FileEntry[];
  files_total: number;
  preview_available: boolean;
  export_available: boolean;
  export_filenames: string[];
  summary: Record<string, any>;
  metadata?: Record<string, any>;
};

export type BatchListEntry = {
  batch_id: string;
  batch_name: string;
  created_at: string;
  updated_at?: string;
  status: string;
  files_count: number;
  invoices_count: number;
  rows_count: number;
  manual_review_count: number;
  export_available: boolean;
  last_export_file?: string | null;
  supported_vendor_summary?: Record<string, any>;
};

export type ProgressStatus =
  | "idle"
  | "uploading"
  | "processing"
  | "cancelling"   // Phase 1N — cancel requested, worker still draining
  | "cancelled"    // Phase 1N — worker stopped after cancel
  | "completed"
  | "failed";

// Phase 1H — processing timeline stage. Optional on the snapshot;
// missing `stages` means the legacy progress-bar-only experience.
export type ProcessingStageStatus =
  | "pending"
  | "running"
  | "completed"
  | "warning"
  | "failed"
  | "skipped";

export type ProcessingStage = {
  key: string;
  label: string;
  status: ProcessingStageStatus;
  detail?: string;
  percent?: number;
  started_at?: string;
  completed_at?: string;
  warnings_count?: number;
};

export type BatchProgress = {
  batch_id: string;
  status: ProgressStatus;
  percent: number;
  current_step: string;
  current_file?: string;
  files_total?: number;
  files_done?: number;
  pages_total?: number;
  pages_done?: number;
  invoices_created?: number;
  rows_created?: number;
  warnings_count?: number;
  error_message?: string;
  updated_at?: string;
  started_at?: string;
  // Phase 1H — declared stages, optional.
  stages?: ProcessingStage[];
  // Phase 1N — cancel state.
  cancel_requested?: boolean;
  cancelled_at?: string;
};

// Phase 1H — batch document mode + AI fallback policy.
export type DocumentMode =
  | "digital_pdf"
  | "scanned_pdf"
  | "mixed_pdf"
  | "csv_excel"
  | "auto_detect";

export type AiFallbackPolicy =
  | "never"
  | "only_low_confidence"
  | "only_manual_review"
  | "always_assist";

export const DOCUMENT_MODES: DocumentMode[] = [
  "auto_detect",
  "digital_pdf",
  "scanned_pdf",
  "mixed_pdf",
  "csv_excel",
];

export const DOCUMENT_MODE_LABELS: Record<DocumentMode, string> = {
  auto_detect: "Auto-detect",
  digital_pdf: "Digital PDFs",
  scanned_pdf: "Scanned PDFs",
  mixed_pdf: "Mixed PDFs",
  csv_excel: "CSV / Excel",
};

export const DOCUMENT_MODE_DESCRIPTIONS: Record<DocumentMode, string> = {
  auto_detect: "Let the system pick per-file. Safe default.",
  digital_pdf: "Text-based bills (e.g. UtilityBill_05_2026.pdf).",
  scanned_pdf: "Image scans of bills — OCR is required.",
  mixed_pdf: "A mix of scanned and digital PDFs.",
  csv_excel: "Billing-history exports (.csv / .xlsx).",
};

export const AI_FALLBACK_POLICY_LABELS: Record<AiFallbackPolicy, string> = {
  never: "Never",
  only_low_confidence: "Only when confidence is low",
  only_manual_review: "Only on manual-review rows",
  always_assist: "Always offer a second opinion",
};

// Phase 2D — template revisions + cross-batch processing queue.
export type RevisionEntry = {
  revision_id: string;
  created_at: string;
  status: string;
  export_name?: string | null;
  files_count: number;
  invoices_count: number;
  rows_count: number;
  manual_review_count: number;
  source_batch_id: string;
  snapshot_filename: string;
};

export type RevisionListResponse = {
  batch_id: string;
  current_revision_id: string | null;
  revisions: RevisionEntry[];
};

export type QueueStatus = {
  running: string | null;
  queued: string[];
};

export type AiStatus = {
  enabled: boolean;
  provider: string;
  configured: boolean;
  reason: string;
  policy?: string;
  max_cost_per_batch_usd?: number;
  allowed_tasks?: string[];
};

// Phase 1H — region hints. Coordinates are normalized to [0,1].
export type RegionLabel =
  | "service_address"
  | "account_number"
  | "invoice_date"
  | "due_date"
  | "total_amount"
  | "line_items"
  | "notice_block"
  | "ignore_zone"
  | "custom";

export type RegionSource = "user" | "ai" | "rules";

export type RegionBBox = {
  x: number;
  y: number;
  w: number;
  h: number;
};

export type RegionHint = {
  id: string;
  file_id: string;
  page_number: number;
  bbox: RegionBBox;
  label: RegionLabel;
  color?: string;
  notes?: string;
  source?: RegionSource;
  confidence?: number;
  created_at?: string;
  updated_at?: string;
};

export type RegionHintsResponse = {
  schema_version: number;
  regions: RegionHint[];
  updated_at?: string;
};

export type ManualReviewItem = {
  source_file: string;
  account_number: string;
  invoice_number: string;
  invoice_date: string;
  property_abbreviation: string;
  location: string;
  service_address: string;
  total_amount: number;
  line_count: number;
  reasons: string[];
  match_strategy: string;
  match_confidence: string;
  service_period_source: string;
};

export type ManualReviewResponse = {
  batch_id: string;
  items: ManualReviewItem[];
};

export type ExportResponse = {
  batch_id: string;
  exported: { vendor_key: string; filename: string; export_path: string }[];
  export_used_edited_rows?: boolean;
  edited_rows_count?: number;
  rows_written?: number;
};
