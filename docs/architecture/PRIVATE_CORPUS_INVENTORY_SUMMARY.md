# Private Corpus Inventory Summary

This file contains aggregate, redacted metadata only. It contains no filenames,
private paths, raw text, account data, tenant names, or document contents.

## Totals

- Files found: 2888
- Readable: 2871
- Errors: 17
- Exact duplicate groups/members excluded: 88 / 98
- Near-duplicate groups/members: 53 / 154
- Selected: 120
- Reserve: 20
- Inventory time: 167.63 seconds
- AI calls: 0
- Strong reasoner used: no
- Originals unchanged: yes

## Counts by authorized source folder

| Source | Count |
|---|---:|
| Bills for training AP | 580 |
| Bills for training TIA | 183 |
| Invoices CC Pictures | 2125 |

## Formats

| Extension | Count |
|---|---:|
| .25 | 1 |
| .com | 2 |
| .csv | 4 |
| .docx | 154 |
| .eml | 1 |
| .heic | 15 |
| .jpeg | 140 |
| .jpg | 288 |
| .pdf | 2235 |
| .png | 37 |
| .url | 3 |
| .xls | 1 |
| .xlsx | 4 |
| .zip | 1 |
| .~tmp | 2 |

## Quality tiers

| Tier | Count |
|---|---:|
| A | 1694 |
| B | 318 |
| C | 96 |
| D | 763 |

## Preliminary cohorts

| Cohort | Count |
|---|---:|
| digital_vendor_invoice | 100 |
| fee_renewal_subscription | 585 |
| handwritten_invoice | 83 |
| labor_service_invoice | 250 |
| materials_invoice | 64 |
| mixed_materials_and_labor | 157 |
| photo_receipt | 383 |
| scanned_bill | 183 |
| scanned_vendor_invoice | 481 |
| unknown_or_unusual | 585 |

## Selected cohorts

| Cohort | Count |
|---|---:|
| clean_photos_receipts | 15 |
| difficult_blurry_photos | 15 |
| digital_vendor_invoices | 20 |
| fees_renewals_subscriptions | 5 |
| handwritten | 10 |
| mixed_materials_labor | 5 |
| multi_line_contractor | 10 |
| scanned_bills | 15 |
| scanned_invoices | 15 |
| unknown_unusual | 10 |

## Selected quality mix

| Tier | Count |
|---|---:|
| A | 50 |
| B | 10 |
| C | 25 |
| D | 35 |

## Deterministic control cohort

Twenty known recurring digital bills already loaded in the application should
be added later as a separate control cohort. They must measure deterministic hit
rate, unnecessary AI calls, latency, GL and reconciliation regressions, and
unexpected review creation. They were not copied or inventoried in this stage.
