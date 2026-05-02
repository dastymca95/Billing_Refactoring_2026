# Hopkinsville Water — Disconnection Notice Parsing Fix Report

**Date:** 2026-05-02
**Phase:** 1I (CLI / script only — no frontend changes)
**Test input:** `Training Bills_Invoices/Water - Sewer/Hopkinsville Water Environment Authority/Bills_Training/HWEA - Aspen - 3-16-26.pdf` (1,068,505 bytes, 4 pages, scanned PDF, 4 disconnection notices stapled together).

---

## TL;DR

| | Before | After |
| --- | --- | --- |
| Invoices produced from the test PDF | **1** | **4** |
| ResMan template lines | 2 | 4 |
| Per-notice support PDFs written | 0 (split crashed) | 4 |
| Locations matched at unit precision | 0 | 4 |
| Reconciliation status | partial (1 unreconciled, 3 silently dropped) | 4 / 4 reconciled |

All four notices now produce one invoice each, with the correct property abbreviation (`AMA`), unit-level location, service address, and reconciled total. The Richmond Utilities regression run produced 28 invoices / 32 lines — unchanged. Source PDFs and `Output/Template.xlsx` were not modified.

---

## 1. Root cause analysis

The original processor had three independent failures that compounded:

1. **3 of 4 notices silently dropped at line-item parsing.** The `_parse_late_notice_service_balances` function requires a dollar amount on each `Water` / `Sewer` / `Sanitation` row. On 3 of the 4 notice pages OCR caught the labels but not the right-column amounts (or no breakdown was printed at all). With no line items, the page returned `bill.line_items == []` and the invoice-from-bill builder skipped it entirely instead of producing an invoice.
2. **No notice-specific service-address extraction.** A disconnection notice prints the service address as a free-standing line in the upper portion of the page (`2629 KENWOOD DR 2, HOPKINSVILLE, KY 42240`), not under a `Service Address:` label. The standard regex bank looked for a label and found nothing, so service-address came up blank or fell back to the customer mailing address.
3. **PDF splitter crashed for every page.** The processor's per-bill split target path was 277 characters — past the Windows MAX_PATH of 260. Every `open(path, "wb")` raised `[Errno 2] No such file or directory`. The processor masked this as `support_pdf_split_failed` and the rows got the full multi-bill PDF as their support link.

Two follow-on issues were uncovered while verifying the fix:

4. **Unit suffix-match bug.** `Properties/Unit Info Clean.csv` stores HWEA units as `<building>-<unit>` (e.g. `1900-9`, `2100-5`). The notice prints the unit as a bare suffix (`9`, `5`). The resolver compared `r.unit_number` (e.g. `1900-9`) directly against `unit_hint` (e.g. `9`) and never matched, so it fell back to `recs[0]` (always `unit-1`).
5. **Substring-match preferred over exact-match.** `100 DENZIL DR` and `2100 DENZIL DR` both exist as separate buildings at AMA. The resolver iterated through `units_by_property_address` and picked whichever building was visited first whose stored address was a substring of the bill's address. For `2100 DENZIL DR`, that meant `100 DENZIL DR` won via substring containment.

---

## 2. Layout of the disconnection notice (vs. a normal bill)

A normal HWEA bill has:
- A header band with mailing address and `Service Address:` label.
- A service-line table in the middle: `WA / WAF / SW / SWF / SA / SAF / UT / ST` with amounts.
- A `Net Due On or Before <date> $X.XX` total line.
- An account-summary block.

A disconnection notice has:
- A header band with `ACCOUNT #`, `Past Due Amount`, `Last Day to Pay <MM/DD/YYYY>`, plus a **`DISCONNECT NOTICE`** stripe.
- A service address printed once, on its own line, in the upper portion of the page (no `Service Address:` label).
- Optionally a `Service Balances` summary block (Water / Sewer / Sanitation with totals). On scanned notices OCR sometimes drops the right column entirely.
- No service-line breakdown table.
- No `Net Due On or Before` line — the total is the `Past Due Amount` value.

OCR'd text excerpt of a representative notice (debug CSV captures up to 300 chars per page):
```
401 East Ninth Street    ACCOUNT #  0035-27867-063
P.O. Box 628             Past Due Amount    $104.41
Hopkinsville, KY 42241-0628    Last Day to Pay  3/12/2026
…
DISCONNECT NOTICE
…
1900 DENZIL DR 9
HOPKINSVILLE, KY 42240
```

---

## 3. YAML changes

[`config/vendors/hopkinsville_water_environment_authority.yaml`](config/vendors/hopkinsville_water_environment_authority.yaml)

- `document_type_detection.supported_types` now lists `[normal_bill, disconnection_notice, late_notice]`.
- New section `disconnection_notice_extraction_rules`:
  - `service_address_patterns` — regex bank for the upper-region address line (matches `<NUM> <STREET>, HOPKINSVILLE, KY <ZIP>` and the all-caps, comma-less variant).
  - `service_address_expected_position` — `y_min: 0.05`, `y_max: 0.55` (advisory, not currently enforced).
  - `catch_all_single_line` — when no usable breakdown is extracted, build a single line equal to `Past Due Amount` mapped to GL `6955`. Description: `"Past due utility charges (disconnection notice)"`.
  - `notice_boundary_detection` — `strategy: [page_level, account_number_anchor]`. Default is one notice per page.
- New manual-review triggers: `disconnection_notice_service_breakdown_missing`, `disconnection_notice_breakdown_incomplete`, `notice_boundary_uncertain`.

---

## 4. Python script changes

[`Training Bills_Invoices/Water - Sewer/Hopkinsville Water Environment Authority/process_hopkinsville_water_environment_authority.py`](Training%20Bills_Invoices/Water%20-%20Sewer/Hopkinsville%20Water%20Environment%20Authority/process_hopkinsville_water_environment_authority.py)

1. **Notice-specific service-address extraction** in `parse_hwea_pdf_page`: when `bill.document_type == DOCUMENT_LATE`, run the YAML-driven `disconnection_notice_extraction_rules.service_address_patterns` and prefer its result if (a) no service address was found by the standard bank, or (b) the standard bank returned a non-Hopkinsville string. Records `service_address_source = "disconnection_notice_upper_region"`.

2. **Catch-all single-line fallback** in the late-notice handling block. Logic:
   - Compute the partial breakdown sum if `_parse_late_notice_service_balances` returned anything.
   - If the breakdown reconciles to within tolerance of `total_amount_due`, use it (label `service_balances_breakdown`).
   - Otherwise, build one catch-all line equal to `total_amount_due` (label `catch_all_total_amount_due` or `catch_all_total_amount_due_breakdown_incomplete` depending on whether a partial breakdown was discarded). Adds `disconnection_notice_breakdown_incomplete` and `disconnection_notice_service_breakdown_missing` to manual review.
   - If no `total_amount_due` AND no items, leave empty — same behaviour as before, surfaced as `no_line_items_extracted`.

3. **Six new debug-CSV columns** captured per invoice: `block_number_on_page`, `notice_detected`, `notice_boundary_method`, `notice_line_items_source`, `service_address_source`, `raw_notice_text_excerpt`. The fieldnames list and the row-writer in `write_debug_csv` were extended in lockstep so old downstream readers don't trip on missing keys.

4. **Unit suffix-match in `UnitDirectory.match_property_address_unit`.** Two changes:
   - When iterating per-property address candidates, exact-match passes first; substring-match runs only as a second pass. Stops `100 DENZIL DR` from winning over `2100 DENZIL DR`.
   - The unit comparison now also accepts `r.unit_number.rsplit("-", 1)[-1] == unit_hint` so a bill that prints `9` matches a Unit Info Clean row of `1900-9`. Tagged in `resolution_trace` as `property_unit_via_address_suffix`.

[`utils/pdf_splitter.py`](utils/pdf_splitter.py)

5. **Windows long-path support.** A new `_long_path()` helper prefixes absolute Windows paths with `\\?\` (and `\\?\UNC\` for UNC paths) so the Win32 path parser is bypassed. The `os.makedirs` and `open()` calls inside the splitter use it as a fallback when the standard call hits `OSError`. The reader also uses it to open the source PDF. Non-Windows: the helper returns `str(p)` unchanged.

---

## 5. Before / after — the four test notices

The 4-page test PDF resolves identically to the table below. All four debug rows below are taken from `Processed_Output/hopkinsville_water_environment_authority_debug_rows_<latest>.csv` after the fix.

| Page | Account | Property | Location | Service Address | Total | Reconciliation | Notice line item source |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `0031-23134-100` | AMA | `2629-2` | 2629 Kenwood Dr | $104.41 | matched | `catch_all_total_amount_due` |
| 2 | `0035-27857-077` | AMA | `1900-7` | 1900 Denzil Dr | $69.41  | matched | `catch_all_total_amount_due` |
| 3 | `0035-27867-063` | AMA | `1900-9` | 1900 Denzil Dr | $104.41 | matched | `catch_all_total_amount_due_breakdown_incomplete` |
| 4 | `0035-27967-053` | AMA | `2100-5` | 2100 Denzil Dr | $104.41 | matched | `catch_all_total_amount_due` |

**Page 3** is the interesting case: OCR caught a partial Water/Sewer breakdown summing to $44.41, but the notice's `Past Due Amount` was $104.41. Pre-fix, the partial breakdown was used and the row was unreconciled by $60.00. Post-fix, the partial breakdown is discarded in favour of the catch-all single line equal to the bill total, the row reconciles, and the partial sum is preserved in `notice_partial_breakdown_sum` for audit.

All four notices are flagged for manual review with the appropriate reason set:
- `late_notice_detected` (always)
- `service_period_inferred`, `service_period_missing` (no period printed on a notice)
- `disconnection_notice_service_breakdown_missing` (3 of 4) or `disconnection_notice_breakdown_incomplete` (1 of 4)
- `dropbox_credentials_missing` (Dropbox not configured in this environment)

---

## 6. Per-notice support PDFs

After the splitter long-path fix, four split PDFs are written to:

```
Processed_Output/hopkinsville_water_environment_authority/support_documents/
├── Hopkinsville_Water_Environment_Authority_0031-23134-100_Feb_26.pdf  268,092 bytes
├── Hopkinsville_Water_Environment_Authority_0035-27857-077_Feb_26.pdf  266,207 bytes
├── Hopkinsville_Water_Environment_Authority_0035-27867-063_Feb_26.pdf  267,698 bytes
└── Hopkinsville_Water_Environment_Authority_0035-27967-053_Feb_26.pdf  265,971 bytes
```

Each invoice's `support_pdf_path` (and Dropbox URL once credentials are configured) points to the page-level split, not the four-bill scan. No `support_pdf_split_failed` flags appear in the manual-review workbook for this run.

---

## 7. Regression — Richmond Utilities

Same processor + utils stack, run unchanged on the existing Richmond Utilities training corpus:

```
Files processed              : 15
PDF pages processed          : 14
Invoices produced            : 28
ResMan line items            : 32
Invoices flagged for review  : 28
```

Matches the prior baseline exactly. None of the changes (notice-specific YAML, catch-all line item, splitter long-path, unit suffix-match) altered Richmond's outputs.

---

## 8. Source-file integrity

All source files were opened read-only. SHA-256 hashes of the relevant files at the end of this work session:

| File | SHA-256 | Last-modified |
| --- | --- | --- |
| `Output/Template.xlsx` | `b753f406c0222f150a9549065fc5c43168488353807ab45623ed2a5c3969c284` | 2026-05-01 17:19 |
| `Properties/Unit Info Clean.csv` | `79d46c7c97ffe1bcf036212d9489a06d1399268b0b80aee8d765c9f219c1a683` | 2026-05-01 20:08 |
| `Gl Codes/General Ledger Report.csv` | `8f8506ec603f5f668dc2609a922bdfb6823e193d9a10a8fe0ed674d73abb6e49` | 2026-05-01 17:12 |
| `Vendors/Vendor List.csv` | `7839a43a493a7c0c64484bcfe9ba3cd364cb0bdc336e985a17c99cefc64863f9` | 2026-05-01 16:21 |
| `Training Bills_Invoices/.../HWEA - Aspen - 3-16-26.pdf` | `a3c28e34da4a6b323013bbed1b0bc8d2e9659c1b2dd818a0cad86a1ed106bc0f` | 2026-03-16 09:47 |

All five files have modification timestamps that pre-date the start of this session (2026-05-02 13:08+), confirming they were not modified by any of the work above.

---

## 9. Files changed in this fix

- `config/vendors/hopkinsville_water_environment_authority.yaml` — new `disconnection_notice_extraction_rules` block, expanded `document_type_detection`, new manual-review triggers.
- `Training Bills_Invoices/Water - Sewer/Hopkinsville Water Environment Authority/process_hopkinsville_water_environment_authority.py` — notice-specific service address extraction, catch-all line item fallback, six new debug fields, unit suffix-match, exact-match-first pass.
- `utils/pdf_splitter.py` — `_long_path()` helper for Windows MAX_PATH bypass, applied to mkdir / open / read.
- `Training Bills_Invoices/Water - Sewer/Hopkinsville Water Environment Authority/README_HOPKINSVILLE_WATER.md` — updated *Late notice / disconnection notice* section + new manual-review reasons table.

---

## 10. Known limitations / future work

- **One notice per page is assumed.** The YAML notice_boundary_detection lists `account_number_anchor` as a secondary strategy, but the parser currently only treats each PDF page as a single notice. If a vendor ever prints two notices on one page, this will need a second pass that splits at the next `ACCOUNT #` token.
- **Description cells fall back to the catch-all string.** When the breakdown is unavailable, the line description reads `"Past due utility charges (disconnection notice)"`. Operators reviewing in the webapp can edit it before exporting, but it isn't a per-service breakdown.
- **Service-period inference is calendar-month.** Notices don't print a service period. Operators should sanity-check the inferred period before exporting.
- **No frontend work.** This fix is CLI/script only. The webapp will pick up the changes the next time the backend reloads the YAML and Python script, but no UI text or export button changes were made in this phase.
