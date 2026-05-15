# EPB Scanned PDF Failure Fix

Date: 2026-05-12

## Scope

Investigated the failed EPB scanned PDF batch using `9b920b94-0767-4f51-b75d-048af95d0efe.pdf`, where the UI previously produced a wrong vendor, missing property, generic invoice number, and a misleading Ready state.

## Root Cause

1. Generic UUID-named scanned PDFs were being accepted as Hardin County Water hints. This allowed unrelated scanned PDFs to bypass the AI-assisted path.
2. Vendor detection results were cached without a detector-version guard, so stale wrong detection could remain attached to the batch.
3. Vision rendering for scanned PDFs used only page 1, while this EPB bill carries the service address and line detail on later pages.
4. Local PDF vision rendering required PyMuPDF only. The workspace has `pypdfium2`, so scanned PDF rendering needed a fallback.
5. Required fields could appear blank in review if AI returned a usable invoice but did not provide a confirmed property.

## Fixes Implemented

- Removed the unsafe UUID-as-Hardin detector behavior.
- Versioned the file detection cache so old vendor-detection decisions are invalidated.
- Added PDF rendering fallback through `pypdfium2` for AI vision page images.
- Added local OCR fallback for scanned PDFs before or alongside provider extraction.
- Changed AI PDF vision processing to render pages `1..AI_VISION_MAX_PAGES` instead of page 1 only.
- Added provider retry handling for transient 429/5xx responses.
- Added required-property fallback using local vendor/property history when the service address or OCR text points to a known property.
- Hardened frontend review flags so missing invoice number, property, or GL cannot silently show as Ready.

## Reprocess Result

Batch: `batch_20260512_112326_990`

Preview now returns 2 rows:

| Field | Result |
| --- | --- |
| Vendor | EPB Fiber Optics |
| Invoice Number | 18547827 |
| Invoice Date | 2026-05-07 |
| Due Date | 2026-05-22 |
| Property Abbreviation | RCC |
| GL Account | 6920 |
| Service Period | 05/08/2026 - 06/07/2026 |
| Service Address | 21762 River Canyon Rd Apt H |
| Row 1 | $67.98 Fi-Speed Internet |
| Row 2 | $0.97 Tax and Surcharges |
| Total | $68.95 |

Remaining review notes are appropriate:

- Invoice number was ambiguous in one OCR/vision location but resolved to `18547827`.
- Location/unit remains blank because no exact unit match was confirmed.
- Property was prefilled from local history and should be confirmed by the operator.

## Tests Performed

- `python -m compileall webapp\backend`
- `python scripts\verify_backend_routes.py`
- `cd webapp/frontend && npm.cmd run build`
- `cd webapp/frontend && npx.cmd tsc --noEmit`
- `cd webapp/frontend && npm.cmd run test:e2e -- --project=chromium`
- Live backend reprocess of `batch_20260512_112326_990`
- Live preview verification from `/api/batches/batch_20260512_112326_990/preview`

## Integrity

- No Dropbox export was triggered.
- `Output/Template.xlsx` was not modified by this fix.
- Source/training bills were not modified.
- API keys were not printed or exposed.

## Remaining Limitation

Location/unit remains optional and blank unless a known unit/location can be validated. Property and GL are now populated for this EPB case, and unresolved required values remain visible as review blockers instead of being silently accepted.
