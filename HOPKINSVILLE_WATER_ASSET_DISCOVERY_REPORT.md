# Hopkinsville Water Environment Authority — Asset Discovery Report

**Date:** 2026-05-02
**Vendor:** Hopkinsville Water Environment Authority (HWEA)
**Status:** Becoming the **second fully supported vendor** after Richmond Utilities.

---

## Vendor folder

The actual folder name in the project tree is:

```
Training Bills_Invoices/
└── Water - Sewer/
    └── Hopkinsville Water Environment Authority/
```

This is the canonical spelling used everywhere downstream (`Vendors/Vendor List.csv`, the new YAML key `hopkinsville_water_environment_authority`, the new processor's `VENDOR_DISPLAY_NAME`). It is the **same** vendor variously referred to in older notes as "Hawkinsville Water" / "Hopkinsville Water" — those are typos / shorthand for HWEA.

253 files total inside the folder. All `.pdf`, no `.csv` / `.xlsx` / `.docx` / images.

## Old script

Found:

```
Old Scripts/HWEA Test.py    (681 lines)
```

Other water-related scripts in `Old Scripts/` (`CPWS.py`, `Hardin CWD2.py`, `Pennyrile Bills.py`) are for **other** vendors and are not used here. `HWEA Test.py` is the only Hopkinsville script.

The script is a self-contained PyPDF2 + xlwings + Dropbox pipeline written before this refactor existed. It is the reference for HWEA's bill format and AP rules.

### Useful logic to migrate to YAML / processor

| Concept | Old-script value / approach | Where it lives in the new architecture |
| --- | --- | --- |
| Account-number regex | `^(\d{4}-\d{5}-\d{3})` | YAML `pdf_extraction_rules.account_number_patterns` |
| Invoice date | `Date Issued: MM/DD/YYYY` | YAML `pdf_extraction_rules.invoice_date_patterns` |
| Due date | `(Net Due On or Before\|Amount Due After) MM/DD/YYYY` | YAML `pdf_extraction_rules.due_date_patterns` |
| Total / Net Due | `Net Due On or Before ... $X.XX` | YAML `pdf_extraction_rules.total_amount_patterns` |
| Service period | `MM/DD/YY - MM/DD/YY` (anywhere on bill) | YAML `pdf_extraction_rules.service_period_patterns` |
| Final-bill flag | "FINAL BILL" → suffix " Final" on invoice number | YAML `pdf_extraction_rules.final_bill_keywords` + processor adds suffix |
| Service-line codes | `WA WAF PW PWF SW SWF SR SRF PS PSF UT UTF ST STF SA SAF BF BFF IR IRF WAC` → service group + GL | YAML `pdf_extraction_rules.service_codes` (code → service name + group + gl override) |
| GL mapping | Water/Sewer/Sewer Pilot → 6955; Storm Water → 6995; Sanitation → 6940; Connect Fee/Balance Forward → 6956 | YAML `service_gl_mapping` |
| Tax allocation | Largest-remainder algorithm proportional to service base | Processor port (Decimal-precise; same algorithm) |
| Reconcile to Net Due | Force sum == net_due_total exactly; cents go to largest line | Processor port |
| Address normalization | `DRIVE→DR, STREET→ST, ROAD→RD, ...`, then Title-Case | YAML `address_normalization_rules.suffix_replacements` + processor |
| Property mapping | "TALBERT" / "PHANTOM HOLDINGS" → LLA; "GRIFFIN GATE" → GGOG; "OAK TREE" / "PIN OAK" → OTF; default AMA | YAML `property_address_overrides` (per-vendor static rules) |
| Unit format | Last token like "B9" → "B-09" (Letter-NN with zero-pad) | YAML `unit_format_rules.resman_format` + helper `format_unit_resman()` |
| Invoice number | `{account} {Mon} {YY}` (title-case month) | YAML `invoice_number_rules.format` |
| Invoice description | `MM/DD/YY-MM/DD/YY - <address>` | YAML `invoice_description_rules.format` |
| Line item description | `<invoice description> - <line detail>` | YAML `line_item_description_rules.format` |
| Expense Type | "General" | YAML `expense_type_rules.fixed_value` |
| Replacement Reserve | "FALSE" | YAML `replacement_reserve_rules.default` |

### Hardcoded items that need explicit handling

| Hardcoded in old script | New-architecture decision |
| --- | --- |
| `APP_KEY`, `APP_SECRET`, `REFRESH_TOKEN` literal defaults at the top of the file | **Redacted in this report.** The new processor uses `utils/dropbox_uploader.py` which reads exclusively from `os.environ` (no defaults baked in). |
| `OneDriveCommercial` env-var override | Removed — the new pipeline doesn't depend on OneDrive. |
| Hardcoded Dropbox path `/Diego Santos/Historic Bills PDFs/` | Replaced by `dropbox_rules.folder_pattern` per-vendor (uses the project's existing `/Billing_Refactoring_2026/<vendor>/<year>/<month>/` pattern). |
| Property mapping (`TALBERT → LLA`, `GRIFFIN GATE → GGOG`, `OAK TREE → OTF`, default AMA) | Encoded in YAML `property_address_overrides`. Default-AMA changed to **manual review** instead of silent default — operator confirms it explicitly. |
| Unit format hardcoded as `Letter-NN` | Encoded in YAML `unit_format_rules` so future overrides don't require Python edits. |
| `xlwings` Excel writes | Removed. Now uses `openpyxl` via the shared ResMan template + the webapp export pipeline. |
| Invoice-number deduplication via reading existing Excel | Out of scope — the project already has duplicate detection via `flag_duplicates(...)` (operator decides whether to skip). |
| Source PDF rename to `<invoice>.pdf` and copy to `Bills Procesados` | Removed. **Source files stay untouched.** Per-bill split PDFs go into `Processed_Output/.../support_documents/` exactly like Richmond. |

## Training bills inventory

253 PDFs, classified by content:

| Category | Count | Notes |
| --- | ---: | --- |
| **HWEA normal bills** (digital, single-bill) | 171 | Filename pattern `UtilityBill_<MM>_<YYYY>*.pdf`. Has `Net Due On or Before`, `Date Issued`, full service-line table. **Primary target for the processor.** |
| **HWEA late / disconnect notices** (digital, single-bill) | 7 | Has `DISCONNECT NOTICE`, `Past Due Amount $X.XX`, `Last Day to Pay`, `Service Balances` summary. **No** `Date Issued` or per-line periods. Processor flags `late_notice_detected` + manual review. |
| **HWEA scanned multi-bill PDFs** | 9 | Filename pattern `HWEA - <Property> - <date>.pdf`, `HWEA UTILITIES.pdf`, `HWEA Utilities <date>.pdf`. `pdfplumber.extract_text()` returns 0 chars → routed to OCR via `utils/pdf_text_extractor.py`. Some are 30+ pages, multi-bill — the existing per-page splitter will produce one support PDF per bill. |
| **Misfiled (City of Henderson Electric)** | 66 | `UtilityBill (NN).pdf`, `UtilityBill - <timestamp>.pdf` whose content actually says "City of Henderson". The vendor detector rejects these so they are flagged `wrong_vendor` instead of being silently mis-processed. |
| Unreadable / ambiguous | 0 | None observed. |

Sample file names per category:

```
hwea_normal:    UtilityBill_01_2026 (1).pdf, UtilityBill_04_2026 (1).pdf, UtilityBill_03_2026 (5).pdf
hwea_late:      UtilityBill_02_2026 (1).pdf, UtilityBill_03_2026 (1).pdf
hwea_scanned:   HWEA - 4-20-26.pdf, HWEA - Aspen - 3-16-26.pdf, HWEA UTILITIES.pdf
misfiled:       UtilityBill (51).pdf, UtilityBill (84).pdf, UtilityBill - 2026-04-29T*.pdf
```

## Recommended extraction strategy

### 1) Detection layer

`webapp/backend/services/vendor_detection.py` adds a new detector. Two cheap signals:

| Signal | Check |
| --- | --- |
| Filename hint | `re.search(r"hopkinsville|HWEA", filename, re.IGNORECASE)` → confidence 0.7 (filename alone is weak — many `UtilityBill (NN).pdf` are actually Henderson). |
| PDF text-layer scan | First page contains `Hopkinsville Water Environment Authority`, `hwea-ky`, or `(270) 887-4246` → confidence 0.95. Mismatched if filename starts with `UtilityBill (` AND text mentions "City of Henderson" → returns vendor_key `unknown` so the file isn't mis-routed. |

### 2) Parsing pipeline

```
PDF → utils/pdf_text_extractor.extract_pdf_text(...)          # digital first, OCR fallback
       ↓
parse_hwea_pdf_bill(text, words, cfg, logger):
   - vendor confirm (else skip + flag)
   - document_type detection (normal_bill / late_notice / unknown)
   - account number     (regex)
   - invoice date       (regex; consensus across pages for multi-bill PDFs)
   - due date           (regex; fallback = invoice + 15 days, flag if used)
   - service period     (regex; explicit dates win via service_period_resolver)
   - service address    (multi-strategy regex: ACCOUNT# anchor, RETAIN-THIS-SECTION anchor, "Service Address:" anchor)
   - net-due-total      (regex)
   - service line items (per-code regex bank: WA/PW/SW/SR/PS/UT/ST/SA/BF/IR/WAC and "F" finalized variants)
   - tax allocation     (largest-remainder, Decimal-precise — port from old script)
   - reconcile to total (force sum == net_due, diff to largest line, flag mismatch > $0.02)
   - synth MovementRow per line item → build_invoice(...)  (shared pipeline)
       ↓
For multi-bill scanned PDFs: split via utils/pdf_splitter.py, upload per-bill to Dropbox.
For single-bill PDFs: upload original to Dropbox.
For Henderson misfiles: skip, flag wrong_vendor.
```

### 3) Late-notice handling (conservative)

When `late_notice_detected`:

- Take `Past Due Amount $X.XX` as the `Total` only when no `Net Due On or Before` is present.
- Take the `Service Balances` block (`Water $X.XX / Sewer $X.XX / Sanitation $X.XX`) as line items, mapped to the standard GL codes.
- **Do not** include the `$50.00 Service Fee` mentioned in the boilerplate (that's a hypothetical fee, not a charge).
- **Do not** import `Past Due Amount` as a "Balance Forward" line — that double-counts.
- Always flag `late_notice_detected`. If line items don't reconcile to `Total Amount Due` within $0.02, also flag `extracted_total_mismatch`.
- Use the `Last Day to Pay` date as both invoice date (if no other date is on the page) and due date.

### 4) Property and unit resolution

YAML `property_address_overrides` carries the explicit street → property abbreviation map (`TALBERT/PHANTOM HOLDINGS → LLA`, etc). When the address contains none of those keywords, the processor uses Unit Info Clean.csv (the project's primary source) and flags `unit_mapping_not_found` if no unit matches. The default-AMA fallback from the old script is **not** ported — silent defaulting is the kind of bug we're trying to fix.

### 5) Risks / unresolved questions for the operator

| Risk | Detail | Mitigation |
| --- | --- | --- |
| 66 misfiled Henderson PDFs | They are valid bills for a different vendor (not yet supported). The HWEA detector explicitly rejects them. | Operator should move them into the City of Henderson folder when that vendor's processor lands; the discovery report flags them by name. |
| Property `default = AMA` in old script | Was a silent fallback for any address without a known keyword. We refuse to silently default; flagged manual review instead. | Operator either adds the new street to YAML `property_address_overrides` or manually fills the cell. |
| Unit format Letter-NN vs ResMan | Old script's `B9 → B-09` rule isn't documented in the project's other YAML — first-time the convention is encoded explicitly. | YAML key `unit_format_rules.resman_format = "{letter}-{number:02d}"` makes it editable. |
| Late-notice lines without periods | Late notices don't show service periods, so the resolver returns "service period inferred from billing month". | Flagged `service_period_inferred`. |
| Multi-bill scanned PDFs | We don't have ground-truth totals to reconcile against. | Falls back to "what OCR can read", flagged `extracted_total_mismatch` if line items don't sum. The Phase 1B inline edit in the webapp lets operators correct. |
| Hardcoded Dropbox tokens visible in old script | The old `HWEA Test.py` literally embeds `APP_KEY` / `APP_SECRET` / `REFRESH_TOKEN` defaults. | **Redacted in this report.** The new processor reads them only from environment variables via `utils/dropbox_uploader.py`. The old script is left untouched on disk; it is deprecated. |

## Secrets / tokens found

The old script `Old Scripts/HWEA Test.py` lines 31–33 contain hardcoded **default** values for `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`, and `DROPBOX_REFRESH_TOKEN`. These are **redacted** in this report (no values reproduced). The new processor reads only from environment variables (`utils/dropbox_uploader.py`); no defaults are baked in. Operators should rotate these tokens at their convenience — the old script's hardcoded values give anyone with read access to the repo a working refresh token.

## Phase plan

This report concludes the discovery phase. The implementation split is:

1. YAML at `config/vendors/hopkinsville_water_environment_authority.yaml`.
2. Processor at `Training Bills_Invoices/Water - Sewer/Hopkinsville Water Environment Authority/process_hopkinsville_water_environment_authority.py`.
3. Vendor detection update + batch processor registration.
4. Webapp progress bar (new infrastructure shared by all vendor processors via `progress_callback`).
5. Tests + integrity check.
6. Final report and READMEs.
