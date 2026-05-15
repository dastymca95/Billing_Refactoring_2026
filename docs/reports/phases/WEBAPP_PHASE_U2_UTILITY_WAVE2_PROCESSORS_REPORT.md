# Phase U2 - Utility Wave 2 Processor Report

Phase U2 migrated the useful business logic from five old utility scripts into the current webapp processor architecture. The old scripts were used as read-only references only. Legacy hardcoded paths, Dropbox setup, token handling, and direct workbook-writing behavior were not copied.

## 1. Vendors Analyzed

| Vendor | Training Evidence | Old Script Reference | U2 Status | Notes |
| --- | --- | --- | --- | --- |
| Alabama Power | 60 PDFs, 3 spreadsheets | `Old Scripts/Alabama_Power.py` | Active | Electric utility bill with account/month invoice number and proportional tax allocation. |
| EPB Fiber Optics | 23 PDFs, 2 spreadsheets | `Old Scripts/EPB_Fiber.py` | Active | Fiber/internet utility bill; parser reads only the statement summary block to avoid double-counting detail lines. |
| The City of Henderson | 66 PDFs, 1 spreadsheet, 1 note | `Old Scripts/Henderson Bills.py` | Active | Electric bill with state/school/911 fees allocated into the service line. |
| CDE Lightband | 81 PDFs, 2 spreadsheets | `Old Scripts/CDE Light Band.py` | Active | Electric bill with sales tax allocation and connection-fee path. |
| Nolin RECC Smarthub | 6 PDFs, 2 spreadsheets | `Old Scripts/Nolin REC.py` | Active | Community-style PDF with multiple sub-accounts; produces one invoice per sub-account. |

## 2. Old Scripts Reused as Reference

Reusable logic:
- Account number and service-period patterns.
- Vendor-specific date positions and invoice-number conventions.
- Property/location lookup clues from service addresses.
- Line item categories for electric, internet/fiber, connection fees, late fees, and utility taxes.
- Sample output expectations from training spreadsheets.

Rejected legacy logic:
- Hardcoded local folders and Dropbox paths.
- Direct Excel workbook writes during processing smoke tests.
- Embedded Dropbox/client setup patterns.
- One-off parsing branches that bypass the current canonical validation layer.

## 3. Processors Implemented

Implemented in `webapp/backend/services/utility_wave2_processors.py`:
- `process_alabama_power_batch`
- `process_epb_fiber_optics_batch`
- `process_the_city_of_henderson_batch`
- `process_cde_lightband_batch`
- `process_nolin_recc_smarthub_batch`

Integration points:
- `webapp/backend/services/vendor_detection.py` now recognizes all five vendors.
- `webapp/backend/services/batch_processor.py` routes all five active vendors to Wave 2 deterministic processors.
- `scripts/smoke_utility_processors.py` validates Wave 2 rows with strict required-field and utility-safety checks.

## 4. Vendors Promoted to Active

All five Wave 2 priority vendors were promoted to active because they have:
- Training data analyzed.
- Vendor YAML with `utility_processing.status: active`.
- Deterministic processor registered in the webapp router.
- Dry-run smoke test with preview rows.
- Mandatory-field validation.
- GL validation.
- Tax allocation with no standalone tax rows.
- Property/location validation.
- No Dropbox calls in dry-run.

## 5. Vendors Left Partial

None of the five U2 priority vendors remain partial. Other U1 vendors that were outside the Wave 2 priority list keep their prior statuses.

## 6. Vendors Needing More Training

No Wave 2 priority vendor was blocked for missing training data. Later utility waves should add broader fixtures for scanned/image variants and second/third sample documents per vendor.

## 7. Tax Allocation Behavior

Wave 2 follows the U1 utility rule:
- Standalone tax rows are forbidden.
- Tax and tax-like utility fees are allocated proportionally to non-tax current charge lines.
- Allocation reconciles exactly to cents.
- Allocation debug is preserved in processor metadata.

Vendor-specific notes:
- EPB Fiber tax/surcharges are allocated only from the statement summary block.
- Henderson KY tax, school tax, and 911 fee are folded into the electric service row.
- CDE and Alabama Power sales/gross-receipts tax are allocated into service lines.

## 8. Fee Behavior

- Connection/reconnection charges route to GL `6956` when present.
- Late fees do not use GL `6956`; they attach to the underlying service/vendor default or are flagged if uncertain.
- Previous balance and payment rows are ignored unless a vendor rule explicitly allows inclusion as current payable.
- Zero amount rows are excluded.

## 9. Community Billing Findings

Nolin RECC Smarthub has explicit vendor-level community billing behavior:
- `community_billing_rules.enabled: true`
- `master_invoice_strategy: separate_per_account`
- line item numbering is sequential per generated invoice

This behavior is intentionally not generalized to all utilities.

## 10. Smoke Results

Direct Wave 2 dry-run checks:

| Vendor | Result | Rows | Review Flags | Representative Invoice |
| --- | --- | ---: | ---: | --- |
| Alabama Power | PASS | 1 | 0 | `16834-38218 Apr 26` |
| EPB Fiber Optics | PASS | 4 | 0 | `C10515497 Apr 26` |
| The City of Henderson | PASS | 1 | 0 | `405170000-004 Mar 26` |
| CDE Lightband | PASS | 1 | 0 | `251072-001 Apr 26` |
| Nolin RECC Smarthub | PASS | 8 | 0 | `822029139 Mar 26` |

Full utility smoke:
- `python scripts/smoke_utility_processors.py`
- Result: PASS
- Validated 26 utility vendor YAML overlays.
- Confirmed Dropbox skipped in dry-run.

## 11. Regressions Checked

Existing active U1 processors remained covered by the full utility smoke:
- Atmos
- Columbia / CPWS
- Hardin
- HWEA
- McMinnville Electric
- Pennyrile
- Richmond
- Shelbyville

The full smoke also exercised the shared utility contract:
- canonical invoice-number formatting
- description composition
- tax allocation and rounding
- connect fee GL `6956`
- late fee guard against GL `6956`
- invalid GL and raw-address-in-location validation

## 12. HWEA Dry-Run Warning Fix

Fixed. The Hopkinsville Water Environment Authority dry-run path now returns:
- `resman_workbook_path: null`
- `manual_review_workbook_path: null`
- `debug_csv_path: null`

It also returns explicit dry-run planning fields:
- `would_write_workbook_path`
- `would_write_manual_review_workbook_path`
- `would_write_debug_csv_path`

Normal non-dry-run workbook behavior is preserved.

## 13. Tests Performed

Frontend:
- `npm.cmd run build` - PASS
- `npx.cmd tsc --noEmit` - PASS
- `npm.cmd run test:e2e` - PASS, 22 passed and 2 skipped

Backend:
- `python -m compileall webapp\backend` - PASS
- `python scripts\verify_backend_routes.py` - PASS
- `python scripts\smoke_canonical_rules_engine.py` - PASS
- `python scripts\smoke_canonical_invoice_fixtures.py` - PASS
- `python scripts\smoke_utility_processors.py` - PASS

Integrity:
- No source training bills modified.
- `Output/Template.xlsx` not modified.
- `.env` not modified.
- `Old Scripts/` not modified.
- No API keys exposed.
- No Dropbox calls during automated tests.

## 14. Limitations

- Wave 2 active status is based on available digital PDF samples and smoke coverage. Broader sample-by-sample golden fixtures should be added in a future wave.
- Scanned/image utility variants still depend on the existing ingestion/OCR/vision fallback path unless a vendor-specific scanned fixture is added.
- Vendor-specific edge cases such as unusual credits, disconnect notices, or multi-meter adjustments should be added as fixtures when encountered.

## 15. Next Wave Recommendation

Phase U3 should implement or harden remaining `needs_processor` vendors with enough training data:
- Clarksville Gas and Water
- Knoxville Utility Board
- Kentucky Utilities
- Tennessee American Water
- Union City Energy Authority
- Birmingham Water Works
- City of McMinnville Water and Sewer
- Guardian Water and Power
- Weakley County Municipal Electric System

U3 should also add multi-sample golden fixtures for every active U1/U2 utility vendor.
