# Hopkinsville Water — Training Data Audit Report

**Date:** 2026-05-02
**Folder audited:** `Training Bills_Invoices/Water - Sewer/Hopkinsville Water Environment Authority/`
**Result:** 253 PDFs inspected; **187 confirmed Hopkinsville Water + 66 confirmed City of Henderson misfiles** (no other vendors found, no ambiguous files). Henderson bills were moved to `Training Bills_Invoices/Electricity - Power/City of Henderson/Bills_Training/`. 253 byte-identical root-level duplicates were archived to `_archived_duplicate_training_files/`. **No source PDFs were modified or deleted** — every file was either moved or copied (with the byte-identical hash verified before each move).

---

## What was audited

We classified every PDF in (and around) the Hopkinsville Water folder by **content**, not filename:

| Source location | Files | Action |
| --- | ---: | --- |
| `Hopkinsville Water Environment Authority/Bills_Training/` (initial state) | 253 | Classified (see below). |
| `Hopkinsville Water Environment Authority/` (root, 253 PDFs duplicate of Bills_Training) | 253 | Each one was hashed against its Bills_Training counterpart; all 253 were byte-identical → moved to `_archived_duplicate_training_files/`. |

Vendor fingerprints used (each PDF's first-page text was scanned for these substrings):

| Vendor | Strong keywords |
| --- | --- |
| Hopkinsville Water Environment Authority (HWEA) | "Hopkinsville Water Environment Authority", "Hopkinsville Water Env. Auth.", "hwea-ky.com", "(270) 887-4246" |
| City of Henderson | "City of Henderson", "Hendersonky.gov", "PO Box 716" |
| Richmond Utilities | "Richmond Utilities", "richmondutilities.com" |
| Other utility vendors checked defensively | Atmos Energy, Duke Energy, City of Clarksville |

---

## Classification result

| Category | Count | Vendor route |
| --- | ---: | --- |
| Hopkinsville Water Environment Authority | **187** | Stays in `Hopkinsville Water Environment Authority/Bills_Training/` |
| City of Henderson | **66** | **Moved** to `Electricity - Power/City of Henderson/Bills_Training/` |
| Other vendors | 0 | — |
| Ambiguous / unknown | 0 | — |
| Unreadable | 0 | — |

The 66 City of Henderson PDFs all have content matching `Charge Code Amount / Electric / Kentucky Sales Tax / Rate Increase for School Tax / 911 Fee` — they're City of Henderson electric utility bills for `Canoe Creek Apartments` accounts (`405156400-004`, `405163200-004`, `405170000-004`, `405173400-004`). They share filename patterns with HWEA bills (`UtilityBill (NN).pdf`, `UtilityBill - <ISO timestamp>.pdf`), which is almost certainly why they were misfiled — an automated download tool produces the same generic filename for multiple utility-portal vendors.

---

## Files moved

### From HWEA Bills_Training → Henderson Bills_Training (66 files)
- `UtilityBill (51).pdf` … `UtilityBill (100).pdf` (50 files)
- `UtilityBill - 2026-03-30T115127.904.pdf` (5 files in this date range)
- `UtilityBill - 2026-04-29T105624.160.pdf` (11 files in this date range)

Each move was an `os.rename` — original bytes preserved. Destination: `Training Bills_Invoices/Electricity - Power/City of Henderson/Bills_Training/`.

### From HWEA root → HWEA `_archived_duplicate_training_files/` (253 files)
The root-level files were byte-identical to the Bills_Training copies (verified via SHA-256 before move). The user noted these were old duplicates; we archived rather than deleted to keep recovery trivial.

### Files left in place
- `process_hopkinsville_water_environment_authority.py` (the processor)
- `README_HOPKINSVILLE_WATER.md`
- `HOPKINSVILLE_WATER_BILL_ANALYSIS_REPORT.md`
- `Processed_Output/`
- `Bills_Training/` (now contains exactly 187 HWEA PDFs)

---

## Impact on previous parser/YAML

Reviewed `config/vendors/hopkinsville_water_environment_authority.yaml` and `process_hopkinsville_water_environment_authority.py` for Henderson contamination. Only Henderson references found:

| File | Line(s) | Type | Decision |
| --- | --- | --- | --- |
| `hopkinsville_water_environment_authority.yaml` | line 122 (`reject_if_text_contains: ["City of Henderson"]`) | **Defensive guard** — rejects any PDF whose content says City of Henderson so the processor never accidentally treats them as HWEA bills. | **Keep**. This is the rule that made detection work in the first place. |
| Same file, lines 36–37, 97–98, 582 | Comments documenting the rejection rule. | **Keep**. |
| Processor `.py` | None | No Henderson-specific code paths. | — |

So no Henderson assumptions had leaked into the parser — the rejection guard was already in place from Phase 1F. The audit confirms **the Hopkinsville processor was never trained on Henderson layouts** because Henderson PDFs were always being filtered out at the vendor-confirm gate.

A change_log entry was added to the YAML to record the audit:

```yaml
# (added by claude_qa_audit on 2026-05-02)
change_summary: |
  Removed/isolated non-Hopkinsville/Henderson training contamination.
  Verified 187 PDFs in Bills_Training are HWEA-only; moved 66 City of
  Henderson misfiled PDFs to a new vendor folder.
```

---

## Before / after the cleanup

| Metric | Before | After |
| --- | ---: | ---: |
| Files in HWEA Bills_Training | 253 | 187 |
| Files routed through the HWEA processor on a CLI run | 187 (66 rejected) | 187 (0 rejected — clean training data) |
| HWEA invoices produced | 262 | 262 |
| HWEA ResMan line items | 1,237 | 1,237 |
| HWEA flagged for review | 262 | 262 |

The CLI numbers are identical because the rejection guard was already preventing Henderson contamination from reaching the parser. **The cleanup is preventative**: it removes the risk that future vendor-detection edits or YAML changes could accidentally route Henderson bills through HWEA's pipeline.

After the cleanup the manual-review reasons changed in two ways:
- New flag: `invalid_location_not_in_unit_info_clean` fires on **41 of 262 invoices** (Phase 1G validation rule — was not enforced before).
- New flag: `property_abbreviation_missing` fires on **79 of 262 invoices** (Phase 1G — was not enforced before).
- New flag: `bill_total_does_not_match_generated_lines` fires on **15 of 262 invoices** (Phase 1G strict reconciliation — was not enforced before).

These are NOT regressions — they are the new validation rules surfacing previously-silent issues.

---

## Henderson placeholder

A new vendor folder was created at:

```
Training Bills_Invoices/
└── Electricity - Power/
    └── City of Henderson/
        ├── README_HENDERSON_VENDOR.md
        └── Bills_Training/        (66 PDFs)
```

Plus a minimal vendor YAML at `config/vendors/city_of_henderson.yaml` with `status: needs_processor`, `active: false`. **Henderson is NOT registered as a supported vendor in the web app** — uploading one of those 66 PDFs to the web app today returns `vendor_key=unknown` (which is the correct, conservative behavior).

The user explicitly asked us to NOT build a Henderson processor in this phase. Future vendor work will:
1. Inspect the Henderson Bills_Training corpus and write a bill-analysis report.
2. Flesh out `city_of_henderson.yaml` with PDF extraction patterns.
3. Build `process_city_of_henderson.py`.
4. Register in the webapp's `_PROCESSOR_LOADERS`.

See `HENDERSON_TRAINING_FILES_PLACEHOLDER_REPORT.md` for the next-steps breakdown.

---

## Confirmation

- **No PDF was deleted.** All 506 (= 253 + 253 archived) remain on disk in their respective folders.
- **No PDF byte content was modified.** Verified by SHA-256 before/after every move.
- **No reference data touched.** `Output/Template.xlsx`, `Properties/Unit Info Clean.csv`, `Gl Codes/*.csv`, `Vendors/Vendor List.csv` — all hashes match Phase 1F baselines.
- **No old scripts touched.** `Old Scripts/HWEA Test.py` is on disk unchanged; its hardcoded Dropbox tokens have **not** been reproduced anywhere.
- **No Dropbox tokens exposed.** Credentials remain in `.env` only.
- **Richmond Utilities CLI baseline unchanged.** Same 28 invoices / 32 line items / 14 PDF pages.
- **Henderson is not registered as a supported processor.** `vendor_detection.py` only knows about `richmond_utilities` and `hopkinsville_water_environment_authority`.
