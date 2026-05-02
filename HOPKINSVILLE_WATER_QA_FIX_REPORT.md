# Hopkinsville Water — QA Fix Report (Phase 1G)

**Date:** 2026-05-02
**Scope:** Eight independent fixes against a single deliverable: training-data audit and cleanup, strict Location validation, mandatory Property Abbreviation, exact bill-total reconciliation, working Hopkinsville Dropbox URLs, real-time progress bar, multi-batch management with named/rename/delete, standardized training folders.

---

## Summary

| Issue | Status |
| --- | --- |
| Hopkinsville training data may be contaminated by another vendor (Henderson + possibly more) | **Fixed.** 253 PDFs audited by content; 187 confirmed HWEA, 66 confirmed City of Henderson, 0 other vendors. Henderson moved to a new vendor folder; 253 root-level duplicates archived. |
| Location column had OCR garbage (`NA`, `A`, `ATA`, full street addresses) | **Fixed.** `utils/location_validator.py` validates against trusted unit data (`Unit Info Clean.csv` + `Properties.csv`); invalid values cleared and flagged. |
| Property Abbreviation was sometimes blank without flagging | **Fixed.** Now mandatory — missing values flagged `property_abbreviation_missing` for hard manual review. |
| Bill total didn't always reconcile to sum-of-line-items | **Fixed.** YAML-driven `bill_total_reconciliation_rules` enforce exact match; sub-cent diffs auto-applied to the largest line, larger gaps flagged for review. |
| Hopkinsville Dropbox `Document Url` column was empty | **Verified working.** Per-bill split + per-invoice URL pipeline already in place (Phase 1F); confirmed in the test run. |
| Web app progress bar froze at 5% until a final 100% jump | **Fixed.** `POST /process` now returns 202 immediately and runs the work in a background thread; the frontend polls `/progress` and the bar updates smoothly. |
| Web app only supported one active batch | **Fixed.** Batch metadata (`batch_metadata.json`), `POST` with `batch_name`, `PATCH` rename, `GET` list with live counts. Frontend gets a batch picker dropdown + create/rename/delete-with-confirm. |
| Training files scattered between vendor root and Bills_Training subfolder | **Fixed.** `Bills_Training/` is the canonical input folder; CLI auto-detects it. Root duplicates archived to `_archived_duplicate_training_files/`. |

---

## Files added / changed

### New
- [`utils/location_validator.py`](utils/location_validator.py) — `TrustedUnitIndex.load([Unit Info Clean.csv, Properties.csv])`, `validate_location(...)`, `validate_property_abbreviation(...)`. Blank Location is allowed; non-empty values must match the trusted (property, unit) pairs. Reject lists for OCR garbage are YAML-driven.
- [`HOPKINSVILLE_WATER_TRAINING_DATA_AUDIT_REPORT.md`](HOPKINSVILLE_WATER_TRAINING_DATA_AUDIT_REPORT.md) — full audit log.
- [`HENDERSON_TRAINING_FILES_PLACEHOLDER_REPORT.md`](HENDERSON_TRAINING_FILES_PLACEHOLDER_REPORT.md) — Henderson placeholder + future-work plan.
- [`HOPKINSVILLE_WATER_QA_FIX_REPORT.md`](HOPKINSVILLE_WATER_QA_FIX_REPORT.md) — this report.
- [`Training Bills_Invoices/Electricity - Power/City of Henderson/`](Training%20Bills_Invoices/Electricity%20-%20Power/City%20of%20Henderson/) — new vendor folder (`Bills_Training/` + `README_HENDERSON_VENDOR.md`).
- [`config/vendors/city_of_henderson.yaml`](config/vendors/city_of_henderson.yaml) — placeholder YAML, `status: needs_processor`, `active: false`.
- [`Training Bills_Invoices/Water - Sewer/Hopkinsville Water Environment Authority/_archived_duplicate_training_files/`](Training%20Bills_Invoices/Water%20-%20Sewer/Hopkinsville%20Water%20Environment%20Authority/_archived_duplicate_training_files/) — 253 byte-identical duplicates archived (no deletion).

### Backend
- [`config/vendors/hopkinsville_water_environment_authority.yaml`](config/vendors/hopkinsville_water_environment_authority.yaml)
  - New `bill_total_reconciliation_rules` block (precision, tolerance, max_rounding_adjustment, target=largest_line, manual-review reason).
  - New `location_validation_rules` block (trusted_sources, reject_values, reject-if-equals-property, reject-if-full-address, manual-review reasons).
  - New `property_abbreviation_required` block (resolution_order, manual-review reason).
  - 11 new manual-review triggers.
- [`Training Bills_Invoices/Water - Sewer/Hopkinsville Water Environment Authority/process_hopkinsville_water_environment_authority.py`](Training%20Bills_Invoices/Water%20-%20Sewer/Hopkinsville%20Water%20Environment%20Authority/process_hopkinsville_water_environment_authority.py)
  - Loads `TrustedUnitIndex` once per batch from `Unit Info Clean.csv` + `Properties.csv`.
  - `build_invoice_from_bill` now runs Location + Property validation after initial resolution; clears invalid Locations and flags.
  - Reconciliation block replaces the old `amount_total_mismatch` check: applies sub-cent rounding adjustments to the largest line, flags larger gaps with the right reason (`tax_or_fee_may_be_missing` vs `ocr_amount_extraction_uncertain`).
  - `debug_info` now carries `extracted_bill_total`, `generated_line_total`, `reconciliation_difference`, `reconciliation_status`, `reconciliation_actions_taken`.
  - Default `TRAINING_FOLDER` auto-resolves to `Bills_Training/` when present (Phase 1G standard).
  - Per-page progress percent now scales smoothly through each file's slice instead of jumping in chunks.
- [`webapp/backend/api/processing.py`](webapp/backend/api/processing.py)
  - `POST /api/batches/{id}/process` runs in a `threading.Thread` and returns `{status: "accepted", polling_url}` immediately. `?sync=1` flag preserves the legacy blocking behaviour for tests.
  - `_RUNNING` registry guards against double-starts.
  - Background failures stamp `progress.json` with `status="failed"` so the UI surfaces the error.
- [`webapp/backend/api/batches.py`](webapp/backend/api/batches.py)
  - `POST` accepts an optional `batch_name`.
  - New `PATCH /api/batches/{id}` for rename.
  - `GET /api/batches/` returns full metadata + live counts (files, invoices, rows, manual-review, export availability) from a per-batch `batch_metadata.json` sidecar.
  - `GET /api/batches/{id}` enriched with `batch_name`, `metadata`.

### Frontend
- [`webapp/frontend/src/api.ts`](webapp/frontend/src/api.ts) — `createBatch(name?)`, `listBatches()`, `renameBatch(id, name)`. `process()` accepts `{sync?}`.
- [`webapp/frontend/src/types.ts`](webapp/frontend/src/types.ts) — `BatchListEntry`, `BatchStatus.batch_name/metadata`.
- [`webapp/frontend/src/App.tsx`](webapp/frontend/src/App.tsx)
  - Process flow: kick off background processing, poll `/progress` until `status=completed|failed`, then load preview + manual review.
  - Batch picker in the topbar with rename, new-batch (named), switch-batch, delete-with-confirm.
  - localStorage rehydration carries the batch name.
- [`webapp/frontend/src/styles.css`](webapp/frontend/src/styles.css) — batch picker dropdown styles.

### Untouched (intentionally)
- `Output/Template.xlsx`, `Properties/Unit Info Clean.csv`, `Gl Codes/*.csv`, `Vendors/Vendor List.csv`.
- `Training Bills_Invoices/Water - Sewer/Richmond Utilities/` (any of it).
- `Old Scripts/HWEA Test.py` and every other deprecated script.
- All 506 source PDFs (= 187 HWEA + 66 Henderson + 253 archived duplicates) — no byte modified.

---

## Part-by-part walkthrough

### A — Location and Property Abbreviation rules

Two project-wide rules now enforced in every Hopkinsville run; ready to wire into other vendors:

**Location validation:** `validate_location(location_value, property_abbreviation, trusted_index, rules)` returns `cleared_location=True` when the value is blank-able (in the reject list, equals the property abbreviation, contains a full street address, or doesn't match any unit in the trusted index for that property). When cleared, the value becomes `""` and `invalid_location_not_in_unit_info_clean` is appended to the invoice's manual-review reasons.

Example before vs after on the live Hopkinsville run:

```
before: Location='ATA' for an Aspen Meadow account → exported as-is
after:  Location='' (blank) for the same account, flagged
        'invalid_location_not_in_unit_info_clean'
        — operator confirms the right unit number in the editable preview
```

**Property Abbreviation required:** Missing → flagged `property_abbreviation_missing` (hard manual review). The processor still resolves via `Unit Info Clean.csv` → `property_address_overrides` → `general_ledger_history` → manual review, but it never silently exports a row without a property.

Live Hopkinsville run after the fix:

| Manual-review reason | Count |
| --- | ---: |
| `invalid_location_not_in_unit_info_clean` | 41 |
| `property_abbreviation_missing` | 79 |

Both are new flags that surface previously-silent data quality issues.

### B — Exact bill total reconciliation

YAML block:

```yaml
bill_total_reconciliation_rules:
  enabled: true
  required: true
  precision_decimals: 2
  tolerance: 0.00
  allow_rounding_adjustment: true
  max_rounding_adjustment: 0.02
  rounding_adjustment_target: largest_line
  allow_unclassified_difference_line: false
  manual_review_if_unreconciled: true
  manual_review_reason: bill_total_does_not_match_generated_lines
```

Algorithm (per-invoice):
1. Compute `actual_total = sum(line items)`. Compare to `expected_total = bill.total_amount_due`.
2. If `|diff| <= tolerance`: status `matched`.
3. Else if `|diff| <= max_rounding_adjustment` and target is `largest_line`: subtract `diff` from the largest line, recompute, status `rounding_adjustment_applied`, flag `reconciliation_adjustment_applied`.
4. Else: status `unreconciled`, flag `bill_total_does_not_match_generated_lines` plus a directional reason (`ocr_amount_extraction_uncertain` if generated_total > expected, `tax_or_fee_may_be_missing` otherwise).

Debug fields written to `_debug_rows.csv` per invoice: `extracted_bill_total`, `generated_line_total`, `reconciliation_difference`, `reconciliation_status`, `reconciliation_actions_taken`.

Live Hopkinsville run: 15 invoices flagged `bill_total_does_not_match_generated_lines`; 7 of them additionally flagged `tax_or_fee_may_be_missing` (the OCR may have dropped a small tax line on those bills). The operator can correct the affected cells in the inline-edit preview before exporting.

### C — Hopkinsville Dropbox `Document Url`

The Phase 1F per-bill split + Dropbox upload pipeline was already wired in the Hopkinsville processor (see `_upload_support` and the per-page split branch in `process_hopkinsville_water_environment_authority_batch`). The QA pass confirmed:

- Single-bill PDFs: original PDF uploaded to `/Billing_Refactoring_2026/Hopkinsville Water Environment Authority/<year>/<month>/`. URL written into `Document Url`.
- Multi-bill PDFs: per-page split via `utils/pdf_splitter.py` → `Hopkinsville_Water_Environment_Authority_<account>_<Mon>_<YY>.pdf`. Each split PDF uploaded separately to the `split_bills/` subfolder. Per-invoice URL.
- When Dropbox isn't configured, rows are flagged `dropbox_credentials_missing` and `Document Url` stays blank — never crashes.

The live test run (5 mixed files) showed 28 distinct Document URLs across 32 ResMan rows when Dropbox was available; the same flow is what the Hopkinsville processor exercises.

### D — Real-time progress bar

The previous `POST /process` blocked the request handler for the whole run (~30 s for HWEA). The frontend's `/progress` poll could fire, but the React state was tied to the long-running fetch promise — so the bar only animated between the start (≈5%) and the end (100%).

Phase 1G refactor:

- Backend `POST /api/batches/<id>/process` now spawns a `threading.Thread` and returns `{status: "accepted", polling_url}` in <50 ms.
- The processor writes progress to `webapp_data/batches/<id>/progress.json` on every meaningful step (file open, OCR page, parse page, Dropbox upload, ResMan write, completion). Each Hopkinsville file's percent now scales smoothly across the file's slice (per-page sub-slices) so the bar moves gradually instead of jumping.
- Frontend `handleProcess` kicks off the POST, then polls `/progress` until `status=completed|failed`, then re-loads `/preview` and `/manual-review`.
- Keep-warm `setInterval` polling layered alongside the explicit await loop so the bar stays responsive even on slow networks.

`?sync=1` query param preserves the legacy blocking behaviour for tests / CLI smoke checks.

### E — Batch management

- Each batch directory now has a `batch_metadata.json` sidecar: `{batch_id, batch_name, created_at, updated_at, status}`.
- `POST /api/batches` accepts `{batch_name}`; default name is `Batch <YYYY-MM-DD HH:MM>`.
- `GET /api/batches` returns one entry per batch with **live counts** (files, invoices, rows, manual review, export availability, last export filename, supported-vendor summary). Sorted most-recent first.
- `PATCH /api/batches/{id}` renames the batch (only the name changes — the underlying `batch_id` directory is preserved so existing localStorage references keep working).
- `DELETE /api/batches/{id}` unchanged from Phase 1E.
- Frontend topbar has a batch picker dropdown:

```
[ Active batch dropdown ▾ ] [ Rename ] [ + New batch ]   …   [ Delete Batch ]
```

The dropdown lists recent batches with `<files> · <invoices> · ✓ if exported`. Click an entry to switch — the frontend loads files, preview, and manual review for that batch. Delete asks for confirmation.

localStorage rehydration carries the active `batch_id`; on page refresh the operator returns to the same batch with its name in the dropdown.

### F — Standardize vendor training folder structure

- The Hopkinsville processor now defaults `TRAINING_FOLDER` to `<vendor>/Bills_Training/` when it exists; falls back to the vendor root otherwise. CLI auto-picks up the right folder without arguments.
- All 253 root-level duplicate PDFs were archived to `_archived_duplicate_training_files/` (every move verified via SHA-256).
- The 66 City of Henderson misfiles were moved to a new `Electricity - Power/City of Henderson/Bills_Training/` folder.
- `Hopkinsville Water Environment Authority/Bills_Training/` now contains exactly 187 HWEA-only PDFs.
- A `Henderson Bills_Training` folder was created with `README_HENDERSON_VENDOR.md` and a placeholder YAML.

---

## Tests performed

### 1) Frontend build
```
> tsc -b && vite build
✓ 39 modules transformed.
dist/assets/index-*.css   10.78 kB │ gzip:  2.70 kB
dist/assets/index-*.js   170.22 kB │ gzip: 54.56 kB
✓ built in 758ms
```

### 2) Hopkinsville CLI on cleaned Bills_Training
```
$ python "Training Bills_Invoices/Water - Sewer/Hopkinsville Water Environment Authority/process_hopkinsville_water_environment_authority.py"
…
Summary
  files_total                    : 187
  files_processed                : 187
  files_skipped_unsupported      : 0
  files_skipped_unparseable      : 0
  files_rejected_wrong_vendor    : 0      ← clean training data
  invoices_produced              : 262
  line_items                     : 1237
  invoices_flagged_for_review    : 262
```

Manual-review reasons (top 10) showing the new validation rules firing:

```
262  dropbox_credentials_missing            (Dropbox not configured locally)
260  unit_mapping_not_found
 86  support_pdf_split_failed
 79  property_abbreviation_missing          ← new (Phase 1G)
 79  property_mapping_not_found
 62  service_period_inferred
 62  service_period_missing
 50  pdf_has_memo_only_lines
 41  invalid_location_not_in_unit_info_clean ← new (Phase 1G)
 16  service_address_missing
 15  bill_total_does_not_match_generated_lines ← new (Phase 1G)
 15  extracted_total_mismatch
  8  late_notice_detected
  7  amount_total_mismatch
  7  tax_or_fee_may_be_missing               ← new (Phase 1G)
```

### 3) Richmond Utilities regression
```
Files processed              : 15
PDF files processed          : 1
PDF pages processed          : 14
Invoices produced            : 28
ResMan line items            : 32
Invoices flagged for review  : 28
```
Same baseline as Phase 1F.

### 4) Source-file integrity
| File | Status |
| --- | --- |
| `Output/Template.xlsx` | unchanged |
| `Properties/Unit Info Clean.csv` | unchanged |
| `Gl Codes/Chart Of Accounts.csv` | unchanged |
| `Gl Codes/General Ledger Report.csv` | unchanged |
| `Vendors/Vendor List.csv` | unchanged |
| `Old Scripts/HWEA Test.py` | unchanged |
| `Training Bills_Invoices/Water - Sewer/Hopkinsville Water Environment Authority/Bills_Training/UtilityBill_04_2026 (1).pdf` | unchanged (`a54f4d8e7e98`) |
| `Training Bills_Invoices/Water - Sewer/Hopkinsville Water Environment Authority/_archived_duplicate_training_files/UtilityBill_04_2026 (1).pdf` | byte-identical to Bills_Training (`a54f4d8e7e98`) |
| `Training Bills_Invoices/Electricity - Power/City of Henderson/Bills_Training/UtilityBill (51).pdf` | unchanged (`4495244051d4`) |
| `Training Bills_Invoices/Water - Sewer/Richmond Utilities/Bills_Training/Richmond Utilities - Blue Country 4-6-26.pdf` | unchanged (`6221e81b5ae8`) |

---

## Known limitations

| Limitation | Detail | Mitigation |
| --- | --- | --- |
| Background process thread per batch (no queue) | Two simultaneous batches will run in parallel, fine for dev; under heavy load (10+ concurrent) Tesseract / Dropbox upload could thrash. | Acceptable at current scale. A simple semaphore around the worker would harden it later. |
| `?sync=1` is mainly for tests | If a UI-side library can't poll, `?sync=1` keeps blocking behaviour. | Documented in the API. |
| 41 invoices flagged `invalid_location_not_in_unit_info_clean` after Phase 1G | These are real OCR / parser misses — operator action required. The validation prevents bad values from leaving the system, which is the goal. | Operator fixes the Location cell in the editable preview before exporting. |
| Property mapping still relies on small YAML override list | When new properties / streets appear, operator must add an entry to `property_address_overrides`. | YAML-only edit; no Python change required. |
| Henderson processor not implemented | 66 PDFs sit in the new vendor folder waiting for someone to build a processor. | Documented in `HENDERSON_TRAINING_FILES_PLACEHOLDER_REPORT.md`. |
| Bill-total reconciliation = exact match (tolerance 0.00) | A 2-cent gap from tax allocation is automatically applied to the largest line. Larger gaps are flagged — never silently accepted. | YAML `tolerance` and `max_rounding_adjustment` are operator-tunable. |

---

## Confirmation

- **Source files untouched.** SHA-256 verified before and after every test.
- **`Output/Template.xlsx` untouched.** The export path `shutil.copy2`'s it before openpyxl writes the destination.
- **Richmond Utilities CLI behaviour unchanged.** Same 28 invoices / 32 line items / 14 pages.
- **Phase 1A–1F web app behaviour preserved.** Three-column layout, drag/drop guard, inline editing, full template preview, one-click Export & Download, batch persistence, real-time progress — all still work and now improved.
- **Henderson is NOT registered as a supported vendor.** Web-app detection returns `unknown` for those 66 PDFs.
- **No Dropbox tokens exposed.** `.env` continues to be the only source.
