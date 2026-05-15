# Phase U3 - Utility Wave 3 Processor Report

Phase U3 implemented the remaining high-priority deterministic utility processors that had training data but no active modern processor after U1/U2. The work reused the shared U1 utility framework and did not modify source training bills, `Output/Template.xlsx`, `.env`, or old scripts.

## 1. Vendors Implemented

| Vendor | Status | Training evidence | Representative dry-run result |
| --- | --- | --- | --- |
| Clarksville Gas and Water | Active | 25 PDFs | 2 rows, sales tax allocated across water/sewer, no review blockers |
| Knoxville Utilities Board | Active | 8 PDFs | 9 community summary rows under one master invoice, no review blockers |
| Kentucky Utilities | Active | 9 PDFs | 1 electric row with taxes/fees allocated, no review blockers |
| Tennessee American Water | Active | 24 PDFs | 1 water row including negative fee adjustment and tax allocation, no review blockers |
| Union City Energy Authority | Active | 24 PDFs | 1 electric row, payment-coupon due date parsed, no review blockers |
| Weakley County Municipal Electric System | Active | 17 PDFs, 7 images | 1 electric row, valid unit detection where present, no review blockers |
| Birmingham Water Works | Active | 15 PDFs | 2 rows for water and sewer service, no review blockers |
| City of McMinnville Water & Sewer Dept | Active | 6 PDFs | 3 current-charge rows; previous balance/payment history ignored |
| City of Chattanooga Wastewater Department | Active | 18 PDFs | 1 sewer usage row; previous statement/payment text ignored |
| City of Martin | Active | 6 PDFs | 2 current-charge rows; payment history ignored |
| City of Union City | Active | 31 PDFs | 4 water/sewer/sanitation/stormwater rows, tax allocated |
| Guardian Water & Power | Active | 5 PDFs | 3 billing-fee rows, total reconciles |
| Hopkinsville Electric System | Active | 25 PDFs | Electric service plus connect fee; connect fee GL `6956` |

## 2. Vendors Left Partial Or Needing More Training

No U3 priority vendor with a `utility_processing` overlay remains `needs_processor`. The generic `City of Chattanooga` name remains distinct from `City of Chattanooga Wastewater Department`; no separate deterministic processor was claimed for a generic city vendor outside the discovered wastewater training set.

## 3. Community Billing Findings

Community/master billing remains explicitly vendor-scoped:

- `knoxville_utilities_board`: `community_billing_rules.enabled: true`; summary-by-address charges stay under one master invoice with sequential line item numbers.
- `kentucky_utilities`: `community_billing_rules.enabled: true`; current training sample produces one master-account invoice.
- No other U3 vendor inherits community behavior automatically.

This preserves the U1/U2 rule that multi-account/master behavior must be configured per vendor, not generalized across utilities.

## 4. Tax Allocation Validation

The U3 processors enforce the shared utility tax contract:

- No standalone tax rows in generated ResMan rows.
- Sales tax, school tax, utility tax, and similar taxes are allocated proportionally into current service lines.
- Rounding remainders are absorbed by the largest taxable line through the shared allocator.
- Tennessee American Water negative fees/adjustments are included in the reconciled current payable amount.
- City municipal statements ignore previous balances and payment history while retaining current taxes.

## 5. Fee Handling Validation

- Hopkinsville Electric connect fee is exported as its own GL `6956` line.
- Late/payment-history text is not exported as expense lines.
- Previous balances are ignored unless a vendor rule explicitly allows current-payable balance-forward behavior.
- Guardian Water & Power final billing fee is captured as a normal water/sewer billing-fee line.

## 6. Routing And Detection

Added Wave 3 routing in:

- `webapp/backend/services/utility_wave3_processors.py`
- `webapp/backend/services/batch_processor.py`
- `webapp/backend/services/vendor_detection.py`

Detection includes scanned/OCR fallback sampling for vendors whose PDFs do not expose a usable text layer.

## 7. Smoke Results

Direct U3 dry-runs were performed against temporary copies of representative training PDFs. All promoted U3 vendors produced preview rows that passed `validate_utility_template_rows`.

Full utility smoke:

```text
python scripts\smoke_utility_processors.py
PASS: utility processor shared contract is valid.
Validated 26 utility vendor YAML overlay(s).
```

The smoke also confirmed dry-run behavior did not call Dropbox and did not write `Output/Template.xlsx`.

## 8. Regressions Checked

The full utility smoke still covers the previously active vendors:

- Atmos
- Columbia / CPWS
- Hardin
- HWEA
- McMinnville Electric
- Pennyrile
- Richmond
- Shelbyville
- Alabama Power
- EPB Fiber Optics
- The City of Henderson
- CDE Lightband
- Nolin RECC Smarthub

No regression was found in the shared utility smoke.

## 9. Known Limitations

- U3 processors are deterministic parsers validated against representative samples, not every possible variant in each vendor folder.
- Image/screenshot training for Weakley is covered by ingestion support, but the smoke currently uses the first PDF sample.
- KUB and Kentucky community/master behaviors are intentionally conservative and vendor-specific.
- Dropbox document links remain skipped in automated dry-runs by design.

## 10. Next Recommended Wave

Add per-vendor multi-sample fixture coverage for U3 vendors, especially scanned/image variants and any vendor with multiple bill layouts. The next phase should also add UI fixture selectors for utility processors so operators can run vendor-specific dry-runs from the app without touching real export/Dropbox flows.
