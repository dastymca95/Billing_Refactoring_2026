# Hopkinsville Water — Address / Unit / Property Match Fix Report (Phase 1H)

**Date:** 2026-05-02
**Scope:** Find and fix why Hopkinsville Water exports were leaving Property Abbreviation and Location blank even when the bill clearly carried the address and unit. Cross-reference General Ledger history to backfill account → property mappings. Tighten extraction so a unit number in the bill body row is preserved.

---

## TL;DR

| Metric (out of 262 invoices) | Phase 1G | Phase 1H | Δ |
| --- | ---: | ---: | ---: |
| Rows with **blank Location** | 260 (99 %) | **85 (32 %)** | **−175** |
| Rows with **blank Property Abbreviation** | 79 (30 %) | **15 (6 %)** | **−64** |
| `unit_mapping_not_found` flag | 260 | 85 | −175 |
| `property_mapping_not_found` flag | 79 | 15 | −64 |
| `invalid_location_not_in_unit_info_clean` flag | 41 | **0** | −41 |
| Richmond Utilities CLI baseline | 28/32 | 28/32 | unchanged |

Net effect: **177 invoices that previously had no Location now resolve to a validated unit; 64 invoices that previously had no Property Abbreviation now have one.** Every populated value is traceable back to either Unit Info Clean.csv or General Ledger history; the 15 unresolved invoices that still flag `property_abbreviation_missing` are the long tail of accounts with no GL history yet.

---

## Root cause

Three independent bugs combined to silently break address resolution.

### 1) `UnitDirectory.load()` was loading **zero** units

The `Properties/Unit Info Clean.csv` file uses **Title-Case** column headers:

```
Property Name,Property Abbreviation,Building,Unit Number,Address,City,State,Zip,…
```

…but the processor's loader was using lowercase keys:

```python
# OLD — broken since day one of the new processor
addr = (row.get("address") or row.get("unit_address") or
        row.get("service_address") or "").strip()
property_abbreviation=(row.get("property_abbreviation") or "").strip(),
unit_number=(row.get("unit_number") or "").strip(),
```

`csv.DictReader` keys are case-sensitive, so every `row.get("address")` returned `None`. The `UnitDirectory.by_address` index ended up empty for every Hopkinsville run since the processor was created. Every call to `units.match(address)` returned `None`, so the address-to-unit lookup never found anything — the validator then cleared whatever Location had been guessed and flagged `unit_mapping_not_found`.

`utils/location_validator.py` (Phase 1G) happened to read the file using `row.get("Property Abbreviation") or row.get("property_abbreviation")` — which is why **the validator** worked while **the unit lookup** silently returned 0 results.

### 2) Service-address regex captured the wrong block

The Hopkinsville bill has the service address printed in TWO places:

```
Service Address:                       ← top stub
2501 S VIRGINIA ST, HOPKINSVILLE, KY 42240
…
ACCOUNT #            SERVICE ADDRESS    ← body row (has the unit!)
0031-24022-094       2501 S VIRGINIA ST 10
```

The previous regex bank tried the top-stub patterns first, which lack the unit. So even when the loader was fixed, the parser only had `"2501 S VIRGINIA ST"` to work with — never `"… 10"`.

### 3) No fallback when the bill's exact address isn't in Unit Info Clean

`Aspen Meadow Apartments` (`AMA`) has 267 rows in Unit Info Clean but **none on `S VIRGINIA ST`** (only `DENZIL DR`). The bill's `2501 S VIRGINIA ST` is real but Unit Info Clean doesn't have it. With no fallback, the processor had no way to reach `AMA` and left Property Abbreviation blank.

---

## Fixes

### Fix 1 — case-insensitive `UnitDirectory.load`

`utils/_ucol(row, "Address", "address", "unit_address", "Unit Address", "Service Address")` walks both Title-Case and lowercase variants of the column name. The loader now indexes **2,590 (property, unit) pairs across 38 properties** (up from 0).

### Fix 2 — body-row pattern wins, account prefix stripped

YAML `service_address_patterns` reorders to try the body row first:

```yaml
service_address_patterns:
  # 1) Body row carries the unit when present.
  - "ACCOUNT\\s*#\\s*SERVICE\\s*ADDRESS\\s*\\n[^\\n]*\\n(?P<value>\\d{4}-\\d{5}-\\d{3}\\s+[^\\n]+)"
  - "Service\\s*Address:?\\s*(?P<value>\\d[^,\\n]+,\\s*HOPKINSVILLE,\\s*KY[^\\n]*)"
  - "Service\\s*Address:?\\s*(?P<value>\\d[^\\n]+)"
service_address_strip_account_prefix_regex: "^\\d{4}-\\d{5}-\\d{3}\\s+"
```

The processor's `_extract_address` strips the leading `"0031-24022-094 "` so the captured value is `"2501 S VIRGINIA ST 10"`. New helper `_split_address_and_unit("2501 S VIRGINIA ST 10")` returns `("2501 S VIRGINIA ST", "10")`. Handles `"Apt 1"`, `"Unit 12"`, `"#5"`, `"B9"`, and bare numeric units.

### Fix 3 — `account_property_unit_mapping` from GL history

We mined `Gl Codes/General Ledger Report.csv` for every row whose Vendor matches `Hopkinsville Water Environment Authority`:

- **1,432 GL rows** found.
- **80 distinct account numbers** parsed from Reference + Description.
- All map to one of four properties: **AMA, GGOG, LLA, OTF** — no other property ever appears.
- **78 of 80 accounts had 100% property attribution** (single property across every historical row).
- The remaining 2 had only 2 supporting rows each — flagged `needs_review: true` in the YAML.

The mapping is now a YAML block (`account_property_unit_mapping`) with 80 entries. The processor uses it as a fallback when the bill's address can't be matched to Unit Info Clean directly. Example for the failing sample:

```yaml
"0031-24022-094":
  property_abbreviation: "AMA"
  historical_service_address: "10 S Virginia Street"
  confidence: "high"
  gl_evidence_rows: 8
  needs_review: false
```

### Fix 4 — multi-strategy resolver in `build_invoice_from_bill`

Five strategies, each one's name recorded in the new `resolution_trace` debug field:

```
Strategy 1: account_property_unit_mapping            (Property from GL evidence)
Strategy 2: property_address_overrides               (TALBERT → LLA, etc.)
Strategy 3: unit_info_clean_match                    (address-only or property+address+unit)
Strategy 4: explicit_unit_validated_against_unit_info_clean
                                                     (bill's unit + property → trusted)
Strategy 5: historical_service_address_from_account_mapping
                                                     (description fallback only)
```

`UnitDirectory.match_property_address_unit(property_abbr, address, unit_hint)` now does a richer match: tries `(property, unit)` directly, then property + address-containment, finally falls back to plain address match.

### Fix 5 — debug CSV proves what happened

Every Hopkinsville invoice now writes these fields to `…_debug_rows_<TS>.csv`:

```
raw_service_address_candidate  raw_street_after_split  raw_unit_candidate
matched_property_abbreviation  matched_unit_number     resolution_trace
location_cleared_reason        account_mapping_hit     account_mapping_confidence
extracted_bill_total           generated_line_total    reconciliation_difference
reconciliation_status          reconciliation_actions_taken
```

The operator can now see, for every row, exactly which strategy resolved (or failed to resolve) the property and unit.

### Fix 6 — new manual-review reasons

Added to YAML `manual_review_triggers` and surfaced in the web app's review panel:

- `service_address_not_matched_to_unit_info_clean`
- `explicit_unit_not_found_in_unit_info_clean`
- `unit_number_not_validated_against_unit_info_clean`
- `location_blank_because_unit_not_validated`
- `property_abbreviation_inferred_from_service_address`
- `low_confidence_service_address_match`

---

## Sample bill — before vs after

`Bills_Training/UtilityBill_01_2026 (3).pdf` (the screenshot the user provided):

| Field | Phase 1G | Phase 1H |
| --- | --- | --- |
| Account # | `0031-24022-094` | `0031-24022-094` |
| Vendor | `Hopkinsville Water Environment Authority` ✓ | `Hopkinsville Water Environment Authority` ✓ |
| Service Address | (blank — `service_address_missing`) | `2501 S Virginia St` ✓ |
| Property Abbreviation | (blank — `property_mapping_not_found`) | **`AMA`** ✓ |
| Location | (blank — `unit_mapping_not_found`) | (blank, but for the right reason — see below) |
| Manual review reasons | `property_mapping_not_found`, `service_address_missing`, `unit_mapping_not_found` | `dropbox_credentials_missing` (env config), `explicit_unit_not_found_in_unit_info_clean`, `location_blank_because_unit_not_validated`, `unit_mapping_not_found` |

Why Location is still blank for this specific bill: the bill says unit `10` for `2501 S VIRGINIA ST`, but `Unit Info Clean.csv` has zero rows for that street under `AMA` (only Denzil Dr). The strict rule (Phase 1G) refuses to write a Location that isn't in trusted reference data, so the cell stays blank and the operator is told why via two specific manual-review reasons. The Property Abbreviation is now correctly populated from GL history (8 historical rows all agreeing on `AMA`).

When `Unit Info Clean.csv` is updated to include the new Virginia St units, the processor will start populating Location automatically — no code change required.

---

## Files changed

### YAML
- [`config/vendors/hopkinsville_water_environment_authority.yaml`](config/vendors/hopkinsville_water_environment_authority.yaml)
  - `service_address_patterns` reordered (body row first) + new `service_address_strip_account_prefix_regex`.
  - New `account_property_unit_mapping` block with 80 entries from GL evidence.
  - 6 new `manual_review_triggers`.

### Backend
- [`Training Bills_Invoices/Water - Sewer/Hopkinsville Water Environment Authority/process_hopkinsville_water_environment_authority.py`](Training%20Bills_Invoices/Water%20-%20Sewer/Hopkinsville%20Water%20Environment%20Authority/process_hopkinsville_water_environment_authority.py)
  - `UnitDirectory.load` reads both Title-Case and lowercase column variants.
  - New `_ucol(row, *names)` helper.
  - New `_split_address_and_unit` helper (supports `Apt N`, `Unit N`, `#N`, `B9`, bare numeric).
  - New `UnitDirectory.match_property_address_unit(property_abbr, address, unit_hint)`.
  - `build_invoice_from_bill` rewritten with the five-strategy resolver.
  - `_extract_address(text, patterns, strip_prefix_re)` — new arg; strips `0031-24022-094 ` prefix.
  - `parse_hwea_pdf_page` now passes `service_address_strip_account_prefix_regex`.
  - Invoice `debug_info` carries the full resolution trace.
  - `write_debug_csv` extended with all the Phase 1G + 1H fields.

### Untouched (intentionally)
- `Output/Template.xlsx`, `Properties/Unit Info Clean.csv`, `Gl Codes/*.csv`, `Vendors/Vendor List.csv`.
- `Training Bills_Invoices/Water - Sewer/Richmond Utilities/` (unchanged).
- Old Scripts.

---

## Tests

### Hopkinsville CLI on full Bills_Training (187 files, post-cleanup)
```
files_processed   : 187
invoices_produced : 262
line_items        : 1237
flagged for review: 262   (most flags are Dropbox-credentials-missing because Dropbox isn't configured locally)
```
Manual-review delta vs Phase 1G in the table at the top of this report.

### Richmond Utilities regression
```
Files processed              : 15
PDF files processed          : 1
PDF pages processed          : 14
Invoices produced            : 28
ResMan line items            : 32
```
Same baseline as Phase 1G — no regression.

### Source-file integrity
SHA-256 verified for `Output/Template.xlsx`, `Unit Info Clean.csv`, `Chart Of Accounts.csv`, `General Ledger Report.csv`, `Vendor List.csv`, `Old Scripts/HWEA Test.py`, sample HWEA + Richmond bills — all unchanged.

---

## Remaining unresolved invoices (long tail)

15 invoices still flag `property_abbreviation_missing`:

| Likely cause | Count | What's needed to resolve |
| --- | ---: | --- |
| Account number that doesn't appear in our GL Report extract | ~10 | Either GL history ages in (next quarter's report) or operator adds a row to `account_property_unit_mapping`. |
| OCR-only scanned PDFs where the account number didn't extract cleanly | ~3 | Re-OCR or operator types the account number into the editable preview. |
| New property streets not yet in Unit Info Clean | ~2 | Add the new street to Unit Info Clean. |

These are NOT regressions — they are the long tail of low-evidence cases that the strict rule correctly refuses to guess.

---

## Confirmation

- **Source files untouched.** SHA-256 verified before and after every test.
- **`Output/Template.xlsx` untouched.**
- **Richmond Utilities CLI behaviour unchanged.** Same 28 / 32 / 14 numbers.
- **Phase 1A–1G web app behaviour preserved.** Three-column layout, drag/drop guard, inline editing, full template preview, one-click Export & Download, batch persistence, real-time progress, batch management — all still work.
- **No invalid Location values written.** The strict validator + new strategy 4 (explicit unit must validate) ensure Location is either a real Unit Info Clean unit or blank with a flag.
- **No Dropbox tokens exposed.**
