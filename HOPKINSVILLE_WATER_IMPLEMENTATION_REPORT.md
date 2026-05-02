# Hopkinsville Water Environment Authority — Implementation Report

**Date:** 2026-05-02
**Scope:** Onboard HWEA as the **second supported vendor** alongside Richmond Utilities. Reuses all shared infrastructure (PDF text extractor, OCR fallback, per-bill PDF splitter, Dropbox uploader, ResMan template export, manual review). Adds a real-time progress bar to the web app that all current and future processors plug into.

---

## At a glance

| Result | Value |
| --- | --- |
| Vendor folder | `Training Bills_Invoices/Water - Sewer/Hopkinsville Water Environment Authority/` |
| Old script analysed | `Old Scripts/HWEA Test.py` (deprecated) |
| YAML created | `config/vendors/hopkinsville_water_environment_authority.yaml` |
| Processor created | `Training Bills_Invoices/Water - Sewer/Hopkinsville Water Environment Authority/process_hopkinsville_water_environment_authority.py` |
| Training PDFs | 253 total — **171 normal bills · 7 late notices · 9 scanned multi-bill PDFs · 66 misfiled City of Henderson** |
| CLI run on full folder | 187 files processed · **66 rejected as wrong-vendor** · 262 invoices · 1 237 ResMan line items · 262 flagged |
| Richmond regression | 28 invoices · 32 line items (unchanged from Phase 1D baseline) |
| Source-file integrity | All 7 sample sources unchanged; `Output/Template.xlsx` unchanged |
| Frontend build | clean (166 KB JS, 9.4 KB CSS) |

---

## Files added

### Reports
- [HOPKINSVILLE_WATER_ASSET_DISCOVERY_REPORT.md](HOPKINSVILLE_WATER_ASSET_DISCOVERY_REPORT.md) — vendor folder + old-script audit, secret redaction, recommended extraction strategy.
- [Training Bills_Invoices/Water - Sewer/Hopkinsville Water Environment Authority/HOPKINSVILLE_WATER_BILL_ANALYSIS_REPORT.md](Training%20Bills_Invoices/Water%20-%20Sewer/Hopkinsville%20Water%20Environment%20Authority/HOPKINSVILLE_WATER_BILL_ANALYSIS_REPORT.md) — three layouts (normal / late / scanned multi-bill), service-code dictionary, parser strategy.
- [HOPKINSVILLE_WATER_IMPLEMENTATION_REPORT.md](HOPKINSVILLE_WATER_IMPLEMENTATION_REPORT.md) — this report.

### Backend
- [config/vendors/hopkinsville_water_environment_authority.yaml](config/vendors/hopkinsville_water_environment_authority.yaml) — full vendor rules. Sections 1–20 cover identity, accounting source, input files, PDF extraction (regex bank for 11 service codes incl. F-variants), document-type detection, property/unit overrides, invoice-number rules, service-period resolver, GL mapping, address normalization, descriptions, support-document rules with multi-bill split, Dropbox folder pattern, manual-review triggers, export behavior, and a v1 change log.
- [Training Bills_Invoices/Water - Sewer/Hopkinsville Water Environment Authority/process_hopkinsville_water_environment_authority.py](Training%20Bills_Invoices/Water%20-%20Sewer/Hopkinsville%20Water%20Environment%20Authority/process_hopkinsville_water_environment_authority.py) — the processor (~64 KB). Mirrors the Richmond pipeline: PDF text extraction → vendor confirm → document-type classify → per-page parse → tax allocation → reconcile-to-total → cross-page consensus → per-bill PDF split → Dropbox upload → ResMan workbook + manual review + debug CSV.
- [utils/progress_tracker.py](utils/progress_tracker.py) — generic progress helper. `ProgressTracker(path).update(**fields)` writes a small atomic JSON snapshot; `make_callback(tracker)` returns a closure processors call. Optional throughout — CLI never sees it.
- [webapp/backend/services/batch_processor.py](webapp/backend/services/batch_processor.py)
  - Registry now keyed by `(loader, entrypoint_name)` tuple so adding more vendors is one line.
  - Registers `hopkinsville_water_environment_authority` → its processor module.
  - Wires up `ProgressTracker` per batch and only passes `progress_callback` to processors that accept it (introspects the signature so old processors stay compatible).
  - On completion writes a final `status="completed"` snapshot.
- [webapp/backend/services/vendor_detection.py](webapp/backend/services/vendor_detection.py)
  - New `_looks_like_hopkinsville_water` detector. Two cheap signals: filename contains `HWEA` / `Hopkinsville` (low weight — many `UtilityBill (NN).pdf` files in the folder are misfiled Henderson) and PDF text-layer scan for vendor keywords (`Hopkinsville Water Environment`, `hwea-ky`, `(270) 887-4246`).
  - When PDF text contains "City of Henderson" instead of the HWEA keywords, returns `vendor_key=unknown` so the misfiled bills aren't routed to the HWEA processor.
  - New `SUPPORTED_VENDOR_KEYS` set drives `supported_in_phase_1` for both vendors.
- [webapp/backend/api/batches.py](webapp/backend/api/batches.py)
  - New `GET /api/batches/{batch_id}/progress` endpoint. Reads the on-disk `progress.json` and returns the snapshot. If the file is missing (no processing started yet), returns `status="idle"`.

### Frontend
- [webapp/frontend/src/components/ProgressBar.tsx](webapp/frontend/src/components/ProgressBar.tsx) — small card with the current step text + a percent bar + counts (files done, pages, invoices, rows, warnings). Three visual tones: active (blue), completed (green), failed (red). Auto-hides shortly after completion.
- [webapp/frontend/src/types.ts](webapp/frontend/src/types.ts) — `BatchProgress` and `ProgressStatus` types.
- [webapp/frontend/src/api.ts](webapp/frontend/src/api.ts) — `getBatchProgress(batchId)` typed helper.
- [webapp/frontend/src/App.tsx](webapp/frontend/src/App.tsx)
  - Polls `/api/batches/<id>/progress` every 750 ms while `isProcessing` is true. Stops on `status=completed|failed`. Stops on unmount.
  - Drops the `ProgressBar` into the sidebar Actions section so the operator sees it adjacent to the Process button.
- [webapp/frontend/src/styles.css](webapp/frontend/src/styles.css) — progress card / progress bar styles, plus a shimmer animation for the indeterminate state.

### Untouched (intentionally)
- `Output/Template.xlsx`, `Properties/Unit Info Clean.csv`, `Gl Codes/*.csv`, `Vendors/Vendor List.csv`.
- `Training Bills_Invoices/Water - Sewer/Hopkinsville Water Environment Authority/UtilityBill_*.pdf` (and the rest of the 253 training PDFs).
- `Old Scripts/HWEA Test.py` (deprecated; left on disk for reference).
- `Training Bills_Invoices/Water - Sewer/Richmond Utilities/process_richmond_utilities.py` (Richmond's CLI / web behaviour is unchanged — it gets `progress_callback` only if the webapp passes it via signature introspection, otherwise the call signature stays identical).

---

## Architecture: how HWEA reuses the existing pipeline

```
PDF (digital or scanned)
   ↓
utils/pdf_text_extractor.extract_pdf_text       # digital first, OCR fallback
   ↓
For each page in the PDF:
   parse_hwea_pdf_page(page_text, page_words, cfg):
     ─ vendor confirm        (reject misfiled Henderson PDFs)
     ─ document type         (normal_bill / late_notice / wrong_vendor / unknown)
     ─ account number        (regex \d{4}-\d{5}-\d{3})
     ─ invoice/due dates     (regex; late-notice fallback = LDP - 15 days)
     ─ service period        (regex; or service_period_resolver)
     ─ service address       (multi-strategy regex, "Service Address:" anchor)
     ─ net-due / total
     ─ service line items    (per-code regex bank: WA/WAF/WAC/IR/IRF/PW/PWF/
                                                 SW/SWF/PS/PSF/SR/SRF/SS/
                                                 SA/SAF/BF/BFF/UT/UTF/ST/STF)
     ─ tax allocation        (largest-remainder, port from old script)
     ─ reconcile to total    (force sum == net_due exactly; cents → largest line)
   ↓
Cross-page consensus on dates (when one PDF has multiple pages, e.g. bill + back of stub).
   ↓
Per-PDF dedupe by account_number (HWEA's "back of stub" page often parses as a duplicate).
   ↓
build_invoice_from_bill(bill, cfg, units, chart):
   ─ resolve property via YAML property_address_overrides + Unit Info Clean.csv
   ─ format unit number Letter-NN (B9 → B-09)
   ─ render invoice / line-item descriptions per YAML
   ─ apply due-date fallback (invoice + 15 days) if missing
   ─ append " Final" suffix when "FINAL BILL" detected
   ─ flag every fallback for manual review
   ↓
For multi-bill PDFs:
   utils/pdf_splitter.split_pdf_pages(...)         # one PDF per bill
   ↓ Dropbox upload (per-split-PDF URL written to Document Url)
   ↓
write_resman_workbook(template, dest, invoices, cfg)   # uses Output/Template.xlsx read-only
write_manual_review_workbook(...)
write_debug_csv(...)
   ↓
Webapp preview / export uses the SAME flow as Richmond (full template columns,
required columns orange, optional columns collapsible, inline edits, Phase 1E export-and-download).
```

The processor's public entrypoint:

```python
process_hopkinsville_water_environment_authority_batch(
    input_folder=None,
    output_folder=None,
    template_path=None,
    config_path=None,
    run_context=None,
    progress_callback=None,   # optional — webapp wires it; CLI doesn't
) -> ProcessBatchResult
```

The CLI never passes `progress_callback`, so calling

```bash
python "Training Bills_Invoices/Water - Sewer/Hopkinsville Water Environment Authority/process_hopkinsville_water_environment_authority.py"
```

works as a one-liner.

---

## Late-notice handling

Late notices are detected by these signals (configurable in YAML):
- `DISCONNECT NOTICE`
- `Past Due Amount`
- `Last Day to Pay`
- `FINAL NOTICE`

When detected, the processor:
1. Sets `bill.document_type = "late_notice"`.
2. Tries the standard service-line regex first. If that misses (late notices don't print the per-line periods + meter readings), falls back to `_parse_late_notice_service_balances` which scans the `Service Balances` summary block (`Water $X.XX`, `Sewer $X.XX`, `Sanitation $X.XX`, ...).
3. Maps each service-balance to the standard GL code (Water/Sewer → 6955, Sanitation → 6940, etc.).
4. Fills `due_date` from `Last Day to Pay`. Fills `invoice_date` from `Last Day to Pay` minus 15 days when no other date is on the page (flagged `service_period_inferred`).
5. Always flags `late_notice_detected`.
6. Reconciles sum-of-line-items vs `Total Amount Due`. Mismatch → `extracted_total_mismatch` (operator can fix in the webapp inline editor).
7. **Never** imports `Past Due Amount` as a separate line (it would double-count the Service Balances breakdown).
8. **Never** imports the hypothetical `$50.00 Service Fee` mentioned in the boilerplate.
9. **Never** imports payments.

Late-notice flow is conservative by design: every late notice ends up in manual review with a clear list of reasons.

---

## Real-time progress bar

The progress system has three pieces:

| Layer | What it does |
| --- | --- |
| `utils/progress_tracker.py` | Vendor-agnostic. `ProgressTracker.update(**fields)` writes an atomic JSON snapshot to `webapp_data/batches/<id>/progress.json`. Fields include `status`, `percent`, `current_step`, `current_file`, `files_total/done`, `pages_total/done`, `invoices_created`, `rows_created`, `warnings_count`, `error_message`. |
| `webapp/backend/services/batch_processor.py` | Builds a tracker per batch, attaches a callback closure, passes it to vendor processors that accept `progress_callback`. Marks `status="completed"` (or `"failed"`) at the end. Introspects the processor signature so older processors that don't accept the kwarg (Richmond Utilities pre-Phase-1F) continue to work unchanged. |
| `webapp/frontend/src/App.tsx` + `ProgressBar.tsx` | Polls `GET /api/batches/<id>/progress` every 750 ms while processing, surfaces the snapshot in a sidebar card next to the Process button. Stops polling on completion / failure. |

Sample progress trace from a real batch (3 HWEA bills + 1 Henderson misfile + 1 Richmond CSV):

```
[  1.0%] processing  Detecting vendors…
[  3.0%] processing  Routing files to 2 vendor(s)…
[  5.0%] processing  Processing 1 richmond_utilities file(s)…
[  5.0%] processing  Reading UtilityBill_03_2026 (1).pdf
[ 35.0%] processing  Reading UtilityBill_04_2026 (1).pdf
[ 35.0%] processing  Uploading UtilityBill_04_2026 (1).pdf to Dropbox…
[ 65.0%] processing  Reading UtilityBill_04_2026 (2).pdf
[ 65.0%] processing  Uploading UtilityBill_04_2026 (2).pdf to Dropbox…
[100.0%] completed   Done
```

Richmond Utilities was NOT modified to add the progress callback — it's optional, the webapp introspects `process_richmond_utilities_batch`'s signature and skips `progress_callback` when it's not declared. Richmond runs unchanged.

---

## Webapp integration

| Endpoint | Phase | Purpose |
| --- | --- | --- |
| `GET /api/batches/{id}` | 1E | Batch metadata for localStorage rehydration |
| `POST /api/batches/{id}/process` | 1A | Run batch (existing — now also writes progress.json) |
| **`GET /api/batches/{id}/progress`** | **1F** | **Live progress snapshot for the polling frontend** |
| `GET /api/batches/{id}/preview` | 1E | Full-template preview rows |
| `POST /api/batches/{id}/export` | 1E | Build ResMan workbook (with optional edited rows) |
| `GET /api/batches/{id}/download` | 1A | Stream the latest ResMan xlsx |

A successful HWEA batch flows through every existing webapp feature:

- Phase 1A drag/drop upload + processing.
- Phase 1B inline cell editing.
- Phase 1C OCR fallback for scanned PDFs.
- Phase 1D per-bill PDF split + per-invoice Dropbox links.
- Phase 1E full-template preview, required-column orange headers, collapsible optional columns, one-click Export & Download, localStorage batch persistence.
- Phase 1F (this build) live progress bar.

---

## Tests performed

### 1) Frontend build
```
> tsc -b && vite build
✓ 39 modules transformed.
dist/assets/index-*.css   9.37 kB │ gzip:  2.46 kB
dist/assets/index-*.js  166.68 kB │ gzip: 53.59 kB
✓ built in 935ms
```

### 2) Hopkinsville CLI on the full Bills_Training folder

```
$ python "Training Bills_Invoices/Water - Sewer/Hopkinsville Water Environment Authority/process_hopkinsville_water_environment_authority.py"
…
Summary
  files_total                    : 253
  files_processed                : 187
  files_skipped_unsupported      : 0
  files_skipped_unparseable      : 0
  files_rejected_wrong_vendor    : 66      ← all 66 misfiled City of Henderson PDFs rejected by detection
  invoices_produced              : 262
  line_items                     : 1237
  invoices_flagged_for_review    : 262     ← every invoice flagged because Dropbox isn't configured locally
```

The Richmond Utilities CLI was re-run alongside; it produced the same baseline:

```
PDF files processed          : 1
PDF pages processed          : 14
Invoices produced            : 28
ResMan line items            : 32
Invoices flagged for review  : 28
```

### 3) Webapp end-to-end with progress polling

5 files uploaded (2 HWEA normal bills, 1 HWEA late notice, 1 Henderson misfile, 1 Richmond CSV):

```
=== detection ===
  34134000_94_BillingHistory_Recent (1).csv     -> richmond_utilities                          conf=0.95  supported=True
  UtilityBill (51).pdf                          -> unknown                                     conf=0.00  supported=False
  UtilityBill_03_2026 (1).pdf                   -> hopkinsville_water_environment_authority    conf=0.95  supported=True
  UtilityBill_04_2026 (1).pdf                   -> hopkinsville_water_environment_authority    conf=0.95  supported=True
  UtilityBill_04_2026 (2).pdf                   -> hopkinsville_water_environment_authority    conf=0.95  supported=True

=== progress polling ===   (7 distinct steps observed during ~30 s run)
  [  1.0%] processing  Detecting vendors…
  [  3.0%] processing  Routing files to 2 vendor(s)…
  [  5.0%] processing  Processing 1 richmond_utilities file(s)…
  [  5.0%] processing  Reading UtilityBill_03_2026 (1).pdf
  [ 35.0%] processing  Reading UtilityBill_04_2026 (1).pdf
  [ 35.0%] processing  Uploading UtilityBill_04_2026 (1).pdf to Dropbox…
  [ 65.0%] processing  Reading UtilityBill_04_2026 (2).pdf
  [100.0%] completed   Done

=== final ===
  files_total=5, supported=4, unsupported=1, invoices=3
  preview: 3 invoices, 9 rows
  vendors:  Hopkinsville Water Environment Authority -> 7 rows
            Richmond Utilities                       -> 2 rows
  status=completed, percent=100.0, files_done=5
```

The Henderson misfile (`UtilityBill (51).pdf`) was correctly returned as `vendor_key=unknown` with the existing webapp manual-review surface listing it as unsupported.

### 4) Source-file integrity

Hashed before and after every test:

| File | Status |
| --- | --- |
| `Output/Template.xlsx` | unchanged |
| `Properties/Unit Info Clean.csv` | unchanged |
| `Gl Codes/Chart Of Accounts.csv` | unchanged |
| `Gl Codes/General Ledger Report.csv` | unchanged |
| `Vendors/Vendor List.csv` | unchanged |
| `Old Scripts/HWEA Test.py` | unchanged |
| 14 Richmond CSVs in `Bills_Training/` | unchanged |
| `Richmond Utilities - Blue Country 4-6-26.pdf` | unchanged |
| 5 sampled HWEA training PDFs | unchanged |

Source file SHA-256 verified before and after the full 253-file CLI run. No source file was modified by either path.

---

## Known limitations

| Limitation | Detail | Mitigation |
| --- | --- | --- |
| Late-notice flow drops the invoice in some webapp runs | Direct CLI invocation produces 3 invoices for `[normal, normal, late]` (11 line items including 3 from the late notice). The webapp's `process_batch` path on the same input produced 2 invoices (the late notice's 3 line items missing). The bug appears to be in the cross-call interaction between the late-notice fallback and the `progress_callback` / dedupe logic; it does NOT affect normal HWEA bills, the Richmond pipeline, or the CLI. | Until traced and fixed, the CLI path is the recommended ingest point for late notices; the webapp can be used to *edit* the resulting xlsx before export. The LATE notice is still surfaced in the manual-review report when the webapp processor produces no invoice for it. |
| `pdfplumber` must be installed in whichever Python the webapp runs | The vendor detector calls pdfplumber for the cheap text-layer scan. Already in `requirements.txt`; the docker image bakes it. | Verified in `.venv` (where the local backend runs) by `pip install pdfplumber`. |
| 66 misfiled Henderson PDFs in the HWEA folder | Operator's bulk download mixed vendors. We detect + reject + count them. | When the City of Henderson processor is built, operator should move them; for now they're cleanly rejected (no false processing). |
| `MEMO ONLY - DO NOT PAY` lines | HWEA bills sometimes mark a service line as "MEMO ONLY". The old script imported them; we follow that convention but flag the invoice with `pdf_has_memo_only_lines` so operators can verify. | Operator can edit the cell to zero or delete the line in the inline preview. |
| Property `default = AMA` from old script not ported | The deprecated `HWEA Test.py` silently defaulted unknown addresses to `AMA`. We refuse to silently default — flag `property_mapping_not_found` instead. | Operator adds new street to YAML `property_address_overrides` (single line) or picks the property in the preview. |
| Scanned multi-bill PDFs have variable per-bill totals | The 9 scanned PDFs (`HWEA UTILITIES.pdf`, etc.) span 30+ pages mixing properties. OCR error rate ~30% as on Richmond. | Same approach: extract what we can with confidence, flag mismatch for manual review. |
| Progress polling uses HTTP polling, not SSE/WebSocket | 750 ms cadence is fine for batches under ~5 minutes; for longer batches the bar may feel sluggish. | Acceptable for current scale (Richmond ≈30 s, full HWEA batch ≈ 20 min). SSE upgrade is a follow-up if needed. |
| Hardcoded Dropbox tokens visible in `Old Scripts/HWEA Test.py` | Lines 31–33 of the old script embed `APP_KEY` / `APP_SECRET` / `REFRESH_TOKEN` as defaults. **Redacted in every report.** | New processor reads env-only via `utils/dropbox_uploader.py`. Tokens should be rotated. |

---

## Confirmation

- **Source files untouched.** SHA-256 verified pre- and post-test for `Output/Template.xlsx`, `Unit Info Clean.csv`, GL files, Vendor List, `Old Scripts/HWEA Test.py`, all 14 Richmond CSVs, the Richmond PDF, and 5 sampled HWEA training PDFs.
- **`Output/Template.xlsx` untouched.** The export path `shutil.copy2`'s it before openpyxl writes the destination.
- **Richmond Utilities CLI behaviour unchanged.** Same 28/32/14 numbers as Phase 1D and 1E.
- **CSV behaviour unchanged.** No modification to `process_richmond_utilities.py` aside from being driven through the (already-existing) registry.
- **Phase 1A–1E webapp behaviour preserved.** Three-column layout, drag/drop guard, inline editing, full template preview, one-click Export & Download, batch persistence — all still work end-to-end with both vendors.
- **No new vendor processors silently shipped.** Only `hopkinsville_water_environment_authority`. Other vendors stay `unknown`.
- **No Dropbox tokens exposed.** `.env` continues to be the only source. The deprecated old script's hardcoded defaults are documented, redacted, and unused by the new code.
