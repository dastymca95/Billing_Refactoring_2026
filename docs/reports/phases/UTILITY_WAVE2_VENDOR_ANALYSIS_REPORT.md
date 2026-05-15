# Utility Wave 2 Vendor Analysis Report

Phase U2 analyzed the five utility vendors that had both stronger training evidence and old-script references. Old scripts were read as reference only; no legacy paths, credentials, Dropbox code, or workbook-writing behavior was copied.

| Vendor | Training Folder | Training Files | Old Script | Current Processor | Document Type | Key Rules Reused Safely | Notes |
| --- | --- | ---: | --- | --- | --- | --- | --- |
| Alabama Power | `Training Bills_Invoices/Electricity - Power/Alabama Power` | 60 PDF, 3 spreadsheets | `Alabama_Power.py` | `webapp.backend.services.utility_wave2_processors` | Digital PDF | Account number, service period, current electric service, Alabama gross receipts tax, account/month invoice number | Tax is allocated into the electric service row; previous bill/payment rows are ignored. |
| EPB Fiber Optics | `Training Bills_Invoices/Electricity - Power/EPB Fiber Optics` | 23 PDF, 2 spreadsheets | `EPB_Fiber.py` | `webapp.backend.services.utility_wave2_processors` | Digital PDF | Account number, billing/due dates, billing date range, internet service lines, tax/surcharge allocation | The U2 parser now reads only the statement summary block so repeated detail lines are not double-counted. |
| The City of Henderson | `Training Bills_Invoices/Electricity - Power/City of Henderson` | 66 PDF, 1 spreadsheet, 1 note | `Henderson Bills.py` | `webapp.backend.services.utility_wave2_processors` | Digital PDF | Account, due date, service period, service address, current billing block, electric GL mapping | Kentucky/school/911 fees are folded into the electric row instead of exported as standalone tax/fee rows. |
| CDE Lightband | `Training Bills_Invoices/Electricity - Power/CDE Lightband` | 81 PDF, 2 spreadsheets | `CDE Light Band.py` | `webapp.backend.services.utility_wave2_processors` | Digital PDF | Account, statement/due dates, service period, service address, electric subtotal, sales tax | Connection fee path is available and maps to GL 6956; ordinary tax is allocated proportionally. |
| Nolin RECC Smarthub | `Training Bills_Invoices/Electricity - Power/Nolin RECC Smarthub` | 6 PDF, 2 spreadsheets | `Nolin REC.py` | `webapp.backend.services.utility_wave2_processors` | Digital PDF, master statement | Master statement parsing, sub-account invoice numbers, per-account service addresses, Final suffix | Community billing is vendor-specific: one invoice per sub-account, not one giant master invoice. |

## Old Script Findings

- All five old scripts contained useful parser/business rules for account numbers, service periods, GL selection, and support-document naming.
- The old scripts also contained legacy Dropbox/client setup, hardcoded local paths, and direct Excel-writing behavior. Those were deliberately not migrated.
- U2 centralizes the useful logic in `webapp/backend/services/utility_wave2_processors.py` and relies on shared U1 helpers for tax allocation, GL validation, property matching, and dry-run-safe output.

## Vendor-Specific Decisions

- **Alabama Power:** invoice number uses `{account_number} {service_period_end_month} {yy}`; current service plus tax reconciles to total current electric service.
- **EPB Fiber:** invoice number uses invoice/billing date month; internet/fiber rows use the configured internet service GL; previous balance/payment sections are ignored.
- **The City of Henderson:** service address drives property/location matching; Kentucky tax, school tax, and 911 fee are treated as allocation amounts, not standalone expense lines.
- **CDE Lightband:** statement date is the invoice/accounting date; date due is due date; sales tax is allocated into electric service.
- **Nolin RECC Smarthub:** master/community billing is explicitly enabled in YAML and produces separate invoices per sub-account.

## Dry-Run Smoke Findings

| Vendor | Smoke Result | Rows | Review Flags | Invoice Example | Property / Location | GL | Total Behavior |
| --- | --- | ---: | ---: | --- | --- | --- | --- |
| Alabama Power | PASS | 1 | 0 | `16834-38218 Apr 26` | `TGAP` / blank | `6915` | Electric service plus tax reconciles to `88.95`. |
| EPB Fiber Optics | PASS | 4 | 0 | `C10515497 Apr 26` | `TFF` / blank | `6960` | Internet/fiber charges and tax/surcharges reconcile to `467.86`. |
| The City of Henderson | PASS | 1 | 0 | `405170000-004 Mar 26` | `OC-CCA` / `3` | `6910` | Electric service plus state/school/911 allocations reconcile to `528.23`. |
| CDE Lightband | PASS | 1 | 0 | `251072-001 Apr 26` | `TPW` / blank | `6915` | Electric service plus sales tax reconciles to `591.62`. |
| Nolin RECC Smarthub | PASS | 8 | 0 | `822029139 Mar 26` | `VILLASPV` / unit-derived | `6920` | Each sub-account invoice reconciles independently. |

## Routing and Registration

- The five Wave 2 processors are registered in `webapp.backend.services.batch_processor` through lazy import of `webapp.backend.services.utility_wave2_processors`.
- Vendor detection now includes direct high-confidence detectors for Alabama Power, EPB Fiber, City of Henderson, CDE Lightband, and Nolin RECC.
- The YAML overlays for all five vendors are marked `status: active` only after dry-run smoke coverage passed.

## Gaps

- This wave is validated against the first available training sample per active vendor. Broader per-vendor fixture coverage should be added in a later utility wave.
- Image/scanned variants are accepted by config and ingestion, but the available Wave 2 samples were digital PDFs.
