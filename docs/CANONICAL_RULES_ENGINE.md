# Canonical Rules Engine

The Canonical Rules Engine is the runtime source of truth for AI-assisted and
fallback invoice processing. AI extracts candidates. The backend applies these
rules before it builds ResMan template rows.

## Runtime Sources

- Operator workbook: `Canonica rules.xlsx`
- Runtime YAML: `config/canonical_rules.yaml`
- Import command: `python scripts/import_canonical_rules.py`

The Excel workbook is preserved as the human-authored matrix. The YAML is the
machine-readable file used by the webapp.

## Pipeline

1. Normalize the source file into a `DocumentCandidate`.
2. Ingest document text/OCR/vision candidates.
3. Extract raw invoice candidates.
4. Classify invoice category from canonical categories.
5. Apply category rules for required fields, descriptions, GL, property, and tax.
6. Validate against Vendor List, Unit Info/Properties, Chart of Accounts, and learned mappings.
7. Build one normalized invoice model.
8. Generate Bulk Mode rows and Single Invoice Mode from that same model.
9. Block export when required fields are missing.

## Universal Document Ingestion

Phase AI-9 adds a document-only normalization layer before canonical reasoning.
The service lives in `webapp/backend/services/document_ingestion.py` and returns
a `DocumentCandidate` with:

- source type: digital PDF, scanned PDF, image/screenshot, Excel, CSV, Word, unsupported, or internal template ignored
- file metadata: MIME type, size, page count, sheet count
- extracted text, per-page candidates, table candidates, image candidates, quality score, and warnings
- vendor/category hints for downstream reasoning only

Ingestion does not choose final vendors, GL accounts, properties, locations, or
ResMan rows. It also does not call external AI. AI text and vision paths consume
the normalized candidate only when the routing layer allows them.

Supported levels:

- Supported: digital PDFs, `.xlsx`, `.csv`, `.docx`
- Supported with OCR/vision caveats: scanned PDFs, PNG/JPG/WebP screenshots/images
- Partial/limited: legacy `.xls`
- Unsupported: legacy `.doc` and unknown binary formats
- Protected: `Output/Template.xlsx` is identified as an internal template and ignored as an invoice source

Diagnostic endpoint:

```powershell
Invoke-RestMethod "http://localhost:8001/api/batches/<batch_id>/files/<filename>/ingestion-preview"
```

The preview caps text/table output and never runs AI, Dropbox, or exports.

## Required Fields

Canonical required ResMan fields are:

- Invoice Number
- Bill or Credit
- Invoice Date
- Accounting Date
- Vendor
- Invoice Description
- Line Item Number
- Property Abbreviation
- GL Account
- Line Item Description
- Amount
- Expense Type
- Is Replacement Reserve
- Due Date
- Document Url

`Location` is optional and must be a valid unit/location when used. Raw full
addresses are not written to the Location column.

## Category Rules

The engine currently includes runtime behavior for:

- Utilities
- Pest Control
- Landscaping
- Marketing
- Subscriptions
- Trash collection services
- Other infrequent maintenance/supplies/purchases
- Unknown

Trash collection services have a category default of GL `6940` and allow blank
Location for property-level services. Previous balances, payments, autopay, and
remittance lines are not expense rows.

## Utility Processing Contract

Phase U1 adds a shared deterministic utility-processing contract for electric,
gas, water/sewer, wastewater, stormwater, fiber/internet, and related community
utility bills.

Runtime sources:

- Shared helpers: `webapp/backend/services/utility_processor_common.py`
- Vendor overlays: `config/vendors/<vendor_key>.yaml` under `utility_processing`
- Compatibility imports for vendor processors:
  - `utils/utility_bill_parser.py`
  - `utils/utility_tax_allocator.py`
  - `utils/utility_invoice_number.py`
  - `utils/utility_line_classifier.py`

Core utility rules:

- Tax is allocated proportionally across current taxable service lines.
- Standalone tax rows are forbidden in final ResMan utility rows.
- Connection/reconnection/utility transfer fees use GL `6956`.
- Late fees never use GL `6956`; they use the underlying service/default GL or trigger review.
- Previous balance and payment rows are not expense rows unless a vendor rule explicitly permits balance-forward treatment.
- Property Abbreviation is mandatory; Location is blank unless a valid Unit Info Clean location is found.
- Raw full addresses are never written into Location.
- Fiber/internet defaults to GL `6960`; cable defaults to `6905`; electric defaults to `6915`/`6920`; water/sewer defaults to `6955`; trash defaults to `6940`.
- Utility invoice-number default is `{account_number} {service_month_abbrev_title} {service_year_yy}`, for example `341340.0094 Apr 26`.

Run the utility smoke:

```powershell
python scripts\smoke_utility_processors.py
```

For a fast contract-only check:

```powershell
python scripts\smoke_utility_processors.py --contract-only
```

## AI Contract

AI prompts now include a canonical summary. The provider must return candidates
with evidence and confidence, not final ResMan rows. The backend owns final
formatting and validation.

AI must not:

- invent missing values
- place vendor-side text such as `MISCELLANEOUS` in GL Account
- place full addresses in Location
- use previous balances/payments as expenses
- produce vague final descriptions

## Smoke Tests

Run:

```powershell
python scripts\smoke_canonical_rules_engine.py
python scripts\smoke_capital_waste_invoice.py
```

The Capital Waste smoke test is the current reference behavior for human-style
reasoning: category, property, GL, line items, descriptions, totals, and ignored
payment/balance lines must match the expected ResMan output.
