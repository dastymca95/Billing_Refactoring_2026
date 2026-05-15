# Webapp Phase AI Formats Canonical Output + Required Fields Report

Date: 2026-05-12

## Scope

This focused patch investigated why an operator-configured Formats rule did not control the final ResMan template output after reprocessing an EPB bill.

The active operator rule was:

- Scope: specific vendor `EPB Fiber Optics`
- Document type: `Bill`
- Invoice number template: `{account_number}-{service_period_start_month3}-{service_period_end_year2}`

Expected final invoice number for the tested EPB bill:

- `C10181446-May-26`

## Root Cause

Formats was not the canonical source for final invoice number output.

The backend only generated a formatted invoice number when AI/OCR returned no invoice number. Because the EPB bill had a visible source invoice number (`18547827`), that extracted source value won and the configured operator format was ignored.

A second extraction issue was also found: the AI/text extraction returned account number `10181446`, while OCR contained the full account number `C10181446`. The formatting engine was correct, but it received a truncated account number.

## Fixes Implemented

1. Added `render_invoice_number(...)` to `webapp/backend/services/invoice_format_rules.py`.
   - Formats now renders the final ResMan invoice number.
   - Extracted source invoice numbers are preserved as provenance.

2. Updated `webapp/backend/services/ai_invoice_processor.py`.
   - Applies the active Formats rule before falling back to the source invoice number.
   - Stores:
     - `ai_invoice_number_policy_applied`
     - `ai_source_invoice_number`
   - Adds a review note explaining when policy formatting replaced the source invoice number.
   - Resolves account numbers from OCR to preserve visible alphanumeric prefixes like `C10181446`.

3. Added operator-configurable required template fields.
   - Stored under `template_requirements.required_columns` in `config/invoice_format_rules.yaml`.
   - Exposed through `/api/invoice-format-rules`.
   - Added UI checkboxes in `InvoiceFormatRulesStudio`.
   - Export validation now uses the Formats-required fields instead of a hardcoded list.

4. Updated smoke test expectations for current learned mapping behavior.
   - Learned vendor/GL mappings may now be visible during validation earlier than older tests expected.
   - The test still verifies invalid GL text is never accepted as the final GL value.

## EPB Verification

Batch tested:

- `batch_20260512_112326_990`

After reprocessing, preview rows now show:

- Invoice Number: `C10181446-May-26`
- Vendor: `EPB Fiber Optics`
- Property Abbreviation: `RCC`
- GL Account: `6920`
- Required fields missing: none

Provenance confirms:

- Source invoice number: `18547827`
- Policy-applied invoice number: `C10181446-May-26`

## Tests Performed

- `npm.cmd run build`
- `npx.cmd tsc --noEmit`
- `python -m compileall webapp\backend`
- `python scripts\verify_backend_routes.py`
- `python scripts\smoke_ai_openai_compatible_provider.py`
- `python scripts\smoke_ai_mapping_review.py`
- Direct format engine smoke: returned `C10181446-May-26`
- Live backend reprocess of EPB batch `batch_20260512_112326_990`
- Live preview required-field check: no configured required fields missing

## Notes

Location remains optional by default because the operator previously clarified that Location can be blank when no exact unit/location is known.

Property Abbreviation and GL Account remain configured as required fields.
