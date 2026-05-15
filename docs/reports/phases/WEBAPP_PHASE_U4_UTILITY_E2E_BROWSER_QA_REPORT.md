# Phase U4 - Utility End-to-End Browser QA + Golden Output Review

Date: 2026-05-14

## 1. Vendors Tested

Representative utility QA batches were created in `webapp_data` using dry-run processing and cached preview output. Dropbox was skipped.

| Vendor | Batch result | Bulk rows | Single Invoice | Review items | Notes |
|---|---:|---:|---:|---:|---|
| Knoxville Utility Board | 1 invoice | 9 | Rendered | 0 | Community/master billing sample; one master invoice with sequential line items. |
| Kentucky Utilities | 1 invoice | 1 | Rendered | 0 | Community/master billing sample; one master invoice. |
| Clarksville Gas and Water | 1 invoice | 2 | Rendered | 0 | Water/sewer rows generated with proportional tax allocation. |
| Alabama Power | 1 invoice | 1 | Rendered | 0 | Deterministic route, required fields present. |
| EPB Fiber Optics | 1 invoice | 4 | Rendered | 0 | Deterministic route, fiber/internet lines generated. |
| The City of Henderson | 1 invoice | 1 | Rendered | 0 | Deterministic route, totals reconciled. |
| Tennessee American Water | 1 invoice | 1 | Rendered | 0 | Deterministic route, required fields present. |
| HWEA | 10 invoices | 32 | Rendered | 10 | Expected review in dry-run: Dropbox skipped and selected OCR/unit issues remain reviewable. |
| Richmond Utilities | 14 invoices | 16 | Rendered | 14 | Expected review in dry-run: Dropbox skipped and selected OCR/unit/total-match issues remain reviewable. |
| Weakley County Municipal Electric image | 0 invoices | 0 | Manual-review empty state | 1 | Deterministic image route confirmed; OCR too weak for mandatory fields. |

## 2. Screenshots Path

Screenshots and the browser fixture manifest are under:

`docs/reports/phases/screenshots/phase_u4_utility_e2e_qa/`

Captured:
- `*_bulk.png` for all 10 vendors.
- `*_single.png` for all vendors with generated invoices.
- `weakley_image_manual_review.png` for the image/manual-review case.
- `fixture_manifest.json` with batch ids, row counts, invoice counts, and review reasons.

## 3. Bulk Mode Results

Bulk Mode rendered generated ResMan rows for all representative PDF utility vendors. Required columns were present in the preview API, including invoice number, bill/credit, invoice/accounting dates, vendor, descriptions, line item number, property abbreviation, GL account, amount, expense type, replacement reserve, due date, and document URL field.

The dry-run support document status leaves `Document Url` empty but annotated as `dry_run_no_dropbox`; no Dropbox call was made.

## 4. Single Invoice Mode Results

Single Invoice Mode opened successfully for all processed vendors with rows. The view displayed the current invoice, source document, line items, totals, and review status.

The Weakley image case correctly showed no generated invoice in Single Invoice Mode instead of fabricating rows from weak OCR.

## 5. Community/Master Billing QA

Knoxville Utility Board and Kentucky Utilities were explicitly validated as community/master billing samples.

Checks passed:
- Expected invoice count: 1 each.
- Line item numbering sequential per master invoice.
- Totals reconciled per invoice.
- Master behavior was not applied to unrelated vendors.

## 6. Golden Output Validation

`scripts/smoke_utility_e2e_outputs.py` now performs dry-run, end-to-end webapp batch processing for the U4 representative set.

Validation includes:
- Deterministic routing only; AI-assisted routing fails the smoke for these vendors.
- No unsupported files for the selected samples.
- Mandatory fields present for generated rows.
- Numeric valid GL accounts.
- No raw full addresses in Location.
- No standalone tax lines.
- Payments excluded.
- Previous balances excluded unless vendor rules allow them.
- Totals reconcile to cents.
- Community/master line numbering for Knoxville and Kentucky.
- `Output/Template.xlsx` modification guard.

## 7. Bugs Found

1. Weakley County image bills could fall through to unknown/AI fallback because OCR text did not contain the full vendor name as a contiguous phrase.
2. The first U4 Playwright screenshot spec trusted only `localStorage` batch selection, which caused occasional screenshots against an unselected/empty template state.
3. Early browser screenshots could capture the document panel before the first PDF/image frame was ready.

## 8. Bugs Fixed

1. Added a Weakley-specific OCR fallback detector in `webapp/backend/services/vendor_detection.py`, matching broken OCR text such as `weakley county` + `municipal electric system` plus billing markers.
2. Added `scripts/smoke_utility_e2e_outputs.py` as the golden dry-run output validator and browser fixture preparer.
3. Added `webapp/frontend/e2e/utility-u4.spec.ts` to select each QA batch explicitly, verify preview readiness through the API, wait for document render, and capture Bulk/Single screenshots.

## 9. Vendors Needing Further Work

Weakley image bills remain deterministic/manual-review when OCR cannot reliably extract account number, invoice date, due date, invoice number, and property. This should be handled in a future image/OCR hardening pass, likely with vendor-specific image preprocessing or controlled vision fallback.

HWEA and Richmond still show review items in dry-run for skipped Dropbox links and selected OCR/unit/total mismatch cases. Rows are generated and visible, but those review states are intentionally not hidden.

## 10. Tests Performed

Frontend:
- `npm.cmd run build`
- `npx.cmd tsc --noEmit`
- `npm.cmd run test:e2e` - 34 passed

Backend:
- `python -m compileall webapp\backend`
- `python scripts\verify_backend_routes.py`
- `python scripts\smoke_canonical_rules_engine.py`
- `python scripts\smoke_canonical_invoice_fixtures.py`
- `python scripts\smoke_utility_processors.py`
- `python scripts\smoke_utility_e2e_outputs.py`

Browser QA:
- `npx.cmd playwright test utility-u4.spec.ts` - 10 passed
- Full Playwright suite also passed with U4 tests included.

Integrity:
- No tracked changes to `Output/Template.xlsx`.
- No tracked changes to `.env`.
- No tracked changes to `Training Bills_Invoices`.
- No tracked changes to `Old Scripts`.
- Dropbox skipped in dry-run.
- No automated AI calls used for the deterministic U4 vendor set after the Weakley routing fix.

## 11. Next Recommended Phase

Phase U5 should focus on utility screenshot/OCR hardening for image-heavy vendors, starting with Weakley County Municipal Electric System. The target should be deterministic image preprocessing and extraction of account/date/property fields before any AI fallback is considered.
