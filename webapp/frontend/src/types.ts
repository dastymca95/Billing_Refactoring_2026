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
  source_type?: string;
  file_support_status?: string;
  file_support_label?: string;
  file_support_reason?: string;
};

export type UploadFileProgress = {
  id: string;
  batchId: string;
  filename: string;
  size_bytes: number;
  extension: string;
  percent: number;
  status: "queued" | "uploading" | "saving" | "done" | "failed";
  error?: string;
};

export type FilesResponse = {
  batch_id: string;
  files: FileEntry[];
};

export type IngestionPreviewResponse = {
  batch_id: string;
  filename: string;
  source_type: string;
  mime_type: string;
  file_size_bytes: number;
  page_count: number;
  sheet_count: number;
  table_count: number;
  text_quality_score: number;
  extraction_quality: string;
  needs_ocr: boolean;
  needs_vision: boolean;
  warnings: string[];
  vendor_hint: string;
  category_hint: string;
  text_preview: string;
  tables_preview: {
    source: string;
    sheet_name?: string | null;
    page_number?: number | null;
    table_index: number;
    headers: string[];
    rows: unknown[][];
    confidence?: number | null;
    warnings: string[];
  }[];
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
  line_item_id?: string | null;
  row_index?: number;
  readiness_snapshot_id?: string;
  readiness_status?: "ready" | "needs_review" | "blocked";
  // Phase 2J — opaque ids of the extraction trace items (regions on
  // the source PDF) that fed this row. Drives the row ↔ overlay
  // highlight in the document viewer.
  trace_ids?: string[];
  // Phase AI-1 — provenance/validation metadata for AI-assisted supplier
  // invoices. These are intentionally optional so deterministic vendor rows
  // remain unchanged.
  ai_generated?: boolean;
  ai_confidence?: number;
  ai_confidence_low?: boolean;
  ai_validation_flags?: string[];
  ai_warnings?: string[];
  ai_provenance?: Record<string, unknown>;
  ai_mapping_provenance?: Record<string, unknown>[];
  ai_detected_vendor?: string | null;
  ai_property_candidate?: string | null;
  ai_raw_property_candidate?: string | null;
  ai_service_address?: string | null;
  ai_sold_to_raw_text?: string | null;
  ai_job_site_raw_text?: string | null;
  ai_source_gl_candidate?: string | null;
  ai_gl_accounting_reasoning?: Record<string, unknown> | null;
  accounting_decision?: Record<string, unknown> | null;
  semantic_classification?: Record<string, unknown> | null;
  document_facts?: Record<string, unknown> | null;
  ai_gl_accounting_confidence?: number | null;
  ai_line_semantics?: Record<string, unknown> | null;
  ai_line_activity?: string | null;
  ai_line_location?: string | null;
  ai_line_location_candidate?: string | null;
  ai_generated_description?: boolean;
  ai_item_plain_language_description?: string | null;
  ai_tax_handling?: string | null;
  ai_invoice_date_source?: string | null;
  ai_service_date?: string | null;
  ai_service_date_raw?: string | null;
  ai_payment_terms?: string | null;
  ai_due_date_text?: string | null;
  ai_unresolved_visual_field_candidates?: Record<string, unknown>[];
  ai_critical_header_verification?: Record<string, unknown>;
  ai_date_provenance?: Record<string, unknown>[];
  tenant_document_policy?: Record<string, unknown>;
  ai_row_identity_evidence?: Record<string, unknown>;
  row_identity_needs_confirmation?: boolean;
  ai_handwritten_row_identities?: Record<string, unknown>[];
  ai_row_identity_verification?: Record<string, unknown>;
  ai_excluded_paid_rows?: Record<string, unknown>[];
  ai_zero_amount_lines_excluded?: number;
  line_type?: string | null;
  line_classification?: string | null;
  line_classification_reason?: string | null;
  line_classification_keywords?: string[];
  source_charge_components?: unknown[];
  allocated_charge_components?: unknown[];
  tax_total?: string | null;
  tax_allocated?: string | null;
  human_adjudication_badges?: Record<string, HumanAdjudicationBadge[]>;
  human_adjudication_applied?: Record<string, {
    revision_id: string;
    revision_number: number;
    reviewer_id: string;
    created_at: string;
    rationale: string;
  }>;
};

export type HumanAdjudicationBadge =
  | "manually_corrected"
  | "benchmark_approved"
  | "learning_approved"
  | "governed_by_rule";

export type HumanAdjudicationOptions = {
  rationale: string;
  add_to_benchmark: boolean;
  approve_learning_example: boolean;
  propose_reusable_rule: boolean;
  bulk_scope_confirmed?: boolean;
  group_equivalent_corrections?: boolean;
};

export type KnowledgeHistoricalPrior = {
  dimension: "vendor" | "property" | "vendor_property";
  gl_code: string;
  count: number;
  amount: string;
  share: number;
  snapshot_id: string;
  authoritative: false;
};

export type KnowledgeLineContext = {
  contract_version: string;
  tenant_id: string;
  line_item_id: string;
  canonical_concept?: string | null;
  document_evidence: Record<string, unknown>;
  historical_profile_state: "ready" | "stale" | "not_generated" | "unavailable";
  historical_vendor_priors: KnowledgeHistoricalPrior[];
  historical_property_priors: KnowledgeHistoricalPrior[];
  vendor_property_joint_priors: KnowledgeHistoricalPrior[];
  similar_approved_learning_examples: {
    learning_example_id: string; revision_id: string; canonical_concept: string;
    document_family: string; line_family: string; trade_family: string; work_mode: string; gl_code: string;
    evidence_fingerprint: string; candidate_only: true;
  }[];
  active_governed_rules: {
    rule_id: string; version: number; title: string; status: string;
    allowed_gl_codes: string[]; scope: Record<string, unknown>;
    candidate_constraint_only: true;
  }[];
  contradictions: { code: string; message: string; source_ids: string[]; requires_review: boolean }[];
  confidence: number;
  provenance: { store: string; contract_version: string; source_id: string; immutable: boolean; tenant_id: string }[];
  benchmark_examples_visible_to_production: 0;
  selection_authority: false;
  export_authority: false;
};

export type KnowledgeImpactEstimate = {
  contract_version: string;
  invoice_corrections: number;
  benchmark_examples: number;
  learning_examples: number;
  learning_duplicates_avoided: number;
  rule_proposals: number;
  affected_rows: number;
  requires_bulk_scope_confirmation: boolean;
  statements: string[];
};

export type KnowledgeAnalytics = {
  contract_version: string;
  tenant_id: string;
  historical_gl_distribution: Record<string, number>;
  approved_export_gl_distribution: Record<string, number>;
  posted_gl_distribution: Record<string, number>;
  final_posted_gl_distribution: Record<string, number>;
  ai_prediction_distribution: Record<string, number>;
  human_correction_distribution: Record<string, number>;
  disagreement_rate: number;
  approved_benchmark_count: number;
  approved_learning_count: number;
  active_rule_count: number;
  rule_coverage: number;
  correction_drift_over_time: {
    window_type: "calendar_month_utc"; window_start: string; month: string; corrections: number;
  }[];
  promotion_thresholds?: Record<string, number>;
  promotion_candidates?: {
    canonical_concept: string; gl_code: string; correction_count: number;
    distinct_invoice_count: number; approved_learning_count: number;
    eligible_for_learning_review: boolean; eligible_for_rule_simulation: boolean;
    automatic_promotion: false;
  }[];
};

export type HumanAdjudicationContext = {
  contract_version: string;
  reviewer_id: string;
  role: "property_manager" | "accountant_ap" | "accounting_manager_controller" | "platform_admin";
  tenant_id: string;
  permissions: {
    invoice_correction: boolean;
    benchmark_submission: boolean;
    learning_approval: boolean;
    rule_proposal: boolean;
    rule_approval: boolean;
    shared_knowledge_promotion: boolean;
  };
};

export type AiVendorCandidate = {
  vendor_name: string;
  vendor_id: string;
  score: number;
  reason: string;
  learned?: boolean;
};

export type AiVendorCandidatesResponse = {
  detected_vendor: string;
  normalized_detected_vendor: string;
  candidates: AiVendorCandidate[];
  needs_confirmation: boolean;
};

export type AiGlCandidate = {
  gl_account: string;
  gl_code: string;
  gl_name: string;
  score: number;
  reason: string;
  learned?: boolean;
  valid?: boolean;
};

export type AiGlCandidatesResponse = {
  line_item_description: string;
  amount: number | null;
  vendor_name: string;
  ai_suggested_gl: string;
  candidates: AiGlCandidate[];
  needs_confirmation: boolean;
};

export type AiPropertyCandidate = {
  property_abbreviation: string;
  property_name: string;
  location: string;
  address: string;
  score: number;
  reason: string;
};

export type AiPropertyCandidatesResponse = {
  query: string;
  service_address: string;
  candidates: AiPropertyCandidate[];
  needs_confirmation: boolean;
};

export type AiLocationCandidate = {
  property_abbreviation: string;
  property_name: string;
  location: string;
  address: string;
};

export type AiLocationCandidatesResponse = {
  property_abbreviation: string;
  query: string;
  locations: AiLocationCandidate[];
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
  ai_generated?: boolean;
  ai_confidence?: number | null;
  ai_validation_flags?: string[];
  ai_warnings?: string[];
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

export type ReadinessIssue = {
  code: string;
  severity: "blocking" | "non_blocking" | "info";
  scope: string;
  invoice_id?: string | null;
  line_item_id?: string | null;
  field?: string | null;
  message: string;
  source: string;
  evidence: Record<string, unknown>[];
  resolution_required: boolean;
  resolved: boolean;
  resolved_by?: string | null;
  resolved_at?: string | null;
  resolution_evidence?: Record<string, unknown> | null;
};

export type AccountingReadiness = {
  contract_version: string;
  snapshot_id: string;
  status: "ready" | "needs_review" | "blocked";
  export_allowed: boolean;
  blockers: ReadinessIssue[];
  non_blocking_issues: ReadinessIssue[];
  validated_fields: Record<string, boolean>;
  reconciliation_status: string;
  duplicate_status: string;
  evaluated_at: string;
};

export type AccountingAssistantCorrection = {
  row_index: number;
  field: "GL Account" | "Property Abbreviation" | "Location" | "Invoice Description" | "Line Item Description";
  new_value: string;
  rationale: string;
  evidence: string[];
};

export type AccountingRuleScope = {
  document_family?: string | null;
  line_family?: string | null;
  trade_family?: string | null;
  work_mode?: string | null;
  description_terms: string[];
  term_match: "any" | "all";
};

export type AccountingRuleConstraint = {
  allowed_gl_codes: string[];
  minimum_gl_code?: string | null;
  maximum_gl_code?: string | null;
};

export type OperatorAccountingRule = {
  contract_version: string;
  rule_id: string;
  title: string;
  description: string;
  scope: AccountingRuleScope;
  constraint: AccountingRuleConstraint;
  status: "draft" | "active" | "disabled" | "rejected";
  created_at: string;
  updated_at: string;
  approved_by?: string | null;
  approved_at?: string | null;
  source_interaction_id?: string | null;
  audit: {
    event: string;
    actor: string;
    at: string;
    details: Record<string, unknown>;
  }[];
};

export type TenantVendorEntity = {
  contract_version: string;
  tenant_id: string;
  conversation_mode: "lightweight" | "advisory" | "action";
  action_extraction_status: "not_requested" | "succeeded" | "failed_safe";
  vendor_entity_id: string;
  canonical_name: string;
  erp_vendor_id?: string | null;
  aliases: string[];
  created_at: string;
  updated_at: string;
  audit: { event: string; actor: string; at: string; details: Record<string, unknown> }[];
};

export type ResManDatasetKind =
  | "vendors"
  | "properties_units"
  | "gl_accounts"
  | "general_ledger"
  | "invoice_history";

export type ResManSnapshot = {
  contract_version: string;
  snapshot_id: string;
  import_id: string;
  tenant_id: string;
  dataset: ResManDatasetKind;
  original_filename: string;
  sha256: string;
  record_count: number;
  created_at: string;
  activated_at: string;
  active: boolean;
};

export type ResManDatasetStatus = {
  contract_version: string;
  tenant_id: string;
  dataset: ResManDatasetKind;
  current_snapshot?: ResManSnapshot | null;
  effective_record_count: number;
  manual_overlay_count: number;
  staged_import_count: number;
};

export type ResManImportPreview = {
  contract_version: string;
  import_id: string;
  tenant_id: string;
  dataset: ResManDatasetKind;
  original_filename: string;
  sha256: string;
  size_bytes: number;
  parsed_records: number;
  added_records: number;
  changed_records: number;
  removed_records: number;
  unchanged_records: number;
  sample_records: Record<string, unknown>[];
  issues: { code: string; severity: "error" | "warning" | "info"; message: string; source_row?: number | null }[];
  excluded_sensitive_columns: string[];
  status: "preview_ready" | "invalid";
  created_at: string;
};

export type ResManContextRecord = Record<string, unknown> & {
  _record: {
    natural_key: string;
    source_kind: "resman_import" | "manual_overlay";
    source_snapshot_id?: string | null;
    updated_at?: string;
  };
};

export type ResManRecordPage = {
  contract_version: string;
  tenant_id: string;
  dataset: ResManDatasetKind;
  page: number;
  page_size: number;
  total: number;
  items: ResManContextRecord[];
};

export type ContextFrequencyItem = {
  key: string;
  label: string;
  count: number;
  amount: string;
  share: number;
};

export type DeterministicPatternField = {
  path: string;
  label: string;
  values: string[];
  editable: boolean;
};

export type DeterministicCoverage = {
  contract_version: string;
  vendor_key: string;
  display_name: string;
  aliases: string[];
  status: "active" | "inactive" | "registered_unavailable";
  implementation_kind: "hybrid" | "code_managed";
  processor_module: string;
  processor_entrypoint: string;
  processor_available: boolean;
  config_present: boolean;
  config_name?: string | null;
  editable: boolean;
  pattern_count: number;
  patterns: DeterministicPatternField[];
  failure_code?: string | null;
};

export type DeterministicBuilderSample = {
  sample_id: string;
  original_filename: string;
  source_type: string;
  size_bytes: number;
  page_count: number;
  sha256: string;
  text_available: boolean;
  warnings: string[];
  uploaded_at: string;
};

export type DeterministicBuilderMessage = {
  message_id: string;
  role: "user" | "assistant" | "system";
  content: string;
  created_at: string;
  provider_profile_id?: string | null;
  estimated_cost_usd: number;
  proposed_paths: string[];
};

export type DeterministicBuilderPreview = {
  status: "not_run" | "passed" | "failed";
  revision: number;
  columns: string[];
  rows: Record<string, unknown>[];
  row_count: number;
  warnings: string[];
  generated_at?: string | null;
};

export type DeterministicBuilderSession = {
  contract_version: string;
  session_id: string;
  vendor_key: string;
  vendor_name: string;
  status: "draft" | "previewed" | "approved" | "rejected";
  revision: number;
  selected_column?: string | null;
  samples: DeterministicBuilderSample[];
  messages: DeterministicBuilderMessage[];
  draft_patch: Record<string, unknown>;
  draft_rationales: Record<string, string>;
  validation_issues: { path?: string; message?: string }[];
  preview: DeterministicBuilderPreview;
  created_at: string;
  updated_at: string;
  audit: Record<string, unknown>[];
};

export type VendorContextProfile = {
  vendor_key: string;
  vendor_name: string;
  vendor_abbreviation?: string | null;
  active: boolean;
  invoice_count: number;
  allocation_count: number;
  ledger_posting_count: number;
  ledger_total_amount: string;
  active_months: number;
  history_span_months: number;
  total_amount: string;
  average_invoice_amount: string;
  top_gl_share: number;
  top_property_share: number;
  gl_usage: ContextFrequencyItem[];
  property_usage: ContextFrequencyItem[];
  property_gl_usage: Record<string, ContextFrequencyItem[]>;
  first_accounting_date?: string | null;
  last_accounting_date?: string | null;
  statistical_score: number;
  recommended_mode: "deterministic_candidate" | "review_candidate" | "variable" | "insufficient_history";
  recommendation_reasons: string[];
  governance_status: "unreviewed" | "approved_candidate" | "excluded" | "needs_review";
  reviewer_notes?: string | null;
  reviewed_by?: string | null;
  reviewed_at?: string | null;
  deterministic_coverage?: DeterministicCoverage | null;
};

export type PropertyContextProfile = {
  property_key: string;
  property_name: string;
  property_code?: string | null;
  invoice_count: number;
  allocation_count: number;
  ledger_posting_count: number;
  total_amount: string;
  gl_usage: ContextFrequencyItem[];
  vendor_usage: ContextFrequencyItem[];
};

export type ContextIntelligenceSnapshotSummary = {
  contract_version: string;
  analytics_version: string;
  snapshot_id: string;
  tenant_id: string;
  generated_at: string;
  generated_by: string;
  source_hashes: Record<string, string>;
  vendor_count: number;
  property_count: number;
  invoice_count: number;
  allocation_count: number;
  gl_account_count: number;
  ledger_record_count: number;
  deterministic_candidate_count: number;
  review_candidate_count: number;
};

export type ContextIntelligenceStatus = {
  contract_version: string;
  tenant_id: string;
  state: "not_generated" | "ready" | "stale";
  required_datasets: ResManDatasetKind[];
  missing_datasets: ResManDatasetKind[];
  current_source_hashes: Record<string, string>;
  snapshot?: ContextIntelligenceSnapshotSummary | null;
};

export type ContextMatrixPage = {
  contract_version: string;
  snapshot_id: string;
  tenant_id: string;
  page: number;
  page_size: number;
  total: number;
  items: (VendorContextProfile | PropertyContextProfile)[];
};

export type TenantPolicyScope = {
  vendor_entity_id?: string | null;
  property_ids: string[];
  document_family?: string | null;
  line_family?: string | null;
  trade_family?: string | null;
  work_mode?: string | null;
  description_terms: string[];
  term_match: "any" | "all";
};

export type TenantPolicyAction = {
  allowed_gl_codes: string[];
  expected_amount?: string | number | null;
  amount_tolerance: string | number;
  amount_mismatch_behavior: "review" | "warning";
};

export type TenantPolicySimulationReport = {
  contract_version: string;
  simulation_id: string;
  tenant_id: string;
  policy_id: string;
  policy_version: number;
  snapshot_id: string;
  evaluated_lines: number;
  matched_lines: number;
  would_constrain_lines: number;
  unchanged_lines: number;
  amount_mismatches: number;
  blocking_conflicts: number;
  missing_vendor_identity: number;
  examples: Record<string, unknown>[];
  simulated_at: string;
  simulated_by: string;
};

export type TenantAccountingPolicy = {
  contract_version: string;
  tenant_id: string;
  policy_id: string;
  version: number;
  title: string;
  description: string;
  policy_type: "semantic_gl" | "vendor_service_gl";
  scope: TenantPolicyScope;
  action: TenantPolicyAction;
  status: "draft" | "simulated" | "active" | "disabled" | "rejected" | "superseded";
  created_at: string;
  updated_at: string;
  approved_by?: string | null;
  approved_at?: string | null;
  source_interaction_id?: string | null;
  latest_simulation?: TenantPolicySimulationReport | null;
  audit: { event: string; actor: string; at: string; details: Record<string, unknown> }[];
};

export type AccountingAssistantChatResult = {
  contract_version: string;
  interaction_id: string;
  batch_id: string;
  invoice_group_id: string;
  tenant_id: string;
  assistant_message: string;
  corrections: AccountingAssistantCorrection[];
  proposed_rule?: OperatorAccountingRule | null;
  proposed_tenant_policy?: TenantAccountingPolicy | null;
  requires_correction_confirmation: boolean;
  requires_rule_confirmation: boolean;
  requires_tenant_policy_simulation: boolean;
  accounting_readiness_changed: false;
  export_authorized: false;
  provider_profile_id: string;
  estimated_cost_usd: number;
  created_at: string;
  correction_status: "not_applicable" | "pending" | "applied" | "rejected";
  corrections_decided_at?: string | null;
  corrections_decided_by?: string | null;
};

export type AccountingAssistantInteraction = {
  user_message: string;
  result: AccountingAssistantChatResult;
};

export type ApprovedInvoiceCorrection = {
  contract_version: string;
  correction_id: string;
  interaction_id: string;
  batch_id: string;
  invoice_group_id: string;
  local_row_index: number;
  line_fingerprint: string;
  field: AccountingAssistantCorrection["field"];
  new_value: string;
  rationale: string;
  evidence: string[];
  approved_by: string;
  approved_at: string;
  status: "active" | "revoked";
};

export type OperatorActivityEvent = {
  contract_version: string;
  event_id: string;
  batch_id: string;
  invoice_group_id?: string | null;
  event_type: string;
  source: "manual" | "ai" | "rule" | "system";
  actor: string;
  summary: string;
  details: Record<string, unknown>;
  created_at: string;
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
  accounting_readiness?: AccountingReadiness;
  invoice_readiness?: Record<string, AccountingReadiness>;
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
  // Phase AI-1 — surfaced by the backend only while an AI-assisted run is
  // active. Unknown fields are tolerated by older progress snapshots.
  processing_mode?: "deterministic" | "ai_assisted" | "hybrid" | string;
  ai_stage?: string;
  ai_enabled?: boolean;
  ai_disabled_reason?: string;
};

// Phase 1H — batch document mode + AI fallback policy.
export type DocumentMode =
  | "digital_pdf"
  | "scanned_pdf"
  | "screenshot_image"
  | "mixed_pdf"
  | "csv_excel"
  | "auto_detect";

export type AiFallbackPolicy =
  | "never"
  | "only_low_confidence"
  | "only_manual_review"
  | "always_assist";

// Cost authorization for document processing.  This is distinct from
// DocumentMode (physical input format) and from AccountingReadiness.
export type ProcessingRouteMode =
  | "auto_cost_safe"
  | "deterministic_only"
  | "ai_fallback_allowed";

export type EffectiveProcessingRoute = "deterministic" | "ai" | "blocked";

export type ProcessingRouteResolution = {
  contract_version: string;
  batch_id: string;
  filename?: string | null;
  page?: number | null;
  requested_mode: ProcessingRouteMode;
  inherited_from: "page" | "document" | "batch" | "default";
  configured_by?: string | null;
  configured_at?: string | null;
};

export type ProcessingRouteDecision = {
  contract_version: string;
  policy_contract_version: string;
  batch_id: string;
  filename?: string | null;
  page?: number | null;
  requested_mode: ProcessingRouteMode;
  inherited_from: "page" | "document" | "batch" | "default";
  effective_route: EffectiveProcessingRoute;
  deterministic_available: boolean;
  vendor_key?: string | null;
  processor_id?: string | null;
  ai_fallback_authorized: boolean;
  reason_code: string;
};

export type ProcessingRouteDocument = {
  filename: string;
  detection: {
    vendor_key?: string | null;
    confidence?: number | null;
    reason?: string | null;
  };
  decision: ProcessingRouteDecision;
};

export type ProcessingRoutePage = {
  filename: string;
  page: number;
  decision: ProcessingRouteDecision;
};

export type ProcessingRouteSnapshot = {
  contract_version: string;
  policy_version: string;
  batch: { resolution: ProcessingRouteResolution };
  documents: ProcessingRouteDocument[];
  pages: ProcessingRoutePage[];
  audit: Array<Record<string, unknown>>;
};

export type ProcessingRouteUpdate = {
  scope: "batch" | "document" | "page";
  mode: ProcessingRouteMode | null;
  filename?: string;
  page?: number;
  actor?: string;
  reset_exceptions?: boolean;
  expected_policy_version?: string;
};

export const DOCUMENT_MODES: DocumentMode[] = [
  "auto_detect",
  "digital_pdf",
  "scanned_pdf",
  "screenshot_image",
  "mixed_pdf",
  "csv_excel",
];

export const DOCUMENT_MODE_LABELS: Record<DocumentMode, string> = {
  auto_detect: "Auto-detect",
  digital_pdf: "Digital PDFs",
  scanned_pdf: "Scanned PDFs",
  screenshot_image: "Screenshots / Photos",
  mixed_pdf: "Mixed PDFs",
  csv_excel: "CSV / Excel",
};

export const DOCUMENT_MODE_DESCRIPTIONS: Record<DocumentMode, string> = {
  auto_detect: "Let the system pick per-file. Safe default.",
  digital_pdf: "Text-based bills (e.g. UtilityBill_05_2026.pdf).",
  scanned_pdf: "Image scans of bills — OCR is required.",
  screenshot_image: "Receipt screenshots or phone photos for AI vision assist.",
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
  provider: string | null;
  model?: string | null;
  configured: boolean;
  supports_vision?: boolean;
  vision_enabled?: boolean;
  vision_provider?: string | null;
  vision_model?: string | null;
  vision_mode?: string | null;
  message?: string;
  reason: string;
  policy?: string;
  max_cost_per_batch_usd?: number;
  allowed_tasks?: string[];
};

export type AiVisionAssistResponse = {
  dry_run: boolean;
  provider: string | null;
  model: string | null;
  vision_enabled: boolean;
  vision_mode: string;
  extraction: Record<string, unknown>;
  validation: {
    valid: boolean;
    manual_review_reasons: string[];
    manual_review_codes: string[];
    warnings: string[];
    row_count: number;
    total_amount: number | string | null;
    confidence?: number;
    text_vision_agreement_fields?: string[];
    text_vision_conflict_fields?: string[];
    [key: string]: unknown;
  };
  normalized: Record<string, unknown>;
  trace_regions: TraceItem[];
};

// Phase 1H — region hints. Coordinates are normalized to [0,1].
export type InvoiceFormatRuleScopeType =
  | "general"
  | "vendor"
  | "vendor_group"
  | "property"
  | "property_group"
  | "gl_account"
  | "gl_group";

export type InvoiceFormatRule = {
  id: string;
  name: string;
  enabled: boolean;
  priority: number;
  scope: {
    type: InvoiceFormatRuleScopeType;
    value: string;
  };
  document_type: "any" | "bill" | "invoice";
  templates: {
    invoice_number: string;
    invoice_description: string;
    line_item_description: string;
  };
};

export type InvoiceFormatRulesConfig = {
  version: number;
  updated_at?: string;
  description?: string;
  rule_priority?: string[];
  template_requirements?: {
    required_columns: string[];
  };
  groups: {
    vendor_groups: Record<string, { label: string; vendors: string[] }>;
    gl_groups: Record<string, { label: string; gl_accounts: string[] }>;
    property_groups: Record<string, { label: string; properties: string[] }>;
  };
  rules: InvoiceFormatRule[];
};

export type InvoiceFormatRulesPayload = {
  config: InvoiceFormatRulesConfig;
  template_columns: string[];
  references: {
    vendors: { vendor_name: string; vendor_id: string; status: string; default_gl?: string }[];
    gl_accounts: { gl_code: string; gl_name: string; type: string }[];
    properties: { property_abbreviation: string; property_name: string }[];
  };
  variables: { key: string; label: string }[];
  presets: Record<string, { label: string; template: string; description: string }[]>;
  scope_types: { value: InvoiceFormatRuleScopeType; label: string }[];
};

export type CanonicalRuleGroup = {
  key: string;
  title: string;
  items: string[];
};

export type CanonicalCategorySummary = {
  key: string;
  label: string;
  summary: string[];
  group_count: number;
  editable: boolean;
};

export type CanonicalRulesPayload = {
  source: Record<string, unknown>;
  required_columns: string[];
  optional_columns: string[];
  categories: CanonicalCategorySummary[];
  variables: { key: string; label: string }[];
};

export type CanonicalCategoryEditable = {
  labels: string[];
  vendor_keywords: string[];
  service_keywords: string[];
  default_gl_candidates: Record<string, string>;
  fee_handling: Record<string, string>;
  ignore_line_keywords: string[];
  location_policy: string;
  use_ai: boolean;
  require_vendor_validation: boolean;
  require_gl_validation: boolean;
  invoice_description_format: string;
  line_item_description_format: string;
};

export type CanonicalCategoryPayload = {
  category: CanonicalCategorySummary;
  groups: CanonicalRuleGroup[];
  editable: CanonicalCategoryEditable;
  validation: CanonicalRulesValidationResponse;
};

export type CanonicalRulesValidationResponse = {
  ok: boolean;
  issues: { severity: string; path: string; message: string }[];
};

export type CanonicalRulesTestBenchResponse = {
  ok: boolean;
  skipped?: boolean;
  skip_reason?: string;
  dry_run: boolean;
  fixture_key?: string;
  test_case: string;
  title: string;
  expected: Record<string, unknown>;
  actual: Record<string, unknown>;
  checks: {
    group?: string;
    field: string;
    expected: unknown;
    actual: unknown;
    pass: boolean;
    reason?: string;
    required?: boolean;
  }[];
  extracted_candidates: Record<string, unknown>;
  canonical_application: Record<string, unknown>;
  rows: PreviewRow[];
  review_flags: Record<string, unknown>[];
  reasoning_timeline: { step: string; detail: string }[];
};

export type CanonicalFixtureSummary = {
  key: string;
  vendor: string;
  category: string;
  description: string;
  status: "complete" | "incomplete" | string;
  requires_live_ai: boolean;
  skip_reason?: string;
  last_result?: Record<string, unknown> | null;
};

export type CanonicalRulesFixtureList = {
  fixtures: CanonicalFixtureSummary[];
};

export type CanonicalRulesRunAllResponse = {
  ok: boolean;
  results: CanonicalRulesTestBenchResponse[];
  summary: { fixture_key: string; status: string; failed_checks: string[]; skip_reason?: string }[];
};

export type CanonicalRulesImportPreview = {
  ok: boolean;
  excel_path: string;
  changed_categories: string[];
  imported_rows: number;
  validation: CanonicalRulesValidationResponse;
};

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
  document_url_warnings?: string[];
  document_url_updates?: {
    by_source_file?: Record<string, string>;
    by_invoice_number?: Record<string, string>;
  };
  accounting_readiness?: AccountingReadiness;
};

export type BillingV2ProcessorAuditEntry = {
  vendor_key: string;
  entrypoint: string;
  module: string;
  deterministic: boolean;
  available: boolean;
  error?: string;
};

export type BillingV2AuditResponse = {
  generated_at: string;
  count: number;
  available_count: number;
  processors: BillingV2ProcessorAuditEntry[];
  ai_fallback_module: {
    module: string;
    available: boolean;
    error?: string;
  };
};

export type BillingV2PrepareLinksResponse = {
  batch_id: string;
  prepared: boolean;
  changed?: boolean;
  reason?: string;
  cache_path?: string;
  rows_total: number;
  rows_with_links: number;
  rows_missing_links: number;
  links: {
    local_webapp: number;
    dropbox: number;
    external: number;
    missing: number;
  };
  audit_dir?: string;
};
