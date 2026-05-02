# Henderson Training Files — Placeholder Report

**Date:** 2026-05-02
**Scope:** Document the City of Henderson placeholder folder created during the Hopkinsville Water training-data audit. **The Henderson processor is intentionally NOT implemented in this phase.**

---

## Folder created

```
Training Bills_Invoices/
└── Electricity - Power/
    └── City of Henderson/                       ← new vendor folder
        ├── README_HENDERSON_VENDOR.md
        └── Bills_Training/                       ← 66 PDFs
```

The category `Electricity - Power` was chosen because every inspected bill carries a `Charge Code: Electric` line — they are City of Henderson **electric** utility bills (not water/sewer). The `City of Henderson Kentucky` and `The City of Henderson` placeholders that already existed under `Government - Tax/` are for property/tax bills, not utility bills, so they were left alone.

## Files moved into `Bills_Training/`

66 PDFs total, moved from `Training Bills_Invoices/Water - Sewer/Hopkinsville Water Environment Authority/Bills_Training/`:

- `UtilityBill (51).pdf` … `UtilityBill (100).pdf` — 50 files
- `UtilityBill - 2026-03-30T*.pdf` — 5 files
- `UtilityBill - 2026-04-29T*.pdf` — 11 files

Source PDFs are unchanged — moved only (`os.rename`, byte-identical).

## Common content fingerprint

```
Account No.   Due Date    Amount Due  After Due Date
405156400-004 1/11/2026   646.57      678.65
Service Address
500 FAIR ST #6
HENDERSON, KY 42420
City of Henderson
PO Box 716
Henderson KY 42420
Phone: (270) 831-1200
Mailing Address
CANOE CREEK APARTMENTS
151A HATCHER LN
CLARKSVILLE, TN 37043
...
Service Period           Meter Readings
11/20/2025 - 12/24/2025  Electric
Meter No. Read Dates Day Previous Current Usage Unit Of Measure
13522    11/14/2025  s32 197       245     4,800  kWh
...
Charge Code               Amount
Electric                  587.63
Kentucky Sales Tax        36.31
Rate Increase for School Tax  17.63
911 Fee                   5.00
```

All 66 are digital PDFs — pdfplumber extracts text directly, no OCR required.

## Vendor identity (placeholder YAML)

`config/vendors/city_of_henderson.yaml`:

```yaml
vendor_identity:
  vendor_name: "City of Henderson"
  normalized_vendor_key: "city_of_henderson"
  category: "Electricity - Power"
  detection_keywords:
    - "City of Henderson"
    - "Hendersonky.gov"
    - "Henderson KY 42420"
    - "(270) 831-1200"
  active: false
  status: "needs_processor"
processor:
  implemented: false
```

## Processor status

- **Processor module:** ❌ not implemented (no `process_city_of_henderson.py`).
- **Web app vendor detection:** ❌ not registered. PDFs of this vendor return `vendor_key=unknown` when uploaded to the web app today.
- **Web app batch processor:** ❌ not registered. Even if a misroute happens via filename, the batch processor's registry won't have a loader for `city_of_henderson`, so the file ends up in `unsupported_files`.

## Next recommended steps (when the operator green-lights Henderson)

1. **Bill-analysis report.** Sample 20+ PDFs from `Electricity - Power/City of Henderson/Bills_Training/`, write `HENDERSON_BILL_ANALYSIS_REPORT.md` modeled on `HOPKINSVILLE_WATER_BILL_ANALYSIS_REPORT.md`. Document the layout (account number `\d{9}-\d{3}`, the four `Charge Code` line items, service period dates, due / after-due-date dates, meter readings).
2. **Flesh out the YAML.** Add `pdf_extraction_rules` (regex bank for the four observed line items: `Electric`, `Kentucky Sales Tax`, `Rate Increase for School Tax`, `911 Fee`), `service_gl_mapping` (suggest GL `6905` for Electricity / `6906` for Sales Tax — confirm against `Gl Codes/General Ledger Report.csv`), `property_address_overrides` (Canoe Creek Apartments → property abbreviation per Unit Info Clean.csv), `service_period_rules`, `due_date_rules`, etc.
3. **Build the processor.** Reuse the shared infrastructure:
   - `utils/pdf_text_extractor.py` (digital-text path; OCR isn't required since these are digital PDFs).
   - `utils/pdf_splitter.py` (probably not needed — each file is a single bill).
   - `utils/dropbox_uploader.py` (env-only credentials).
   - `utils/service_period_resolver.py`.
   - `utils/progress_tracker.py`.
   - `utils/location_validator.py` (Phase 1G validation rules — Location must come from trusted sources, Property Abbreviation mandatory).
4. **Register in the web app.**
   - Add `_looks_like_city_of_henderson` to `webapp/backend/services/vendor_detection.py`.
   - Add `("city_of_henderson", _import_henderson_processor, "process_city_of_henderson_batch")` to `_PROCESSOR_LOADERS` in `webapp/backend/services/batch_processor.py`.
   - Add `city_of_henderson` to `SUPPORTED_VENDOR_KEYS`.
5. **Test.**
   - CLI run on Bills_Training.
   - Webapp end-to-end with a mixed batch (HWEA + Henderson).
   - Confirm reconciliation rules from Phase 1G fire.
   - Source-file integrity verified.

## Confirmation

- **No bill PDF was deleted.** All 66 are at `Electricity - Power/City of Henderson/Bills_Training/`.
- **No bill PDF byte content was modified.** Verified by SHA-256.
- **City of Henderson is not registered as a supported vendor.** Web-app detection still returns `unknown` for these files until a processor lands.
- **No Dropbox tokens exposed.** Placeholder YAML has no credentials at all.
