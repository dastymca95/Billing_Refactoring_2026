# Phase AI-1.3 — Real Variable Invoice PDF Test

Date: 2026-05-10  
Project: Billing Refactoring Web Console  
Stack referenced: backend `http://localhost:8001`, frontend `http://localhost:5174`

## Scope

This phase tested the AI-assisted invoice processor against one real variable supplier PDF using the configured OpenAI-compatible provider. The test used one QA-created batch only and did not run bulk processing.

Guardrails observed:

- Did not change Richmond Utilities or HWEA deterministic processors.
- Did not trigger export.
- Did not modify `Output/Template.xlsx`.
- Did not modify the source training PDF.
- Did not expose API keys.
- Did not call Dropbox from the AI-assisted path.

## Provider Status

Live backend `/api/ai/status` returned:

- `enabled`: `true`
- `provider`: `openai_compatible`
- `model`: `deepseek-v4-flash`
- `configured`: `true`
- `supports_vision`: `false`
- `message`: `AI invoice processing is configured.`

No API key was returned by the status endpoint.

## Vendor Tested

Vendor/document source:

- Vendor folder: `Training Bills_Invoices/Building Supplies/Lowes Pro Supply`
- PDF: `0ba23822-877a-44cc-b616-6465a89da4b6.pdf`
- Size: `27,801` bytes
- Extracted text length sent to AI path: `1,104` characters
- Page images sent: none

The file was read and uploaded into a QA webapp batch. The source training file hash was checked before and after processing and remained unchanged.

## QA Batch

Created batch:

- `batch_id`: `batch_20260510_151125_482`
- Batch name: `QA AI 1.3 Lowes Pro Supply`
- Document mode: `digital_pdf`
- Uploaded file: `0ba23822-877a-44cc-b616-6465a89da4b6.pdf`

Vendor detection on the uploaded file:

```json
{
  "vendor_key": "lowes",
  "confidence": 0.85,
  "reason": "variable supplier invoice: Lowe's",
  "supported_in_phase_1": false,
  "processing_mode": "ai_assisted"
}
```

This confirmed the unknown/variable vendor path routed to AI-assisted processing.

## Processing Result

Normal API processing flow used:

- `POST /api/batches`
- `POST /api/batches/{batch_id}/upload`
- `GET /api/batches/{batch_id}/files`
- `POST /api/batches/{batch_id}/process?sync=1`
- `GET /api/batches/{batch_id}/preview`
- `GET /api/batches/{batch_id}/manual-review`
- `GET /api/batches/{batch_id}/revisions`

Processing summary:

```json
{
  "files_total": 1,
  "files_supported": 1,
  "files_unsupported": 0,
  "invoices_total": 1,
  "manual_review_total": 1
}
```

AI vendor summary:

```json
{
  "processing_mode": "ai_assisted",
  "files_total": 1,
  "files_processed": 1,
  "files_unsupported": 0,
  "invoices_produced": 1,
  "rows_total": 3,
  "line_items": 3,
  "manual_review_total": 1,
  "invoices_flagged_for_review": 1
}
```

Preview result:

- Invoice count: `1`
- Row count: `3`
- AI-generated row count: `3`
- Revision created: `rev_20260510T201155890795Z`

## Extraction Quality

Extracted invoice-level values:

- Vendor: `Lowe's`
- Invoice/reference number selected by AI: `83690`
- Invoice date: `05/09/2026`
- Due date: `06/08/2026`
- Account number: `201078`
- Total amount: `$6.75`
- Confidence: `0.90`
- Confidence source: provider

Validation summary:

```json
{
  "valid": true,
  "required_fields_present": true,
  "line_item_count": 2,
  "dates_valid": true,
  "total_reconciliation_passed": true,
  "reconciled_total": 6.75,
  "invoice_total": 6.75,
  "confidence": 0.9,
  "confidence_source": "provider"
}
```

Generated ResMan rows:

1. Promotional discount app — `$0.00`
2. 3-3/4-In (96mm) bar pull — `$6.16`
3. Sales tax synthetic reconciliation row — `$0.59`

The line-item subtotal plus sales tax reconciled to the invoice total.

## Manual Review Reasons

The batch did not crash and produced template rows, but it correctly flagged operator review.

Manual review reasons:

- AI warning: PO number OAKLEY CO204 present but not mapped.
- AI warning: Property candidate not identified from invoice; service address may not correspond to a known property.
- One or more line items have missing or unverified GL account candidates. Confirm GL mapping.
- Vendor `Lowe's` was extracted but was not found in the vendor reference. Confirm the vendor mapping.

Validation flags:

- `ai_warning_po_number_oakley_co204_present_but_not_mapped`
- `ai_warning_property_candidate_not_identified_from_invoice_service_addre`
- `ambiguous_gl_mapping`
- `vendor_mapping_not_found`

The rows were marked with `_meta.ai_generated=true`, AI confidence values, AI validation flags, and provenance pointing to the configured provider/model. Low-confidence styling was not triggered for these rows because row confidence ranged from `0.90` to `0.95`.

## Deterministic Vendor Bypass

Confirmed source-level routing for deterministic vendors:

```json
{
  "filename": "Richmond Utilities - Blue Country 4-6-26.pdf",
  "vendor_key": "richmond_utilities",
  "supported": true,
  "processor_registered": true,
  "processing_mode": "deterministic"
}
```

```json
{
  "filename": "HWEA - 4-20-26.pdf",
  "vendor_key": "hopkinsville_water_environment_authority",
  "supported": true,
  "processor_registered": true,
  "processing_mode": "deterministic"
}
```

This confirms Richmond and HWEA still bypass the AI-assisted route.

## Integrity Checks

`Output/Template.xlsx` SHA-256 before:

```text
b753f406c0222f150a9549065fc5c43168488353807ab45623ed2a5c3969c284
```

`Output/Template.xlsx` SHA-256 after:

```text
b753f406c0222f150a9549065fc5c43168488353807ab45623ed2a5c3969c284
```

Result: unchanged.

Source PDF SHA-256 remained unchanged:

```text
69a6e3aa17ebb90977f458d1b36dcd71dccac2eed126b581f1549040df9eb0bf
```

No export was run. No `Output/Template.xlsx` write occurred.

## Scan Overlay UX

The real provider processing flow was verified through the backend/API path. The browser-use in-app automation backend was unavailable in this session:

```text
Failed to connect to browser-use backend "iab". No Codex IAB backends were discovered.
```

The existing Playwright suite was used as fallback browser verification. The test `AI assisted processing shows the in-document scan overlay` passed, confirming the frontend still renders the AI scan overlay for AI-assisted processing state without external provider calls.

## Token / Cost Estimate

Exact provider token usage was not available from the current provider adapter response. The PDF text payload supplied to the AI path was `1,104` characters before prompt/reference context. The adapter does not persist token usage or cost metadata yet.

Recommended follow-up: capture provider `usage` from OpenAI-compatible responses when available and store a safe, non-secret per-run estimate in the processing metadata.

## Tests Performed

Frontend:

- `npm.cmd run build` — passed
- `npx.cmd tsc --noEmit` — passed
- `npm.cmd run test:e2e` — passed, 19 passed / 1 skipped

Backend:

- `python -m compileall webapp\backend` — passed
- `python scripts\verify_backend_routes.py` — passed
- `python scripts\smoke_ai_openai_compatible_provider.py` — passed

Additional smoke:

- Live `GET http://localhost:8001/api/ai/status` — configured, no key exposed
- Real QA batch create/upload/process/preview/manual-review/revisions with Lowe's Pro Supply PDF — passed
- Richmond/HWEA deterministic routing check — passed

## Limitations

- The AI selected `83690` as the invoice/reference number from the visible invoice text. The source text includes `Reference 983690`, so invoice-number selection should be reviewed by an operator.
- Vendor mapping did not resolve `Lowe's` to the vendor reference, so `vendor_mapping_not_found` is expected until vendor reference aliases are expanded.
- GL candidates were generated, but backend validation flagged them as unverified. This is correct for Phase AI-1.3 because AI suggestions are not trusted blindly.
- Browser-use in-app automation was unavailable; Playwright was used for visual overlay regression.
- Token/cost data is not yet captured from provider responses.

## Next Recommended Phase

Phase AI-1.4 should focus on AI extraction hardening for real supplier PDFs:

- Add vendor aliases for Lowe's / Lowe's Pro Supply in the vendor reference mapping.
- Improve invoice/reference number validation and ask the provider to preserve exact identifiers when only a reference number exists.
- Normalize common supplier GL candidates to real General Ledger entries through a backend mapping table.
- Capture provider token usage/cost metadata when the response includes it.
- Add a repeatable real-PDF smoke script that uses one QA batch and one user-approved variable supplier PDF.
