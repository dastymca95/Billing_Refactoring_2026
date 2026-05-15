# Phase AI-6 - Canonical Rules Engine + Universal Invoice Reasoning Pipeline

## 1. Current Failure Audit

Recent unseen invoices failed because AI extraction candidates were still being normalized through scattered fallback logic:

- invoice number formatting could be overridden by the old Formats policy
- required fields were not centrally enforced from one business rule source
- GL mapping could remain blank or depend on weak AI/category text
- property/location behavior varied by helper instead of by invoice category
- descriptions could be built from generic AI summaries instead of canonical rules
- Bulk Mode and Single Invoice Mode depended on preview rows that were not guaranteed to share a single normalized model
- AI prompts did not give the provider the complete ResMan rule contract

The failure points were in the AI prompt, `validate_ai_extraction`, GL/property fallback, description composition, required-column metadata, and final ResMan row construction.

## 2. Canonical Rules Import Strategy

Added a runtime importer for the operator workbook:

- Source workbook: `Canonica rules.xlsx`
- Runtime config: `config/canonical_rules.yaml`
- Import command: `python scripts/import_canonical_rules.py`

The Excel file remains the editable business matrix. The generated YAML is the backend source of truth.

## 3. canonical_rules.yaml Schema

The YAML now contains:

- `template_requirements.required_columns`
- `template_columns` behavior for each ResMan column
- `categories` for utilities, pest control, landscaping, marketing, subscriptions, trash collection services, other infrequent, and unknown
- category defaults such as trash GL `6940`
- category location policies
- imported Excel matrix text preserved under each column

Mandatory fields now include Invoice Number, Bill/Credit, Invoice Date, Accounting Date, Vendor, Invoice Description, Line Item Number, Property Abbreviation, GL Account, Line Item Description, Amount, Expense Type, Is Replacement Reserve, Due Date, and Document Url.

## 4. Universal Invoice Reasoning Pipeline

Added `webapp/backend/services/universal_invoice_reasoner.py`.

The pipeline model is:

1. ingest extraction candidates
2. validate AI payload
3. classify invoice category
4. apply Canonical Rules
5. validate Vendor List, property references, and Chart of Accounts
6. build canonical descriptions and line descriptions
7. enforce required fields
8. build ResMan rows

Dedicated deterministic processors remain untouched and still run before AI-assisted routing.

## 5. AI Prompt Changes

Updated text and vision prompts to include Canonical Rules guidance:

- AI must return candidates only
- GL Account must be numeric and valid, not vendor-side text
- Location must not be a raw full address
- previous balances/payments/remittance lines are not expenses
- required fields and category definitions are visible to the model
- descriptions are backend-owned and category-driven

## 6. Capital Waste Test Result

Added `scripts/smoke_capital_waste_invoice.py`.

Expected case now passes:

- category: `trash_collection_services`
- vendor: `Capital Waste Services`
- invoice number: `3150854`
- invoice date: `04/30/2026`
- due date: `05/30/2026`
- property: `RCC`
- location: blank
- GL: `6940`
- line amounts: `365.40` and `34.93`
- total: `400.33`
- invoice description: `05/01/26-05/31/26 - River Canyon Apartments`
- line descriptions include service period, property/site, and line detail
- payment/remittance text is not posted as an expense row

## 7. Category-Specific Behavior

Implemented initial category behavior:

- Utilities: service-period descriptions and utility GL candidates by service type
- Trash collection services: GL `6940`, blank Location allowed for property-level service, fuel/environmental fees use same GL, previous balance/payment ignored
- Pest/Landscaping/Marketing/Subscriptions: category slots and defaults ready for refinement
- Other infrequent/unknown: strict AI-assisted validation with review flags for unresolved vendor/property/GL

## 8. Required Field Enforcement

`template_rules.py` now reads canonical required columns before the older Formats config.

Rows missing required fields are marked as blocking review items. Missing Document Url is flagged before export when Dropbox/support linking has not produced a URL.

## 9. Single/Bulk Mode Consistency

The canonical layer runs before row construction, so Bulk Mode and Single Invoice Mode receive the same final row data and metadata. Description, GL, property, and required-field flags now come from the same normalized invoice model.

## 10. File Type Support Status

Current validated path:

- PDF digital
- scanned PDF via OCR/vision fallback
- PNG/JPG screenshots via image/vision path

Supported upload extensions already include XLSX/DOCX, but Word/Excel ingestion normalization was not expanded in this phase. Recommended next phase: normalize Word/Excel text extraction into the same universal candidate model.

## 11. Tests Performed

Passed:

- `python scripts\import_canonical_rules.py`
- `python scripts\smoke_canonical_rules_engine.py`
- `python scripts\smoke_capital_waste_invoice.py`
- `python scripts\smoke_ai_mock_provider.py`
- `python scripts\smoke_ai_openai_compatible_provider.py`
- `python scripts\smoke_ai_mapping_review.py`
- `python scripts\smoke_ai_vision_assist.py`
- `python -m compileall webapp\backend`
- `python scripts\verify_backend_routes.py`
- `cd webapp\frontend; npm.cmd run build`
- `cd webapp\frontend; npx.cmd tsc --noEmit`
- `cd webapp\frontend; npm.cmd run test:e2e` (`22 passed`, `1 skipped`)

## 12. Limitations

- The engine interprets Canonica rules and adds practical defaults, but more category-specific GL/property defaults should be filled over time in YAML rather than Python.
- Word/Excel invoice ingestion still needs a dedicated normalization phase.
- Existing Settings/Formats UI still exists, but backend required-column truth now prioritizes Canonical Rules.
- Capital Waste is covered by a deterministic smoke fixture; additional fixture smokes should be added for EPB, A-1, Servall, Rasa Floors, TK Elevator, and Lowe's screenshots.

## 13. Next Recommended Phase

Phase AI-7 should add a Canonical Rules Studio that edits `config/canonical_rules.yaml` directly, with:

- category rule editor
- vendor group editor
- GL group editor
- property override editor
- live Single Invoice preview
- fixture replay button for saved invoices
- safe import/export of canonical rules
